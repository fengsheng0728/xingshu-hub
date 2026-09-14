# -*- coding: utf-8 -*-
"""tests/test_jsonl_rotate.py — CD-022：jsonl 轮转 + verify 多段适配

背景：audit/*.jsonl append-only 无界增长。修复 = JsonlRollingChain 超阈值
轮转（rename 归档）+ verify_windows 按段校验（锚区间跨轮转不失真）。

覆盖：
① 超阈值 append 触发轮转（归档文件出现 + 主文件行号重计）
② 轮转后 verify_windows 全绿（旧锚从归档验、新锚从主文件验）
③ 篡改归档文件 → verify 报 hash_mismatch（轮转后篡改仍可检测——审计完整性核心）
④ 删除归档 → verify 报错（真丢失可检测）
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit_chain import JsonlRollingChain


def _make_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE audit_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT, entry_type TEXT,
        ref_table TEXT DEFAULT '', ref_id TEXT DEFAULT '', payload TEXT,
        prev_hash TEXT DEFAULT '', entry_hash TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path):
    db = str(tmp_path / "audit.db")
    jl = str(tmp_path / "audit" / "probe.jsonl")
    _make_db(db)
    # window=5, rotate 阈值 ~200B(每行 ~40B → 第 6-7 行触发轮转)
    jc = JsonlRollingChain(db, jl, "probe.jsonl", window=5, rotate_bytes=200)
    return db, jl, jc


def _line(i: int) -> str:
    return json.dumps({"seq": i, "pad": "x" * 20}, ensure_ascii=False) + "\n"


def _append_many(jc, n: int) -> int:
    anchors = 0
    for i in range(n):
        if jc.append_line(_line(i)):
            anchors += 1
    return anchors


def _archives(jl: str):
    d = os.path.dirname(jl)
    base = os.path.basename(jl)
    return sorted(f for f in os.listdir(d) if f.startswith(base + "."))


def test_rotate_creates_archive_and_resets(env):
    """超过 200B 阈值后 append 触发轮转:归档出现、主文件重新累积。"""
    _db, jl, jc = env
    _append_many(jc, 10)  # 每行 ~44B:第 5 行 ~220B → 第 6 次 append 前轮转
    arch = _archives(jl)
    assert len(arch) == 1, f"应有 1 个归档,实际 {arch}"
    # 主文件行数 = 10 - 轮转前行数(5) = 5
    with open(jl, encoding="utf-8") as f:
        main_lines = f.readlines()
    assert 1 <= len(main_lines) <= 5
    # 归档 + 主文件总行数 = 10
    with open(os.path.join(os.path.dirname(jl), arch[0]), encoding="utf-8") as f:
        total = len(f.readlines()) + len(main_lines)
    assert total == 10


def test_verify_valid_across_rotation(env):
    """轮转跨段后 verify_windows 全绿(旧锚验归档段、新锚验主文件段)。"""
    _db, jl, jc = env
    a1 = _append_many(jc, 7)   # 段1:5 行满窗 1 锚 → 轮转;段2:2 行
    a2 = _append_many(jc, 8)   # 段2 再 8 行 → 5 行满窗 1 锚(段2 共 10 行,可能二次轮转)
    # 用独立实例 verify(模拟重启后校验,行号/段指针全从文件重建)
    jv = JsonlRollingChain(_db, jl, "probe.jsonl", window=5, rotate_bytes=200)
    r = jv.verify_windows()
    assert r["valid"] is True, f"轮转跨段后应全绿: {r}"
    assert r["windows"] >= 2, f"应有 ≥2 个锚(跨段): {r}"


def test_tamper_archive_detected(env):
    """篡改归档段内行内容 → verify 报 hash_mismatch(审计完整性跨轮转保持)。"""
    _db, jl, jc = env
    _append_many(jc, 10)  # 触发轮转,归档含段1
    arch = _archives(jl)
    assert arch
    ap = os.path.join(os.path.dirname(jl), arch[0])
    # 篡改段内行(锚区间=段尾 window 行,须改区间内行而非追加)
    with open(ap, encoding="utf-8") as f:
        lines = f.readlines()
    lines[0] = lines[0].replace("x", "Y")  # 改第一行内容
    with open(ap, "w", encoding="utf-8") as f:
        f.writelines(lines)
    jv = JsonlRollingChain(_db, jl, "probe.jsonl", window=5, rotate_bytes=200)
    r = jv.verify_windows()
    assert r["valid"] is False, "篡改归档必须被检出"
    assert any(b["reason"] == "hash_mismatch" for b in r["bad_windows"]), r


def test_delete_archive_detected(env):
    """删除归档(整段丢失)→ verify 报错(segment_missing/line_range)。"""
    _db, jl, jc = env
    _append_many(jc, 10)
    arch = _archives(jl)
    assert arch
    os.remove(os.path.join(os.path.dirname(jl), arch[0]))
    jv = JsonlRollingChain(_db, jl, "probe.jsonl", window=5, rotate_bytes=200)
    r = jv.verify_windows()
    # 段1 的锚失去载体 → 非 valid
    assert r["valid"] is False, "删除归档必须被检出"
