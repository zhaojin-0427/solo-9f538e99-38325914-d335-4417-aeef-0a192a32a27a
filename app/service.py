"""Transactional catalog operations and scoped effective-result recomputation."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models, schemas
from .reasoning import FormulaRule, KnownFact, solve_group
from .units import ALLOWED_UNITS, expression_text, normalize_operator, normalize_unit


class CatalogError(Exception):
    status_code = 400


class NotFoundError(CatalogError):
    status_code = 404


class ValidationError(CatalogError):
    status_code = 422


class ProjectionUnavailableError(CatalogError):
    status_code = 500


class WriteFailureError(CatalogError):
    status_code = 500


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_formula_no(number: int) -> str:
    return f"F-{number:03d}"


_FORMULA_CODE_RE = re.compile(r"#?\s*[Ff]\s*[-_]?\s*0*(\d+)")


def parse_formula_identifier(identifier: Any) -> int:
    """Parse an *explicit business code* carrying an F- prefix.

    F-001 / f1 / #F-001 all denote business number 1.  A bare number is not
    accepted here: callers that want the legacy numeric behaviour use
    :func:`find_formula`, which may also match the internal row id.
    """
    if isinstance(identifier, int):
        raise ValidationError(
            f"公式业务编号必须带 F- 前缀（例如 F-{identifier:03d}）；"
            "纯数字引用请走数字 ID 查找"
        )
    text = str(identifier).strip()
    match = _FORMULA_CODE_RE.fullmatch(text)
    if not match:
        raise ValidationError(
            f"无法识别的公式业务编号 {identifier!r}；请使用 F-001 形式"
        )
    number = int(match.group(1))
    if number <= 0:
        raise ValidationError("公式编号必须为正整数")
    return number


def is_formula_code(identifier: Any) -> bool:
    """Return True when the reference explicitly carries the F- business prefix."""
    if isinstance(identifier, int):
        return False
    return _FORMULA_CODE_RE.fullmatch(str(identifier).strip()) is not None


def claimed_business_number(raw: Any) -> Optional[int]:
    """Business number claimed by a *write request*.

    Registration/correction requests never expose internal row ids: an int or
    bare numeric string is therefore a business-number claim (this also covers
    legacy volumes whose expression parser stored an int).  Withdrawal refs use
    the stricter :func:`is_formula_code` check because they are free-form.
    """
    if isinstance(raw, int):
        number = int(raw)
    elif is_formula_code(raw):
        number = parse_formula_identifier(raw)
    else:
        text = str(raw).strip()
        if not text.isdigit():
            return None
        number = int(text)
    return number if number > 0 else None


def find_formula(db: Session, identifier: Any) -> Optional[models.Formula]:
    """Locate a formula by reference.

    * An F- prefixed reference is a **business number** (``formula_no``) only;
      it never falls back to the internal primary key, so F-001 can never
      resolve to the row whose internal id happens to be 1 (e.g. F-100).
    * A plain numeric reference keeps the legacy behaviour: match
      ``formula_no`` first, then the internal row id.
    """
    if isinstance(identifier, int):
        number = int(identifier)
    elif is_formula_code(identifier):
        number = parse_formula_identifier(identifier)
        return (
            db.query(models.Formula)
            .filter(models.Formula.formula_no == number)
            .first()
        )
    else:
        text = str(identifier).strip()
        if text.isdigit():
            number = int(text)
        else:
            raise ValidationError(
                f"无法识别的公式编号 {identifier!r}；请使用数字或 F-001 形式"
            )
    if number <= 0:
        raise ValidationError("公式编号必须为正整数")
    row = db.query(models.Formula).filter(models.Formula.formula_no == number).first()
    if row is None:
        # Numeric REST ids are also accepted when they differ from formula_no.
        row = db.get(models.Formula, number)
    return row


def find_known_unit(db: Session, identifier: Any) -> Optional[models.KnownUnit]:
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.strip().isdigit()):
        number = int(identifier)
        row = (
            db.query(models.KnownUnit)
            .filter(
                or_(
                    models.KnownUnit.id == number,
                    models.KnownUnit.evidence_no == f"KU-{number:04d}",
                    models.KnownUnit.evidence_no == f"KU-{number:03d}",
                    models.KnownUnit.evidence_no == f"KU-{number}",
                )
            )
            .first()
        )
        if row:
            return row

    text = str(identifier).strip()
    row = db.query(models.KnownUnit).filter(models.KnownUnit.evidence_no == text).first()
    if row:
        return row

    match = re.fullmatch(r"KU\s*[-_]?\s*0*(\d+)", text, flags=re.IGNORECASE)
    if match:
        number = int(match.group(1))
        return (
            db.query(models.KnownUnit)
            .filter(
                or_(
                    models.KnownUnit.id == number,
                    models.KnownUnit.evidence_no == f"KU-{number:04d}",
                    models.KnownUnit.evidence_no == f"KU-{number:03d}",
                    models.KnownUnit.evidence_no == f"KU-{number}",
                )
            )
            .first()
        )
    return None


def field_dict(field: models.Field) -> Dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "display_name": field.display_name,
        "description": field.description,
        "created_at": field.created_at.isoformat() if field.created_at else None,
        "updated_at": field.updated_at.isoformat() if field.updated_at else None,
    }


def known_dict(row: models.KnownUnit, field_name: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": row.id,
        "evidence_no": row.evidence_no,
        "field_id": row.field_id,
        "field": field_name or (row.field.name if row.field else None),
        "unit": row.unit,
        "note": row.note,
        "active": bool(row.active),
        "version": row.version,
        "source_batch_id": row.source_batch_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
    }


def formula_dict(row: models.Formula, names: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
    names = names or {}

    def name(field_id: int) -> Optional[str]:
        return names.get(field_id)

    result_name = name(row.result_field_id)
    left_name = name(row.left_field_id)
    right_name = name(row.right_field_id)
    expression = row.expression
    if result_name and left_name and right_name:
        expression = expression_text(result_name, left_name, row.operator, right_name)
    return {
        "id": row.id,
        "formula_no": row.formula_no,
        "formula_code": format_formula_no(row.formula_no),
        "result_field_id": row.result_field_id,
        "left_field_id": row.left_field_id,
        "right_field_id": row.right_field_id,
        "result": result_name,
        "left": left_name,
        "right": right_name,
        "operator": row.operator,
        "expression": expression,
        "note": row.note,
        "active": bool(row.active),
        "version": row.version,
        "source_batch_id": row.source_batch_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
    }


class CatalogService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.seed_field_ids: Set[int] = set()
        self.old_group_ids: Set[int] = set()

    def remember(self, field_ids: Iterable[Optional[int]]) -> None:
        ids = {int(value) for value in field_ids if value is not None}
        if not ids:
            return
        self.seed_field_ids.update(ids)
        rows = (
            self.db.query(models.FieldEffective)
            .filter(models.FieldEffective.field_id.in_(ids))
            .all()
        )
        self.old_group_ids.update(row.group_id for row in rows)

    def get_field_by_name(self, name: str) -> Optional[models.Field]:
        return self.db.query(models.Field).filter(models.Field.name == name.strip()).first()

    def get_or_create_field(self, name: str, batch_id: str) -> models.Field:
        name = name.strip()
        field = self.get_field_by_name(name)
        if field is None:
            field = models.Field(name=name)
            self.db.add(field)
            self.db.flush()
            self.remember([field.id])
        return field

    def apply_batch(self, payload: schemas.BatchRequest) -> Dict[str, Any]:
        batch_id = "B-" + uuid.uuid4().hex[:16]
        batch = models.Batch(
            batch_id=batch_id,
            batch_no=payload.batch_no,
            note=payload.note,
            status="processing",
            request_json=payload.model_dump(mode="json"),
            result_json={},
        )
        self.db.add(batch)
        self.db.flush()

        applied_fields: List[Dict[str, Any]] = []
        applied_known: List[Dict[str, Any]] = []
        applied_formulas: List[Dict[str, Any]] = []
        withdrawn_known: List[Dict[str, Any]] = []
        withdrawn_formulas: List[Dict[str, Any]] = []

        # 1. Fields: register metadata or correct/rename an existing field.
        for item in payload.fields:
            field: Optional[models.Field] = None
            if item.field_id is not None:
                field = self.db.get(models.Field, item.field_id)
                if field is None:
                    raise NotFoundError(f"字段 ID {item.field_id} 不存在")
            elif item.current_name:
                field = self.get_field_by_name(item.current_name)
                if field is None:
                    raise NotFoundError(f"字段 {item.current_name!r} 不存在，无法更正")
            else:
                field = self.get_field_by_name(item.name)

            if field is None:
                field = models.Field(name=item.name)
                self.db.add(field)
                self.db.flush()
                self.remember([field.id])
            else:
                self.remember([field.id])
                if item.name != field.name:
                    clash = self.get_field_by_name(item.name)
                    if clash is not None and clash.id != field.id:
                        raise ValidationError(f"字段名 {item.name!r} 已被其他字段占用")
                    field.name = item.name
                    # Stored expressions use names; regenerate formulas below.
                    self._regenerate_expressions_for_field(field.id)

            field.display_name = item.display_name if item.display_name is not None else field.display_name
            field.description = item.description if item.description is not None else field.description
            if item.note is not None:
                field.description = item.note
            self.db.flush()
            applied_fields.append(field_dict(field))

        # 2. Known units.  Multiple active facts for one field are allowed so
        #    that a genuine contradiction can be reported as a conflict core.
        for item in payload.known_units:
            row: Optional[models.KnownUnit] = None
            if item.evidence_no is not None:
                row = find_known_unit(self.db, item.evidence_no)
                if row is None and isinstance(item.evidence_no, str):
                    # A supplied business code may be new; numeric codes get
                    # canonical KU-000n formatting only when they look numeric.
                    pass
                elif row is not None:
                    self.remember([row.field_id])

            field = self.get_or_create_field(item.field, batch_id)
            self.remember([field.id])

            if row is not None:
                row.field_id = field.id
                row.unit = normalize_unit(item.unit)
                row.note = item.note if item.note is not None else row.note
                row.active = True
                row.withdrawn_at = None
                row.version += 1
                row.source_batch_id = batch_id
            else:
                evidence_no = self._new_known_evidence_no(item.evidence_no)
                row = models.KnownUnit(
                    evidence_no=evidence_no,
                    field_id=field.id,
                    unit=normalize_unit(item.unit),
                    note=item.note,
                    active=True,
                    version=1,
                    source_batch_id=batch_id,
                )
                self.db.add(row)
            self.db.flush()
            self.remember([row.field_id])
            applied_known.append(known_dict(row, field.name))

        # 3. Numbered formulas.
        for item in payload.formulas:
            existing: Optional[models.Formula] = None
            number: Optional[int] = None
            explicit_business_code = item.formula_no is not None and is_formula_code(
                item.formula_no
            )
            if item.formula_no is not None:
                if explicit_business_code:
                    # F- prefixed references resolve by business number only;
                    # they never touch another row's internal primary key.
                    number = parse_formula_identifier(item.formula_no)
                    existing = (
                        self.db.query(models.Formula)
                        .filter(models.Formula.formula_no == number)
                        .first()
                    )
                else:
                    # Plain numeric reference: business number with the legacy
                    # internal-id fallback when no such business number exists.
                    existing = find_formula(self.db, item.formula_no)
                    number = existing.formula_no if existing is not None else int(
                        item.formula_no
                    )
                if existing is not None:
                    self.remember(
                        [
                            existing.result_field_id,
                            existing.left_field_id,
                            existing.right_field_id,
                        ]
                    )

            result_field = self.get_or_create_field(item.result, batch_id)
            left_field = self.get_or_create_field(item.left, batch_id)
            right_field = self.get_or_create_field(item.right, batch_id)
            self.remember([result_field.id, left_field.id, right_field.id])

            if existing is not None:
                formula = existing
                formula.version += 1
            else:
                if number is None:
                    max_no = self.db.query(models.Formula.formula_no).order_by(
                        models.Formula.formula_no.desc()
                    ).first()
                    number = (max_no[0] if max_no else 0) + 1
                # Guard against a caller-supplied business number that is free
                # as formula_no while a stale internal id still occupies it.
                while (
                    self.db.query(models.Formula.id)
                    .filter(models.Formula.formula_no == number)
                    .first()
                    is not None
                ):
                    number += 1
                formula = models.Formula(formula_no=number, version=1)
                self.db.add(formula)

            formula.result_field_id = result_field.id
            formula.left_field_id = left_field.id
            formula.right_field_id = right_field.id
            formula.operator = item.operator
            formula.expression = expression_text(
                result_field.name, left_field.name, item.operator, right_field.name
            )
            formula.note = item.note if item.note is not None else formula.note
            formula.active = True
            formula.withdrawn_at = None
            formula.source_batch_id = batch_id
            self.db.flush()
            self.remember(
                [formula.result_field_id, formula.left_field_id, formula.right_field_id]
            )
            applied_formulas.append(formula_dict(formula, self._name_map_for_formula(formula)))

        # 4. Withdrawals.  Explicit top-level arrays and generic objects are
        #    both supported.  Withdrawing an already-withdrawn item is idempotent.
        known_refs = list(payload.withdraw_known_units)
        formula_refs = list(payload.withdraw_formulas)
        for item in payload.withdrawals:
            kind = item.type.strip().lower()
            if kind in ("known", "known_unit", "known-unit", "unit", "已知单位"):
                known_refs.append(item.id)
            elif kind in ("formula", "f", "公式"):
                formula_refs.append(item.id)
            else:
                raise ValidationError(
                    f"未知撤回类型 {item.type!r}；只支持 formula / known_unit"
                )

        for ref in known_refs:
            row = find_known_unit(self.db, ref)
            if row is None:
                raise NotFoundError(f"已知单位编号 {ref!r} 不存在，无法撤回")
            self.remember([row.field_id])
            if row.active:
                row.active = False
                row.withdrawn_at = utcnow()
            row.source_batch_id = batch_id
            self.db.flush()
            withdrawn_known.append(known_dict(row))

        for ref in formula_refs:
            row = find_formula(self.db, ref)
            if row is None:
                raise NotFoundError(f"找不到公式编号 {ref!r} 对应的公式，无法撤回；目录未改变")
            self.remember(
                [row.result_field_id, row.left_field_id, row.right_field_id]
            )
            if row.active:
                row.active = False
                row.withdrawn_at = utcnow()
            row.source_batch_id = batch_id
            self.db.flush()
            withdrawn_formulas.append(
                formula_dict(row, self._name_map_for_formula(row))
            )

        # Capture every member of old groups before replacing effective rows.
        if self.old_group_ids:
            old_members = (
                self.db.query(models.FieldEffective.field_id)
                .filter(models.FieldEffective.group_id.in_(self.old_group_ids))
                .all()
            )
            self.seed_field_ids.update(row[0] for row in old_members)

        affected = self._recompute_affected(batch_id)

        feasible_groups = [group for group in affected["groups"] if group["feasible"]]
        conflict_groups = [group for group in affected["groups"] if not group["feasible"]]
        status = "effective" if not conflict_groups else "conflict"

        warehouse = self.list_warehouse()
        result = {
            "batch_id": batch_id,
            "batch_no": payload.batch_no,
            "status": status,
            "solvable": not conflict_groups,
            "allowed_units": ALLOWED_UNITS,
            "applied": {
                "fields": applied_fields,
                "known_units": applied_known,
                "formulas": applied_formulas,
            },
            "withdrawn": {
                "known_units": withdrawn_known,
                "formulas": withdrawn_formulas,
            },
            "recomputed_group_count": len(affected["groups"]),
            "recomputed_groups": affected["groups"],
            "affected_groups": affected["groups"],
            "conflicts": [
                {
                    "group_id": group["group_id"],
                    "group_key": group["group_key"],
                    "cannot_hold_together": group["conflict_core"],
                    "conflict_core": group["conflict_core"],
                }
                for group in conflict_groups
            ],
            # Convenience for callers expecting exactly one first conflict set.
            "unsat_core": conflict_groups[0]["conflict_core"] if conflict_groups else [],
            "warehouse": warehouse,
            "warehouse_count": len(warehouse),
        }

        batch.status = status
        batch.result_json = result
        self.db.commit()
        return result

    def _new_known_evidence_no(self, supplied: Any) -> str:
        if supplied is not None:
            text = str(supplied).strip()
            # Numeric supplied IDs are canonicalized; non-numeric codes are kept.
            if text.isdigit():
                return f"KU-{int(text):04d}"
            match = re.fullmatch(r"KU\s*[-_]?\s*0*(\d+)", text, flags=re.IGNORECASE)
            if match and text.upper().replace(" ", "") != f"KU-{int(match.group(1)):04d}":
                return f"KU-{int(match.group(1)):04d}"
            return text

        max_id = self.db.query(models.KnownUnit.id).order_by(models.KnownUnit.id.desc()).first()
        next_id = (max_id[0] if max_id else 0) + 1
        return f"KU-{next_id:04d}"

    def _name_map_for_formula(self, formula: models.Formula) -> Dict[int, str]:
        ids = [formula.result_field_id, formula.left_field_id, formula.right_field_id]
        rows = self.db.query(models.Field).filter(models.Field.id.in_(ids)).all()
        return {row.id: row.name for row in rows}

    def _regenerate_expressions_for_field(self, field_id: int) -> None:
        formulas = (
            self.db.query(models.Formula)
            .filter(
                or_(
                    models.Formula.result_field_id == field_id,
                    models.Formula.left_field_id == field_id,
                    models.Formula.right_field_id == field_id,
                )
            )
            .all()
        )
        if not formulas:
            return
        field_ids = {
            value
            for formula in formulas
            for value in (
                formula.result_field_id,
                formula.left_field_id,
                formula.right_field_id,
            )
        }
        fields = self.db.query(models.Field).filter(models.Field.id.in_(field_ids)).all()
        names = {row.id: row.name for row in fields}
        for formula in formulas:
            formula.expression = expression_text(
                names[formula.result_field_id],
                names[formula.left_field_id],
                formula.operator,
                names[formula.right_field_id],
            )

    def _formula_closure(self, seed_ids: Set[int]) -> Tuple[Set[int], Set[int]]:
        """Traverse only active formula hyperedges touching the seed fields."""

        reached = set(seed_ids)
        frontier = set(seed_ids)
        formula_ids: Set[int] = set()

        while frontier:
            rows = (
                self.db.query(models.Formula)
                .filter(
                    models.Formula.active.is_(True),
                    or_(
                        models.Formula.result_field_id.in_(frontier),
                        models.Formula.left_field_id.in_(frontier),
                        models.Formula.right_field_id.in_(frontier),
                    ),
                )
                .all()
            )
            new_fields: Set[int] = set()
            for formula in rows:
                if formula.id in formula_ids:
                    # Still safe to inspect; all its fields were added before.
                    pass
                formula_ids.add(formula.id)
                ids = {
                    formula.result_field_id,
                    formula.left_field_id,
                    formula.right_field_id,
                }
                for field_id in ids:
                    if field_id not in reached:
                        reached.add(field_id)
                        new_fields.add(field_id)
            frontier = new_fields

        return reached, formula_ids

    def _recompute_affected(self, batch_id: str) -> Dict[str, Any]:
        seed_ids = {field_id for field_id in self.seed_field_ids if field_id is not None}
        if not seed_ids:
            return {"groups": []}

        reached_ids, _ = self._formula_closure(seed_ids)
        reached_ids.update(seed_ids)

        field_rows = self.db.query(models.Field).filter(models.Field.id.in_(reached_ids)).all()
        field_names = {row.id: row.name for row in field_rows}
        # Should not happen due FKs, but keep IDs represented if needed.
        for field_id in reached_ids:
            field_names.setdefault(field_id, f"field_{field_id}")

        formula_rows = (
            self.db.query(models.Formula)
            .filter(
                models.Formula.active.is_(True),
                or_(
                    models.Formula.result_field_id.in_(reached_ids),
                    models.Formula.left_field_id.in_(reached_ids),
                    models.Formula.right_field_id.in_(reached_ids),
                ),
            )
            .all()
        )
        known_rows = (
            self.db.query(models.KnownUnit)
            .filter(
                models.KnownUnit.active.is_(True),
                models.KnownUnit.field_id.in_(reached_ids),
            )
            .all()
        )

        formula_rules: List[FormulaRule] = []
        for row in formula_rows:
            formula_rules.append(
                FormulaRule(
                    ref=format_formula_no(row.formula_no),
                    result=field_names[row.result_field_id],
                    left=field_names[row.left_field_id],
                    operator=row.operator,
                    right=field_names[row.right_field_id],
                )
            )

        known_facts: List[KnownFact] = []
        for row in known_rows:
            known_facts.append(
                KnownFact(
                    ref=row.evidence_no,
                    field=field_names[row.field_id],
                    unit=row.unit,
                )
            )

        # Hypergraph DSU gives the current split/merge components.
        parent = {field_id: field_id for field_id in reached_ids}

        def find(field_id: int) -> int:
            root = field_id
            while parent[root] != root:
                root = parent[root]
            while parent[field_id] != field_id:
                nxt = parent[field_id]
                parent[field_id] = root
                field_id = nxt
            return root

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for row in formula_rows:
            ids = [row.result_field_id, row.left_field_id, row.right_field_id]
            for field_id in ids[1:]:
                union(ids[0], field_id)

        components_map: Dict[int, List[int]] = {}
        for field_id in reached_ids:
            components_map.setdefault(find(field_id), []).append(field_id)
        component_ids = [sorted(ids) for ids in components_map.values()]

        # Clear effective list projections for exactly this affected closure.
        self.db.query(models.WarehouseEntry).filter(
            models.WarehouseEntry.field_id.in_(reached_ids)
        ).delete(synchronize_session=False)
        self.db.query(models.FieldEffective).filter(
            models.FieldEffective.field_id.in_(reached_ids)
        ).delete(synchronize_session=False)
        self.db.flush()

        current_group_keys: Set[str] = set()
        group_results: List[Dict[str, Any]] = []

        known_by_no = {row.evidence_no: row for row in known_rows}
        formula_by_code = {format_formula_no(row.formula_no): row for row in formula_rows}

        for component in component_ids:
            comp_set = set(component)
            # Stable, human-readable response order; solving itself uses IDs.
            names = sorted(field_names[field_id] for field_id in component)
            comp_formula_rules = [
                rule
                for rule in formula_rules
                if field_name_to_id(field_names, rule.result) in comp_set
            ]
            comp_known_rules = [
                fact
                for fact in known_facts
                if field_name_to_id(field_names, fact.field) in comp_set
            ]

            group_key = self._group_key(component)
            current_group_keys.add(group_key)
            group = (
                self.db.query(models.ReasoningGroup)
                .filter(models.ReasoningGroup.group_key == group_key)
                .first()
            )
            if group is None:
                group = models.ReasoningGroup(group_key=group_key, version=0)
                self.db.add(group)
            group.feasible = True
            group.conflict_core = []
            group.formula_count = len(comp_formula_rules)
            group.known_count = len(comp_known_rules)
            group.version += 1
            group.last_batch_id = batch_id
            group.computed_at = utcnow()
            self.db.flush()

            reasoning = solve_group(names, comp_formula_rules, comp_known_rules)
            group.feasible = reasoning.feasible

            enriched_core: List[Dict[str, Any]] = []
            for item in reasoning.conflict_core:
                if item.type == "formula":
                    row = formula_by_code.get(item.id)
                    enriched_core.append(
                        {
                            "type": "formula",
                            "id": item.id,
                            "formula_code": item.id,
                            "formula_no": row.formula_no if row else parse_safe_no(item.id),
                            "expression": row.expression if row else item.id,
                            "active": bool(row.active) if row else None,
                        }
                    )
                else:
                    row = known_by_no.get(item.id)
                    enriched_core.append(
                        {
                            "type": "known_unit",
                            "id": item.id,
                            "evidence_no": item.id,
                            "field": field_names.get(row.field_id) if row else None,
                            "unit": row.unit if row else None,
                            "active": bool(row.active) if row else None,
                        }
                    )
            group.conflict_core = enriched_core

            candidates_json: Dict[str, List[str]] = {}
            unique_json: Dict[str, str] = {}
            for field_id in component:
                name = field_names[field_id]
                units = sorted(
                    reasoning.candidates.get(name, frozenset()),
                    key=ALLOWED_UNITS.index,
                )
                candidates_json[name] = units
                unique_unit = units[0] if reasoning.feasible and len(units) == 1 else None
                if unique_unit:
                    unique_json[name] = unique_unit

                effective = models.FieldEffective(
                    field_id=field_id,
                    group_id=group.id,
                    feasible=reasoning.feasible,
                    candidates=units,
                    unique_unit=unique_unit,
                    updated_at=utcnow(),
                )
                self.db.add(effective)
                if unique_unit:
                    entry = models.WarehouseEntry(
                        field_id=field_id,
                        unit=unique_unit,
                        group_id=group.id,
                        batch_id=batch_id,
                        decided_at=utcnow(),
                    )
                    self.db.add(entry)

            self.db.flush()
            group_results.append(
                {
                    "group_id": group.group_key,
                    "group_key": group.group_key,
                    "group_pk": group.id,
                    "version": group.version,
                    "fields": names,
                    "field_ids": component,
                    "feasible": reasoning.feasible,
                    "candidates": candidates_json,
                    "unique_units": unique_json,
                    "conflict_core": enriched_core,
                    "formula_count": group.formula_count,
                    "known_count": group.known_count,
                }
            )

        # Remove old split/merge group rows belonging to touched old groups.
        if self.old_group_ids:
            stale_groups = (
                self.db.query(models.ReasoningGroup)
                .filter(models.ReasoningGroup.id.in_(self.old_group_ids))
                .all()
            )
            for group in stale_groups:
                if group.group_key not in current_group_keys:
                    self.db.delete(group)
        self.db.flush()

        # Stable response order.
        group_results.sort(key=lambda item: item["group_key"])
        return {"groups": group_results}

    @staticmethod
    def _group_key(component_ids: List[int]) -> str:
        raw = ",".join(str(value) for value in sorted(component_ids)).encode("utf-8")
        return "G-" + hashlib.sha1(raw).hexdigest()[:12]

    # ----- Direct read models; these functions do not invoke reasoning -----

    def list_warehouse(self) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(models.WarehouseEntry, models.Field, models.ReasoningGroup)
            .join(models.Field, models.WarehouseEntry.field_id == models.Field.id)
            .join(
                models.ReasoningGroup,
                models.WarehouseEntry.group_id == models.ReasoningGroup.id,
            )
            .order_by(models.Field.name.asc())
            .all()
        )
        return [
            {
                "field_id": field.id,
                "field": field.name,
                "unit": entry.unit,
                "group_id": group.group_key,
                "batch_id": entry.batch_id,
                "decided_at": entry.decided_at.isoformat() if entry.decided_at else None,
            }
            for entry, field, group in rows
        ]

    def list_candidates(self) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(models.FieldEffective, models.Field, models.ReasoningGroup)
            .join(models.Field, models.FieldEffective.field_id == models.Field.id)
            .join(
                models.ReasoningGroup,
                models.FieldEffective.group_id == models.ReasoningGroup.id,
            )
            .order_by(models.Field.name.asc())
            .all()
        )
        return [
            {
                "field_id": field.id,
                "field": field.name,
                "feasible": effective.feasible,
                "candidates": effective.candidates or [],
                "is_unique": bool(effective.unique_unit),
                "unique_unit": effective.unique_unit,
                "group_id": group.group_key,
                "group_version": group.version,
                "updated_at": effective.updated_at.isoformat() if effective.updated_at else None,
            }
            for effective, field, group in rows
        ]

    # ----- Historical F- number swap repair (old SQLite volumes) -----

    def repair_formula_swap(self, mistaken_batch_id: str) -> Dict[str, Any]:
        """Repair a pair of formulas whose F- business numbers were swapped.

        The only required input is the mistaken registration batch id; every
        other piece of evidence comes from that batch's stored request and
        receipt and from later batches that mention one of the two involved
        business numbers.  The operation is idempotent: a repeat call returns
        the prior ledger entry without touching later corrections.
        """

        bad_batch = (
            self.db.query(models.Batch)
            .filter(models.Batch.batch_id == mistaken_batch_id)
            .first()
        )
        if bad_batch is None:
            raise NotFoundError(f"批次 {mistaken_batch_id!r} 不存在，无法修复")

        prior = (
            self.db.query(models.FormulaRepair)
            .filter(models.FormulaRepair.mistaken_batch_id == mistaken_batch_id)
            .first()
        )
        if prior is not None:
            return {
                "status": "already_repaired",
                "mistaken_batch_id": mistaken_batch_id,
                "repair_batch_id": prior.repair_batch_id,
                "victim_formula_code": format_formula_no(prior.victim_formula_no),
                "missing_formula_code": format_formula_no(prior.missing_formula_no),
                "recomputed_groups": [],
                "recomputed_group_count": 0,
                "warehouse": self.list_warehouse(),
                "warehouse_count": len(self.list_warehouse()),
                "note": "该误登记批次已修复过；重复修复不会覆盖后来的更正",
                **(prior.detail_json or {}),
            }

        pair = self._detect_swap_pair(bad_batch)
        if pair is None:
            raise ValidationError(
                f"批次 {mistaken_batch_id!r} 中未发现 F- 编号串号（同批 F- 业务编号在回执里落到别的业务号）；目录不变"
            )
        victim_no, missing_no = pair

        # Only read batches related to these two business numbers: the
        # mistaken batch itself, earlier batches whose receipts touch the
        # victim number, and later batches that explicitly reference either
        # of the two F- business numbers.
        target_codes = {format_formula_no(victim_no), format_formula_no(missing_no)}
        target_numbers = {victim_no, missing_no}
        all_batches = (
            self.db.query(models.Batch).order_by(models.Batch.id.asc()).all()
        )
        relevant = [
            batch
            for batch in all_batches
            if batch.id == bad_batch.id
            or (
                batch.id < bad_batch.id
                and self._batch_receipt_touches_numbers(batch, target_numbers)
            )
            or (
                batch.id > bad_batch.id
                and self._batch_references_numbers(batch, target_codes, target_numbers)
            )
        ]

        field_ids = self._collect_swap_events(
            relevant, bad_batch, victim_no, missing_no
        )

        # Rebuild the two rows purely from the collected event timelines.
        for number in (victim_no, missing_no):
            row = (
                self.db.query(models.Formula)
                .filter(models.Formula.formula_no == number)
                .first()
            )
            if row is None:
                # Do not flush until the NOT NULL columns are populated below.
                row = models.Formula(formula_no=number, version=1)
                self.db.add(row)
            state = self._formula_state_for(number)
            row.result_field_id = state["result_field_id"]
            row.left_field_id = state["left_field_id"]
            row.right_field_id = state["right_field_id"]
            row.operator = state["operator"]
            row.expression = state["expression"]
            row.note = state["note"]
            row.active = state["active"]
            row.version = state["version"]
            row.withdrawn_at = (
                datetime.fromisoformat(state["withdrawn_at"])
                if state["withdrawn_at"]
                else None
            )
            row.source_batch_id = state["source_batch_id"]
            self.db.flush()
            self.remember(
                [row.result_field_id, row.left_field_id, row.right_field_id]
            )

        repair_batch_id = "B-" + uuid.uuid4().hex[:16]
        # Recompute only the connected groups touched by this formula pair.
        affected = self._recompute_affected(repair_batch_id)

        ledger = models.FormulaRepair(
            mistaken_batch_id=mistaken_batch_id,
            repair_batch_id=repair_batch_id,
            victim_formula_no=victim_no,
            missing_formula_no=missing_no,
            detail_json={
                "formula_codes": sorted(target_codes),
                "relevant_batch_ids": [batch.batch_id for batch in relevant],
            },
        )
        self.db.add(ledger)

        warehouse = self.list_warehouse()
        result = {
            "status": "repaired",
            "mistaken_batch_id": mistaken_batch_id,
            "repair_batch_id": repair_batch_id,
            "victim_formula_code": format_formula_no(victim_no),
            "missing_formula_code": format_formula_no(missing_no),
            "relevant_batch_ids": [batch.batch_id for batch in relevant],
            "recomputed_groups": affected["groups"],
            "recomputed_group_count": len(affected["groups"]),
            "warehouse": warehouse,
            "warehouse_count": len(warehouse),
            "restored": {
                format_formula_no(number): self._formula_state_for(number)
                for number in (victim_no, missing_no)
            },
        }
        repair_batch = models.Batch(
            batch_id=repair_batch_id,
            batch_no=f"REPAIR-{mistaken_batch_id}",
            note=(
                f"按原请求与回执修复串号公式 "
                f"{format_formula_no(victim_no)}/{format_formula_no(missing_no)}"
            ),
            status="effective",
            request_json={"repair_mistaken_batch_id": mistaken_batch_id},
            result_json=result,
        )
        self.db.add(repair_batch)
        try:
            self.db.commit()
        except Exception as exc:  # pragma: no cover - defensive write guard
            self.db.rollback()
            raise WriteFailureError(
                f"串号修复写入失败，目录保持不变：{type(exc).__name__}: {exc}"
            ) from exc
        return result

    def _detect_swap_pair(
        self, bad_batch: models.Batch
    ) -> Optional[Tuple[int, int]]:
        """Find (victim_number, missing_number) from one mistaken batch."""

        request = bad_batch.request_json or {}
        result = bad_batch.result_json or {}
        request_formulas = request.get("formulas") or []
        receipt_formulas = (
            (result.get("applied") or {}).get("formulas") or []
        )
        for req, rcpt in zip(request_formulas, receipt_formulas):
            if not isinstance(req, dict) or not isinstance(rcpt, dict):
                continue
            wanted = claimed_business_number(req.get("formula_no"))
            if wanted is None:
                continue
            landed = rcpt.get("formula_no")
            if isinstance(landed, int) and landed != wanted:
                return landed, wanted

        # Withdrawal-only batches can also carry the mistaken reference.
        for ref in self._all_withdrawal_refs(bad_batch):
            if not is_formula_code(ref):
                continue
            wanted = parse_formula_identifier(ref)
            withdrawn = (result.get("withdrawn") or {}).get("formulas") or []
            for item in withdrawn:
                landed = item.get("formula_no")
                if isinstance(landed, int) and landed != wanted:
                    return landed, wanted
        return None

    @staticmethod
    def _all_withdrawal_refs(batch: models.Batch) -> List[Any]:
        request = batch.request_json or {}
        refs: List[Any] = list(request.get("withdraw_formulas") or [])
        for item in request.get("withdrawals") or []:
            if isinstance(item, dict) and str(item.get("type", "")).lower() in (
                "formula",
                "f",
                "公式",
            ):
                refs.append(item.get("id"))
        return refs

    def _batch_receipt_touches_numbers(
        self, batch: models.Batch, target_numbers: Set[int]
    ) -> bool:
        result = batch.result_json or {}
        for section in ("applied", "withdrawn"):
            for item in (result.get(section) or {}).get("formulas") or []:
                if isinstance(item, dict) and item.get("formula_no") in target_numbers:
                    return True
        return False

    def _batch_references_numbers(
        self,
        batch: models.Batch,
        target_codes: Set[str],
        target_numbers: Set[int],
    ) -> bool:
        request = batch.request_json or {}
        result = batch.result_json or {}
        for item in request.get("formulas") or []:
            if isinstance(item, dict):
                number = claimed_business_number(item.get("formula_no"))
                if number in target_numbers:
                    return True
        for ref in self._all_withdrawal_refs(batch):
            if is_formula_code(ref) and format_formula_no(
                parse_formula_identifier(ref)
            ) in target_codes:
                return True
        for section in ("applied", "withdrawn"):
            for item in (result.get(section) or {}).get("formulas") or []:
                if isinstance(item, dict) and item.get("formula_no") in target_numbers:
                    return True
        return False

    def _collect_swap_events(
        self,
        batches: List[models.Batch],
        bad_batch: models.Batch,
        victim_no: int,
        missing_no: int,
    ) -> Set[int]:
        """Replay every stored event for the two numbers; return touched field ids."""

        self._formula_timelines: Dict[int, List[Dict[str, Any]]] = {
            victim_no: [],
            missing_no: [],
        }
        field_ids: Set[int] = set()
        target_numbers = {victim_no, missing_no}

        for batch in batches:
            request = batch.request_json or {}
            result = batch.result_json or {}
            applied = (result.get("applied") or {}).get("formulas") or []
            withdrawn = (result.get("withdrawn") or {}).get("formulas") or []
            req_formulas = request.get("formulas") or []

            for index, req in enumerate(req_formulas):
                if not isinstance(req, dict):
                    raise ValidationError(
                        f"批次 {batch.batch_id!r} 的公式请求不是结构化对象；材料不足，目录不变"
                    )
                number = claimed_business_number(req.get("formula_no"))
                if number is None or number not in target_numbers:
                    continue
                if index >= len(applied):
                    raise ValidationError(
                        f"批次 {batch.batch_id!r} 缺少 {format_formula_no(number)} 的回执；材料不足，目录不变"
                    )
                receipt = applied[index]
                self._require_structured_formula(req, batch)
                event = self._event_from_receipt(receipt, batch, req, "upsert")
                self._formula_timelines[number].append(event)
                field_ids.update(
                    value
                    for value in (
                        event["result_field_id"],
                        event["left_field_id"],
                        event["right_field_id"],
                    )
                    if value is not None
                )

            # Withdrawals: only explicit F- codes are unambiguous enough.
            refs = self._all_withdrawal_refs(batch)
            receipt_by_index = list(withdrawn)
            for index, ref in enumerate(refs):
                if not is_formula_code(ref):
                    raise ValidationError(
                        f"批次 {batch.batch_id!r} 的撤回引用 {ref!r} 未写明 F- 编号；"
                        "无法判定应归属哪个业务号，材料不足，目录不变"
                    )
                number = parse_formula_identifier(ref)
                if number not in target_numbers:
                    continue
                if index >= len(receipt_by_index):
                    raise ValidationError(
                        f"批次 {batch.batch_id!r} 缺少 {format_formula_no(number)} 的撤回回执；材料不足，目录不变"
                    )
                event = self._event_from_receipt(
                    receipt_by_index[index], batch, None, "withdraw"
                )
                self._formula_timelines[number].append(event)

        if not self._formula_timelines[victim_no]:
            raise ValidationError(
                "被串号覆盖的公式在历史批次中找不到原始登记请求/回执；材料不足，目录不变"
            )
        if not any(
            event["kind"] == "upsert"
            for event in self._formula_timelines[missing_no]
        ):
            raise ValidationError(
                f"误登记批次中缺少 {format_formula_no(missing_no)} 的结构化登记请求；"
                "材料不足，目录不变"
            )

        existing_field_ids = {
            row[0] for row in self.db.query(models.Field.id).all()
        }
        unknown = field_ids - existing_field_ids
        if unknown:
            raise ValidationError(
                f"回执引用的字段 ID {sorted(unknown)} 在当前目录不存在；材料不足，目录不变"
            )
        return field_ids

    def _require_structured_formula(
        self, req: Dict[str, Any], batch: models.Batch
    ) -> None:
        for key in ("result", "left", "operator", "right"):
            value = req.get(key)
            if value is None or not str(value).strip():
                raise ValidationError(
                    f"批次 {batch.batch_id!r} 中公式 {req.get('formula_no')!r} "
                    "未用 result/left/operator/right 结构化字段提交；材料不足，目录不变"
                )
        try:
            normalize_operator(req["operator"])
        except ValueError as exc:
            raise ValidationError(f"材料不足，目录不变：{exc}") from exc

    def _event_from_receipt(
        self,
        receipt: Dict[str, Any],
        batch: models.Batch,
        req: Optional[Dict[str, Any]],
        kind: str,
    ) -> Dict[str, Any]:
        required = ("result_field_id", "left_field_id", "right_field_id", "operator")
        for key in required:
            if receipt.get(key) is None:
                raise ValidationError(
                    f"批次 {batch.batch_id!r} 的公式回执缺少字段 {key}；材料不足，目录不变"
                )
        note = req.get("note") if req else None
        return {
            "kind": kind,
            "batch_id": batch.batch_id,
            "result_field_id": receipt["result_field_id"],
            "left_field_id": receipt["left_field_id"],
            "right_field_id": receipt["right_field_id"],
            "operator": normalize_operator(receipt["operator"]),
            "note": note,
        }

    def _formula_state_for(self, number: int) -> Dict[str, Any]:
        events = self._formula_timelines.get(number, [])
        upserts = [event for event in events if event["kind"] == "upsert"]
        if not upserts:
            raise ValidationError(
                f"{format_formula_no(number)} 没有可用于恢复的登记/更正回执；材料不足，目录不变"
            )
        last_upsert = upserts[-1]
        last_event = events[-1]
        active = last_event["kind"] != "withdraw"
        names = {
            row.id: row.name
            for row in self.db.query(models.Field)
            .filter(
                models.Field.id.in_(
                    [
                        last_upsert["result_field_id"],
                        last_upsert["left_field_id"],
                        last_upsert["right_field_id"],
                    ]
                )
            )
            .all()
        }
        withdrawn_at = utcnow() if not active else None
        expression = expression_text(
            names[last_upsert["result_field_id"]],
            names[last_upsert["left_field_id"]],
            last_upsert["operator"],
            names[last_upsert["right_field_id"]],
        )
        return {
            "formula_no": number,
            "formula_code": format_formula_no(number),
            "result_field_id": last_upsert["result_field_id"],
            "left_field_id": last_upsert["left_field_id"],
            "right_field_id": last_upsert["right_field_id"],
            "operator": last_upsert["operator"],
            "expression": expression,
            "note": last_upsert["note"],
            "active": active,
            "withdrawn_at": withdrawn_at.isoformat() if withdrawn_at else None,
            "version": len(upserts),
            "source_batch_id": last_event["batch_id"],
            "event_count": len(events),
        }

    def reconcile_missing(self) -> int:
        """Create projections only for fields missing an effective row."""

        missing = (
            self.db.query(models.Field.id)
            .outerjoin(
                models.FieldEffective,
                models.Field.id == models.FieldEffective.field_id,
            )
            .filter(models.FieldEffective.field_id.is_(None))
            .all()
        )
        ids = {row[0] for row in missing}
        if not ids:
            return 0
        self.remember(ids)
        self._recompute_affected("startup-reconcile")
        self.db.commit()
        return len(ids)

    def get_field_units(self, field_name: str) -> Dict[str, Any]:
        field = self.get_field_by_name(field_name)
        if field is None:
            raise NotFoundError(f"字段 {field_name!r} 不存在")
        effective = self.db.get(models.FieldEffective, field.id)
        if effective is None:
            # Writes and startup reconciliation guarantee this row.  Queries
            # deliberately do not run the solver.
            raise ProjectionUnavailableError(
                f"字段 {field_name!r} 的生效单位结果尚未生成"
            )
        group = self.db.get(models.ReasoningGroup, effective.group_id)
        return {
            "field_id": field.id,
            "field": field.name,
            "display_name": field.display_name,
            "feasible": effective.feasible,
            "candidates": effective.candidates,
            "is_unique": bool(effective.unique_unit),
            "unique_unit": effective.unique_unit,
            "group_id": group.group_key if group else None,
            "group_version": group.version if group else None,
            "updated_at": effective.updated_at.isoformat() if effective.updated_at else None,
        }

    def list_conflicts(self) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(models.ReasoningGroup)
            .filter(models.ReasoningGroup.feasible.is_(False))
            .order_by(models.ReasoningGroup.group_key.asc())
            .all()
        )
        result = []
        for group in rows:
            result.append(
                {
                    "group_id": group.group_key,
                    "cannot_hold_together": group.conflict_core or [],
                    "conflict_core": group.conflict_core or [],
                    "formula_count": group.formula_count,
                    "known_count": group.known_count,
                    "version": group.version,
                    "last_batch_id": group.last_batch_id,
                    "computed_at": group.computed_at.isoformat() if group.computed_at else None,
                }
            )
        return result

    def catalog_snapshot(self, include_withdrawn: bool = True) -> Dict[str, Any]:
        field_rows = self.db.query(models.Field).order_by(models.Field.name.asc()).all()
        names = {row.id: row.name for row in field_rows}

        known_query = self.db.query(models.KnownUnit)
        formula_query = self.db.query(models.Formula)
        if not include_withdrawn:
            known_query = known_query.filter(models.KnownUnit.active.is_(True))
            formula_query = formula_query.filter(models.Formula.active.is_(True))

        known_rows = known_query.order_by(models.KnownUnit.evidence_no.asc()).all()
        formula_rows = formula_query.order_by(models.Formula.formula_no.asc()).all()
        return {
            "fields": [field_dict(row) for row in field_rows],
            "known_units": [known_dict(row, names.get(row.field_id)) for row in known_rows],
            "formulas": [formula_dict(row, names) for row in formula_rows],
        }


def field_name_to_id(names: Dict[int, str], name: str) -> Optional[int]:
    for field_id, field_name in names.items():
        if field_name == name:
            return field_id
    return None


def parse_safe_no(code: str) -> Optional[int]:
    try:
        return parse_formula_identifier(code)
    except CatalogError:
        return None
