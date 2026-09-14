# -*- coding: utf-8 -*-
"""
XS-002 + XS-012（2026-09-08）单测：披露身份单真相源（心跳全量重载）+ 远程判定纯函数化

覆盖（无真实 Hub：SyncHub() 直建 + monkeypatch CONFIG.DB_PATH → 临时 sqlite）：
  1. test_heartbeat_reloads_role_from_db — dict 命中分支心跳重载 role（过期窗口闭合）
  2. test_heartbeat_reloads_department_and_policy — department / disclosure_policy 跟随 DB
  3. test_downgrade_effect_on_disclosure — 越权方向回归：manager 降 worker 后规则 5 不再给 FULL
  4. test_disclose_for_remote_no_dict_injection — 远程判定不注入/残留 hub.agents
  5. test_calculate_override_params — requester_info / owner_info override 直接生效
  6. 无 override 行为兼容（并入用例 1/3 的 dict 回退断言）
"""
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
import pytest
from models import DisclosureLevel
from disclosure import DisclosureEngine
from hub_core import SyncHub


# agents 表全列：抄 db.py init_db 的 CREATE TABLE，另加 register INSERT 用到的
# api_key / api_key_created_at / api_key_expires_at（heartbeat 装载路径 SELECT * 后
# json.loads(capabilities/managed_agents/disclosure_policy) 不炸为准）
_AGENTS_DDL = """
    CREATE TABLE agents (
        agent_id TEXT PRIMARY KEY,
        agent_name TEXT,
        department TEXT,
        capabilities TEXT,
        role TEXT DEFAULT 'worker',
        managed_agents TEXT,
        disclosure_policy TEXT,
        endpoint TEXT,
        registered_at TEXT,
        last_heartbeat TEXT,
        status TEXT DEFAULT 'offline',
        api_key TEXT,
        api_key_created_at TEXT,
        api_key_expires_at TEXT
    )
"""

# memory_pool 全列（抄 db.py），disclose_for_remote 走 SELECT *
_MEMORY_POOL_DDL = """
    CREATE TABLE memory_pool (
        memory_id TEXT PRIMARY KEY,
        owner_agent_id TEXT NOT NULL,
        memory_key TEXT,
        content TEXT,
        summary TEXT,
        embedding BLOB,
        importance REAL,
        tags TEXT,
        kind TEXT DEFAULT 'fact',
        source_session_id TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        source_type TEXT DEFAULT 'user',
        disclosure_level TEXT DEFAULT 'summary',
        disclosure_scope TEXT DEFAULT 'manager',
        allowed_viewers TEXT,
        created_at TEXT,
        updated_at TEXT,
        access_count INTEGER DEFAULT 0,
        last_accessed TEXT
    )
"""


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """干净 Hub：DB_PATH 指向临时 sqlite（agents 全列 + memory_pool 全列）"""
    tmpdb = str(tmp_path / "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute(_AGENTS_DDL)
    conn.execute(_MEMORY_POOL_DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    h = SyncHub()
    h.agents = {}
    # 披露策略固定为默认值，避免受真实 config.yaml 影响
    h._disclosure_policy = {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
    }
    h._xs002_db = tmpdb
    return h


def _insert_agent(hub, agent_id, role="worker", department="", managed=None, policy=None):
    conn = sqlite3.connect(hub._xs002_db)
    conn.execute(
        """INSERT OR REPLACE INTO agents
        (agent_id, agent_name, department, capabilities, role, managed_agents,
         disclosure_policy, endpoint, registered_at, last_heartbeat, status,
         api_key, api_key_created_at, api_key_expires_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (agent_id, agent_id, department, "[]", role, json.dumps(managed or []),
         json.dumps(policy or {}), "", "2026-09-08T00:00:00", "2026-09-08T00:00:00",
         "offline", "", None, None),
    )
    conn.commit()
    conn.close()


def _update_agent(hub, agent_id, **fields):
    conn = sqlite3.connect(hub._xs002_db)
    for col, val in fields.items():
        conn.execute(f"UPDATE agents SET {col} = ? WHERE agent_id = ?", (val, agent_id))
    conn.commit()
    conn.close()


def _insert_memory(hub, memory_id, owner, disclosure_level="summary", content="XS-002 测试内容"):
    conn = sqlite3.connect(hub._xs002_db)
    conn.execute(
        """INSERT OR REPLACE INTO memory_pool
        (memory_id, owner_agent_id, memory_key, content, summary, importance, tags,
         disclosure_level, allowed_viewers, created_at, updated_at, access_count)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (memory_id, owner, "xs002-key", content, "xs002 摘要", 1.0, "[]",
         disclosure_level, "[]", "2026-09-08T00:00:00", "2026-09-08T00:00:00", 0),
    )
    conn.commit()
    conn.close()


def _mem(owner, **kw):
    mem = {
        "owner_agent_id": owner,
        "disclosure_level": "full",
        "allowed_viewers": "[]",
        "importance": 1.0,
        "tags": "[]",
        "memory_key": "xs002-key",
        "content": "XS-002 测试内容",
        "summary": "xs002 摘要",
        "created_at": "2026-09-08T00:00:00",
        "access_count": 0,
    }
    mem.update(kw)
    return mem


# 1. 心跳 dict 命中分支全量重载 role：直改 DB 后下一个心跳即生效（过期窗口闭合）
def test_heartbeat_reloads_role_from_db(hub):
    _insert_agent(hub, "ag-1", role="worker")
    # 首次心跳：dict 未命中 → 从 DB 重建
    asyncio.run(hub.heartbeat("ag-1"))
    assert hub.agents["ag-1"]["role"] == "worker"
    # 直改 DB（模拟绕过 Hub 的管理操作），dict 此刻已过期
    _update_agent(hub, "ag-1", role="manager")
    assert hub.agents["ag-1"]["role"] == "worker"
    # 再次心跳：dict 命中分支从 DB 重载身份字段
    asyncio.run(hub.heartbeat("ag-1"))
    assert hub.agents["ag-1"]["role"] == "manager"
    assert hub.agents["ag-1"]["status"] == "online"


# 2. department 与 disclosure_policy（JSON 文本）同样跟随 DB
def test_heartbeat_reloads_department_and_policy(hub):
    _insert_agent(hub, "ag-1", role="worker", department="销售部", policy={})
    asyncio.run(hub.heartbeat("ag-1"))
    assert hub.agents["ag-1"]["department"] == "销售部"
    assert hub.agents["ag-1"]["disclosure_policy"] == {}
    _update_agent(hub, "ag-1", department="客服部",
                  disclosure_policy='{"max_disclosure": "summary"}')
    asyncio.run(hub.heartbeat("ag-1"))
    assert hub.agents["ag-1"]["department"] == "客服部"
    assert hub.agents["ag-1"]["disclosure_policy"] == {"max_disclosure": "summary"}


# 3. 越权方向回归：manager 降 worker 后，规则 5 不再对原下属记忆给 FULL
def test_downgrade_effect_on_disclosure(hub):
    # dict 中 ag-1 先按 manager + managed=[ag-2] 建好（无 override，走 dict 回退——行为兼容）
    hub.agents["ag-1"] = {
        "agent_id": "ag-1", "role": "manager", "managed_agents": ["ag-2"],
        "department": "", "disclosure_policy": {},
    }
    hub.agents["ag-2"] = {"agent_id": "ag-2", "role": "worker", "department": ""}
    # DB 中已是降职后的状态：worker + managed=[]
    _insert_agent(hub, "ag-1", role="worker", managed=[])
    engine = DisclosureEngine(hub)
    mem = _mem("ag-2")
    lv_before = engine._calculate_disclosure_level(
        memory=mem, requester="ag-1", task={}, required_level=DisclosureLevel.FULL)
    assert lv_before == DisclosureLevel.FULL  # 降职生效前规则 5 仍给 FULL（复现越权窗口）
    # 心跳重载身份 → 规则 5 不再触发
    asyncio.run(hub.heartbeat("ag-1"))
    assert hub.agents["ag-1"]["role"] == "worker"
    assert hub.agents["ag-1"]["managed_agents"] == []
    lv_after = engine._calculate_disclosure_level(
        memory=mem, requester="ag-1", task={}, required_level=DisclosureLevel.FULL)
    assert lv_after != DisclosureLevel.FULL


# 4. disclose_for_remote 不再注入 hub.agents（XS-012）
def test_disclose_for_remote_no_dict_injection(hub):
    _insert_memory(hub, "mem-1", owner="ag-local", disclosure_level="summary")
    # virtual manager 管辖 ag-local → 规则 5 命中 → SUMMARY（结果非空）
    virtual_agent = {
        "agent_id": "remote-x", "role": "manager",
        "department": "", "managed_agents": ["ag-local"],
    }
    engine = DisclosureEngine(hub)
    result = asyncio.run(
        engine.disclose_for_remote(virtual_agent, "ag-local", "", "summary"))
    assert result["disclosed_count"] == 1
    assert result["memories"][0]["memory_id"] == "mem-1"
    assert result["memories"][0]["disclosure_level"] == "summary"
    # 关键断言：虚拟身份未注入/残留 hub.agents
    assert "remote-x" not in hub.agents


# 5. requester_info / owner_info override 直接生效，不经 hub.agents
def test_calculate_override_params(hub):
    engine = DisclosureEngine(hub)
    # requester_info 侧：ghost 不在 dict，override 角色 manager 管辖 owner → 规则 5 给 FULL
    lv = engine._calculate_disclosure_level(
        memory=_mem("ag-2"), requester="ghost", task={},
        required_level=DisclosureLevel.FULL,
        requester_info={"role": "manager", "managed_agents": ["ag-2"]})
    assert lv == DisclosureLevel.FULL
    assert "ghost" not in hub.agents
    # owner_info 侧：requester/owner 均不在 dict，override 双 worker 同部门
    # + department_peer_visibility → 规则 7 给 SUMMARY
    hub._disclosure_policy["department_peer_visibility"] = True
    lv2 = engine._calculate_disclosure_level(
        memory=_mem("ghost-owner", disclosure_level="summary"),
        requester="ghost-req", task={}, required_level=DisclosureLevel.SUMMARY,
        requester_info={"role": "worker", "department": "销售部"},
        owner_info={"role": "worker", "department": "销售部"})
    assert lv2 == DisclosureLevel.SUMMARY
    assert "ghost-req" not in hub.agents
    assert "ghost-owner" not in hub.agents
