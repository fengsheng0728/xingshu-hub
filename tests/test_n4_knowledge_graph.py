# -*- coding: utf-8 -*-
"""N4 权限级记忆图谱验收测试（2026-08-05）

覆盖：
1. 无 requester → 全量节点（向后兼容）
2. requester=owner → 自己创建的节点可见
3. requester 无权限（跨 Agent worker 无同任务）→ NONE 节点隐藏
4. manager 查下属 → 可见
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_hub(db_path):
    """构造最小 hub（agents + knowledge_base + 披露引擎可用的 context）。"""
    from hub_core import SyncHub
    import models

    hub = SyncHub.__new__(SyncHub)
    hub._disclosure_policy = {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
    }
    # agents: alice(worker), bob(worker), manager1(manager 管 alice)
    hub.agents = {
        "alice": {"role": "worker", "department": "sales", "managed_agents": []},
        "bob": {"role": "worker", "department": "it", "managed_agents": []},
        "manager1": {"role": "manager", "managed_agents": ["alice"], "department": "sales"},
    }

    def _db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    hub._db = _db
    return hub


@pytest.fixture()
def graph_env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="n4-")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE knowledge_base (
            entry_id TEXT PRIMARY KEY, title TEXT, content TEXT, tags TEXT,
            links TEXT, category TEXT, importance REAL, created_by TEXT,
            created_at TEXT, updated_at TEXT, embedding BLOB)"""
    )
    # alice 的节点 + bob 的节点
    conn.execute(
        "INSERT INTO knowledge_base (entry_id, title, content, created_by, tags, importance) "
        "VALUES ('e-alice', 'Alice 的笔记', '秘密', 'alice', '[]', 0.9)")
    conn.execute(
        "INSERT INTO knowledge_base (entry_id, title, content, created_by, tags, importance) "
        "VALUES ('e-bob', 'Bob 的笔记', '内容', 'bob', '[]', 0.8)")
    conn.commit()
    conn.close()
    # D-11: 迁移后读路径经 db_facade（运行时读 CONFIG.DB_PATH），把门面指向本测试临时库
    import models as _models
    monkeypatch.setattr(_models.CONFIG, "DB_PATH", db)
    yield db
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_graph_no_requester_all_nodes(graph_env):
    """无 requester → 全量节点（向后兼容）。"""
    import asyncio
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph())
    ids = {n["id"] for n in r["nodes"]}
    assert "e-alice" in ids and "e-bob" in ids, f"无过滤应全量: {ids}"


def test_graph_owner_sees_own(graph_env):
    """alice 看图谱 → 自己的节点可见，bob 的隐藏（跨 Agent 无权限）。"""
    import asyncio
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="alice"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-alice" in ids, "owner 应看到自己的节点"
    assert "e-bob" not in ids, f"跨 Agent 无权限应隐藏: {ids}"


def test_graph_manager_sees_subordinate(graph_env):
    """manager1 管 alice → 看到 alice 的节点（r5 主管看下属）。"""
    import asyncio
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="manager1"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-alice" in ids, "manager 应看到下属节点"
    assert "e-bob" not in ids, "manager 看不到无关节点"


def test_graph_peer_no_visibility(graph_env):
    """bob 查 → alice 的节点隐藏（worker×worker 不同部门无同任务 → NONE）。"""
    import asyncio
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="bob"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-bob" in ids
    assert "e-alice" not in ids, f"bob 不应看到 alice 节点: {ids}"
