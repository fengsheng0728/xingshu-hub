# -*- coding: utf-8 -*-
"""CD-073（2026-09-21）：jsonl 窗口「已声明缺口」语义。

背景（实测）：jsonl 运行产物曾被 git 跟踪，`git checkout -- audit` 把运行期增长段回退成旧提交版本
→ 主链里的 jsonl_anchor 指向已不存在的内容（transport.jsonl 窗口 w-5609-6608，segment_missing）。

口径（用户 2026-09-21 拍板 A+B）：
- 内容确实丢失且不可恢复时，用 `scripts/audit_declare_gap.py` 向主链追加 `anchor_gap` 事件；
- 校验把**声明过**的窗口列进 `declared_gaps`（不再算未解释断点），未声明的照样判非法——
  即 `valid` 的含义收紧为「没有未解释的断点」，而不是「什么都看不见」；
- 不改历史、不补内容；声明本身也上链（谁在何时登记）。
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit_chain import AuditChain, JsonlRollingChain, verify_all  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def env(tmp_path):
    db = str(tmp_path / "audit.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE audit_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_type TEXT NOT NULL DEFAULT '', ref_table TEXT DEFAULT '',
        ref_id TEXT DEFAULT '', payload TEXT DEFAULT '',
        prev_hash TEXT NOT NULL DEFAULT '', entry_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT)""")
    conn.execute("""CREATE TABLE disclosure_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT, from_agent_id TEXT, to_agent_id TEXT, memory_id TEXT,
        disclosed_level TEXT, disclosed_content TEXT, disclosed_at TEXT,
        reason TEXT, trace_id TEXT,
        prev_hash TEXT NOT NULL DEFAULT '', entry_hash TEXT NOT NULL DEFAULT '')""")
    conn.commit(); conn.close()
    return db


def _jsonl(tmp_path, n, name="transport.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps({"i": i}) for i in range(n)) + "\n", encoding="utf-8")
    return str(p)


def _anchor(ac, ref, s, e, wh):
    return ac.append("jsonl_anchor", ref, f"w-{s}-{e}",
                     {"file": ref, "start_line": s, "end_line": e, "window_hash": wh})


def _chain(res, ref="transport.jsonl"):
    return res["chains"][f"jsonl:{ref}"]


def test_declared_gap_turns_break_into_declared(env, tmp_path):
    jl = _jsonl(tmp_path, 10)
    jc = JsonlRollingChain(env, jl, "transport.jsonl", window=5)
    ac = AuditChain(env)
    _anchor(ac, "transport.jsonl", 1, 5, jc.window_hash(jc._read_lines()[:5]))

    ch = _chain(verify_all(env, {"transport.jsonl": jl}))
    assert ch["valid"] is True and ch["declared_gaps"] == []

    # 内容被回退成更旧版本（模拟 git checkout 抹掉运行期增长段）
    _jsonl(tmp_path, 2)
    res = verify_all(env, {"transport.jsonl": jl})
    ch = _chain(res)
    assert ch["valid"] is False and res["valid"] is False
    assert ch["bad_windows"][0]["reason"] == "segment_missing"
    assert ch["bad_windows"][0]["lines"] == "1-5"

    # 如实上链声明 → 变成「已声明缺口」，不再是未解释断点
    ac.append("anchor_gap", "transport.jsonl", "w-1-5",
              {"reason": "运行产物被 git checkout 回退", "declared_by": "test"})
    res = verify_all(env, {"transport.jsonl": jl})
    ch = _chain(res)
    assert ch["valid"] is True and ch["bad_windows"] == []
    assert len(ch["declared_gaps"]) == 1 and ch["declared_gaps"][0]["declared"] is True
    assert ch["declared_gaps"][0]["reason"] == "segment_missing"
    assert res["valid"] is True


def test_undeclared_missing_window_still_invalid(env, tmp_path):
    """只豁免声明过的那一段：同文件里另一段缺失仍判非法。"""
    jl = _jsonl(tmp_path, 10)
    jc = JsonlRollingChain(env, jl, "transport.jsonl", window=5)
    ac = AuditChain(env)
    lines = jc._read_lines()
    _anchor(ac, "transport.jsonl", 1, 5, jc.window_hash(lines[:5]))
    _anchor(ac, "transport.jsonl", 6, 10, jc.window_hash(lines[5:10]))
    _jsonl(tmp_path, 2)
    ac.append("anchor_gap", "transport.jsonl", "w-1-5", {"reason": "只声明这段"})

    res = verify_all(env, {"transport.jsonl": jl})
    ch = _chain(res)
    assert ch["valid"] is False
    assert len(ch["declared_gaps"]) == 1
    assert [b["lines"] for b in ch["bad_windows"]] == ["6-10"]
    assert res["valid"] is False


def test_declare_script_writes_once_and_is_idempotent(env):
    """声明脚本：写入一条 anchor_gap，重复跑不重复写链（幂等）。"""
    script = os.path.join(ROOT, "scripts", "audit_declare_gap.py")
    args = [sys.executable, script, "--file", "transport.jsonl", "--window", "w-9-9",
            "--reason", "测试：内容确认不可恢复", "--declared-by", "pytest", "--db", env]
    r1 = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r1.returncode == 0 and "[OK]" in r1.stdout, r1.stdout + r1.stderr
    r2 = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r2.returncode == 0 and "[SKIP]" in r2.stdout, r2.stdout

    conn = sqlite3.connect(env); conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT entry_type, ref_table, ref_id, payload FROM audit_log WHERE entry_type='anchor_gap'")]
    conn.close()
    assert len(rows) == 1 and rows[0]["ref_id"] == "w-9-9"
    payload = json.loads(rows[0]["payload"])
    assert payload["declared_by"] == "pytest" and "不可恢复" in payload["reason"]
    # 声明本身也在链上且链完整
    assert AuditChain(env).verify()["valid"] is True


def test_last_verify_reports_declared_gaps(env, tmp_path, monkeypatch):
    """CD-073：`/api/audit/last-verify` 必须带出已声明缺口清单。

    否则控制台首屏那张卡片只能说"完整"——缺口要等用户点过校验才看得见（等于藏进绿灯）。
    """
    import asyncio
    import routes_audit
    from models import CONFIG
    db = env
    monkeypatch.setattr(CONFIG, "DB_PATH", db)   # handler 读 CONFIG.DB_PATH，须指向本 fixture 的库
    ac = AuditChain(db)
    ac.append("anchor_gap", "transport.jsonl", "w-1-5",
              {"reason": "运行产物被 git 回退", "declared_by": "ops"})
    res = asyncio.run(routes_audit.api_audit_last_verify(current_agent=""))
    gaps = res.get("declared_gaps")
    assert gaps and len(gaps) == 1, f"last-verify 未带 declared_gaps: {res}"
    assert gaps[0]["ref_table"] == "transport.jsonl" and gaps[0]["ref_id"] == "w-1-5"
    assert gaps[0]["declared_by"] == "ops" and "git" in gaps[0]["reason"]
