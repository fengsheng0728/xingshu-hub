# -*- coding: utf-8 -*-
"""shadow_verify.py — 影子双写一致性校验（阶段3-P1 通过条件）

SQLite 落库行 ↔ git 仓库镜像文件 diff：
- memory    → vault/memory/<date>/<memory_id>.md
- knowledge → vault/knowledge/<date>/<entry_id>.md
- wiki      → vault/wiki/<doc_id>/<NNN>.md（piece_index 三位）
- shared    → vault/shared/<date>/<doc_id>.md

用法：
  python tools/shadow_verify.py --db sync_hub.db --trunk ./data-trunk [--since 2026-08-31]
--since 只校验该日期起写入的行（存量未镜像是预期，不算缺失）。
缺失 0 → 输出 PASS；否则列出缺失清单并 exit 1。
"""
import argparse
import json
import os
import re
import sqlite3
import sys


def _safe(name: str) -> str:
    """与 hub_mixins/shadow.py _safe_name 一致的 Windows 文件名安全化。"""
    return re.sub(r'[<>:"/\\|?*]', "_", str(name))


def _git_files(trunk: str) -> set:
    """git ls-files 列全部分干文件（相对路径）。"""
    import subprocess
    p = subprocess.run(["git", "-C", trunk, "ls-files"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return {ln.strip().replace("\\", "/") for ln in p.stdout.splitlines() if ln.strip()}


def _check(kind: str, rows: list, files: set, missing: dict):
    for row in rows:
        rel = row["rel"]
        if rel not in files:
            missing.setdefault(kind, []).append(rel)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="./sync_hub.db")
    ap.add_argument("--trunk", default="./data-trunk")
    ap.add_argument("--since", default="", help="只校验该日期(YYYY-MM-DD)起写入的行")
    ap.add_argument("--since-ts", type=float, default=0,
                    help="只校验该 unix 时间戳后写入的行（统一口径，防 UTC/本地时区错位）")
    args = ap.parse_args()

    branch = os.path.join(args.trunk, "branches", "default")
    if not os.path.isdir(os.path.join(branch, ".git")):
        print("FAIL: 分干仓库不存在", branch)
        sys.exit(1)
    files = _git_files(branch)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    missing = {}
    total = 0

    since = args.since or ""
    since_ts = args.since_ts or 0

    def iso_ge(iso: str, day: str, ts: float) -> bool:
        """ISO 时间戳 >= 过滤条件：--since(日期) 或 --since-ts(unix)。"""
        if since and day < since:
            return False
        if since_ts:
            try:
                t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
                if t < since_ts:
                    return False
            except Exception:
                return False
        return True

    import datetime

    # memory：created_at ISO
    rows = [dict(r) for r in conn.execute(
        "SELECT memory_id, created_at FROM memory_pool ORDER BY memory_id")]
    rows = [{"rel": f"vault/memory/{r['created_at'][:10]}/{_safe(r['memory_id'])}.md"}
            for r in rows
            if iso_ge(r["created_at"] or "", (r["created_at"] or "")[:10], since_ts)]
    total += len(rows)
    _check("memory", rows, files, missing)

    # knowledge：updated_at ISO
    rows = [dict(r) for r in conn.execute(
        "SELECT entry_id, updated_at FROM knowledge_base ORDER BY entry_id")]
    rows = [{"rel": f"vault/knowledge/{(r['updated_at'] or '')[:10]}/{_safe(r['entry_id'])}.md"}
            for r in rows
            if iso_ge(r["updated_at"] or "", (r["updated_at"] or "")[:10], since_ts)]
    total += len(rows)
    _check("knowledge", rows, files, missing)

    # wiki：document_chunks created_at ISO，piece_index 三位补零
    rows = [dict(r) for r in conn.execute(
        "SELECT parent_doc_id, piece_index, created_at FROM document_chunks"
        " ORDER BY parent_doc_id, piece_index")]
    rows = [{"rel": f"vault/wiki/{_safe(r['parent_doc_id'])}/{r['piece_index']:03d}.md"}
            for r in rows
            if iso_ge(r["created_at"] or "", (r["created_at"] or "")[:10], since_ts)]
    total += len(rows)
    _check("wiki", rows, files, missing)

    # shared：created_at REAL(unix)
    rows = [dict(r) for r in conn.execute(
        "SELECT doc_id, created_at FROM shared_docs")]
    shared_rows = []
    for r in rows:
        t = r["created_at"] or 0
        if since and datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d") < since:
            continue
        if since_ts and t < since_ts:
            continue
        d = datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        shared_rows.append({"rel": f"vault/shared/{d}/{_safe(r['doc_id'])}.md"})
    total += len(shared_rows)
    _check("shared", shared_rows, files, missing)

    conn.close()

    n_missing = sum(len(v) for v in missing.values())
    print(f"校验: {total} 行 ↔ git 文件 | 缺失: {n_missing}")
    for kind, rels in missing.items():
        print(f"  [{kind}] 缺失 {len(rels)}:")
        for rel in rels[:10]:
            print(f"    {rel}")
        if len(rels) > 10:
            print(f"    ... 等 {len(rels) - 10} 条")
    if n_missing == 0:
        print("PASS: 影子双写一致")
        sys.exit(0)
    print("FAIL: 存在缺失镜像")
    sys.exit(1)


if __name__ == "__main__":
    main()
