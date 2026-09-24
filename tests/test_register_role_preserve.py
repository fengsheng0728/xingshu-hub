# -*- coding: utf-8 -*-
"""tests/test_register_role_preserve.py — CD-020：register/bootstrap 角色不降级

背景：Agent 端 bootstrap payload 原写死 role='worker'，每次连接经 INSERT OR
REPLACE 把服务端已设定的 manager/orchestrator 静默覆盖降级回 worker →
知识写入变 403「仅主管/店长可编辑」，且失败形态无「角色被重置」提示。

修复语义（角色真相在服务端）：
- 已注册 agent 再次 register/bootstrap：role 保持服务端现值（不降级也不提权）
- 首次注册：CD-099（2026-09-23，H-4 默认值收口）起**强制 worker**——自报 role /
  managed_agents / disclosure_policy 属提权维度，一律不采信；角色变更只能走管理端点。

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
    for aid in ("cd020-mgr", "cd020-first", "cd020-fresh",
                "cd082-quota", "cd082-fail"):
        hub.agents.pop(aid, None)
    yield db_path
    for aid in ("cd020-mgr", "cd020-first", "cd020-fresh",
                "cd082-quota", "cd082-fail"):
        hub.agents.pop(aid, None)


def test_re_register_worker_does_not_demote_manager(env):
    """核心场景：manager 被 worker 请求（Agent 端 bootstrap 形态）二次注册 → 仍 manager。

    CD-099 后首注强制 worker，故 manager 态由「管理端点直改 DB」预置（等价 hub_cli
    agent create --role manager 的建号路径），再走二次注册验证不降级。"""
    db = env
    assert asyncio.run(hub.register(_reg("cd020-mgr", "manager"))).get("status")
    assert _db_role(db, "cd020-mgr") == "worker", "CD-099: 首注自报 manager 不生效，落库为 worker"
    # 管理端预置角色（等价 hub_cli agent create / 管理端点）
    conn = sqlite3.connect(db)
    conn.execute("UPDATE agents SET role='manager' WHERE agent_id='cd020-mgr'")
    conn.commit()
    conn.close()
    hub.agents["cd020-mgr"]["role"] = "manager"
    # 模拟 Agent 端 bootstrap：不传 role（默认 worker）
    asyncio.run(hub.register(_reg("cd020-mgr", "worker")))
    assert hub.agents["cd020-mgr"]["role"] == "manager", "内存态被降级"
    assert _db_role(db, "cd020-mgr") == "manager", "DB 被降级"


def test_re_register_worker_does_not_escalate(env):
    """对称防御：worker 被 manager 请求二次注册 → 不提权（角色真相在服务端）。"""
    db = env
    asyncio.run(hub.register(_reg("cd020-first", "worker")))
    asyncio.run(hub.register(_reg("cd020-first", "manager")))
    assert hub.agents["cd020-first"]["role"] == "worker"
    assert _db_role(db, "cd020-first") == "worker"


def test_first_register_forces_worker(env):
    """CD-099（H-4 默认值收口）：首次注册强制 worker——自报 role/managed_agents/
    disclosure_policy 全部不采信（提权维度）；角色变更只能走管理端点。"""
    db = env
    reg = AgentRegistration(agent_id="cd020-fresh", agent_name="cd020-fresh",
                            role="manager", capabilities=[], department="",
                            managed_agents=["someone"], disclosure_policy={"k": "v"})
    asyncio.run(hub.register(reg))
    assert hub.agents["cd020-fresh"]["role"] == "worker", "首注自报 manager 不得生效"
    assert _db_role(db, "cd020-fresh") == "worker"
    assert hub.agents["cd020-fresh"]["managed_agents"] == [], "首注自报 managed_agents 不得生效"
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT managed_agents, disclosure_policy FROM agents WHERE agent_id='cd020-fresh'"
    ).fetchone()
    conn.close()
    assert row[0] == "[]" and row[1] == "{}", \
        f"首注落库的 managed_agents/disclosure_policy 必须为空，实际 {row!r}"


# ═══ 数据层修复轮：register 配额块 + 内存/DB 写入顺序 ═══


def _make_quota_table(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_quotas (
        agent_id TEXT PRIMARY KEY, qps_limit REAL NOT NULL DEFAULT 50.0,
        mode TEXT NOT NULL DEFAULT 'alert_only', window_sec REAL NOT NULL DEFAULT 1.0,
        burst INTEGER NOT NULL DEFAULT 3,
        updated_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    conn.close()


def test_register_writes_default_quota_row(env):
    """先红锚点：旧代码在 conn.close() 之后才 conn.cursor() 写 agent_quotas——
    ProgrammingError 被 except-pass 吞掉，默认配额行从未落库。"""
    db = env
    _make_quota_table(db)
    res = asyncio.run(hub.register(_reg("cd082-quota")))
    assert res["status"] == "registered"
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT qps_limit, mode FROM agent_quotas WHERE agent_id=?",
        ("cd082-quota",)).fetchone()
    conn.close()
    assert row == (50.0, "alert_only"), \
        f"register 后必须存在默认配额行（alert_only），实际 {row!r}"
    # 二次注册不得覆盖已有配置（INSERT OR IGNORE 语义保留）
    conn = sqlite3.connect(db)
    conn.execute("UPDATE agent_quotas SET qps_limit=7.0 WHERE agent_id=?",
                 ("cd082-quota",))
    conn.commit()
    conn.close()
    asyncio.run(hub.register(_reg("cd082-quota")))
    conn = sqlite3.connect(db)
    row2 = conn.execute("SELECT qps_limit FROM agent_quotas WHERE agent_id=?",
                        ("cd082-quota",)).fetchone()
    conn.close()
    assert row2[0] == 7.0, f"已有配额配置不得被重注册覆盖，实际 {row2!r}"


def test_register_db_failure_leaves_no_memory_residue(env):
    """DB INSERT 失败时内存 dict 不得残留（旧顺序先写内存后 INSERT，
    DB 一崩内存残留到下次重启 _restore_agents 才自愈）。"""
    db = env
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TRIGGER fail_agents_insert BEFORE INSERT ON agents
                    BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END""")
    conn.commit()
    conn.close()
    hub.agents.pop("cd082-fail", None)
    with pytest.raises(Exception):
        asyncio.run(hub.register(_reg("cd082-fail")))
    assert "cd082-fail" not in hub.agents, "DB 写失败后内存镜像不得残留"
    # 摘掉故障注入后注册恢复（内存/DB 一致，重注册路径不回归）
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER fail_agents_insert")
    conn.commit()
    conn.close()
    res = asyncio.run(hub.register(_reg("cd082-fail")))
    assert res["status"] == "registered"
    assert hub.agents["cd082-fail"]["status"] == "online"
    assert _db_role(db, "cd082-fail") == "worker"
