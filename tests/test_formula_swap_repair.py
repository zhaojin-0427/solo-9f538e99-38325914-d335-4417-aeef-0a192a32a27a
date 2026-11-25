"""Repair of historical F- number swaps persisted in an old SQLite volume.

The pre-fix service resolved F-001 to the formula row whose internal id was 1
(F-100), so registrations, corrections and withdrawals of F-001 overwrote
F-100 and no F-001 row was ever created.  These tests rebuild that corrupted
volume state (including the original requests and receipts stored per batch),
then exercise the repair endpoint using only those stored records.
"""

from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified

from app.database import SessionLocal
from app.models import Batch, Formula, FormulaRepair
from app.service import format_formula_no


def _set_batch_request_formula_no(batch_id: str, code: str) -> None:
    """Rewrite a stored request so it claims an F- code, mimicking old traffic."""
    db = SessionLocal()
    try:
        batch = db.query(Batch).filter(Batch.batch_id == batch_id).first()
        request = dict(batch.request_json)
        for item in request.get("formulas", []):
            item["formula_no"] = code
        batch.request_json = request
        flag_modified(batch, "request_json")
        db.commit()
    finally:
        db.close()


def _set_batch_withdrawal_ref(batch_id: str, code: str) -> None:
    db = SessionLocal()
    try:
        batch = db.query(Batch).filter(Batch.batch_id == batch_id).first()
        request = dict(batch.request_json)
        request["withdraw_formulas"] = [code]
        batch.request_json = request
        flag_modified(batch, "request_json")
        db.commit()
    finally:
        db.close()


def _build_corrupted_volume(client) -> dict[str, str]:
    """Recreate the legacy swap: F-001 traffic keeps landing on internal row 1."""
    # B1: legitimate F-100 registration; it becomes internal id 1.
    response = client.post(
        "/batches",
        json={
            "fields": [
                {"name": "p", "display_name": "功率"},
                {"name": "h", "display_name": "小时"},
                {"name": "e", "display_name": "电量"},
            ],
            "known_units": [
                {"evidence_no": "KU-P", "field": "p", "unit": "kW"},
                {"evidence_no": "KU-H", "field": "h", "unit": "h"},
            ],
            "formulas": [
                {
                    "formula_no": "F-100",
                    "result": "e",
                    "left": "p",
                    "operator": "*",
                    "right": "h",
                    "note": "原始 F-100",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    b1 = response.json()["batch_id"]

    # B2 (the mistaken batch): client sent F-001, pre-fix code overwrote row 1.
    response = client.post(
        "/formulas",
        json={
            "formula_no": "F-100",
            "result": "p",
            "left": "e",
            "operator": "/",
            "right": "h",
            "note": "本想登记 F-001",
        },
    )
    assert response.status_code == 200
    b2 = response.json()["batch_id"]
    _set_batch_request_formula_no(b2, "F-001")

    # B3: a later correction of F-001 (again really hit F-100 in the old code).
    response = client.put(
        "/formulas/F-100",
        json={"result": "p", "left": "e", "operator": "/", "right": "p"},
    )
    assert response.status_code == 200
    b3 = response.json()["batch_id"]
    _set_batch_request_formula_no(b3, "F-001")

    # B4: withdrawal of F-001 (pre-fix withdrew F-100 instead).
    response = client.post("/formulas/F-100/withdraw")
    assert response.status_code == 200
    b4 = response.json()["batch_id"]
    _set_batch_withdrawal_ref(b4, "F-001")

    return {"b1": b1, "b2": b2, "b3": b3, "b4": b4}


def _formula_map(client):
    return {
        row["formula_code"]: row
        for row in client.get("/catalog").json()["formulas"]
    }


def test_repair_restores_both_formulas_and_recomputes_warehouse(client):
    ids = _build_corrupted_volume(client)

    # Corrupted state: only F-100 exists and it carries the F-001 content,
    # withdrawn; there is no F-001 row.
    catalog = _formula_map(client)
    assert set(catalog) == {"F-100"}
    assert catalog["F-100"]["active"] is False
    assert catalog["F-100"]["result"] == "p"

    response = client.post(f"/repair/formula-swap/{ids['b2']}")
    assert response.status_code == 200, response.text
    repair = response.json()
    assert repair["status"] == "repaired"
    assert repair["victim_formula_code"] == "F-100"
    assert repair["missing_formula_code"] == "F-001"
    assert set(repair["relevant_batch_ids"]) == set(ids.values())

    catalog = _formula_map(client)
    assert set(catalog) == {"F-001", "F-100"}

    f100 = catalog["F-100"]
    assert f100["result"] == "e"
    assert f100["left"] == "p"
    assert f100["operator"] == "*"
    assert f100["right"] == "h"
    assert f100["active"] is True
    assert f100["version"] == 1
    assert f100["source_batch_id"] == ids["b1"]

    f001 = catalog["F-001"]
    assert f001["result"] == "p"
    assert f001["left"] == "e"
    assert f001["operator"] == "/"
    assert f001["right"] == "p"
    assert f001["active"] is False
    assert f001["version"] == 2
    assert f001["source_batch_id"] == ids["b4"]

    # Effective projections and warehouse are consistent after the same txn.
    candidates = {row["field"]: row for row in client.get("/candidates").json()}
    assert candidates["e"]["unique_unit"] == "kWh"
    assert candidates["p"]["unique_unit"] == "kW"
    assert candidates["h"]["unique_unit"] == "h"
    warehouse = {row["field"]: row["unit"] for row in client.get("/warehouse").json()}
    assert warehouse == {"p": "kW", "h": "h", "e": "kWh"}
    assert client.get("/conflicts").json()["solvable"] is True


def test_historical_requests_and_receipts_remain_queryable(client):
    ids = _build_corrupted_volume(client)
    client.post(f"/repair/formula-swap/{ids['b2']}")

    for batch_id in ids.values():
        batch = client.get(f"/batches/{batch_id}")
        assert batch.status_code == 200
        body = batch.json()
        assert "applied" in body or "withdrawn" in body

    # The repair itself is recorded as its own batch and in the repair ledger.
    db = SessionLocal()
    try:
        ledger = db.query(FormulaRepair).one()
        assert ledger.mistaken_batch_id == ids["b2"]
        repair_batch = db.query(Batch).filter(Batch.batch_id == ledger.repair_batch_id).one()
        assert repair_batch.request_json == {
            "repair_mistaken_batch_id": ids["b2"]
        }
        repair_batch_id = ledger.repair_batch_id
    finally:
        db.close()

    repair_view = client.get(f"/batches/{repair_batch_id}")
    assert repair_view.status_code == 200
    assert repair_view.json()["status"] == "repaired"


def test_repeat_repair_is_idempotent_and_keeps_later_corrections(client):
    ids = _build_corrupted_volume(client)
    first = client.post(f"/repair/formula-swap/{ids['b2']}").json()

    # A post-repair correction to F-001 must survive a repeated repair call.
    response = client.put(
        "/formulas/F-001",
        json={"result": "e", "left": "p", "operator": "*", "right": "h", "note": "新更正"},
    )
    assert response.status_code == 200, response.text

    repeat = client.post(f"/repair/formula-swap/{ids['b2']}")
    assert repeat.status_code == 200
    body = repeat.json()
    assert body["status"] == "already_repaired"
    assert body["repair_batch_id"] == first["repair_batch_id"]
    assert body["recomputed_group_count"] == 0

    catalog = _formula_map(client)
    assert catalog["F-001"]["note"] == "新更正"
    assert catalog["F-001"]["result"] == "e"
    assert catalog["F-100"]["result"] == "e"
    assert catalog["F-100"]["active"] is True


def test_repair_unknown_batch_returns_404_and_keeps_catalog(client):
    _build_corrupted_volume(client)
    response = client.post("/repair/formula-swap/B-nope")
    assert response.status_code == 404
    assert set(_formula_map(client)) == {"F-100"}


def test_repair_without_swap_pattern_is_rejected(client):
    response = client.post(
        "/formulas",
        json={
            "formula_no": "F-100",
            "result": "e",
            "left": "p",
            "operator": "*",
            "right": "h",
        },
    )
    batch_id = response.json()["batch_id"]
    response = client.post(f"/repair/formula-swap/{batch_id}")
    assert response.status_code == 422
    assert "串号" in response.json()["detail"]
    # Catalog untouched.
    assert set(_formula_map(client)) == {"F-100"}


def test_repair_recomputes_only_the_pair_connected_groups(client):
    ids = _build_corrupted_volume(client)

    # An independent group whose version must not move during the repair.
    response = client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "KU-D", "field": "d", "unit": "kW"},
                {"evidence_no": "KU-T", "field": "t", "unit": "h"},
            ],
            "formulas": [
                {
                    "formula_no": "F-200",
                    "result": "z",
                    "left": "d",
                    "operator": "*",
                    "right": "t",
                }
            ],
        },
    )
    assert response.status_code == 200
    before = {row["field"]: row for row in client.get("/candidates").json()}
    untouched_group = before["z"]["group_id"]
    untouched_version = before["z"]["group_version"]

    repair = client.post(f"/repair/formula-swap/{ids['b2']}").json()
    recomputed_fields = {
        field
        for group in repair["recomputed_groups"]
        for field in group["fields"]
    }
    assert recomputed_fields == {"p", "h", "e"}

    # The independent F-200 batch is not related to the F-100/F-001 pair, so
    # its request and receipt must not even be read during the repair.
    unrelated_batch_id = response.json()["batch_id"]
    assert unrelated_batch_id not in repair["relevant_batch_ids"]
    assert set(repair["relevant_batch_ids"]) == set(ids.values())

    after = {row["field"]: row for row in client.get("/candidates").json()}
    assert after["z"]["group_id"] == untouched_group
    assert after["z"]["group_version"] == untouched_version


def test_repair_rolls_back_when_material_is_insufficient(client):
    ids = _build_corrupted_volume(client)

    # Damage the stored receipt of the mistaken batch: remove field ids so the
    # structured evidence can no longer rebuild the formula.
    db = SessionLocal()
    try:
        batch = db.query(Batch).filter(Batch.batch_id == ids["b2"]).first()
        result = dict(batch.result_json)
        for item in result["applied"]["formulas"]:
            item["result_field_id"] = None
        batch.result_json = result
        flag_modified(batch, "result_json")
        db.commit()
    finally:
        db.close()

    response = client.post(f"/repair/formula-swap/{ids['b2']}")
    assert response.status_code == 422
    assert "材料不足" in response.json()["detail"]

    # Nothing changed: still one row, ledger empty.
    assert set(_formula_map(client)) == {"F-100"}
    db = SessionLocal()
    try:
        assert db.query(FormulaRepair).count() == 0
        assert db.query(Formula).count() == 1
    finally:
        db.close()


def test_repair_rejects_unqualified_numeric_withdrawal_reference(client):
    ids = _build_corrupted_volume(client)

    # A later batch really withdraws the victim row, but its stored request
    # used a bare numeric reference instead of an explicit F- code, so the
    # repair cannot know which business number the operator meant.
    response = client.post("/formulas/F-100/withdraw")
    assert response.status_code == 200
    extra = response.json()["batch_id"]
    db = SessionLocal()
    try:
        batch = db.query(Batch).filter(Batch.batch_id == extra).first()
        request = dict(batch.request_json)
        request["withdraw_formulas"] = [100]
        batch.request_json = request
        flag_modified(batch, "request_json")
        db.commit()
    finally:
        db.close()

    response = client.post(f"/repair/formula-swap/{ids['b2']}")
    assert response.status_code == 422
    assert "F- 编号" in response.json()["detail"]
    assert set(_formula_map(client)) == {"F-100"}


def test_ledger_table_is_created_on_startup_for_old_volumes(client):
    # The TestClient startup runs init_db(); the ledger must exist even though
    # no repair has happened yet (create_all adds new tables without data loss).
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='formula_repairs'")
        ).fetchall()
        assert rows == [("formula_repairs",)]
    finally:
        db.close()


def test_repair_tracks_fields_by_id_across_field_renames(client):
    """Renamed fields must still resolve via the original field IDs in receipts."""
    ids = _build_corrupted_volume(client)

    # Rename the result field after the corruption; the stored receipts keep
    # the original field ids, so the repair must survive the rename.
    response = client.put(
        "/fields/e",
        json={"current_name": "e", "name": "energy_total", "display_name": "总电量"},
    )
    assert response.status_code == 200, response.text

    repair = client.post(f"/repair/formula-swap/{ids['b2']}")
    assert repair.status_code == 200, repair.text
    catalog = _formula_map(client)
    assert set(catalog) == {"F-001", "F-100"}

    # F-100's restored expression uses the current name of the same field id.
    f100 = catalog["F-100"]
    fields = {row["name"]: row["id"] for row in client.get("/fields").json()}
    assert f100["result_field_id"] == fields["energy_total"]
    assert f100["result"] == "energy_total"
    assert f100["expression"].startswith("energy_total = p")


def test_repair_detects_swap_when_legacy_request_stored_numeric_number(client):
    """Old expression-prefix traffic could store formula_no as a bare int."""
    ids = _build_corrupted_volume(client)

    # Simulate an even older volume where the mistaken request kept number 1
    # as an integer rather than the F-001 string.
    db = SessionLocal()
    try:
        batch = db.query(Batch).filter(Batch.batch_id == ids["b2"]).first()
        request = dict(batch.request_json)
        request["formulas"][0]["formula_no"] = 1
        batch.request_json = request
        flag_modified(batch, "request_json")
        db.commit()
    finally:
        db.close()

    repair = client.post(f"/repair/formula-swap/{ids['b2']}")
    assert repair.status_code == 200, repair.text
    body = repair.json()
    assert body["victim_formula_code"] == "F-100"
    assert body["missing_formula_code"] == "F-001"
    assert set(_formula_map(client)) == {"F-001", "F-100"}


def test_repair_rejects_non_structured_formula_request(client):
    ids = _build_corrupted_volume(client)

    # Strip structured fields so only a free-text expression remains.
    db = SessionLocal()
    try:
        batch = db.query(Batch).filter(Batch.batch_id == ids["b2"]).first()
        request = dict(batch.request_json)
        for item in request["formulas"]:
            item.pop("result", None)
            item.pop("left", None)
            item.pop("operator", None)
            item.pop("right", None)
            item["expression"] = "p = e / h"
        batch.request_json = request
        flag_modified(batch, "request_json")
        db.commit()
    finally:
        db.close()

    response = client.post(f"/repair/formula-swap/{ids['b2']}")
    assert response.status_code == 422
    assert "结构化字段" in response.json()["detail"]
    assert set(_formula_map(client)) == {"F-100"}
    db = SessionLocal()
    try:
        assert db.query(FormulaRepair).count() == 0
    finally:
        db.close()
