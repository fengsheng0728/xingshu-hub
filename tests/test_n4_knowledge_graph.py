# -*- coding: utf-8 -*-
"""N4 权限级记忆图谱验收测试（2026-08-05）

覆盖：
1. 无 requester → 全量节点（向后兼容）
2. requester=owner → 自己创建的节点可见
3. 已发布条目全员可见（语义随 CD-065 变更，2026-09-20 用户拍板：
   知识库条目 = 已发布内容，图谱层 METADATA 级 id/标题/标签对任何认证主体
   可见；原「跨 Agent worker 无同任务 → NONE 节点隐藏」三条断言随之翻转并加严）
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


def test_n4_owner_sees_own_published_visible(graph_env):
    """alice 看图谱 → 自己的节点可见；bob 的节点同样可见（已发布条目全员可见）。
    语义随 CD-065 变更（2026-09-20 用户拍板）：原断言「e-bob 跨 Agent 无权限应隐藏」
    按新口径翻转并加严 —— 不仅要求可见，还锁定全集/隐藏计数/节点只含 metadata 键。"""
    import asyncio
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="alice"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-alice" in ids, "owner 应看到自己的节点"
    assert "e-bob" in ids, f"已发布条目对任何认证主体可见（CD-065）: {ids}"
    assert ids == {"e-alice", "e-bob"}, f"加严：节点全集应为两条已发布条目: {ids}"
    assert r["hidden"] == 0, f"加严：已发布条目不应有隐藏计数: {r['hidden']}"
    for n in r["nodes"]:
        assert set(n) == {"id", "title", "category", "importance", "tags"}, \
            f"加严：图谱节点只许 metadata 键（正文永不进图谱）: {sorted(n)}"


def test_n4_manager_sees_subordinate_published_visible(graph_env):
    """manager1 管 alice → 看到 alice 的节点（r5 主管看下属）；bob 的已发布条目
    同样可见。语义随 CD-065 变更（2026-09-20 用户拍板）：原断言「manager 看不到
    无关节点」按新口径翻转并加严。"""
    import asyncio
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="manager1"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-alice" in ids, "manager 应看到下属节点"
    assert "e-bob" in ids, f"已发布条目对 manager 同样可见（CD-065）: {ids}"
    assert ids == {"e-alice", "e-bob"}, f"加严：节点全集应为两条已发布条目: {ids}"
    assert r["hidden"] == 0, f"加严：已发布条目不应有隐藏计数: {r['hidden']}"


def test_n4_peer_published_visible(graph_env):
    """bob 查 → alice 的已发布条目可见（CD-065：知识库条目 = 已发布内容，
    图谱层 METADATA 级全员可见，正文仍走披露链不进图谱）。
    语义随 CD-065 变更（2026-09-20 用户拍板）：原断言「bob 不应看到 alice 节点」
    按新口径翻转并加严。"""
    import asyncio
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="bob"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-bob" in ids
    assert "e-alice" in ids, f"已发布条目对跨部门 worker 可见（CD-065）: {ids}"
    assert ids == {"e-alice", "e-bob"}, f"加严：节点全集应为两条已发布条目: {ids}"
    assert r["hidden"] == 0, f"加严：已发布条目不应有隐藏计数: {r['hidden']}"
    node = next(n for n in r["nodes"] if n["id"] == "e-alice")
    assert "content" not in node, "加严：正文永不进图谱"
