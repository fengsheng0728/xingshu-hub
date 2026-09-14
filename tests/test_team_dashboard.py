"""P1: 团队仪表盘 — EXPECTED_ROUTES 反向断言 + /api/v1/team/stats 数据一致性

- T1-1 反向断言: ("GET", "/api/v1/team/stats") + ("GET", "/team") 必须注册
- T1-2 stats 数据一致性: agents(register+heartbeat) / tasks by_status+by_agent /
       memory by_kind / automation jobs 统计, 与 DB 实测一致
- T1-3 /team 页面壳: 200 + auth.js 注入 + 免 CDN
- T1-4 鉴权: 独立鉴权 Hub 无 token → 401 (subprocess 配方)
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import app

EXPECTED_TEAM_ENDPOINTS = [
    ("GET", "/api/v1/team/stats"),   # P1: 团队聚合统计(老板视图)
]

EXPECTED_PAGES = ["/team"]


def _registered_routes():
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods:
            if m in ("GET", "POST", "PUT", "DELETE", "WEBSOCKET"):
                out.add((m, r.path))
    return out


def test_t1_1_team_endpoints_all_registered():
    registered = _registered_routes()
    missing = [f"{m} {p}" for m, p in EXPECTED_TEAM_ENDPOINTS if (m, p) not in registered]
    assert not missing, f"以下 team 端点未注册: {missing}"
    for pg in EXPECTED_PAGES:
        assert (pg, pg) not in [("x", "x")]  # 页面路由是 /team, 由单独用例验证


def test_t1_1_pages_registered():
    """页面路由 /team 必须注册(HTMLResponse)"""
    registered = _registered_routes()
    assert ("GET", "/team") in registered, "GET /team 页面未注册"


TEST_AGENTS = ("team-a", "team-b", "team-c")
TEST_TASKS = ("t-1", "t-2", "t-3")
TEST_MEMS = ("mem-1", "mem-2", "mem-b")
TEST_JOBS = ("job-on", "job-off")


def _cleanup_test_data():
    """清理本测试文件可能残留的数据(重复跑幂等, 不污染生产库)"""
    from hub_core import hub as _hub
    for aid in TEST_AGENTS:
        _hub.agents.pop(aid, None)
    conn = _hub._db()
    conn.execute("DELETE FROM agents WHERE agent_id IN ('team-a','team-b','team-c')")
    conn.execute("DELETE FROM tasks WHERE task_id IN ('t-1','t-2','t-3')")
    conn.execute("DELETE FROM memory_pool WHERE memory_key IN ('mem-1','mem-2','mem-b')")
    conn.execute("DELETE FROM automation_jobs WHERE name IN ('job-on','job-off')")
    conn.commit()
    conn.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        _cleanup_test_data()
        yield c
    _cleanup_test_data()


def _create_agent(client, agent_id, name, role="worker"):
    r = client.post("/api/v1/agents/register",
                    json={"agent_id": agent_id, "agent_name": name, "role": role})
    assert r.status_code == 200, r.text
    return r.json()


def test_t1_2_stats_agents_tasks_memory_automation(client):
    """stats 聚合正确反映本次操作(相对增量断言, 防生产 DB 数据污染)"""
    # 基线
    r0 = client.get("/api/v1/team/stats")
    assert r0.status_code == 200
    base = r0.json()
    base_tasks = base["tasks"]["by_status"]
    base_mem = base["memory"]["total"]
    base_auto = base["automation"]["total"]
    base_agents = len(base["agents"])

    # 双 agent(manager + worker)
    _create_agent(client, "team-a", "团队甲", role="manager")
    _create_agent(client, "team-b", "团队乙")
    # a 心跳上线
    r = client.post("/api/v1/agents/team-a/heartbeat", json={})
    assert r.status_code == 200

    # 任务: create 3 → a: 1 completed + 1 in_progress, b: 1 pending
    for tid, desc in (("t-1", "任务一"), ("t-2", "任务二"), ("t-3", "任务三")):
        r = client.post("/api/v1/tasks/create",
                        json={"task_id": tid, "description": desc, "creator_agent_id": "team-a"})
        assert r.status_code == 200, r.text
    # t-1: schedule → start → complete (manager)
    assert client.post("/api/v1/tasks/t-1/schedule",
                       params={"agent_id": "team-a", "assigned_agent_id": "team-a"}).status_code == 200
    assert client.post("/api/v1/tasks/t-1/start",
                       params={"agent_id": "team-a"}).status_code == 200
    assert client.post("/api/v1/tasks/t-1/complete",
                       params={"agent_id": "team-a"}).status_code == 200
    # t-2: schedule → start (in_progress)
    assert client.post("/api/v1/tasks/t-2/schedule",
                       params={"agent_id": "team-a", "assigned_agent_id": "team-a"}).status_code == 200
    assert client.post("/api/v1/tasks/t-2/start",
                       params={"agent_id": "team-a"}).status_code == 200

    # 记忆: a 两条不同 kind(内容差异化, 避免 store 相似度去重合并), b 一条
    for key, kind, content in (("mem-1", "fact", "客户预算是三万,倾向季度付款"),
                               ("mem-2", "todo", "今天完成了任务看板的前端开发"),
                               ("mem-b", "fact", "B 正在处理报价方案文档")):
        r = client.post(f"/api/v1/memory/store?agent_id={'team-a' if key != 'mem-b' else 'team-b'}",
                        json={"memory_key": key, "content": content, "kind": kind})
        assert r.status_code == 200, r.text

    # 自动化: a 1 个 enabled + 1 个 disabled(create 默认 enabled, disabled 走 toggle)
    r = client.post("/api/v1/automation/jobs",
                    json={"name": "job-on", "instruction": "定期汇报",
                          "trigger_type": "schedule", "trigger_spec": "0 9 * * *",
                          "owner_agent_id": "team-a"})
    assert r.status_code == 200, r.text
    r = client.post("/api/v1/automation/jobs",
                    json={"name": "job-off", "instruction": "暂停的定时任务",
                          "trigger_type": "schedule", "trigger_spec": "0 10 * * *",
                          "owner_agent_id": "team-a"})
    assert r.status_code == 200, r.text
    job_off_id = r.json()["job_id"]
    r = client.post(f"/api/v1/automation/jobs/{job_off_id}/toggle", json={"enabled": False})
    assert r.status_code == 200, r.text

    # 拉 stats, 断言增量
    r = client.get("/api/v1/team/stats")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st.get("status") == "ok", st

    agents = st["agents"]
    assert len(agents) == base_agents + 2
    by_id = {a["agent_id"]: a for a in agents}
    assert "team-a" in by_id and "team-b" in by_id
    # Hub 语义: register 即 online(注册即上线, 心跳更新 last_heartbeat)
    assert by_id["team-a"]["status"] == "online"
    assert by_id["team-b"]["status"] == "online"
    assert by_id["team-a"]["role"] == "manager"
    assert by_id["team-b"]["role"] == "worker"

    tasks = st["tasks"]
    assert tasks["by_status"].get("completed", 0) == base_tasks.get("completed", 0) + 1
    assert tasks["by_status"].get("in_progress", 0) == base_tasks.get("in_progress", 0) + 1
    assert tasks["by_status"].get("pending", 0) == base_tasks.get("pending", 0) + 1
    by_agent = tasks["by_agent"]
    assert by_agent.get("team-a", {}).get("completed", 0) == 1
    assert by_agent.get("team-a", {}).get("in_progress", 0) == 1

    mem = st["memory"]
    assert mem["total"] == base_mem + 3
    assert mem["by_kind"].get("fact", 0) >= 2      # 生产库可能已有 fact
    assert mem["by_kind"].get("todo", 0) >= 1

    auto = st["automation"]
    assert auto["total"] == base_auto + 2
    assert auto["enabled"] >= 1
    assert auto["disabled"] >= 1


def test_t1_3_team_page_served(client):
    """/team 页面壳: 200 + auth.js + 免 CDN + 关键区块"""
    r = client.get("/team")
    assert r.status_code == 200
    html = r.text
    assert "auth.js" in html, "必须注入 auth.js(与其它 API 页同标准)"
    assert html.count("cdn.") == 0, "禁止 CDN 外链"
    for k in ("团队", "agent", "task", "memory", "automation"):
        assert k.lower() in html.lower(), f"页面缺 {k} 区块"


def test_t1_3b_stats_reflects_inmemory_status(client):
    """stats 的 status 直接透传 hub.agents 内存状态(离线分支)"""
    _create_agent(client, "team-c", "团队丙")
    from hub_core import hub as _hub
    _hub.agents["team-c"]["status"] = "offline"   # 模拟 cleanup 标离线
    r = client.get("/api/v1/team/stats")
    assert r.status_code == 200
    by_id = {a["agent_id"]: a for a in r.json()["agents"]}
    assert by_id["team-c"]["status"] == "offline"
    assert by_id["team-c"]["online"] is False


# ── T1-4 鉴权: 独立鉴权 Hub 无 token → 401 ──

def _start_auth_hub(tmpdir, port):
    cfg = os.path.join(tmpdir, "config.yaml")
    db = os.path.join(tmpdir, "hub.db")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(f"""server:
  host: 127.0.0.1
  port: {port}
database:
  path: {db}
backup_enabled: False
auth:
  hub_token: "test-hub-token"
""")
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = tmpdir
    env.pop("SYNC_HUB_NO_AUTH", None)
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("auth hub did not start")


def test_t1_4_stats_requires_auth():
    """鉴权开启时 /api/v1/team/stats 无 token → 401"""
    tmpdir = tempfile.mkdtemp(prefix="team-auth-")
    proc = _start_auth_hub(tmpdir, 3066)
    try:
        req = urllib.request.Request("http://127.0.0.1:3066/api/v1/team/stats")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "期望 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"期望 401, got {e.code}"
    finally:
        proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
