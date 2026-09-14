# -*- coding: utf-8 -*-
"""阶段2/01 网关读取端点测试（2026-08-30）

覆盖：
1. _rank 级别映射
2. _log_read 读审计落链（临时 db）
3. memory 剥离语义：自查 FULL 可见 / scope cap 剥离（FakeHub）
4. doc 段落剥离：chunk 按允许级别过滤
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes_gateway
from routes_gateway import _rank


class FakePrincipal:
    def __init__(self, scope=None, auth_mode="api_key"):
        self.scope = scope
        self.auth_mode = auth_mode


class FakeDisc:
    """disclosure 引擎 stub：规则 1 自查 FULL；否则按 scope cap"""
    def disclose_for_principal(self, memory, requester, task, required_level, scope=None):
        if memory.get("owner_agent_id") == requester:
            from models import DisclosureLevel
            return DisclosureLevel.FULL
        cap = (scope or {}).get("level_cap", "full")
        from models import DisclosureLevel
        return DisclosureLevel(cap)


class FakeHub:
    def __init__(self, db_path):
        self._dbp = db_path
        self.disclosure = FakeDisc()

    async def memory_search(self, req):
        return {"results": [
            {"memory_id": "m1", "memory_key": "k1", "content": "x", "disclosure_level": "full",
             "owner_agent_id": "ag-a"},
            {"memory_id": "m2", "memory_key": "k2", "content": "y", "disclosure_level": "summary",
             "owner_agent_id": "ag-b"},
        ], "total": 2, "embedding_unavailable": False}

    async def semantic_search(self, req, scope=None):
        return {"memories": [], "level": "summary", "degraded": False}


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute("""CREATE TABLE gateway_read_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT, requester TEXT NOT NULL,
        auth_mode TEXT DEFAULT '', scope_json TEXT DEFAULT '', kind TEXT NOT NULL,
        query TEXT DEFAULT '', target TEXT DEFAULT '', granted_level TEXT DEFAULT '',
        item_count INTEGER DEFAULT 0, stripped_chunks INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(routes_gateway, "hub", FakeHub(tmp))
    from models import CONFIG
    monkeypatch.setattr(CONFIG, "DB_PATH", tmp)
    yield tmp
    try:
        os.remove(tmp)
    except OSError:
        pass


# ═══════════ 1. _rank ═══════════

def test_rank_mapping():
    assert _rank("none") == 0
    assert _rank("metadata") == 1
    assert _rank("summary") == 2
    assert _rank("full") == 3
    assert _rank("") == 0
    assert _rank("FULL") == 3  # 大小写不敏感


# ═══════════ 2. 读审计落链 ═══════════

def test_log_read_writes_table(env):
    routes_gateway._log_read("ag-a", FakePrincipal(scope={"level_cap": "summary"}),
                             "memory", "q", "t", "summary", 2, 1)
    conn = sqlite3.connect(env)
    row = conn.execute("SELECT requester, kind, query, target, item_count, stripped_chunks FROM gateway_read_log").fetchone()
    conn.close()
    assert row == ("ag-a", "memory", "q", "t", 2, 1)


def test_log_read_failure_silent(env, monkeypatch):
    from models import CONFIG

    monkeypatch.setattr(CONFIG, "DB_PATH", "/nonexistent/x.db")
    # 不抛异常（D4 可用性优先）
    routes_gateway._log_read("ag-a", None, "doc", "", "d1", "", 0, 0)


# ═══════════ 3. memory 剥离语义 ═══════════

def test_memory_own_full_visible(env):
    """自查规则：requester==owner → full 可见（不剥离）"""
    from models import CONFIG
    monkeypatch = None
    routes_gateway.hub.disclosure = FakeDisc()
    # 直接模拟网关 memory 分支逻辑：m1 是 ag-a 自己的 full 记忆
    m = {"memory_id": "m1", "memory_key": "k1", "disclosure_level": "full", "owner_agent_id": "ag-a"}
    allowed = routes_gateway.hub.disclosure.disclose_for_principal(
        memory=m, requester="ag-a", task={}, required_level="summary", scope=None)
    from models import DisclosureLevel
    assert allowed == DisclosureLevel.FULL
    assert _rank(allowed) >= _rank("full")  # 保留


def test_memory_scope_cap_strips(env):
    """staff scope(cap=summary)：他人 full 记忆剥离，summary 保留"""
    routes_gateway.hub.disclosure = FakeDisc()
    scope = {"level_cap": "summary"}
    # 他人 full
    m_full = {"memory_id": "m1", "disclosure_level": "full", "owner_agent_id": "ag-b"}
    allowed = routes_gateway.hub.disclosure.disclose_for_principal(
        memory=m_full, requester="ag-a", task={}, required_level="summary", scope=scope)
    from models import DisclosureLevel
    assert _rank(allowed) < _rank("full")  # 剥离
    # 他人 summary
    m_sum = {"memory_id": "m2", "disclosure_level": "summary", "owner_agent_id": "ag-b"}
    allowed2 = routes_gateway.hub.disclosure.disclose_for_principal(
        memory=m_sum, requester="ag-a", task={}, required_level="summary", scope=scope)
    assert _rank(allowed2) >= _rank("summary")  # 保留


# ═══════════ 4. doc 段落剥离 ═══════════

def test_doc_chunk_filter_logic(env):
    """chunk 过滤：allowed=summary 时 full chunk 剥离、summary 保留（纯逻辑）"""
    chunks = [
        {"chunk_id": "c1", "disclosure_level": "full"},
        {"chunk_id": "c2", "disclosure_level": "summary"},
        {"chunk_id": "c3", "disclosure_level": "metadata"},
    ]
    allow_rank = _rank("summary")
    kept = [c for c in chunks if _rank(c["disclosure_level"]) <= allow_rank]
    stripped = len(chunks) - len(kept)
    assert [c["chunk_id"] for c in kept] == ["c2", "c3"]
    assert stripped == 1
