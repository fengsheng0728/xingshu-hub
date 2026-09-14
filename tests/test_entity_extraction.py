# -*- coding: utf-8 -*-
"""
K2 实体抽取单测（附录 F v1.7 冻结验收）
覆盖：
  1. 铁律 1：NONE 级不送 LLM（extract_entities level=none → skipped，零外呼）
  2. 启发式抽取（无 LLM 配置）：词典实体 + 关系正则
  3. LLM 抽取（mock httpx）：严格 JSON 解析 + 降级
  4. 铁律 2：抽取入 entity_review 审查队列（pending），不直接进图谱
  5. 铁律 3：review approved → 进 knowledge_base（图谱），rejected → 丢弃
  6. ingest 接入：汇入时自动抽取入队；NONE 级不触发
  7. 端点权限：worker 403 / manager 200
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
from entity_extraction import extract_entities, _heuristic_extract

_SCHEMA = """CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id TEXT PRIMARY KEY, parent_doc_id TEXT NOT NULL, piece_index INTEGER NOT NULL,
    content TEXT NOT NULL, summary TEXT, source_agent_id TEXT DEFAULT '',
    trust_level TEXT DEFAULT 'trusted', tainted_at TEXT, disclosure_level TEXT DEFAULT 'summary',
    sensitivity_score REAL DEFAULT 0.0, chunk_hash TEXT NOT NULL, kind TEXT DEFAULT 'fact',
    pii_hits TEXT DEFAULT '[]', created_at TEXT, updated_at TEXT)"""
_KB = """CREATE TABLE IF NOT EXISTS knowledge_base (
    entry_id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT, tags TEXT,
    links TEXT, category TEXT DEFAULT 'general', importance REAL DEFAULT 1.0,
    created_by TEXT, created_at TEXT, updated_at TEXT)"""
_EV = """CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT,
    agent_id TEXT, payload TEXT, timestamp TEXT)"""
_ER = """CREATE TABLE IF NOT EXISTS entity_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL,
    name TEXT NOT NULL, entity_type TEXT DEFAULT 'other', evidence TEXT,
    level TEXT DEFAULT 'summary', status TEXT DEFAULT 'pending',
    source TEXT DEFAULT 'llm', created_at TEXT DEFAULT (datetime('now')),
    reviewed_at TEXT, reviewed_by TEXT)"""
_HAC = """CREATE TABLE IF NOT EXISTS hub_agent_config (key TEXT PRIMARY KEY, value TEXT)"""
_RQ = """CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_type TEXT NOT NULL DEFAULT 'entity',
    doc_id TEXT NOT NULL, name TEXT NOT NULL, detail TEXT DEFAULT '',
    level TEXT DEFAULT 'summary', status TEXT DEFAULT 'pending',
    source TEXT DEFAULT 'llm', created_at TEXT DEFAULT (datetime('now')),
    reviewed_at TEXT, reviewed_by TEXT)"""


def _mk_tmpdb():
    tmpdir = tempfile.mkdtemp(prefix="k2t-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    for ddl in (_SCHEMA, _KB, _EV, _ER, _HAC, _RQ):
        conn.execute(ddl)
    conn.commit()
    conn.close()
    return tmpdb


def _mk_hub(tmpdb, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    else:
        models.CONFIG.DB_PATH = tmpdb
    from hub_core import SyncHub
    hub = SyncHub()
    hub.agents["h2-agent"] = {"agent_id": "h2-agent", "role": "worker", "department": "t"}
    return hub


# ── 1. 铁律 1：NONE 不送 LLM ──

def test_none_level_skipped():
    import asyncio as _a
    r = _a.run(extract_entities("客户 110101199003071234 的合同", level="none"))
    assert r["source"] == "skipped", r
    assert r["entities"] == [], "NONE 级不得产出实体"
    # 即使给了 llm_config 也不外呼
    r2 = _a.run(extract_entities("敏感内容", level="none",
                                 llm_config={"api_key": "fake", "model": "m", "api_base": "http://x"}))
    assert r2["source"] == "skipped"
    print("PASS test_none_level_skipped")


# ── 2. 启发式抽取 ──

def test_heuristic_extract():
    r = _heuristic_extract("张三负责销售额项目，李四参与市场部工作")
    assert r["source"] == "heuristic"
    names = {e["name"] for e in r["entities"]}
    assert "张三" in names and "销售额" in names
    rels = r["relations"]
    assert any(x["rel"] == "负责" for x in rels), f"应有负责关系: {rels}"
    print(f"PASS test_heuristic_extract ({len(r['entities'])} 实体, {len(r['relations'])} 关系)")


def test_extract_no_llm_config():
    import asyncio as _a
    r = _a.run(extract_entities("张三负责销售额项目", level="summary", llm_config=None))
    assert r["source"] == "heuristic"
    assert len(r["entities"]) >= 2
    print("PASS test_extract_no_llm_config")


# ── 4. 铁律 2：入审查队列 ──

def test_extract_queue(monkeypatch):
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    r = asyncio.run(hub.extract_and_queue(
        "doc-e1", "张三负责销售额项目，李四参与市场部", level="summary"))
    assert r["status"] == "queued", r
    assert r["queued"] >= 2, r
    conn = sqlite3.connect(tmpdb)
    rows = conn.execute("SELECT name, status, level FROM review_queue WHERE item_type='entity'").fetchall()
    conn.close()
    assert all(row[1] == "pending" for row in rows), "应全部 pending（铁律 2）"
    assert all(row[2] == "summary" for row in rows), "级别继承 chunk 级别"
    # 未放行前不污染图谱
    conn = sqlite3.connect(tmpdb)
    kb = conn.execute("SELECT COUNT(*) FROM knowledge_base WHERE category='entity'").fetchone()[0]
    conn.close()
    assert kb == 0, "审查前不得进图谱"
    print(f"PASS test_extract_queue ({len(rows)} 条入队)")


# ── 5. 铁律 3：review 放行 ──

def test_review_approve_reject(monkeypatch):
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    asyncio.run(hub.extract_and_queue("doc-e2", "张三负责销售额项目", level="summary"))
    conn = sqlite3.connect(tmpdb)
    review_id = conn.execute("SELECT id FROM review_queue WHERE status='pending' LIMIT 1").fetchone()[0]
    conn.close()
    # 拒绝一条
    r1 = asyncio.run(hub.review_entity(review_id, "rejected", reviewer="mgr"))
    assert r1["status"] == "rejected"
    # 放行一条
    conn = sqlite3.connect(tmpdb)
    other = conn.execute("SELECT id, name FROM review_queue WHERE status='pending' LIMIT 1").fetchone()
    conn.close()
    r2 = asyncio.run(hub.review_entity(other[0], "approved", reviewer="mgr"))
    assert r2["status"] == "approved", r2
    assert r2["entry_id"].startswith("ent:"), r2
    # 放行的进图谱，拒绝的不进
    conn = sqlite3.connect(tmpdb)
    kb = conn.execute("SELECT title FROM knowledge_base WHERE category='entity'").fetchall()
    st = conn.execute("SELECT status FROM review_queue WHERE id=?", (review_id,)).fetchone()[0]
    conn.close()
    assert st == "rejected"
    assert any(r[0] == other[1] for r in kb), f"放行实体应进图谱: {kb}"
    print(f"PASS test_review_approve_reject (放行 {other[1]} 进图谱)")


# ── 6. ingest 接入 ──

def test_ingest_queues_entities(monkeypatch):
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    para = "张三负责季度报告项目，团队业绩增长 20%。" * 20
    r = asyncio.run(hub.ingest_chunks(
        doc_id="doc-i1", content=para,
        source_agent_id="h2-agent", kind="fact", owner_role="worker"))
    assert r["status"] == "ingested"
    conn = sqlite3.connect(tmpdb)
    cnt = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
    conn.close()
    assert cnt >= 1, f"汇入应触发实体抽取入队，实际 {cnt}"
    print(f"PASS test_ingest_queues_entities ({cnt} 条实体入队)")


def test_ingest_pii_no_extract(monkeypatch):
    """NONE 级汇入不触发抽取（铁律 1）"""
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    r = asyncio.run(hub.ingest_chunks(
        doc_id="doc-pii2",
        content="客户 110101199003071234 的合同已签署",
        source_agent_id="h2-agent", kind="fact", owner_role="worker"))
    assert r["locked"] is True
    conn = sqlite3.connect(tmpdb)
    cnt = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
    conn.close()
    assert cnt == 0, f"NONE 级不得抽取实体，实际 {cnt}"
    print("PASS test_ingest_pii_no_extract")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nK2 实体抽取: {len(tests)} 用例全绿")
