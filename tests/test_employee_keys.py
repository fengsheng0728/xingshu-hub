# -*- coding: utf-8 -*-
"""员工凭据账本（CD-072，2026-09-20）：多把并存 / 单把吊销 / 过期 / 调用画像 / legacy 回落 / 迁移回填。

口径（用户 2026-09-20 选「完整 key 账本」）：
- 每把一个 key_id，同员工可多把；补签不再覆盖旧凭据；
- 每把有 expires_at（空=永久）、status（active/revoked）、last_used_at + call_count；
- `employee_accounts.key_hash` 保留为「最近一把有效凭据」的镜像（双写）→ 老路径/回滚可用；
- 认证先查账本，未命中回落 legacy 列（回填未跑的库/测试直插行）；
- 归属校验：拿别人的 emp_id 吊销 → 404。

脚手架同 test_departments.py：临时库 db.init_db() + 直调 handler（显式传 current_agent），
`employee_keys._store` 单例按测试重置（否则跨测试串库）。
"""
import asyncio
import importlib.util
import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import employee_keys  # noqa: E402
import models  # noqa: E402
import routes_access  # noqa: E402
from auth_provider import LocalProvider  # noqa: E402


class _Req:
    def __init__(self, data=None, principal=None):
        self._data = data if data is not None else {}
        self.scope = {"principal": principal}

    async def json(self):
        return self._data


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "empkeys.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    monkeypatch.setattr(employee_keys, "_store", None)   # 单例重置：防跨测试串库
    db.init_db()
    return db_path


def _run(coro):
    return asyncio.run(coro)


def _q(db_path, sql, params=()):
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _new_employee(dept="客服部", name="小王", email="w@corp.local", **extra):
    body = {"name": name, "email": email, "role_template": "staff", "department": dept, **extra}
    return _run(routes_access.api_access_accounts_create(_Req(body), current_agent="mgr"))


def _auth(db_path, token):
    """走真实认证入口（含账本查询 + 调用画像）"""
    return LocalProvider(models.CONFIG).authenticate(token)


# ---------------- 1. 建号即入账本 + legacy 镜像 ----------------


def test_create_employee_issues_ledger_key(env):
    r = _new_employee()
    assert r["status"] == "created" and r["key"].startswith("emp_") and r["key_id"].startswith("key-")
    rows = _q(env, "SELECT * FROM employee_keys WHERE employee_id = ?", (r["employee_id"],))
    assert len(rows) == 1
    k = rows[0]
    assert (k["status"], k["call_count"]) == ("active", 0)
    assert k["key_hash"] == employee_keys.key_hash(r["key"])
    # legacy 镜像 = 最新一把的 hash（老代码/回滚路径可用）
    emp = _q(env, "SELECT key_hash FROM employee_accounts WHERE employee_id = ?", (r["employee_id"],))[0]
    assert emp["key_hash"] == k["key_hash"]

    pr = _auth(env, r["key"])
    assert pr is not None and pr.subject_id == r["employee_id"]
    assert pr.scope["level_cap"] == "summary"
    assert "客服部" in pr.scope["data_domain"]


# ---------------- 2. 多把并存 + 单把吊销 ----------------


def test_multiple_keys_and_single_revoke(env):
    r1 = _new_employee()
    emp = r1["employee_id"]
    r2 = _run(routes_access.api_access_accounts_key(emp, _Req({"label": "第二把"}), current_agent="mgr"))
    assert r2["key_id"] != r1["key_id"]
    assert len(_q(env, "SELECT 1 FROM employee_keys WHERE employee_id = ?", (emp,))) == 2
    # 两把都能认证（补签不再互相覆盖）
    assert _auth(env, r1["key"]) is not None
    assert _auth(env, r2["key"]) is not None
    # 列表接口（不含明文）
    lst = _run(routes_access.api_access_account_keys(emp, current_agent="mgr"))
    assert lst["count"] == 2
    assert all("key_hash" not in k and "key" not in k for k in lst["keys"])

    # 单把吊销：只死一把
    rv = _run(routes_access.api_access_account_key_revoke(emp, r1["key_id"], current_agent="mgr"))
    assert rv["status"] == "revoked"
    assert _auth(env, r1["key"]) is None
    assert _auth(env, r2["key"]) is not None
    # 镜像切到剩下那把
    assert _q(env, "SELECT key_hash FROM employee_accounts WHERE employee_id = ?", (emp,))[0]["key_hash"] \
        == employee_keys.key_hash(r2["key"])
    # 二次吊销同一把 → 404
    with pytest.raises(HTTPException) as e:
        _run(routes_access.api_access_account_key_revoke(emp, r1["key_id"], current_agent="mgr"))
    assert e.value.status_code == 404


def test_revoke_one_rejects_cross_employee(env):
    a = _new_employee(name="甲", email="a@corp.local")
    b = _new_employee(name="乙", email="b@corp.local")
    with pytest.raises(HTTPException) as e:
        _run(routes_access.api_access_account_key_revoke(b["employee_id"], a["key_id"], current_agent="mgr"))
    assert e.value.status_code == 404           # 防跨人吊销
    assert _auth(env, a["key"]) is not None      # 甲那把没被误杀


def test_revoke_all_employee_keys(env):
    r1 = _new_employee()
    emp = r1["employee_id"]
    r2 = _run(routes_access.api_access_accounts_key(emp, _Req({}), current_agent="mgr"))
    out = _run(routes_access.api_access_accounts_revoke(emp, current_agent="mgr"))
    assert out["revoked"] == 2 and out["status"] == "revoked"
    assert _auth(env, r1["key"]) is None and _auth(env, r2["key"]) is None
    assert _q(env, "SELECT key_hash FROM employee_accounts WHERE employee_id = ?", (emp,))[0]["key_hash"] == ""


# ---------------- 3. 过期 / 调用画像 ----------------


def test_expired_key_denied(env):
    r = _new_employee()
    emp = r["employee_id"]
    r2 = _run(routes_access.api_access_accounts_key(
        emp, _Req({"expires_at": "2020-01-01T00:00:00"}), current_agent="mgr"))
    assert _auth(env, r2["key"]) is None          # 过期 → 认证拒
    assert _auth(env, r["key"]) is not None       # 另一把不受影响
    # 过期那把不进镜像（镜像仍指向有效的 r1）
    assert _q(env, "SELECT key_hash FROM employee_accounts WHERE employee_id = ?", (emp,))[0]["key_hash"] \
        == employee_keys.key_hash(r["key"])
    # 重扫时过期把数不计入 active 画像
    assert employee_keys.EmployeeKeyStore(env).count_active(emp) == 1


def test_touch_records_call_profile(env):
    r = _new_employee()
    assert _auth(env, r["key"]) is not None
    row = _q(env, "SELECT last_used_at, call_count FROM employee_keys WHERE key_id = ?", (r["key_id"],))[0]
    assert row["call_count"] == 1 and row["last_used_at"]
    _auth(env, r["key"])
    assert _q(env, "SELECT call_count FROM employee_keys WHERE key_id = ?", (r["key_id"],))[0]["call_count"] == 2


# ---------------- 4. legacy 回落 + 迁移回填 ----------------


def test_legacy_key_hash_still_authenticates(env):
    """账本未命中（未回填的库/直插行）→ 回落 employee_accounts.key_hash。"""
    plain = "emp_legacy_plain_0001"
    conn = sqlite3.connect(env)
    conn.execute("INSERT INTO employee_accounts (employee_id, name, email, role_template,"
                 " department, key_hash, status) VALUES ('emp-legacy','老员工','l@corp.local',"
                 "'staff','售后部',?,'active')", (employee_keys.key_hash(plain),))
    conn.commit(); conn.close()
    pr = _auth(env, plain)
    assert pr is not None and pr.subject_id == "emp-legacy"


def _load_migration():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "migrations", "alembic", "versions", "0010_employee_keys.py")
    spec = importlib.util.spec_from_file_location("mig_0010", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_backfill_creates_ledger_rows(env):
    """存量 key_hash → 账本一行（幂等），老 key 原样继续可用。"""
    plain = "emp_old_plain_0002"
    h = employee_keys.key_hash(plain)
    conn = sqlite3.connect(env)
    conn.execute("INSERT INTO employee_accounts (employee_id, name, email, role_template,"
                 " department, key_hash, status, created_at) VALUES ('emp-old','老员工',"
                 "'o@corp.local','staff','客服部',?,'active','2026-01-01 00:00:00')", (h,))
    conn.commit()
    mig = _load_migration()
    cur = conn.cursor()
    assert mig._backfill(cur) == 1
    conn.commit()
    assert mig._backfill(cur) == 0            # 幂等
    conn.close()
    rows = _q(env, "SELECT key_id, label, status, created_at, call_count FROM employee_keys"
                   " WHERE employee_id = 'emp-old'")
    assert len(rows) == 1 and rows[0]["label"] == "迁移前签发（legacy）"
    assert rows[0]["status"] == "active" and rows[0]["call_count"] == 0
    assert rows[0]["created_at"] == "2026-01-01 00:00:00"   # 沿用员工创建时间
    assert _auth(env, plain) is not None                    # 老 key 仍可用


# ---------------- 5. 员工列表画像 ----------------


def test_employee_list_reports_key_count(env):
    r = _new_employee()
    _run(routes_access.api_access_accounts_key(r["employee_id"], _Req({}), current_agent="mgr"))
    lst = _run(routes_access.api_access_accounts_employees(current_agent="mgr"))
    row = lst["accounts"][0]
    assert row["key_count"] == 2 and row["has_key"] == 1
    _run(routes_access.api_access_accounts_revoke(r["employee_id"], current_agent="mgr"))
    row = _run(routes_access.api_access_accounts_employees(current_agent="mgr"))["accounts"][0]
    assert row["key_count"] == 0                      # 吊销后画像归零


# ---------------- 6. 角色门 ----------------


def test_key_endpoints_require_manager(env, monkeypatch):
    r = _new_employee()                      # 先建号（NO_AUTH 下不拦门）
    from hub_core import hub
    monkeypatch.setattr(routes_access, "NO_AUTH", False)
    monkeypatch.setitem(hub.agents, "w1", {"role": "worker"})
    worker = _Req({}, principal={"auth_mode": "api_key", "subject_id": "w1"})
    ok = _Req({}, principal={"auth_mode": "hub_token"})
    with pytest.raises(HTTPException) as e1:
        _run(routes_access.api_access_account_keys(r["employee_id"], request=worker, current_agent="w1"))
    with pytest.raises(HTTPException) as e2:
        _run(routes_access.api_access_account_key_revoke(r["employee_id"], r["key_id"],
                                                        request=worker, current_agent="w1"))
    assert (e1.value.status_code, e2.value.status_code) == (403, 403)
    assert _run(routes_access.api_access_account_keys(r["employee_id"], request=ok,
                                                     current_agent=""))["status"] == "ok"
