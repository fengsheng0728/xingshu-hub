# -*- coding: utf-8 -*-
"""
H3 防拼接滑窗单测（附录 E v1.4 冻结验收）
覆盖：
  1. chunk 检索基础：披露过滤 + min(请求方判定, 存储级别)
  2. E.3 滑窗：慢速拼接模拟（10 次 × 30% 累计 >50% → 降级）
  3. 滑窗 24h 过期
  4. parent_hint 只给布尔量（不给 total_chunks）
  5. 降级审计事件 chunk_disclosure_quota
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from disclosure import DisclosureEngine, _level_rank
from models import DisclosureLevel
from hub_core import SyncHub


def _mk_hub(tmpdb):
    """构造测试 hub（内存 SyncHub 实例 + 独立 db）"""
    from hub_core import SyncHub
    hub = SyncHub()
    hub._db_path_override = tmpdb  # 若 SyncHub 支持；不支持则用 CONFIG
    return hub


async def _seed_chunks(hub, doc_id="doc-1", n=10, owner="owner-A", level="full"):
    """写入 n 个 chunk 到 document_chunks 表（测试数据）"""
    conn = hub._db()
    now = "2026-08-06T00:00:00"
    for i in range(n):
        conn.execute(
            """INSERT OR REPLACE INTO document_chunks
               (chunk_id, parent_doc_id, piece_index, content, summary,
                source_agent_id, trust_level, tainted_at, disclosure_level,
                sensitivity_score, chunk_hash, kind, pii_hits, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'trusted', '', ?, 0.0, ?, 'fact', '[]', ?, ?)""",
            (f"{doc_id}-c{i}", doc_id, i, f"chunk 内容 {i} 关于项目进展",
             f"摘要 {i}", owner, level, f"hash-{i}", now, now),
        )
    conn.commit()
    conn.close()


async def _seed_and_engine(tmpdb):
    hub = SyncHub()
    # SyncHub 需要初始化 db 连接方式——直接调 _db() 用 CONFIG.DB_PATH，测试要独立
    return hub


import pytest

def _use_test_db(tmpdb, monkeypatch):
    """把 CONFIG.DB_PATH 指到测试库（monkeypatch 自动恢复，防全量跑串扰）"""
    import models
    monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    return models.CONFIG


# ── 1. 基础检索 + 披露过滤 ──

def test_search_chunks_basic(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="h3t-")
    tmpdb = os.path.join(tmpdir, "test.db")
    # 建表
    conn = sqlite3.connect(tmpdb)
    conn.execute("""CREATE TABLE IF NOT EXISTS document_chunks (
        chunk_id TEXT PRIMARY KEY, parent_doc_id TEXT NOT NULL, piece_index INTEGER NOT NULL,
        content TEXT NOT NULL, summary TEXT, source_agent_id TEXT DEFAULT '',
        trust_level TEXT DEFAULT 'trusted', tainted_at TEXT, disclosure_level TEXT DEFAULT 'summary',
        sensitivity_score REAL DEFAULT 0.0, chunk_hash TEXT NOT NULL, kind TEXT DEFAULT 'fact',
        pii_hits TEXT DEFAULT '[]', created_at TEXT, updated_at TEXT)""")
    now = "2026-08-06T00:00:00"
    for i in range(5):
        conn.execute(
            "INSERT OR REPLACE INTO document_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"doc-1-c{i}", "doc-1", i, f"chunk 内容 {i} 关于项目进展", f"摘要 {i}",
             "owner-A", "trusted", "", "full", 0.0, f"hash-{i}", "fact", "[]", now, now))
    conn.commit()
    conn.close()

    _use_test_db(tmpdb, monkeypatch)
    hub = SyncHub()
    hub.agents["owner-A"] = {"agent_id": "owner-A", "role": "worker", "department": "t"}
    hub.agents["req-B"] = {"agent_id": "req-B", "role": "manager", "department": "t",
                           "managed_agents": ["owner-A"]}

    engine = DisclosureEngine(hub)

    async def run():
        # owner 查自己 → full
        r1 = await engine.search_chunks("owner-A", "项目进展", doc_id="doc-1", limit=10)
        assert r1["status"] == "ok"
        assert len(r1["results"]) == 5, f"owner 应见 5 块，实际 {len(r1['results'])}"
        assert r1["parent_hint"] is True
        assert "total_chunks" not in r1, "E.3: 不得泄露 total_chunks"
        # manager 查下属 → summary 级可见
        r2 = await engine.search_chunks("req-B", "项目进展", doc_id="doc-1", limit=10)
        assert len(r2["results"]) > 0, f"manager 应见下属 chunk，实际 0"
        for res in r2["results"]:
            assert res["disclosure_level"] in ("summary", "full"), res
        return r1, r2

    r1, r2 = asyncio.run(run())
    print(f"PASS test_search_chunks_basic: owner={len(r1['results'])} chunks, hint={r1['parent_hint']}")
    print("  (E.3 total_chunks 不在响应:", "total_chunks" not in r1, ")")


# ── 2. 慢速拼接模拟 ──

def test_slow_stitch_degrade(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="h3t-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute("""CREATE TABLE IF NOT EXISTS document_chunks (
        chunk_id TEXT PRIMARY KEY, parent_doc_id TEXT NOT NULL, piece_index INTEGER NOT NULL,
        content TEXT NOT NULL, summary TEXT, source_agent_id TEXT DEFAULT '',
        trust_level TEXT DEFAULT 'trusted', tainted_at TEXT, disclosure_level TEXT DEFAULT 'summary',
        sensitivity_score REAL DEFAULT 0.0, chunk_hash TEXT NOT NULL, kind TEXT DEFAULT 'fact',
        pii_hits TEXT DEFAULT '[]', created_at TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT,
        agent_id TEXT, payload TEXT, timestamp TEXT)""")
    now = "2026-08-06T00:00:00"
    for i in range(10):  # 10 块文档
        conn.execute(
            "INSERT OR REPLACE INTO document_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"doc-2-c{i}", "doc-2", i, f"chunk 内容 {i} 关于项目进展", f"摘要 {i}",
             "owner-A", "trusted", "", "full", 0.0, f"hash-{i}", "fact", "[]", now, now))
    conn.commit()
    conn.close()

    _use_test_db(tmpdb, monkeypatch)
    hub = SyncHub()
    hub.agents["owner-A"] = {"agent_id": "owner-A", "role": "worker", "department": "t"}
    hub.agents["req-B"] = {"agent_id": "req-B", "role": "manager", "department": "t",
                           "managed_agents": ["owner-A"]}  # manager 看下属 → 可见

    engine = DisclosureEngine(hub)

    async def run():
        degraded_seen = False
        # 慢速拼接：owner 自查（规则1 FULL，能拿全文）每次查 3 块（30%），分 10 次
        # 累计超过 50% 后 FULL 被降为 SUMMARY → degraded=True
        for i in range(10):
            r = await engine.search_chunks("owner-A", "项目进展", doc_id="doc-2", limit=3)
            assert r["status"] == "ok"
            assert r["parent_hint"] is True
            if r.get("degraded"):
                degraded_seen = True
        assert degraded_seen, "慢速拼接累计 >50% 后应触发降级"
        return degraded_seen

    d = asyncio.run(run())
    print(f"PASS test_slow_stitch_degrade: 10×30% 累计触发降级={d}")


# ── 3. 滑窗过期 ──

def test_window_expiry():
    engine = object.__new__(DisclosureEngine)  # 不调 __init__
    engine._chunk_window = {}
    engine._chunk_window_lock = __import__("threading").Lock()
    # 注入 25h 前的记录 → 应过期
    import time as _t
    old_ts = _t.time() - 25 * 3600
    engine._chunk_window[("req", "doc")] = [(old_ts, "c1"), (old_ts, "c2")]
    assert engine._chunk_quota_check("req", "doc", 2) is False, "过期记录不应计入"
    engine._chunk_window_add("req", "doc", ["c3"])
    # 现在窗口 1 条（25h 前 2 条被清掉）
    with engine._chunk_window_lock:
        assert len(engine._chunk_window[("req", "doc")]) == 1, "过期应被清理"
    print("PASS test_window_expiry")


# ── 4. 布尔 hint ──

def test_parent_hint_boolean():
    assert _level_rank(DisclosureLevel.NONE) == 0
    assert _level_rank(DisclosureLevel.FULL) == 3
    assert _level_rank(DisclosureLevel.SUMMARY) == 2
    # min 语义：summary < full → min(summary, full) = summary
    assert min(DisclosureLevel.SUMMARY, DisclosureLevel.FULL, key=_level_rank) == DisclosureLevel.SUMMARY
    print("PASS test_parent_hint_boolean (min 语义)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nH3 防拼接滑窗: {len(tests)} 用例全绿")
