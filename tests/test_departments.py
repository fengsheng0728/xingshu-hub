# -*- coding: utf-8 -*-
"""身份供给 · 部门目录（2026-09-20）：departments CRUD + 员工按部门过滤 + 零迁移归位。

口径（用户认可）：
- department 表**只做目录 + 建员工默认值**，权限判定仍走 employee_accounts.department /
  project_scope（CD-025 读时派生不动）→ 本测试只验「目录/统计/改名同步/删除守卫」，
  不碰披露判定；
- 与员工记录按 name join → 存量员工（自由文本部门）建部门后**自动归位**，零数据迁移；
- 删部门有员工即拒（409）；员工只停用不删。

脚手架（对齐 test_read_audit_deny.py / test_403_policy_matrix.py）：临时库走
db.init_db() 建完整 schema（先 monkeypatch CONFIG.DB_PATH 再 init_db）；直调 handler
协程显式传 current_agent（SYNC_HUB_NO_AUTH=1 绕 Depends）；身份门用
monkeypatch routes_access.NO_AUTH=False 才测得到。不起 TestClient、不绑端口、不 spawn Hub。
"""
import asyncio
import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import models  # noqa: E402
import routes_access  # noqa: E402
from hub_core import hub  # noqa: E402


class _Req:
    """最小 Request 替身：handler 用 await request.json() 与 request.scope["principal"]。"""

    def __init__(self, data=None, principal=None):
        self._data = data if data is not None else {}
        self.scope = {"principal": principal}

    async def json(self):
        return self._data


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "dept.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    db.init_db()
    return db_path


def _run(coro):
    return asyncio.run(coro)


def _q(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _employees(db_path, dept=""):
    return _run(routes_access.api_access_accounts_employees(department=dept, current_agent="mgr"))


def _depts(db_path):
    return _run(routes_access.api_access_departments(current_agent="mgr"))


# ---------------- 1. 建 / 列 / 统计 ----------------


def test_create_then_list_with_stats(env):
    r = _run(routes_access.api_access_departments_create(
        _Req({"name": "客服部", "description": "前台接待", "default_role_template": "staff"}),
        current_agent="mgr"))
    assert r["status"] == "created" and r["department_id"].startswith("dept-")

    got = _depts(env)
    assert got["count"] == 1
    d = got["departments"][0]
    assert d["name"] == "客服部" and d["description"] == "前台接待"
    assert d["default_role_template"] == "staff"
    assert (d["employee_count"], d["key_issued"], d["active_count"]) == (0, 0, 0)
    assert got["templates"] == ["dept_head", "external", "owner", "staff"]
    assert got["unmanaged"] == []


def test_create_validation_and_conflict(env):
    _run(routes_access.api_access_departments_create(_Req({"name": "售后部"}), current_agent="mgr"))
    with pytest.raises(HTTPException) as e1:
        _run(routes_access.api_access_departments_create(_Req({"name": "售后部"}), current_agent="mgr"))
    assert e1.value.status_code == 409
    with pytest.raises(HTTPException) as e2:
        _run(routes_access.api_access_departments_create(_Req({"name": ""}), current_agent="mgr"))
    assert e2.value.status_code == 400
    with pytest.raises(HTTPException) as e3:
        _run(routes_access.api_access_departments_create(
            _Req({"name": "生产部", "default_role_template": "boss"}), current_agent="mgr"))
    assert e3.value.status_code == 400 and "非法" in e3.value.detail


# ---------------- 2. 部门内建员工 + 按部门过滤 ----------------


def test_employee_under_department_and_filter(env):
    d = _run(routes_access.api_access_departments_create(_Req({"name": "客服部"}), current_agent="mgr"))
    created = _run(routes_access.api_access_accounts_create(
        _Req({"name": "小王", "email": "wang@corp.local", "role_template": "staff",
              "department": "客服部"}), current_agent="mgr"))
    assert created["status"] == "created" and created["key"]

    assert _employees(env, "客服部")["count"] == 1
    assert _employees(env, "售后部")["count"] == 0
    assert _employees(env)["count"] == 1  # 不传 = 全部

    got = _depts(env)
    assert got["departments"][0]["employee_count"] == 1
    assert got["departments"][0]["key_issued"] == 1   # 建号已自动签 key
    assert got["unmanaged"] == []


def test_existing_free_text_employees_auto_adopt(env):
    """零迁移兑现：存量员工（自由文本部门）在部门建好后自动归位。"""
    conn = sqlite3.connect(env)
    conn.execute("INSERT INTO employee_accounts (employee_id, name, email, role_template,"
                 " department, status) VALUES ('emp-old','老王','lao@corp.local','staff','售后部','active')")
    conn.commit(); conn.close()
    assert _depts(env)["unmanaged"] == [{"department": "售后部", "employee_count": 1}]
    _run(routes_access.api_access_departments_create(_Req({"name": "售后部"}), current_agent="mgr"))
    got = _depts(env)
    assert got["departments"][0]["employee_count"] == 1   # 自动归位
    assert got["unmanaged"] == []


# ---------------- 3. 改名：同步员工 / 未纳管兜底 ----------------


def test_rename_with_sync_moves_employees(env):
    _run(routes_access.api_access_departments_create(_Req({"name": "客服部"}), current_agent="mgr"))
    _run(routes_access.api_access_accounts_create(
        _Req({"name": "小王", "email": "wang@corp.local", "role_template": "staff",
              "department": "客服部"}), current_agent="mgr"))
    dept_id = _depts(env)["departments"][0]["department_id"]

    r = _run(routes_access.api_access_departments_patch(
        dept_id, _Req({"name": "客户服务部", "sync_employees": True}), current_agent="mgr"))
    assert r["moved_employees"] == 1
    assert _employees(env, "客户服务部")["count"] == 1
    assert _employees(env, "客服部")["count"] == 0
    assert _depts(env)["unmanaged"] == []


def test_rename_without_sync_lands_in_unmanaged(env):
    _run(routes_access.api_access_departments_create(_Req({"name": "客服部"}), current_agent="mgr"))
    _run(routes_access.api_access_accounts_create(
        _Req({"name": "小王", "email": "wang@corp.local", "role_template": "staff",
              "department": "客服部"}), current_agent="mgr"))
    dept_id = _depts(env)["departments"][0]["department_id"]

    r = _run(routes_access.api_access_departments_patch(
        dept_id, _Req({"name": "客户服务部"}), current_agent="mgr"))
    assert r["moved_employees"] == 0
    got = _depts(env)
    assert got["departments"][0]["employee_count"] == 0        # 新名字下没人
    assert got["unmanaged"] == [{"department": "客服部", "employee_count": 1}]  # 不隐形


def test_patch_validation(env):
    _run(routes_access.api_access_departments_create(_Req({"name": "A部"}), current_agent="mgr"))
    _run(routes_access.api_access_departments_create(_Req({"name": "B部"}), current_agent="mgr"))
    ids = {d["name"]: d["department_id"] for d in _depts(env)["departments"]}
    with pytest.raises(HTTPException) as e1:
        _run(routes_access.api_access_departments_patch(
            ids["A部"], _Req({"name": "B部"}), current_agent="mgr"))
    assert e1.value.status_code == 409
    with pytest.raises(HTTPException) as e2:
        _run(routes_access.api_access_departments_patch(ids["A部"], _Req({}), current_agent="mgr"))
    assert e2.value.status_code == 400
    with pytest.raises(HTTPException) as e3:
        _run(routes_access.api_access_departments_patch(
            "dept-nope", _Req({"description": "x"}), current_agent="mgr"))
    assert e3.value.status_code == 404
    r = _run(routes_access.api_access_departments_patch(
        ids["A部"], _Req({"description": "改了", "default_role_template": "dept_head"}),
        current_agent="mgr"))
    assert r["status"] == "updated"
    d = [x for x in _depts(env)["departments"] if x["name"] == "A部"][0]
    assert d["description"] == "改了" and d["default_role_template"] == "dept_head"


# ---------------- 4. 删除守卫 ----------------


def test_delete_blocked_when_employees_exist(env):
    _run(routes_access.api_access_departments_create(_Req({"name": "客服部"}), current_agent="mgr"))
    _run(routes_access.api_access_accounts_create(
        _Req({"name": "小王", "email": "wang@corp.local", "role_template": "staff",
              "department": "客服部"}), current_agent="mgr"))
    dept_id = _depts(env)["departments"][0]["department_id"]
    with pytest.raises(HTTPException) as e:
        _run(routes_access.api_access_departments_delete(dept_id, current_agent="mgr"))
    assert e.value.status_code == 409 and "1 名员工" in e.value.detail
    assert _depts(env)["count"] == 1


def test_delete_empty_department_ok(env):
    _run(routes_access.api_access_departments_create(_Req({"name": "空部门"}), current_agent="mgr"))
    dept_id = _depts(env)["departments"][0]["department_id"]
    r = _run(routes_access.api_access_departments_delete(dept_id, current_agent="mgr"))
    assert r["status"] == "deleted" and r["name"] == "空部门"
    assert _depts(env)["count"] == 0


# ---------------- 5. 角色门（manager/orchestrator） ----------------


def test_manager_gate_on_all_four_endpoints(env, monkeypatch):
    """CD-071：门 = hub_token 或 manager/orchestrator；worker 一律 403。"""
    monkeypatch.setattr(routes_access, "NO_AUTH", False)
    monkeypatch.setitem(hub.agents, "w1", {"role": "worker"})
    worker = _Req({}, principal={"auth_mode": "api_key", "subject_id": "w1"})
    ok = _Req({}, principal={"auth_mode": "hub_token"})

    with pytest.raises(HTTPException):
        _run(routes_access.api_access_departments(request=worker, current_agent="w1"))
    with pytest.raises(HTTPException):
        _run(routes_access.api_access_departments_create(worker, current_agent="w1"))
    with pytest.raises(HTTPException):
        _run(routes_access.api_access_departments_patch("dept-x", worker, current_agent="w1"))
    with pytest.raises(HTTPException):
        _run(routes_access.api_access_departments_delete("dept-x", worker, current_agent="w1"))
    with pytest.raises(HTTPException) as e:
        _run(routes_access.api_access_accounts_employees(request=worker, department="", current_agent="w1"))
    assert e.value.status_code == 403

    # hub_token（部署级全权凭据）：CD-071 起放行 —— 修前这里也是 403（控制台整页打不开）
    assert _run(routes_access.api_access_departments(request=ok, current_agent=""))["status"] == "ok"
    r = _run(routes_access.api_access_departments_create(
        _Req({"name": "Token建部"}, principal={"auth_mode": "hub_token"}), current_agent=""))
    assert r["status"] == "created"

    # DB 里 role=manager 的 api_key 主体同样放行（principal_is_privileged 走 DB 单真相源）
    conn = sqlite3.connect(env)
    conn.execute("INSERT INTO agents (agent_id, agent_name, role, department, capabilities, status)"
                 " VALUES ('mgr-1','主管','manager','客服部','[]','offline')")
    conn.commit(); conn.close()
    mgr = _Req({}, principal={"auth_mode": "api_key", "subject_id": "mgr-1"})
    assert _run(routes_access.api_access_departments(request=mgr, current_agent="mgr-1"))["status"] == "ok"
    # orchestrator 亦可
    conn = sqlite3.connect(env)
    conn.execute("UPDATE agents SET role='orchestrator' WHERE agent_id='mgr-1'")
    conn.commit(); conn.close()
    assert _run(routes_access.api_access_departments(request=mgr, current_agent="mgr-1"))["status"] == "ok"


def test_direct_call_without_request_is_not_gated(env, monkeypatch):
    """进程内直调（request=None）不拦 —— 同 require_ops_privilege 兼容分支口径。"""
    monkeypatch.setattr(routes_access, "NO_AUTH", False)
    monkeypatch.setitem(hub.agents, "w2", {"role": "worker"})
    assert _run(routes_access.api_access_departments(current_agent="w2"))["status"] == "ok"


# ---------------- 6. 审计留痕 ----------------


def test_actions_are_audited(env):
    _run(routes_access.api_access_departments_create(_Req({"name": "客服部"}), current_agent="mgr"))
    dept_id = _depts(env)["departments"][0]["department_id"]
    _run(routes_access.api_access_departments_patch(dept_id, _Req({"description": "d"}), current_agent="mgr"))
    _run(routes_access.api_access_departments_delete(dept_id, current_agent="mgr"))
    types = [r["event_type"] for r in _q(
        env, "SELECT event_type FROM events WHERE event_type LIKE 'department_%' ORDER BY rowid")]
    assert types == ["department_created", "department_updated", "department_deleted"]
