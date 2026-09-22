# -*- coding: utf-8 -*-
"""T30 / CD-024（2026-09-20）：增长型表索引 + 慢查询护栏。

口径（用户 2026-09-20 选项 A）：
- 只给**随业务增长**的表补索引，每条都有代码里实际存在的查询模式支撑；
- 索引必须同时进 `db.py` 内联 DDL 与 alembic `0008_growth_indexes`（CD-060 硬等式门禁保持 = 0）；
- 慢查询护栏**不新增** —— D-10 门面底座的 `db_facade._record` 已实现（阈值
  `CONFIG.DB_SLOW_QUERY_MS` 默认 200ms → WARNING + `slow_calls`/`_slow_top` 计数），
  本文件只**固化其行为**，不改实现。

先红：索引未加时下列 `EXPLAIN QUERY PLAN` 全为 SCAN，G-1 必须失败。
不 spawn Hub、不绑端口。
"""
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from models import CONFIG  # noqa: E402

# (标签, SQL) —— 每条对应一个增长型索引
QUERIES = [
    ("memory_pool.owner_agent_id", "SELECT memory_id FROM memory_pool WHERE owner_agent_id='a1'"),
    ("memory_pool.owner+key", "SELECT memory_id FROM memory_pool WHERE memory_key='k' AND owner_agent_id='a1'"),
    ("memory_pool.disclosure_level", "SELECT memory_id FROM memory_pool WHERE disclosure_level='summary'"),
    ("memory_pool.updated_at", "DELETE FROM memory_pool WHERE updated_at < '2020-01-01'"),
    ("document_chunks.parent_doc_id", "SELECT chunk_id FROM document_chunks WHERE parent_doc_id='d'"),
    ("document_chunks.disclosure_level", "SELECT chunk_id FROM document_chunks WHERE disclosure_level='none'"),
    ("gateway_read_log.created_at", "DELETE FROM gateway_read_log WHERE created_at < '2020-01-01'"),
    ("events.timestamp", "DELETE FROM events WHERE timestamp < '2020-01-01'"),
    ("events.agent_id", "SELECT event_id FROM events WHERE agent_id='a1'"),
    ("wiki_inbox.status", "SELECT COUNT(*) FROM wiki_inbox WHERE status='pending'"),
]

INDEXES = [
    "idx_memory_pool_owner_key", "idx_memory_pool_level", "idx_memory_pool_updated",
    "idx_document_chunks_parent", "idx_document_chunks_level",
    "idx_gateway_read_log_created", "idx_events_timestamp", "idx_events_agent",
    "idx_wiki_inbox_status",
]


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    import db as db_mod
    db_mod.init_db()
    return db_path


def test_g1_indexes_used_by_query_plans(fresh_db):
    """G-1 逐条查询的 PLAN 必须用上索引（**改动前全为 SCAN → 先红**）。"""
    con = sqlite3.connect(fresh_db)
    try:
        bad = []
        for label, sql in QUERIES:
            plan = " | ".join(str(r[-1]) for r in con.execute("EXPLAIN QUERY PLAN " + sql))
            if "USING INDEX" not in plan and "USING COVERING INDEX" not in plan:
                bad.append(f"{label}: {plan}")
        assert not bad, "下列查询未用上索引：\n" + "\n".join(bad)
    finally:
        con.close()


def test_g2_all_growth_indexes_present(fresh_db):
    """G-2 9 条增长型索引全部存在（内联 DDL 侧）。"""
    con = sqlite3.connect(fresh_db)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()
    missing = [n for n in INDEXES if n not in names]
    assert not missing, f"缺少索引: {missing}"


def test_g3_slow_query_guard_warns_and_counts(fresh_db, monkeypatch, caplog):
    """G-3 护栏（D-10 既有实现）行为固化：阈值内不告警；超阈值 WARNING + 计数累加 + 结果不变。"""
    import db_facade

    con = sqlite3.connect(fresh_db)
    con.execute("CREATE TABLE IF NOT EXISTS guard_probe (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("DELETE FROM guard_probe")
    con.execute("INSERT INTO guard_probe (v) VALUES ('x')")
    con.commit()
    con.close()

    monkeypatch.setattr(CONFIG, "DB_SLOW_QUERY_MS", 10 ** 9, raising=False)
    with caplog.at_level(logging.WARNING, logger="db_facade"):
        rows = asyncio.run(db_facade.query("SELECT v FROM guard_probe"))
    assert rows and rows[0]["v"] == "x", "护栏不得改变查询行为/结果"
    assert not [r for r in caplog.records if "慢查询" in r.getMessage()], "阈值极大时不应告警"

    before = db_facade._stats.get("slow_calls", 0)
    monkeypatch.setattr(CONFIG, "DB_SLOW_QUERY_MS", 0, raising=False)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="db_facade"):
        rows2 = asyncio.run(db_facade.query("SELECT v FROM guard_probe"))
    assert rows2 and rows2[0]["v"] == "x", "超阈值时结果仍须正常（护栏不改行为）"
    msgs = [r.getMessage() for r in caplog.records if "慢查询" in r.getMessage()]
    assert msgs, "超阈值必须产生 WARNING（含 SQL 与耗时）"
    assert db_facade._stats.get("slow_calls", 0) > before, "slow_calls 计数应累加"
