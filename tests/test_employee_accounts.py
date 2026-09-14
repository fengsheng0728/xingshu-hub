# -*- coding: utf-8 -*-
"""1e 员工账号验收测试（2026-08-30）

覆盖：
1. 模板→scope 映射（四模板 + 未知模板 fail-closed）
2. 员工 key 认证 → user principal + scope
3. 吊销 / 禁用 / 租约过期 → 拒绝
4. 存量 agent key 认证不受影响（service principal）
5. 披露链 scope 生效：level_cap min 封顶 + data_domain 越域降 METADATA
6. CSV 解析（正常/坏行拒绝）
"""
import hashlib
import json
import os
import secrets
import sqlite3
import tempfile

import pytest

from models import Config, DisclosureLevel
from auth_provider import LocalProvider, Principal
from disclosure import DisclosureEngine


def _make_db(path: str):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE agents (
            agent_id TEXT PRIMARY KEY, api_key TEXT, api_key_prev TEXT,
            api_key_created_at TEXT, api_key_expires_at TEXT,
            api_key_prev_expires_at TEXT, api_key_ip_whitelist TEXT, last_used_at TEXT)"""
    )
    conn.execute(
        """CREATE TABLE employee_accounts (
            employee_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            role_template TEXT NOT NULL DEFAULT 'staff',
            department TEXT DEFAULT '',
            project_scope TEXT DEFAULT '',
            key_hash TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            lease_expires_at TEXT DEFAULT '')"""
    )
    conn.commit()
    conn.close()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture()
def provider():
    tmp = tempfile.mktemp(suffix=".db")
    _make_db(tmp)
    cfg = Config()
    cfg.DB_PATH = tmp
    cfg.HUB_TOKEN = ""
    p = LocalProvider(cfg)
    # 存量 agent（有 role 的普通 Agent，验证不受影响）
    conn = sqlite3.connect(tmp)
    conn.execute("INSERT INTO agents (agent_id, api_key) VALUES ('ag1', ?)", ("agent-key-1",))
    conn.commit()
    conn.close()
    yield p
    try:
        os.remove(tmp)
    except OSError:
        pass


def _add_employee(provider, emp_id="emp-test1", name="张三", email="zhangsan@corp.cn",
                  tpl="staff", dept="销售部", project="", status="active", lease="", key_plain=None):
    conn = sqlite3.connect(provider._config.DB_PATH)
    conn.execute(
        "INSERT INTO employee_accounts (employee_id, name, email, role_template, department,"
        " project_scope, key_hash, status, lease_expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (emp_id, name, email, tpl, dept, project, _hash(key_plain) if key_plain else "",
         status, lease),
    )
    conn.commit()
    conn.close()


# ═══════════ 1. 模板→scope 映射 ═══════════

def test_scope_owner_full_global(provider):
    s = provider._employee_scope({"role_template": "owner"})
    assert s["level_cap"] == "full" and s["data_domain"] == []


def test_scope_dept_head_domain(provider):
    s = provider._employee_scope({"role_template": "dept_head", "department": "销售部"})
    assert s["level_cap"] == "full" and s["data_domain"] == ["销售部"]


def test_scope_staff_domain(provider):
    s = provider._employee_scope({"role_template": "staff", "department": "销售部"})
    assert s["level_cap"] == "summary" and s["data_domain"] == ["销售部", "公共区"]


def test_scope_external_project(provider):
    s = provider._employee_scope({"role_template": "external", "project_scope": "项目A"})
    assert s["level_cap"] == "metadata" and s["data_domain"] == ["项目A"]


def test_scope_unknown_template_fail_closed(provider):
    s = provider._employee_scope({"role_template": "hacker"})
    assert s["level_cap"] == "metadata" and s["data_domain"] == []


# ═══════════ 2. 员工 key 认证 ═══════════

def test_employee_key_authenticate_user_principal(provider):
    key = "emp_" + secrets.token_hex(24)
    _add_employee(provider, key_plain=key)
    p = provider.authenticate(key)
    assert p is not None
    assert p.subject_type == "user"
    assert p.subject_id == "emp-test1"
    assert p.auth_mode == "api_key"
    assert p.scope["level_cap"] == "summary"  # staff 模板


def test_employee_revoked_key_denied(provider):
    key = "emp_" + secrets.token_hex(24)
    _add_employee(provider, key_plain=key)
    provider.authenticate(key)  # 先验证可登
    conn = sqlite3.connect(provider._config.DB_PATH)
    conn.execute("UPDATE employee_accounts SET key_hash = '' WHERE employee_id = 'emp-test1'")
    conn.commit()
    conn.close()
    assert provider.authenticate(key) is None


def test_employee_disabled_denied(provider):
    key = "emp_" + secrets.token_hex(24)
    _add_employee(provider, status="disabled", key_plain=key)
    assert provider.authenticate(key) is None


def test_employee_lease_expired_denied(provider):
    key = "emp_" + secrets.token_hex(24)
    _add_employee(provider, tpl="external", lease="2020-01-01T00:00:00", key_plain=key)
    assert provider.authenticate(key) is None


def test_employee_lease_active_allowed(provider):
    key = "emp_" + secrets.token_hex(24)
    _add_employee(provider, tpl="external", lease="2099-01-01T00:00:00", key_plain=key)
    p = provider.authenticate(key)
    assert p is not None and p.scope["level_cap"] == "metadata"


def test_agent_key_authenticate_unaffected(provider):
    """存量 agent key 仍走 service principal（员工路径不干扰）"""
    p = provider.authenticate("agent-key-1")
    assert p is not None
    assert p.subject_type == "service"
    assert p.subject_id == "ag1"


# ═══════════ 3. 披露链 scope 生效 ═══════════

class _FakeHub:
    _disclosure_policy = {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
    }
    agents = {
        "ag-owner": {"agent_id": "ag-owner", "role": "worker", "department": "销售部",
                     "managed_agents": [], "disclosure_policy": {}},
    }

    def _db(self):
        """T2-2 对齐: 披露引擎复用 hub._db 连接工厂(busy_timeout 5000)。"""
        from models import CONFIG

        conn = sqlite3.connect(CONFIG.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn


def _mem(owner="ag-owner", level="full", tags=None):
    return {
        "owner_agent_id": owner,
        "disclosure_level": level,
        "allowed_viewers": "[]",
        "importance": 1.0,
        "tags": json.dumps(tags or []),
        "memory_key": "k1",
        "content": "机密内容",
        "summary": "摘要",
        "created_at": "2026-08-01T00:00:00",
        "access_count": 0,
        "department": "销售部",
    }


def _seed_employee_db(monkeypatch):
    """把员工记录种进 CONFIG.DB_PATH（disclosure 引擎用模块级 CONFIG）"""
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute(
        """CREATE TABLE employee_accounts (
            employee_id TEXT PRIMARY KEY, name TEXT, email TEXT,
            role_template TEXT, department TEXT, project_scope TEXT,
            key_hash TEXT, status TEXT, lease_expires_at TEXT)"""
    )
    conn.execute(
        "INSERT INTO employee_accounts (employee_id, name, role_template, department, status)"
        " VALUES ('emp-test1', '张三', 'staff', '销售部', 'active')"
    )
    conn.commit()
    conn.close()
    from models import CONFIG

    monkeypatch.setattr(CONFIG, "DB_PATH", tmp)
    return tmp


def test_disclosure_scope_cap_summary(provider, monkeypatch):
    """staff scope(level_cap=summary) + 已知员工基础 SUMMARY → 封顶 SUMMARY"""
    _seed_employee_db(monkeypatch)
    engine = DisclosureEngine(_FakeHub())
    lv = engine.disclose_for_principal(
        memory=_mem(), requester="emp-test1", task={},
        required_level=DisclosureLevel.FULL,
        scope={"endpoints": [], "data_domain": [], "level_cap": "summary"},
    )
    assert lv == DisclosureLevel.SUMMARY


def test_disclosure_scope_domain_downgrade(provider, monkeypatch):
    """已知员工 + scope 越域(data_domain=[技术部]) 查销售部记忆 → METADATA"""
    _seed_employee_db(monkeypatch)
    engine = DisclosureEngine(_FakeHub())
    lv = engine.disclose_for_principal(
        memory=_mem(), requester="emp-test1", task={},
        required_level=DisclosureLevel.FULL,
        scope={"endpoints": [], "data_domain": ["技术部"], "level_cap": "full"},
    )
    assert lv == DisclosureLevel.METADATA


def test_disclosure_unknown_agent_fail_closed(provider, monkeypatch):
    """完全未知主体(非 agent 非员工) → METADATA 封顶（2b）"""
    _seed_employee_db(monkeypatch)
    engine = DisclosureEngine(_FakeHub())
    lv = engine.disclose_for_principal(
        memory=_mem(), requester="ghost-xyz", task={},
        required_level=DisclosureLevel.FULL,
        scope={"endpoints": [], "data_domain": [], "level_cap": ""},
    )
    assert lv == DisclosureLevel.METADATA


def test_disclosure_no_scope_backward_compat(provider):
    """无 scope → 与原判定一致（向后兼容）"""
    engine = DisclosureEngine(_FakeHub())
    lv = engine.disclose_for_principal(
        memory=_mem(), requester="ag-owner", task={},
        required_level=DisclosureLevel.FULL, scope=None,
    )
    assert lv == DisclosureLevel.FULL  # 自己查自己


# ═══════════ 4. CSV 解析 ═══════════

def test_csv_parse_ok_and_bad_lines(provider):
    from routes_access import _parse_csv_accounts

    csv_text = (
        "李四,lisi@corp.cn,staff\n"
        "王五,wangwu@corp.cn,dept_head\n"
        "坏行1,缺模板\n"
        "赵六,zhaoliu@corp.cn,hacker\n"
        "孙七,no-at-sign,staff\n"
    )
    ok, bad = _parse_csv_accounts(csv_text)
    assert len(ok) == 2
    assert {r["email"] for r in ok} == {"lisi@corp.cn", "wangwu@corp.cn"}
    assert len(bad) == 3
    reasons = [b["reason"] for b in bad]
    assert any("列数" in r for r in reasons)
    assert any("模板非法" in r for r in reasons)
    assert any("邮箱" in r for r in reasons)
