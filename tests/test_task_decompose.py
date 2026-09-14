"""P2: 任务拆解并行 — EXPECTED_ROUTES + parent_task_id 全链路(先红后绿)

- T2-1 反向断言: ("GET", "/api/v1/tasks/{task_id}/subtasks") 必须注册
- T2-2 建父子: create 父 + 3 子(parent_task_id) → subtasks 返回 3
- T2-3 父完成门: 子未全完成 → 父 complete 拒绝(含缺失清单); 全完成 → 父 complete 成功
- T2-4 聚合可见: subtasks 带 completed_count/total; GET /tasks 父任务带 subtask_summary
- T2-5 防环: parent 自指/指向不存在 → 拒绝
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import app

EXPECTED_TASK_ENDPOINTS = [
    ("GET", "/api/v1/tasks/{task_id}/subtasks"),  # P2: 子任务列表+完成聚合
]

TEST_TASKS = ("parent-1", "sub-1", "sub-2", "sub-3", "parent-2", "sub-4", "parent-3", "sub-a", "sub-b", "sub-c")
TEST_AGENTS = ("p2-agent-a", "p2-agent-b", "p2-agent-c")


def _registered_routes():
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods:
            if m in ("GET", "POST", "PUT", "DELETE", "WEBSOCKET"):
                out.add((m, r.path))
    return out


def test_t2_1_subtasks_endpoint_registered():
    registered = _registered_routes()
    missing = [f"{m} {p}" for m, p in EXPECTED_TASK_ENDPOINTS if (m, p) not in registered]
    assert not missing, f"以下端点未注册: {missing}"


def _cleanup():
    from hub_core import hub as _hub
    for aid in TEST_AGENTS:
        _hub.agents.pop(aid, None)
    conn = _hub._db()
    conn.execute("DELETE FROM agents WHERE agent_id IN ('p2-agent-a','p2-agent-b','p2-agent-c')")
    conn.execute("DELETE FROM tasks WHERE task_id IN ('parent-1','sub-1','sub-2','sub-3','parent-2','sub-4','parent-3','sub-a','sub-b','sub-c')")
    conn.commit()
    conn.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        _cleanup()
        yield c
    _cleanup()


def _create(client, tid, desc, parent=None, creator="p2-agent-a"):
    body = {"task_id": tid, "description": desc, "creator_agent_id": creator}
    if parent:
        body["parent_task_id"] = parent
    r = client.post("/api/v1/tasks/create", json=body)
    assert r.status_code == 200, r.text
    return r.json()




def _assign_start(client, tid, agent_id):
    """直接 DB 置 in_progress + assigned(绕过 schedule 的内存匹配, 聚焦 P2 门)"""
    from hub_core import hub as _hub
    conn = _hub._db()
    conn.execute("UPDATE tasks SET status='in_progress', assigned_agent_id=? WHERE task_id=?",
                 (agent_id, tid))
    conn.commit()
    conn.close()

def test_t2_2_create_parent_and_subtasks(client):
    """建父任务 + 3 子任务 → subtasks 端点返回 3 个 + 聚合计数"""
    _create(client, "parent-1", "大任务")
    for s in ("sub-1", "sub-2", "sub-3"):
        _create(client, s, f"子任务{s}", parent="parent-1")
    r = client.get("/api/v1/tasks/parent-1/subtasks")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("status") == "ok", d
    assert d["total"] == 3
    assert d["completed_count"] == 0
    sids = {s["task_id"] for s in d["subtasks"]}
    assert sids == {"sub-1", "sub-2", "sub-3"}
    # 父任务详情带 subtask_summary
    r2 = client.get("/api/v1/tasks/parent-1/subtasks")
    assert r2.json()["total"] == 3


def test_t2_3_parent_complete_gate(client):
    """子任务未全完成 → 父 complete 拒绝; 全完成 → 成功"""
    r = client.post("/api/v1/agents/register",
                    json={"agent_id": "p2-agent-a", "agent_name": "P2甲", "role": "manager"})
    assert r.status_code == 200
    _create(client, "parent-2", "父任务")
    for s in ("sub-1", "sub-2", "sub-3"):
        _create(client, s, f"子{s}", parent="parent-2")
    # 全部 assigned 给 p2-agent-a 并 start
    for tid in ("parent-2", "sub-1", "sub-2", "sub-3"):
        _assign_start(client, tid, "p2-agent-a")
    # 只完成 1 个子任务 → 父 complete 被拒
    assert client.post("/api/v1/tasks/sub-1/complete",
                       params={"agent_id": "p2-agent-a"}).status_code == 200
    r = client.post("/api/v1/tasks/parent-2/complete", params={"agent_id": "p2-agent-a"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("status") == "error", f"父任务应被拒: {d}"
    assert "sub-2" in json_str(d) and "sub-3" in json_str(d), f"缺失清单应含未完成子任务: {d}"
    # 完成剩余 2 个 → 父 complete 成功
    for s in ("sub-2", "sub-3"):
        assert client.post(f"/api/v1/tasks/{s}/complete",
                           params={"agent_id": "p2-agent-a"}).status_code == 200
    r = client.post("/api/v1/tasks/parent-2/complete", params={"agent_id": "p2-agent-a"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("status") == "completed", f"父任务应完成: {d}"
    # 聚合: subtasks 全完成
    r = client.get("/api/v1/tasks/parent-2/subtasks")
    assert r.json()["completed_count"] == 3


def test_t2_4_tasks_list_carries_subtask_summary(client):
    """GET /api/v1/tasks 的父任务带 subtask_summary(看板聚合用)"""
    _create(client, "parent-1", "大任务")
    _create(client, "sub-1", "子1", parent="parent-1")
    _create(client, "sub-2", "子2", parent="parent-1")
    r = client.get("/api/v1/tasks")
    assert r.status_code == 200
    tasks = r.json().get("tasks", [])
    parent = next(t for t in tasks if t.get("task_id") == "parent-1")
    assert parent.get("subtask_summary") == {"total": 2, "completed": 0}, parent
    # 完成一个子任务后聚合更新
    r = client.post("/api/v1/agents/register",
                    json={"agent_id": "p2-agent-a", "agent_name": "P2甲", "role": "manager"})
    assert r.status_code == 200
    _assign_start(client, "sub-1", "p2-agent-a")
    client.post("/api/v1/tasks/sub-1/complete", params={"agent_id": "p2-agent-a"})
    r = client.get("/api/v1/tasks")
    tasks = r.json().get("tasks", [])
    parent = next(t for t in tasks if t.get("task_id") == "parent-1")
    assert parent.get("subtask_summary") == {"total": 2, "completed": 1}


def test_t2_5_cycle_and_missing_parent_rejected(client):
    """parent_task_id 指向不存在 / 指向自己 → 拒绝"""
    r = _create(client, "parent-1", "大任务")
    assert r.get("status") == "created"
    # 子任务 parent 指向不存在的任务
    r = client.post("/api/v1/tasks/create",
                    json={"task_id": "sub-4", "description": "孤儿子任务",
                          "creator_agent_id": "p2-agent-a", "parent_task_id": "no-such-parent"})
    assert r.status_code == 200
    assert r.json().get("status") == "error", f"应拒绝不存在的 parent: {r.json()}"
    # 任务 parent 指向自己
    r = client.post("/api/v1/tasks/create",
                    json={"task_id": "self-cycle", "description": "自指",
                          "creator_agent_id": "p2-agent-a", "parent_task_id": "self-cycle"})
    assert r.json().get("status") == "error"


def json_str(d):
    import json
    return json.dumps(d, ensure_ascii=False)


def test_t2_6_three_agents_parallel_subtasks(client):
    """三 agent 并行推进子任务: 各自 complete → 父任务可 complete(方案验收场景)"""
    for aid, nm in (("p2-agent-a", "甲"), ("p2-agent-b", "乙"), ("p2-agent-c", "丙")):
        r = client.post("/api/v1/agents/register",
                        json={"agent_id": aid, "agent_name": nm, "role": "manager"})
        assert r.status_code == 200
    _create(client, "parent-3", "并行大任务", creator="p2-agent-a")
    for s in ("sub-a", "sub-b", "sub-c"):
        _create(client, s, f"子任务{s}", parent="parent-3", creator="p2-agent-a")
    # 分派: sub-a→a, sub-b→b, sub-c→c, 父→a
    for tid, ag in (("parent-3", "p2-agent-a"), ("sub-a", "p2-agent-a"),
                    ("sub-b", "p2-agent-b"), ("sub-c", "p2-agent-c")):
        _assign_start(client, tid, ag)
    # a 只完成自己的子任务 → 父被拒(缺 b/c)
    assert client.post("/api/v1/tasks/sub-a/complete",
                       params={"agent_id": "p2-agent-a"}).status_code == 200
    r = client.post("/api/v1/tasks/parent-3/complete", params={"agent_id": "p2-agent-a"})
    d = r.json()
    assert d.get("status") == "error" and "sub-b" in json_str(d), d
    # b/c 各自完成 → 父可完成
    for s, ag in (("sub-b", "p2-agent-b"), ("sub-c", "p2-agent-c")):
        assert client.post(f"/api/v1/tasks/{s}/complete",
                           params={"agent_id": ag}).status_code == 200
    r = client.post("/api/v1/tasks/parent-3/complete", params={"agent_id": "p2-agent-a"})
    assert r.json().get("status") == "completed", r.json()
    # 聚合终态
    r = client.get("/api/v1/tasks/parent-3/subtasks")
    d = r.json()
    assert d["completed_count"] == 3 and d["total"] == 3
