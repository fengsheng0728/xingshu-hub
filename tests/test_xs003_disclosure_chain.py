# -*- coding: utf-8 -*-
"""
XS-003（2026-09-08）披露审计 INSERT+hash 回填合并同事务 + verify 显式报告断链行

覆盖（无真实 Hub：SyncHub() 直建 + 临时 sqlite + monkeypatch CONFIG.DB_PATH）：
  1. append_row 原子落链（返回 log_id/prev_hash/entry_hash，verify valid）
  2. 连续 append_row 无 entry_hash='' 孤儿行
  3. 断链行（entry_hash=''）夹在已链区间 → verify 报 unlinked_row
  4. S2 前历史无 hash 行（链头之前）仍豁免
  5. backfill_unlinked 补链后 verify 恢复 valid；幂等
  6. 并发 append_row 不产生链 fork
  7. _log_disclosure(FULL) 全量披露必落链（XS-009 落链回归锁定）
  8. content=None / level 传字符串 → 归一不炸且落链
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
import pytest
from models import DisclosureLevel
from audit_chain import DisclosureChain
from hub_core import SyncHub


@pytest.fixture
def chain_env(monkeypatch):
    """临时 sqlite：disclosure_log 全列（db.py:233-245 CREATE TABLE +
    S2 迁移 ALTER 的 prev_hash/entry_hash 两列），CONFIG.DB_PATH 指向它。"""
    tmpdir = tempfile.mkdtemp(prefix="xs003-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute("""
        CREATE TABLE disclosure_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            from_agent_id TEXT,
            to_agent_id TEXT,
            memory_id TEXT,
            disclosed_level TEXT,
            disclosed_content TEXT,
            disclosed_at TEXT,
            reason TEXT,
            trace_id TEXT,
            prev_hash TEXT NOT NULL DEFAULT '',
            entry_hash TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    yield tmpdb


def _row(content="机密内容"):
    return {
        "task_id": "t1", "from_agent_id": "a", "to_agent_id": "b",
        "memory_id": "m1", "disclosed_level": "full",
        "disclosed_content": content, "disclosed_at": "2026-09-08T00:00:00",
        "reason": "test", "trace_id": "",
    }


def _bare_insert(db, content="裸行"):
    """手工 INSERT 一条 entry_hash='' 裸行（模拟旧两段式写入崩溃残留）。"""
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO disclosure_log (task_id, from_agent_id, to_agent_id,"
        " memory_id, disclosed_level, disclosed_content, disclosed_at,"
        " reason, trace_id) VALUES (?,?,?,?,?,?,?,?,?)",
        ("t-raw", "a", "b", "m-raw", "summary", content,
         "2026-09-08T00:00:00", "manual", ""),
    )
    log_id = cur.lastrowid
    conn.commit()
    conn.close()
    return log_id


def test_append_row_atomic_linked(chain_env):
    dc = DisclosureChain(chain_env)
    r = dc.append_row(_row())
    assert r["log_id"]
    assert r["prev_hash"]
    assert r["entry_hash"]
    conn = sqlite3.connect(chain_env)
    row = conn.execute(
        "SELECT entry_hash FROM disclosure_log WHERE log_id=?",
        (r["log_id"],),
    ).fetchone()
    conn.close()
    assert row[0] != ""
    res = dc.verify()
    assert res["valid"] is True
    assert res["checked"] == 1


def test_append_row_no_orphan_on_success(chain_env):
    dc = DisclosureChain(chain_env)
    for i in range(3):
        dc.append_row(_row(f"内容-{i}"))
    conn = sqlite3.connect(chain_env)
    n = conn.execute(
        "SELECT COUNT(*) FROM disclosure_log WHERE entry_hash=''"
    ).fetchone()[0]
    conn.close()
    assert n == 0
    assert dc.verify()["valid"] is True


def test_verify_reports_unlinked_row(chain_env):
    dc = DisclosureChain(chain_env)
    dc.append_row(_row("前"))
    bare_id = _bare_insert(chain_env)
    dc.append_row(_row("后"))
    res = dc.verify()
    assert res["valid"] is False
    assert res["reason"] == "unlinked_row"
    assert res["first_unlinked_id"] == bare_id
    assert res["unlinked_count"] >= 1


def test_verify_legacy_pre_chain_rows_exempt(chain_env):
    dc = DisclosureChain(chain_env)
    _bare_insert(chain_env, content="S2前历史")  # log_id=1，链头之前
    dc.append_row(_row("链化-1"))
    dc.append_row(_row("链化-2"))
    res = dc.verify()
    assert res["valid"] is True
    assert res["checked"] == 2
    assert res.get("unlinked_count", 0) == 0


def test_backfill_unlinked(chain_env):
    dc = DisclosureChain(chain_env)
    dc.append_row(_row("前"))
    _bare_insert(chain_env)
    dc.append_row(_row("后"))
    assert dc.verify()["valid"] is False  # 先确认断链状态
    r = dc.backfill_unlinked()
    assert r["backfilled"] >= 1
    res = dc.verify()
    assert res["valid"] is True
    # 幂等：重复调用不再补
    assert dc.backfill_unlinked()["backfilled"] == 0


def test_concurrent_append_no_fork(chain_env):
    dc = DisclosureChain(chain_env)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: dc.append_row(_row(f"并发-{i}")), range(16)))
    res = dc.verify()
    assert res["valid"] is True
    assert res["checked"] == 16


def test_log_disclosure_full_lands_on_chain(chain_env):
    hub = SyncHub()
    hub.agents = {}
    asyncio.run(hub.disclosure._log_disclosure(
        level=DisclosureLevel.FULL, from_agent="a", to_agent="b",
        memory_id="m1", content="机密全文",
    ))
    conn = sqlite3.connect(chain_env)
    rows = conn.execute("SELECT entry_hash FROM disclosure_log").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] != ""
    assert DisclosureChain(chain_env).verify()["valid"] is True


def test_log_disclosure_none_content(chain_env):
    hub = SyncHub()
    hub.agents = {}
    # content=None → 归一为 ""，不炸且落链
    asyncio.run(hub.disclosure._log_disclosure(
        level=DisclosureLevel.SUMMARY, from_agent="a", to_agent="b",
        memory_id="m1", content=None,
    ))
    # level 传字符串 → str() 归一，不炸且落链
    asyncio.run(hub.disclosure._log_disclosure(
        level="summary", from_agent="a", to_agent="b",
        memory_id="m2", content="内容",
    ))
    conn = sqlite3.connect(chain_env)
    n = conn.execute("SELECT COUNT(*) FROM disclosure_log").fetchone()[0]
    orphan = conn.execute(
        "SELECT COUNT(*) FROM disclosure_log WHERE entry_hash=''"
    ).fetchone()[0]
    conn.close()
    assert n == 2
    assert orphan == 0
    res = DisclosureChain(chain_env).verify()
    assert res["valid"] is True
    assert res["checked"] == 2
