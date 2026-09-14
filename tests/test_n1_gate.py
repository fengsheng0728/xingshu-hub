# -*- coding: utf-8 -*-
"""1c N1 全访问授权审批门测试（2026-08-30）

覆盖：
1. full_access 默认 0（fail-closed）→ 删除放行（现有 role 门管）
2. full_access=1 → 删除拦截入 review_queue(pending) + 202
3. 审批执行器：approved 后真执行删除 / 未知端点拒绝
4. 授权端点权限门（manager+）在端到端层验证
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes_n1 import _n1_enqueue, _n1_gate, _execute_n1_delete


@pytest.fixture(autouse=True)
def _no_auth_off(monkeypatch):
    """conftest 强制 SYNC_HUB_NO_AUTH=1 → routes_n1.NO_AUTH=True 全放行；
    N1 审批门测试必须在鉴权语义下跑（NO_AUTH 会绕过 gate）。"""
    import routes_n1

    monkeypatch.setattr(routes_n1, "NO_AUTH", False)


@pytest.fixture(autouse=True)
def _hub_patch(monkeypatch, hub):
    """routes_n1 的 hub 是模块级单例（真实 SyncHub）——测试注入 FakeHub。"""
    import routes_n1

    monkeypatch.setattr(routes_n1, "hub", hub)


class FakeHub:
    def __init__(self, db_path):
        self._db_path = db_path
        self.agents = {}
        self.events = []

    def _db(self):
        return sqlite3.connect(self._db_path)

    async def _log_event(self, *a, **k):
        self.events.append((a, k))

    async def knowledge_delete(self, entry_id):
        self.deleted_knowledge = entry_id

    async def delete_memory(self, memory_key, agent_id):
        self.deleted_memory = (memory_key, agent_id)

    async def remove_team_member(self, member_id, owner):
        self.deleted_member = (member_id, owner)


@pytest.fixture()
def hub():
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute(
        """CREATE TABLE review_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL DEFAULT 'entity',
            doc_id TEXT NOT NULL, name TEXT NOT NULL, detail TEXT DEFAULT '',
            level TEXT DEFAULT 'summary', status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'llm', created_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT, reviewed_by TEXT)"""
    )
    conn.execute("CREATE TABLE automation_jobs (id INTEGER PRIMARY KEY, owner_agent_id TEXT, name TEXT)")
    conn.execute("INSERT INTO automation_jobs (id, owner_agent_id, name) VALUES (1, 'ag-a', 'job1')")
    conn.execute("INSERT INTO automation_jobs (id, owner_agent_id, name) VALUES (2, 'ag-b', 'job2')")
    conn.commit()
    conn.close()
    h = FakeHub(tmp)
    yield h
    try:
        os.remove(tmp)
    except OSError:
        pass


def _qcount(hub, qid):
    conn = hub._db()
    row = conn.execute("SELECT status, item_type FROM review_queue WHERE id = ?", (qid,)).fetchone()
    conn.close()
    return row


# ═══════════ 1. fail-closed 默认关 ═══════════

def test_gate_default_off_passes(hub):
    """full_access=0（默认）→ 放行（None），现有 role 门照旧管"""
    hub.agents["ag-a"] = {"agent_id": "ag-a", "full_access": 0}
    r = asyncio.run(_n1_gate("ag-a", "automation_jobs", {"job_id": 1, "owner": "ag-a"}))
    assert r is None
    assert len(hub.events) == 0  # 未入队、未审计


def test_gate_missing_full_access_passes(hub):
    """旧 agent（无 full_access 字段）→ 视为 0，放行"""
    hub.agents["ag-a"] = {"agent_id": "ag-a"}
    r = asyncio.run(_n1_gate("ag-a", "automation_jobs", {"job_id": 1, "owner": "ag-a"}))
    assert r is None


# ═══════════ 2. full_access=1 → 拦截入队 ═══════════

def test_gate_full_access_intercepts(hub):
    hub.agents["ag-a"] = {"agent_id": "ag-a", "full_access": 1}
    r = asyncio.run(_n1_gate("ag-a", "automation_jobs", {"job_id": 1, "owner": "ag-a"}))
    assert r is not None
    assert r["status"] == "pending_approval"
    assert r["queue_id"] > 0
    status, item_type = _qcount(hub, r["queue_id"])
    assert status == "pending" and item_type == "n1_delete"
    # 审计入链
    assert any("n1_delete_pending" in str(e) for e in hub.events)


def test_gate_unknown_agent_no_intercept(hub):
    """非登记 agent（幽灵）→ hub.agents 无记录 → 无 full_access → 放行（后续 role 门兜底）"""
    r = asyncio.run(_n1_gate("ghost", "automation_jobs", {"job_id": 1}))
    assert r is None


# ═══════════ 3. 审批执行器 ═══════════

def test_execute_automation_delete(hub):
    detail = {"endpoint": "automation_jobs", "params": {"job_id": 1, "owner": "ag-a"}}
    r = asyncio.run(_execute_n1_delete(detail))
    assert r["status"] == "executed"
    conn = hub._db()
    row = conn.execute("SELECT * FROM automation_jobs WHERE id = 1").fetchone()
    conn.close()
    assert row is None  # 真删了
    # 非本人 job 不受影响
    conn = hub._db()
    row2 = conn.execute("SELECT * FROM automation_jobs WHERE id = 2").fetchone()
    conn.close()
    assert row2 is not None


def test_execute_knowledge_delete(hub):
    detail = {"endpoint": "knowledge", "params": {"entry_id": "e-1"}}
    r = asyncio.run(_execute_n1_delete(detail))
    assert r["status"] == "executed"
    assert hub.deleted_knowledge == "e-1"


def test_execute_memory_delete(hub):
    detail = {"endpoint": "memory", "params": {"memory_key": "m1", "agent_id": "ag-a"}}
    r = asyncio.run(_execute_n1_delete(detail))
    assert hub.deleted_memory == ("m1", "ag-a")


def test_execute_team_delete(hub):
    detail = {"endpoint": "team_members", "params": {"member_id": 7, "owner": "ag-a"}}
    r = asyncio.run(_execute_n1_delete(detail))
    assert hub.deleted_member == (7, "ag-a")


def test_execute_unknown_endpoint_raises(hub):
    detail = {"endpoint": "nope", "params": {}}
    with pytest.raises(Exception):
        asyncio.run(_execute_n1_delete(detail))


def test_enqueue_detail_roundtrip(hub):
    qid = _n1_enqueue("ag-a", "knowledge", {"entry_id": "e-9"})
    conn = hub._db()
    row = conn.execute("SELECT detail FROM review_queue WHERE id = ?", (qid,)).fetchone()
    conn.close()
    d = json.loads(row[0])
    assert d["endpoint"] == "knowledge" and d["params"] == {"entry_id": "e-9"}
    assert d["requester"] == "ag-a"
