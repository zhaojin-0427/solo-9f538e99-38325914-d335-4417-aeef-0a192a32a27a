#!/usr/bin/env python3
"""旧 SQLite 卷 F- 公式串号修复工具。

按误登记批次 ID，用历史批次里保存的原请求和回执恢复一对串号公式
（例如 F-001 误覆盖内部 ID 为 1 的 F-100），并只重算这对公式影响的
连通组；候选单位投影与入仓清单在同一事务内一起生效。

用法（在仓库根目录执行）：

    python3 -I scripts/repair_formula_swap.py --list
    python3 -I scripts/repair_formula_swap.py --batch B-xxxxxxxxxxxxxxxx
    DATABASE_URL=sqlite:////data/catalog.db \\
        python3 -I scripts/repair_formula_swap.py --batch B-xxxxxxxxxxxxxxxx

说明：
- 只读取与这两个 F- 编号有关的请求和回执；历史回执原样保留、可继续查询；
- 重复修复同一误登记批次不会覆盖后来的更正，直接返回已有台账；
- 材料不足（缺结构化公式字段、缺回执、撤回未写 F- 编号等）或写入失败时
  整体回滚，目录保持不变，并打印原因。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from app.database import SessionLocal, init_db  # noqa: E402
    from app.models import Batch, FormulaRepair  # noqa: E402
    from app.service import CatalogError, CatalogService  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - host-only convenience
    # Under "python -I" the user site-packages directory is not on sys.path.
    # The container image installs dependencies system-wide, so this fallback
    # only matters for ad-hoc host runs; it never adds the current directory.
    import site

    user_site = site.getusersitepackages()
    if user_site not in sys.path:
        sys.path.append(user_site)
    from app.database import SessionLocal, init_db  # noqa: E402
    from app.models import Batch, FormulaRepair  # noqa: E402
    from app.service import CatalogError, CatalogService  # noqa: E402


def list_batches(db) -> list[dict]:
    rows = db.query(Batch).order_by(Batch.id.asc()).all()
    repairs = {
        row.mistaken_batch_id: row for row in db.query(FormulaRepair).all()
    }
    result = []
    for row in rows:
        result.append(
            {
                "batch_id": row.batch_id,
                "batch_no": row.batch_no,
                "status": row.status,
                "already_repaired": row.batch_id in repairs,
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按误登记批次 ID 修复一对串号 F- 公式"
    )
    parser.add_argument(
        "--batch",
        help="误登记批次 ID（POST /batches 回执中的 batch_id）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列出全部历史批次及修复台账，不执行写入",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="覆盖 DATABASE_URL，例如 sqlite:////data/catalog.db",
    )
    args = parser.parse_args(argv)

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    init_db()
    db = SessionLocal()
    try:
        if args.list:
            print(json.dumps(list_batches(db), ensure_ascii=False, indent=2))
            return 0

        if not args.batch:
            parser.error("请用 --batch 指定误登记批次 ID，或用 --list 查看历史批次")

        try:
            result = CatalogService(db).repair_formula_swap(args.batch)
        except CatalogError as exc:
            print(f"修复未执行，目录保持不变：{exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
