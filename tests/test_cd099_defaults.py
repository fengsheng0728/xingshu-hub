# -*- coding: utf-8 -*-
"""tests/test_cd099_defaults.py — CD-099：H-4 默认值收口（「配置正确才安全」→ 默认安全）

覆盖：
- models.Config 代码默认 AUTH_REGISTRATION == "guarded"
- guarded 默认生效：未预签发 agent 注册 → 403 且不建号
- register 首注自报 orchestrator → 落库/内存均为 worker（提权维度不采信）
- main.check_startup_token_policy：空/占位 token + 非回环 → fatal；回环 → warn；真实 token → ok
- 占位判定与 config.example.yaml 的 hub_token 占位一致
"""
import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub_core import hub
from models import CONFIG, AgentRegistration, Config


def _make_full_db(db_path: str):
    """register 路径依赖的最小表集（同 test_register_role_preserve 口径）。"""
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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cd099.db")
    _make_full_db(db_path)
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    for aid in ("cd099-self-orch", "cd099-guarded"):
        hub.agents.pop(aid, None)
    yield db_path
    for aid in ("cd099-self-orch", "cd099-guarded"):
        hub.agents.pop(aid, None)


def test_default_registration_mode_is_guarded():
    """代码默认（dataclass 字段默认值）必须是 guarded —— 不读 config/env 的裸默认。"""
    assert Config().AUTH_REGISTRATION == "guarded"


def test_guarded_default_blocks_unprovisioned_register(env, monkeypatch):
    """guarded 生效：未预签发 agent_id → error/403 且不建号。"""
    monkeypatch.setattr(CONFIG, "AUTH_REGISTRATION", "guarded")
    reg = AgentRegistration(agent_id="cd099-guarded", agent_name="x", role="worker")
    res = asyncio.run(hub.register(reg))
    assert res.get("status") == "error" and res.get("code") == 403, f"guarded 应拒绝, 实际 {res}"
    conn = sqlite3.connect(env)
    row = conn.execute(
        "SELECT 1 FROM agents WHERE agent_id='cd099-guarded'").fetchone()
    conn.close()
    assert row is None, "guarded 拒绝时不得建号"


def test_first_register_self_reported_orchestrator_becomes_worker(env, monkeypatch):
    """首注自报 orchestrator → 落库为 worker（自报 role/managed_agents/disclosure_policy 不采信）。"""
    monkeypatch.setattr(CONFIG, "AUTH_REGISTRATION", "open")  # 走到建号路径再验角色收口
    reg = AgentRegistration(agent_id="cd099-self-orch", agent_name="x",
                            role="orchestrator", managed_agents=["a1"],
                            disclosure_policy={"level": "full"})
    res = asyncio.run(hub.register(reg))
    assert res.get("status") == "registered", f"open 模式首注应成功, 实际 {res}"
    assert hub.agents["cd099-self-orch"]["role"] == "worker"
    assert hub.agents["cd099-self-orch"]["managed_agents"] == []
    conn = sqlite3.connect(env)
    row = conn.execute(
        "SELECT role, managed_agents, disclosure_policy FROM agents "
        "WHERE agent_id='cd099-self-orch'").fetchone()
    conn.close()
    assert row == ("worker", "[]", "{}"), f"落库提权维度必须收口, 实际 {row!r}"


def test_startup_token_policy_matrix():
    """CD-099 启动硬门：占位/空 token + 0.0.0.0 → fatal；回环 → warn；真实 token → ok。"""
    import main
    assert main.check_startup_token_policy("0.0.0.0", "") == "fatal"
    assert main.check_startup_token_policy("0.0.0.0", "CHANGE_ME_强随机值") == "fatal"
    assert main.check_startup_token_policy("0.0.0.0", "change_me_lower") == "fatal"
    assert main.check_startup_token_policy("192.168.1.10", "") == "fatal"
    assert main.check_startup_token_policy("127.0.0.1", "") == "warn"
    assert main.check_startup_token_policy("localhost", "CHANGE_ME_x") == "warn"
    assert main.check_startup_token_policy("::1", "") == "warn"
    assert main.check_startup_token_policy("0.0.0.0", "real-random-token-32bytes") == "ok"


def test_placeholder_matches_config_example():
    """占位判定与 config.example.yaml 的 hub_token 占位一致（漂移即红）。"""
    import main
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config.example.yaml"), "r", encoding="utf-8") as f:
        example = yaml.safe_load(f)
    token = example["auth"]["hub_token"]
    assert main._token_placeholder(token), \
        f"config.example.yaml 占位 {token!r} 必须被启动硬门识别为占位符"
    # 示例模板的 registration 默认也必须收为 guarded
    assert example["auth"]["registration"] == "guarded"
