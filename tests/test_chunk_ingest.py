# -*- coding: utf-8 -*-
"""
H4 汇入管道 + E.7 重判定单测（附录 E v1.4 冻结验收）
覆盖：
  1. ingest 三路分流：NONE(只审计不入图谱) / SUMMARY+FULL(建图谱条目)
  2. E.4 幂等：重复汇入按 chunk_hash 跳过
  3. E.1 PII 汇入 locked + 全部 NONE
  4. E.7 重判定：PII 历史残留条目摘除 / 普通文档条目保留
  5. 审计事件 chunks_ingested / chunks_reclassified
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
from hub_core import SyncHub

PII_RAW = "110101199003071234"

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


def _mk_tmpdb():
    tmpdir = tempfile.mkdtemp(prefix="h4t-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute(_SCHEMA)
    conn.execute(_KB)
    conn.execute(_EV)
    conn.commit()
    conn.close()
    return tmpdb


import pytest

def _mk_hub(tmpdb, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    else:
        models.CONFIG.DB_PATH = tmpdb
    hub = SyncHub()
    hub.agents["h4-agent"] = {"agent_id": "h4-agent", "role": "worker", "department": "t"}
    return hub


def test_ingest_normal_splits_and_entry(monkeypatch):
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    para = "季度报告详细内容。" * 40  # ~160 token
    r = asyncio.run(hub.ingest_chunks(
        doc_id="report-001", content=para + "\n\n" + para + "\n\n" + para,
        source_agent_id="h4-agent", kind="fact", owner_role="worker"))
    assert r["status"] == "ingested"
    assert r["chunks"] == 3, f"长文应切 3 块，实际 {r['chunks']}"
    assert r["inserted"] == 3
    assert r["locked_none"] == 0
    # 图谱条目存在
    conn = sqlite3.connect(tmpdb)
    row = conn.execute("SELECT entry_id FROM knowledge_base WHERE entry_id='doc:report-001'").fetchone()
    conn.close()
    assert row, "普通文档应建图谱条目"
    print("PASS test_ingest_normal_splits_and_entry")


def test_ingest_idempotent(monkeypatch):
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    para = "季度报告详细内容。" * 40
    doc = para + "\n\n" + para + "\n\n" + para
    r1 = asyncio.run(hub.ingest_chunks(doc_id="doc-dup", content=doc,
                                       source_agent_id="h4-agent", kind="fact", owner_role="worker"))
    assert r1["inserted"] == 3
    r2 = asyncio.run(hub.ingest_chunks(doc_id="doc-dup", content=doc,
                                       source_agent_id="h4-agent", kind="fact", owner_role="worker"))
    assert r2["inserted"] == 0, f"重复汇入应全跳过，实际 {r2['inserted']}"
    assert r2["skipped_hash"] == 3, r2
    print("PASS test_ingest_idempotent")


def test_ingest_pii_locked_no_entry(monkeypatch):
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    r = asyncio.run(hub.ingest_chunks(
        doc_id="report-pii",
        content=f"客户 {PII_RAW} 的合同已签署，金额 500 万。",
        source_agent_id="h4-agent", kind="fact", owner_role="worker"))
    assert r["locked"] is True, f"PII 应 locked，实际 {r}"
    assert r["disclosure_level"] == "none"
    assert r["locked_none"] >= 1
    # chunk 落库级别 none + 无图谱条目
    conn = sqlite3.connect(tmpdb)
    row = conn.execute("SELECT disclosure_level FROM document_chunks WHERE parent_doc_id='report-pii'").fetchone()
    kb = conn.execute("SELECT entry_id FROM knowledge_base WHERE entry_id='doc:report-pii'").fetchone()
    conn.close()
    assert row and row[0] == "none", f"chunk 级别应 none，实际 {row}"
    assert kb is None, "PII 文档不应建图谱条目"
    # 审计事件
    conn = sqlite3.connect(tmpdb)
    ev = conn.execute("SELECT payload FROM events WHERE event_type='chunks_ingested' ORDER BY event_id DESC LIMIT 1").fetchone()
    conn.close()
    assert ev and PII_RAW not in ev[0], f"审计不得含原始 PII: {ev}"
    print("PASS test_ingest_pii_locked_no_entry")


def test_reclassify_removes_stale_entry(monkeypatch):
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    # PII 汇入（none）后模拟历史残留条目
    asyncio.run(hub.ingest_chunks(doc_id="doc-pii",
        content=f"客户 {PII_RAW} 的合同已签署",
        source_agent_id="h4-agent", kind="fact", owner_role="worker"))
    asyncio.run(hub._upsert_doc_entry("doc-pii", "合同已签署", "h4-agent", "fact", "summary"))
    conn = sqlite3.connect(tmpdb)
    assert conn.execute("SELECT entry_id FROM knowledge_base WHERE entry_id='doc:doc-pii'").fetchone()
    conn.close()
    # 重判 → 无条件摘除（E.7：即使 chunk 级别无变化也清理历史残留）
    r = asyncio.run(hub.reclassify_chunks(doc_id="doc-pii", requester="h4-agent"))
    conn = sqlite3.connect(tmpdb)
    row = conn.execute("SELECT entry_id FROM knowledge_base WHERE entry_id='doc:doc-pii'").fetchone()
    conn.close()
    assert row is None, f"PII 残留条目应被摘除，实际 {row}"
    # 审计
    conn = sqlite3.connect(tmpdb)
    ev = conn.execute("SELECT payload FROM events WHERE event_type='chunks_reclassified' ORDER BY event_id DESC LIMIT 1").fetchone()
    conn.close()
    assert ev and "doc-pii" in ev[0]
    print("PASS test_reclassify_removes_stale_entry")


def test_reclassify_keeps_normal_entry(monkeypatch):
    tmpdb = _mk_tmpdb()
    hub = _mk_hub(tmpdb, monkeypatch)
    para = "季度报告详细内容。" * 40
    asyncio.run(hub.ingest_chunks(doc_id="doc-ok", content=para + "\n\n" + para,
                                  source_agent_id="h4-agent", kind="fact", owner_role="worker"))
    r = asyncio.run(hub.reclassify_chunks(doc_id="doc-ok", requester="h4-agent"))
    conn = sqlite3.connect(tmpdb)
    row = conn.execute("SELECT entry_id FROM knowledge_base WHERE entry_id='doc:doc-ok'").fetchone()
    conn.close()
    assert row, "普通文档条目应保留"
    print("PASS test_reclassify_keeps_normal_entry")


def test_chunk_level_pii_not_overridden_by_parent():
    """回归：chunk_level 方向 bug（PII 硬锁不被父级 summary 顶上去）"""
    from sensitivity import chunk_level
    r = chunk_level(f"客户 {PII_RAW} 的合同", "h1", parent_level="summary")
    assert r["level"] == "none", f"PII 硬锁被父级覆盖: {r['level']}"
    r2 = chunk_level("普通内容", "h2", parent_level="summary")
    assert r2["level"] == "summary"
    r3 = chunk_level("普通内容", "h3", parent_level="full")
    assert r3["level"] == "full"
    print("PASS test_chunk_level_pii_not_overridden_by_parent")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nH4 汇入管道: {len(tests)} 用例全绿")
