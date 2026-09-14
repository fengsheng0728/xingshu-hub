# -*- coding: utf-8 -*-
"""tests/test_register_role_preserve.py — CD-020：register/bootstrap 角色不降级

背景：Agent 端 bootstrap payload 原写死 role='worker'，每次连接经 INSERT OR
REPLACE 把服务端已设定的 manager/orchestrator 静默覆盖降级回 worker →
知识写入变 403「仅主管/店长可编辑」，且失败形态无「角色被重置」提示。

修复语义（角色真相在服务端）：
- 已注册 agent 再次 register/bootstrap：role 保持服务端现值（不降级也不提权）
- 首次注册：采信请求 role（默认 worker）

直调 hub.register（不起 HTTP/TestClient），monkeypatch CONFIG.DB_PATH 指向
临时库（完整 agents + register 依赖表），断言双写一致（hub.agents 内存 + DB）。
"""
import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub_core import hub
from models import CONFIG, AgentRegistration


def _make_full_db(db_path: str):
    """register 路径依赖的完整表集（agents 对齐 db.py DDL 列集 + events/audit 等）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE agents (
        agent_id TEXT PRIMARY KEY, agent_name TEXT, api_key TEXT,
        department TEXT DEFAULT '', capabilities TEXT DEFAULT '[]',
        role TEXT DEFAULT 'worker', managed_agents TEXT DEFAULT '[]',
        disclosure_policy TEXT DEFAULT '{}', endpoint TEXT DEFAULT '',
        registered_at TEXT, last_heartbeat TEXT, status TEXT DEFAULT 'offline',
        api_key_created_at TEXT, api_key_expires_at TEXT,
        full_access INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, agent_id TEXT, payload TEXT, timestamp TEXT)""")
    conn.execute("""CREATE TABLE audit_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT, entry_type TEXT,
        ref_table TEXT DEFAULT '', ref_id TEXT DEFAULT '', payload TEXT,
        entry_hash TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE disclosure_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT, requester TEXT, owner TEXT,
        target_id TEXT, level TEXT, entry_hash TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE memory_pool (
        memory_id TEXT PRIMARY KEY, owner_agent_id TEXT, kind TEXT,
        content TEXT, disclosure_level TEXT DEFAULT 'summary',
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE notifications (
        notif_id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT,
        type TEXT, title TEXT, body TEXT, is_read INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    conn.close()


def _reg(agent_id: str, role: str = "worker"):
    return AgentRegistration(agent_id=agent_id, agent_name=agent_id,
                             role=role, capabilities=[], department="")


def _db_role(db_path: str, agent_id: str) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT role FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    conn.close()
    return row[0] if row else ""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库 + CONFIG 重绑 + hub.agents 隔离（不跑 lifespan）。"""
    db_path = str(tmp_path / "role.db")
    _make_full_db(db_path)
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    for aid in ("cd020-mgr", "cd020-first", "cd020-fresh"):
        hub.agents.pop(aid, None)
    yield db_path
    for aid in ("cd020-mgr", "cd020-first", "cd020-fresh"):
        hub.agents.pop(aid, None)


def test_re_register_worker_does_not_demote_manager(env):
    """核心场景：manager 被 worker 请求（Agent 端 bootstrap 形态）二次注册 → 仍 manager。"""
    db = env
    assert asyncio.run(hub.register(_reg("cd020-mgr", "manager"))).get("status")
    assert _db_role(db, "cd020-mgr") == "manager"
    # 模拟 Agent 端 bootstrap：不传 role（默认 worker）
    asyncio.run(hub.register(_reg("cd020-mgr", "worker")))
    assert hub.agents["cd020-mgr"]["role"] == "manager", "内存态被降级"
    assert _db_role(db, "cd020-mgr") == "manager", "DB 被降级"


def test_re_register_worker_does_not_escalate(env):
    """对称防御：worker 被 manager 请求二次注册 → 不提权（角色真相在服务端，首次才采信）。"""
    db = env
    asyncio.run(hub.register(_reg("cd020-first", "worker")))
    asyncio.run(hub.register(_reg("cd020-first", "manager")))
    assert hub.agents["cd020-first"]["role"] == "worker"
    assert _db_role(db, "cd020-first") == "worker"


def test_first_register_honors_requested_role(env):
    """首次注册采信请求 role（manager 显式注册生效——测试 Hub 造 manager 的既有路径不受影响）。"""
    db = env
    asyncio.run(hub.register(_reg("cd020-fresh", "manager")))
    assert hub.agents["cd020-fresh"]["role"] == "manager"
    assert _db_role(db, "cd020-fresh") == "manager"
