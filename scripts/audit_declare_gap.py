# -*- coding: utf-8 -*-
"""声明一段已丢失的 jsonl 审计窗口（CD-073，2026-09-21）。

用途：jsonl 滚动链的内容**确实丢了**（文件被覆盖/回退/删除）时，向主链追加一条
`anchor_gap` 事件——写明窗口、原因、影响范围。之后审计校验把这一段标为
「已声明缺口」而不是「未解释断链」，但**不会**让内容回来，也不改历史哈希。

**纪律**：只在内容真的无法恢复时使用。能用备份/归档恢复就先恢复——声明缺口是
最后的如实记账，不是"消红工具"。声明本身也上链（谁在何时登记的，一并可查）。

用法：
  python scripts/audit_declare_gap.py --file transport.jsonl --window w-5609-6608 \
      --reason "运行产物被 git checkout 回退到旧提交版本，运行期增长段丢失" \
      --declared-by "运维" [--anchor-id 3254] [--db ./sync_hub.db]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description="声明一段已丢失的 jsonl 审计窗口（上链留痕）")
    ap.add_argument("--file", required=True, help="jsonl 文件名（如 transport.jsonl）")
    ap.add_argument("--window", required=True, help="窗口 id（anchor 行的 ref_id，形如 w-5609-6608）")
    ap.add_argument("--reason", required=True, help="丢失原因（如实写，会公开在审计链上）")
    ap.add_argument("--declared-by", default="", help="登记人（默认取环境变量 USERNAME）")
    ap.add_argument("--anchor-id", type=int, default=0, help="对应的锚事件 log_id（可选，便于定位）")
    ap.add_argument("--db", default="", help="生产库路径（默认 CONFIG.DB_PATH）")
    args = ap.parse_args()

    from models import CONFIG
    db_path = args.db or CONFIG.DB_PATH
    if not os.path.exists(db_path):
        print(f"[FATAL] 库不存在: {db_path}", file=sys.stderr)
        return 2

    from audit_chain import AuditChain
    ac = AuditChain(db_path)
    # 幂等：同一窗口只声明一次（重复声明等于污染链）
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        dup = conn.execute(
            "SELECT log_id FROM audit_log WHERE entry_type='anchor_gap'"
            " AND ref_table=? AND ref_id=?", (args.file, args.window)).fetchone()
    finally:
        conn.close()
    if dup:
        print(f"[SKIP] 该窗口已声明过（log_id={dup[0]}），未重复写链")
        return 0

    rec = ac.append("anchor_gap", args.file, args.window, {
        "file": args.file,
        "window": args.window,
        "reason": args.reason,
        "anchor_id": args.anchor_id or None,
        "declared_by": args.declared_by or os.environ.get("USERNAME", ""),
        "declared_at": datetime.now(timezone.utc).isoformat(),
        "note": "本段内容已丢失且不可恢复；本条声明上链后，校验将把该窗口标为已声明缺口",
    })
    print(f"[OK] 已上链: log_id={rec.get('log_id')} hash={str(rec.get('entry_hash'))[:16]}…")
    print(json.dumps({"file": args.file, "window": args.window}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
