# -*- coding: utf-8 -*-
"""星枢断言机械化 batch3 · 任务3：门卫负向存活探针

「主动触发拒绝路径，验证门卫活着」。TestClient 直测 routes.app（不起真实 Hub 进程、
不跑 lifespan），monkeypatch 把 NO_AUTH 拨回 False + CONFIG 指向临时库，
使 TokenAuthMiddleware（ASGI 统一门卫）以真实鉴权语义运行。

探针清单：
1. 无凭据请求受保护端点            → 401
2. 无效/伪造凭据                  → 401
3. 白名单外端点（含未注册路径）无凭据 → 401（证明门卫先于路由表，404 之前拦截）
4. 高危操作审批门（出站写外部系统）  → pending_approval 拦截 + 审计
   注：代码库无 shell 执行端点，「无审批上下文直接执行高危操作 → 拒绝」
   语义的现存等价物是 integrations 出站审批门（registry.run_outbound），
   任务书要求「参考现有 shell 审批逻辑」，此处按现状取其等价门卫。
5. scoped key 越域                → 403（白名单内路径过门 → 404 证明是门拒不是路由缺失）
6. N1 删除端点无审批              → 拦截入队（pending_approval），数据未删；
   对照组：无 full_access 的 agent 同操作真实删除（证明拦截来自 N1 门）
7. N1 审批队列 role 门            → worker 403（manager+ 专属）
8. 正向对照：allowlist /healthz 无凭据 → 200（证明 app 活着、门卫只拦该拦的）
"""
import json
import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes
import routes_common
import routes_n1
import routes_integrations
import key_scopes
from auth_provider import LocalProvider
from hub_core import hub
from models import CONFIG

HUB_TOKEN = "probe-hub-token-batch3"


def _make_db(db_path: str):
    """最小表集合：agents / events / review_queue / automation_jobs / agent_keys"""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE agents (
        agent_id TEXT PRIMARY KEY, agent_name TEXT, api_key TEXT,
        role TEXT DEFAULT 'worker', full_access INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, agent_id TEXT, payload TEXT, timestamp TEXT)""")
    conn.execute("""CREATE TABLE review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_type TEXT NOT NULL DEFAULT 'entity',
        doc_id TEXT NOT NULL, name TEXT NOT NULL, detail TEXT DEFAULT '',
        level TEXT DEFAULT 'summary', status TEXT DEFAULT 'pending',
        source TEXT DEFAULT 'llm', created_at TEXT DEFAULT (datetime('now')),
        reviewed_at TEXT, reviewed_by TEXT)""")
    conn.execute("""CREATE TABLE automation_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '',
        trigger_type TEXT NOT NULL DEFAULT 'cron', trigger_spec TEXT NOT NULL DEFAULT '',
        instruction TEXT NOT NULL DEFAULT '', owner_agent_id TEXT)""")
    conn.execute("""CREATE TABLE agent_keys (
        key_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, key_hash TEXT NOT NULL,
        scope TEXT DEFAULT '{"endpoints": [], "data_domain": [], "level_cap": ""}',
        status TEXT DEFAULT 'active', created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')), expires_at TEXT,
        last_used_at TEXT, call_count INTEGER DEFAULT 0)""")
    conn.commit()
    conn.close()


def _add_agent(db_path: str, agent_id: str, api_key: str,
               role: str = "worker", full_access: int = 0):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO agents (agent_id, agent_name, api_key, role, full_access)"
        " VALUES (?, ?, ?, ?, ?)",
        (agent_id, agent_id, api_key, role, full_access))
    conn.commit()
    conn.close()


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """鉴权语义环境：NO_AUTH=False + 临时库 + LocalProvider 重绑 + TestClient（无 lifespan）。"""
    db_path = str(tmp_path / "gate.db")
    _make_db(db_path)
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    monkeypatch.setattr(CONFIG, "HUB_TOKEN", HUB_TOKEN)
    # NO_AUTH 是各模块 import 时的绑定，门卫/依赖/审批门各自读取本模块全局 → 全部拨回 False
    for mod in (routes, routes_common, routes_n1, routes_integrations):
        monkeypatch.setattr(mod, "NO_AUTH", False)
    # provider 单例重绑到临时库语义（LocalProvider 每次 authenticate 实时读 CONFIG）
    monkeypatch.setattr(routes_common, "_AUTH_PROVIDER", LocalProvider(CONFIG))
    # scoped key store 单例重绑（否则指向会话中先到者创建的库）
    monkeypatch.setattr(key_scopes, "_store", None)

    injected = []

    class Gate:
        client = TestClient(routes.app)  # 不用 with：不跑 lifespan（备份/调度循环）
        db = db_path

        @staticmethod
        def agent(agent_id, api_key, role="worker", full_access=0):
            _add_agent(db_path, agent_id, api_key, role, full_access)
            # 内存态：role 门 / N1 门读 hub.agents
            hub.agents[agent_id] = {"agent_id": agent_id, "role": role,
                                    "full_access": full_access}
            injected.append(agent_id)

        @staticmethod
        def auth(token):
            return {"Authorization": f"Bearer {token}"}

    yield Gate
    for aid in injected:
        hub.agents.pop(aid, None)


# ═══════════ 探针 1：无凭据 → 401 ═══════════

def test_no_credential_rejected(gate):
    r = gate.client.get("/api/v1/tasks")
    assert r.status_code == 401, f"无凭据应 401，实际 {r.status_code}"
    assert "Unauthorized" in r.json().get("detail", "")


# ═══════════ 探针 2：伪造凭据 → 401 ═══════════

def test_forged_credential_rejected(gate):
    gate.agent("probe-real", "probe-real-key")
    r = gate.client.get("/api/v1/tasks", headers=gate.auth("sk-forged-not-exists"))
    assert r.status_code == 401, f"伪造凭据应 401，实际 {r.status_code}"


# ═══════════ 探针 3：白名单外端点（ASGI 统一门卫先于路由） ═══════════

def test_gate_intercepts_before_router(gate):
    # 未注册路径：无凭据 → 401（门卫先拦，轮不到 404）
    r = gate.client.get("/api/v1/ghost-endpoint-probe")
    assert r.status_code == 401, \
        f"白名单外未注册路径无凭据应 401（门卫先于路由），实际 {r.status_code}"
    # 有效 hub_token → 过门后才是路由语义 404（证明 401 来自门卫而非路由缺失）
    r2 = gate.client.get("/api/v1/ghost-endpoint-probe", headers=gate.auth(HUB_TOKEN))
    assert r2.status_code == 404, f"有效凭据过门后应 404，实际 {r2.status_code}"


# ═══════════ 探针 4：高危操作审批门（出站写外部系统 = 现存 shell 审批等价物） ═══════════

def test_outbound_high_risk_blocked_pending_approval(gate):
    gate.agent("probe-mgr", "probe-mgr-key", role="manager")
    r = gate.client.post("/api/v1/integrations/example/outbound",
                         json={"event_type": "shell.exec", "payload": {"cmd": "rm -rf /"}},
                         headers=gate.auth("probe-mgr-key"))
    assert r.status_code == 200, f"审批门拦截响应应 200，实际 {r.status_code}"
    body = r.json()
    assert body.get("status") == "pending_approval", \
        f"无审批上下文的高危出站操作必须拦截为 pending_approval，实际 {body}"
    # 拦截事件入审计（门卫活着的证据）
    conn = sqlite3.connect(gate.db)
    row = conn.execute(
        "SELECT payload FROM events WHERE event_type='integration_outbound_blocked'"
    ).fetchone()
    conn.close()
    assert row, "审批门拦截未入审计（events 缺 integration_outbound_blocked）"
    assert "outbound_requires_approval" in row[0]


# ═══════════ 探针 5：scoped key 越域 → 403 ═══════════

def test_scoped_key_cross_domain_rejected(gate):
    store = key_scopes.ScopedKeyStore(gate.db)
    created = store.create("probe-scoped", {"endpoints": ["/probe-allowed"]},
                           created_by="probe")
    scoped_key = created["key"]
    gate.agent("probe-scoped", scoped_key)
    # 越域：scope 白名单外的业务端点 → 403
    r = gate.client.get("/api/v1/tasks", headers=gate.auth(scoped_key))
    assert r.status_code == 403, f"scoped key 越域应 403，实际 {r.status_code}"
    assert "scoped key" in r.json().get("detail", "")
    # 域内：白名单路径过门（404 = 过了门卫、路由不存在；证明 403 是门拒而非 key 无效）
    r2 = gate.client.get("/api/v1/probe-allowed", headers=gate.auth(scoped_key))
    assert r2.status_code == 404, \
        f"scoped key 域内路径应过门（404），实际 {r2.status_code}"


# ═══════════ 探针 6：N1 删除端点无审批 → 拦截 ═══════════

def test_n1_delete_intercepted_without_approval(gate):
    gate.agent("probe-n1", "probe-n1-key", role="worker", full_access=1)
    conn = sqlite3.connect(gate.db)
    conn.execute("INSERT INTO automation_jobs (name, owner_agent_id)"
                 " VALUES ('victim-job', 'probe-n1')")
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    r = gate.client.delete(f"/api/v1/automation/jobs/{job_id}",
                           headers=gate.auth("probe-n1-key"))
    assert r.status_code == 200, f"N1 拦截响应应 200，实际 {r.status_code}"
    body = r.json()
    assert body.get("status") == "pending_approval", \
        f"full_access agent 删除无审批必须拦截，实际 {body}"
    assert body.get("queue_id"), "拦截应生成审批队列号"
    # 数据未被真删
    conn = sqlite3.connect(gate.db)
    alive = conn.execute("SELECT 1 FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
    pending = conn.execute(
        "SELECT status, item_type FROM review_queue WHERE id=?",
        (body["queue_id"],)).fetchone()
    conn.close()
    assert alive, "审批未通过前数据不得真删"
    assert pending == ("pending", "n1_delete"), f"审批队列状态异常: {pending}"


def test_n1_gate_pass_through_without_full_access(gate):
    """对照组：无 full_access 的同操作真实删除 —— 证明上一探针的拦截来自 N1 门本身。"""
    gate.agent("probe-plain", "probe-plain-key", role="worker", full_access=0)
    conn = sqlite3.connect(gate.db)
    conn.execute("INSERT INTO automation_jobs (name, owner_agent_id)"
                 " VALUES ('plain-job', 'probe-plain')")
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    r = gate.client.delete(f"/api/v1/automation/jobs/{job_id}",
                           headers=gate.auth("probe-plain-key"))
    assert r.status_code == 200 and r.json().get("ok") is True, \
        f"无 full_access 应直接删除，实际 {r.status_code} {r.text[:200]}"
    conn = sqlite3.connect(gate.db)
    gone = conn.execute("SELECT 1 FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    assert gone is None, "对照组应真删"


# ═══════════ 探针 7：N1 审批队列 role 门 → worker 403 ═══════════

def test_n1_review_queue_role_gate(gate):
    gate.agent("probe-worker", "probe-worker-key", role="worker")
    r = gate.client.get("/api/v1/n1/reviews", headers=gate.auth("probe-worker-key"))
    assert r.status_code == 403, f"worker 查 N1 审批队列应 403，实际 {r.status_code}"


# ═══════════ 探针 8：正向对照 —— allowlist 无凭据可达 ═══════════

def test_allowlisted_health_open(gate):
    r = gate.client.get("/healthz")
    assert r.status_code == 200, f"allowlist /healthz 应无凭据 200，实际 {r.status_code}"
    assert r.json().get("status") == "alive"
