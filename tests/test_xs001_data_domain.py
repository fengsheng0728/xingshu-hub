# -*- coding: utf-8 -*-
"""
XS-001（2026-09-08）data_domain 读时派生 + fail-closed 判定单测

覆盖（无真实 Hub：SyncHub() 直建 + agents dict 注入 + 临时 sqlite 兜底）：
  1. owner 部门命中 scope 域 → 原级别保留
  2. owner 部门越域 → 降 METADATA（fail-closed）
  3. memory 显式 department 键优先于 owner 部门
  4. 空域记忆归公共区（PUBLIC_DOMAIN）
  5. 无 scope / 空 scope → 向后兼容
  6. owner 不在 hub.agents → DB 兜底 employee_accounts
  7. owner 完全未知 → 归公共区
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
import pytest
from models import DisclosureLevel
from disclosure import DisclosureEngine
from hub_core import SyncHub


@pytest.fixture
def hub(monkeypatch):
    """干净 Hub：DB_PATH 指向临时 sqlite（agents/employee_accounts 仅本次查询用到的列）"""
    tmpdir = tempfile.mkdtemp(prefix="xs001-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY, department TEXT DEFAULT '')")
    conn.execute(
        "CREATE TABLE employee_accounts (employee_id TEXT PRIMARY KEY, department TEXT DEFAULT '')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    h = SyncHub()
    h.agents = {}
    h._xs001_tmpdb = tmpdb
    return h


def _mem(owner, **kw):
    mem = {
        "owner_agent_id": owner,
        "disclosure_level": "full",
        "allowed_viewers": "[]",
        "importance": 1.0,
        "tags": "[]",
        "memory_key": "xs001-key",
        "content": "XS-001 测试内容",
        "summary": "xs001",
        "created_at": "2026-09-08T00:00:00",
        "access_count": 0,
    }
    mem.update(kw)
    return mem


def _lv(hub, mem, scope, requester=None):
    engine = DisclosureEngine(hub)
    return engine.disclose_for_principal(
        mem, requester or mem["owner_agent_id"], {}, DisclosureLevel.FULL, scope=scope)


# 1. owner 部门命中 → 原级别保留（memory 无 department 键，域随 owner 派生）
def test_domain_match_keeps_level(hub):
    hub.agents["cs-wang"] = {"agent_id": "cs-wang", "role": "worker", "department": "客服部"}
    lv = _lv(hub, _mem("cs-wang"), {"data_domain": ["客服部"]})
    assert lv == DisclosureLevel.FULL


# 2. owner 部门越域 → METADATA
def test_domain_mismatch_downgrades_metadata(hub):
    hub.agents["cs-wang"] = {"agent_id": "cs-wang", "role": "worker", "department": "客服部"}
    lv = _lv(hub, _mem("cs-wang"), {"data_domain": ["销售部"]})
    assert lv == DisclosureLevel.METADATA


# 3. memory 显式 department 键优先于 owner 部门
def test_explicit_memory_department_wins(hub):
    hub.agents["cs-wang"] = {"agent_id": "cs-wang", "role": "worker", "department": "客服部"}
    lv = _lv(hub, _mem("cs-wang", department="销售部"), {"data_domain": ["客服部"]})
    assert lv == DisclosureLevel.METADATA


# 4. owner 无 department → 空域记忆归公共区
def test_empty_domain_memory_is_public(hub):
    hub.agents["cs-wang"] = {"agent_id": "cs-wang", "role": "worker", "department": ""}
    assert _lv(hub, _mem("cs-wang"), {"data_domain": ["公共区"]}) == DisclosureLevel.FULL
    assert _lv(hub, _mem("cs-wang"), {"data_domain": ["客服部"]}) == DisclosureLevel.METADATA


# 5. 无 scope / 空 scope → 向后兼容
def test_no_scope_backward_compat(hub):
    hub.agents["cs-wang"] = {"agent_id": "cs-wang", "role": "worker", "department": "客服部"}
    assert _lv(hub, _mem("cs-wang"), None) == DisclosureLevel.FULL
    assert _lv(hub, _mem("cs-wang"), {}) == DisclosureLevel.FULL


# 6. owner 不在 hub.agents → DB 兜底 employee_accounts
def test_employee_owner_db_fallback(hub):
    conn = sqlite3.connect(hub._xs001_tmpdb)
    conn.execute(
        "INSERT INTO employee_accounts (employee_id, department) VALUES ('emp-x', '销售部')")
    conn.commit()
    conn.close()
    assert _lv(hub, _mem("emp-x"), {"data_domain": ["销售部"]}) == DisclosureLevel.FULL
    assert _lv(hub, _mem("emp-x"), {"data_domain": ["客服部"]}) == DisclosureLevel.METADATA


# 7. owner 完全未知（agents 无、employee_accounts 无）→ 归公共区
def test_unknown_owner_domain_public(hub):
    assert _lv(hub, _mem("ghost-1"), {"data_domain": ["公共区"]}) == DisclosureLevel.FULL
    assert _lv(hub, _mem("ghost-1"), {"data_domain": ["客服部"]}) == DisclosureLevel.METADATA
