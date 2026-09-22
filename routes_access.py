"""星枢 Sync Hub — 访问与权限 API（U3 新增）

账号视图（agents 身份表）+ 例外与租约视图：
- 例外 = 已批准的披露申请（disclosure_requests status='approved'）
- 租约 = 带 expires_at 的 scoped key（agent_keys），7 天内到期倒计时
S1-SMB 账号体系（阶段 1e）落地前，账号 = 已注册 Agent 身份。
"""
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from hub_core import hub
from routes_common import NO_AUTH, get_current_agent, require_role

# CD-072（2026-09-20）员工凭据账本：签发/吊销走 employee_keys；employee_accounts.key_hash
# 保留为「最近一把」镜像（双写）→ 老代码与回滚路径仍可用。
from employee_keys import get_store as _get_emp_keys, key_hash as _emp_key_hash


def _emp_keys():
    return _get_emp_keys()


def _mirror_key_hash(conn, employee_id: str) -> None:
    """把 legacy 列同步成该员工最新一把有效凭据的 hash（无 → 空串）。"""
    conn.execute("UPDATE employee_accounts SET key_hash = ? WHERE employee_id = ?",
                 (_emp_keys().latest_active_hash(employee_id), employee_id))


router = APIRouter()


def _require_manager(request, current_agent: str):
    """CD-071/CD-074：委托 canonical `routes_common.require_role`（hub_token 放行）。

    保留本函数只为兼容既有调用点（都传 request）；`request is None`（进程内直调）不拦，
    与 `require_ops_privilege` 的兼容分支同口径。历史：原实现只看
    `hub.agents[current_agent].role` → 控制台用 hub_token 登录时 /api/v1/access/* 整组 403。
    """
    if request is None:
        return
    require_role(current_agent, detail="仅主管/店长可访问", agents=hub.agents,
                 no_auth=NO_AUTH, principal=getattr(request, "scope", {}).get("principal"))



@router.get("/api/v1/access/accounts")
async def api_access_accounts(request: Request = None,
                               current_agent: str = Depends(get_current_agent)):
    """账号表：已注册 Agent 身份（S1-SMB 落地前的现实映射）"""
    _require_manager(request, current_agent)
    conn = hub._db()
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            "SELECT agent_id, agent_name, department, role, managed_agents,"
            " registered_at, last_heartbeat, status FROM agents ORDER BY last_heartbeat DESC"
        ).fetchall()
    finally:
        conn.close()
    import json as _json
    accounts = []
    for r in rows:
        accounts.append({
            "agent_id": r["agent_id"],
            "agent_name": r["agent_name"] or "",
            "department": r["department"] or "",
            "role": r["role"],
            "managed_count": len(_json.loads(r["managed_agents"] or "[]")),
            "registered_at": r["registered_at"] or "",
            "last_heartbeat": r["last_heartbeat"] or "",
            "status": r["status"],
        })
    return {"status": "ok", "accounts": accounts, "count": len(accounts)}


@router.get("/api/v1/access/exceptions")
async def api_access_exceptions(request: Request = None,
                                 current_agent: str = Depends(get_current_agent)):
    """例外与租约：
    - exceptions: 已批准的披露申请（例外必须被阳光晒到；收回语义随 1e 落地）
    - expiring_keys: 7 天内到期的 scoped key（到期倒计时）
    """
    _require_manager(request, current_agent)
    conn = hub._db()
    conn.row_factory = __import__("sqlite3").Row
    try:
        ex_rows = conn.execute(
            "SELECT request_id, task_id, agent_id, reason, new_phase, status,"
            " created_at, audit_decision, audit_reason, audit_risk_level"
            " FROM disclosure_requests WHERE status = 'approved'"
            " ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        key_rows = conn.execute(
            "SELECT key_id, agent_id, scope, status, created_by, created_at,"
            " expires_at, last_used_at, call_count FROM agent_keys"
            " WHERE status = 'active' AND expires_at != '' ORDER BY expires_at ASC"
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=7)
    expiring = []
    for k in key_rows:
        try:
            exp = datetime.fromisoformat(str(k["expires_at"]).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        days = round((exp - now).total_seconds() / 86400, 1)
        if exp <= horizon:
            expiring.append({
                "key_id": k["key_id"], "agent_id": k["agent_id"],
                "expires_at": k["expires_at"], "days_left": days,
                "expired": exp < now,
                "last_used_at": k["last_used_at"] or "",
                "call_count": k["call_count"],
            })

    exceptions = [dict(r) for r in ex_rows]
    return {
        "status": "ok",
        "exceptions": exceptions,
        "expiring_keys": expiring,
        "counts": {
            "exceptions": len(exceptions),
            "expiring_7d": len([e for e in expiring if not e["expired"]]),
            "expired": len([e for e in expiring if e["expired"]]),
        },
    }


# ══════════════════════════════════════════════════════════════
# 1e 员工账号（阶段1/2026-08-30）：SMB 无 AD 的入场券
# 模板→scope 映射在 auth_provider.TEMPLATE_SCOPE（认证时现算）
# 全部端点 manager+ 门 + 审计（events 表）
# ══════════════════════════════════════════════════════════════

VALID_TEMPLATES = {"owner", "dept_head", "staff", "external"}


def _parse_csv_accounts(csv_text):
    """CSV 三列(姓名,邮箱,模板) → (ok 行列表, bad 行列表[带原因])"""
    ok, bad = [], []
    for i, ln in enumerate(csv_text.strip().splitlines(), 1):
        if not ln.strip():
            continue
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) != 3:
            bad.append({"line": i, "raw": ln, "reason": "列数!=3"})
            continue
        name, email, tpl = parts
        if not name or not email or tpl not in VALID_TEMPLATES:
            bad.append({"line": i, "raw": ln, "reason": f"模板非法({tpl})或字段空"})
            continue
        if "@" not in email:
            bad.append({"line": i, "raw": ln, "reason": "邮箱格式非法"})
            continue
        ok.append({"name": name, "email": email, "role_template": tpl})
    return ok, bad


@router.get("/api/v1/access/accounts/employees")
async def api_access_accounts_employees(request: Request = None, department: str = "",
                                        current_agent: str = Depends(get_current_agent)):
    """员工账号表（1e）：模板/部门/状态/租约/key 是否已签发

    ?department= 精确过滤（身份供给页二级用；空 = 全部，后向兼容）
    """
    _require_manager(request, current_agent)
    conn = hub._db()
    conn.row_factory = __import__("sqlite3").Row
    try:
        sql = ("SELECT employee_id, name, email, role_template, department, project_scope,"
               " status, created_at, lease_expires_at,"
               " CASE WHEN key_hash != '' THEN 1 ELSE 0 END AS has_key,"
               " (SELECT COUNT(*) FROM employee_keys ek"
               "   WHERE ek.employee_id = employee_accounts.employee_id"
               "   AND ek.status = 'active') AS key_count"
               " FROM employee_accounts")
        params = ()
        if department:
            sql += " WHERE department = ?"
            params = (department,)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return {"status": "ok", "accounts": [dict(r) for r in rows], "count": len(rows),
            "department": department}


@router.post("/api/v1/access/accounts/import")
async def api_access_accounts_import(request: Request, current_agent: str = Depends(get_current_agent)):
    """CSV 批量导入员工账号(姓名,邮箱,模板)。幂等: email 存在则更新模板，坏行拒绝并回报告。"""
    _require_manager(request, current_agent)
    try:
        data = await request.json()
    except Exception:
        data = {}
    csv_text = data.get("csv", "") or ""
    if not csv_text.strip():
        raise HTTPException(status_code=400, detail="csv 不能为空")
    rows, bad = _parse_csv_accounts(csv_text)
    import uuid as _uuid

    conn = hub._db()
    imported = updated = 0
    try:
        for r in rows:
            cur = conn.execute(
                "SELECT employee_id, role_template FROM employee_accounts WHERE email = ?",
                (r["email"],),
            )
            exist = cur.fetchone()
            if exist:
                conn.execute(
                    "UPDATE employee_accounts SET role_template = ? WHERE employee_id = ?",
                    (r["role_template"], exist[0]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO employee_accounts"
                    " (employee_id, name, email, role_template, status)"
                    " VALUES (?,?,?,?, 'active')",
                    ("emp-" + _uuid.uuid4().hex[:8], r["name"], r["email"], r["role_template"]),
                )
                imported += 1
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("accounts_import", current_agent,
                         {"imported": imported, "updated": updated, "rejected": len(bad)})
    return {"status": "ok", "imported": imported, "updated": updated, "rejected": bad}


@router.post("/api/v1/access/accounts")
async def api_access_accounts_create(request: Request, current_agent: str = Depends(get_current_agent)):
    """单建员工账号 + 自动签 key（明文仅返回一次）"""
    _require_manager(request, current_agent)
    try:
        data = await request.json()
    except Exception:
        data = {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    tpl = (data.get("role_template") or "").strip()
    if not name or not email or tpl not in VALID_TEMPLATES:
        raise HTTPException(status_code=400, detail="name/email/role_template 必填且模板合法")
    import uuid as _uuid

    emp_id = "emp-" + _uuid.uuid4().hex[:8]
    # CD-072：凭据进账本（可多把、可单把吊销、有调用画像）
    _k = _emp_keys().create(emp_id, label=(data.get("label") or "建号自动签发"),
                            created_by=current_agent,
                            expires_at=data.get("lease_expires_at", "") or "")
    plain = _k["key"]
    h = _emp_key_hash(plain)
    conn = hub._db()
    try:
        conn.execute(
            "INSERT INTO employee_accounts (employee_id, name, email, role_template,"
            " department, project_scope, key_hash, status, lease_expires_at)"
            " VALUES (?,?,?,?,?,?,?, 'active', ?)",
            (emp_id, name, email, tpl, data.get("department", "") or "",
             data.get("project_scope", "") or "", h, data.get("lease_expires_at", "") or ""),
        )
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("account_created", current_agent,
                         {"employee_id": emp_id, "role_template": tpl})
    return {"status": "created", "employee_id": emp_id, "key": plain,
            "key_id": _k["key_id"]}


@router.post("/api/v1/access/accounts/{emp_id}/key")
async def api_access_accounts_key(emp_id: str, request: Request,
                                  current_agent: str = Depends(get_current_agent)):
    """签发一把新凭据（CD-072：账本模型，**不覆盖**旧的；明文仅此一次可见）。

    body 可选：{"label": "小王的主钥匙", "expires_at": "2026-12-31"}。
    """
    _require_manager(request, current_agent)
    try:
        body = await request.json()
    except Exception:
        body = {}
    conn = hub._db()
    try:
        row = conn.execute(
            "SELECT employee_id FROM employee_accounts"
            " WHERE employee_id = ? AND status = 'active'", (emp_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="账号不存在或已禁用")
        conn.commit()
    finally:
        conn.close()
    _k = _emp_keys().create(emp_id, label=(body or {}).get("label", "") or "补签",
                            created_by=current_agent,
                            expires_at=(body or {}).get("expires_at", "") or "")
    conn = hub._db()
    try:
        _mirror_key_hash(conn, emp_id)
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("account_key_issued", current_agent,
                         {"employee_id": emp_id, "key_id": _k["key_id"]})
    return {"status": "issued", "employee_id": emp_id, "key": _k["key"],
            "key_id": _k["key_id"]}


@router.get("/api/v1/access/accounts/{emp_id}/keys")
async def api_access_account_keys(emp_id: str, request: Request = None,
                                  current_agent: str = Depends(get_current_agent)):
    """列某员工的凭据账本（key_id/标签/状态/创建/到期/最近使用/调用次数；不含明文）。"""
    _require_manager(request, current_agent)
    conn = hub._db()
    try:
        row = conn.execute("SELECT employee_id FROM employee_accounts WHERE employee_id = ?",
                           (emp_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="账号不存在")
        conn.commit()
    finally:
        conn.close()
    keys = _emp_keys().list_keys(emp_id)
    return {"status": "ok", "employee_id": emp_id, "keys": keys, "count": len(keys)}


@router.post("/api/v1/access/accounts/{emp_id}/keys/{key_id}/revoke")
async def api_access_account_key_revoke(emp_id: str, key_id: str, request: Request = None,
                                        current_agent: str = Depends(get_current_agent)):
    """按 key_id 吊销**单把**凭据（其余凭据继续可用）。归属不匹配 → 404（防跨人吊销）。"""
    _require_manager(request, current_agent)
    owner = _emp_keys().key_of_employee(key_id)
    if owner != emp_id:
        raise HTTPException(status_code=404, detail="凭据不存在或不属于该员工")
    changed = _emp_keys().revoke(key_id)
    if not changed:
        raise HTTPException(status_code=404, detail="凭据不存在或已吊销")
    conn = hub._db()
    try:
        _mirror_key_hash(conn, emp_id)
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("account_key_revoked_one", current_agent,
                         {"employee_id": emp_id, "key_id": key_id})
    return {"status": "revoked", "employee_id": emp_id, "key_id": key_id}


@router.post("/api/v1/access/accounts/{emp_id}/revoke")
async def api_access_accounts_revoke(emp_id: str, request: Request = None,
                                     current_agent: str = Depends(get_current_agent)):
    """吊销员工 key（清 key_hash，立即失效）"""
    _require_manager(request, current_agent)
    conn = hub._db()
    try:
        conn.execute("UPDATE employee_accounts SET key_hash = '' WHERE employee_id = ?", (emp_id,))
        conn.commit()
    finally:
        conn.close()
    # CD-072：整人吊销 = 账本里全部 active 凭据置 revoked
    _n = _emp_keys().revoke_all(emp_id)
    await hub._log_event("account_key_revoked", current_agent,
                         {"employee_id": emp_id, "revoked": _n})
    return {"status": "revoked", "employee_id": emp_id, "revoked": _n}


@router.patch("/api/v1/access/accounts/{emp_id}")
async def api_access_accounts_patch(emp_id: str, request: Request,
                                    current_agent: str = Depends(get_current_agent)):
    """改模板/部门/项目域/状态/租约（role_template 变更 → 下次认证 scope 现算生效）"""
    _require_manager(request, current_agent)
    try:
        data = await request.json()
    except Exception:
        data = {}
    fields, vals = [], []
    for col in ("role_template", "department", "project_scope", "status", "lease_expires_at"):
        if col in data:
            fields.append(f"{col} = ?")
            vals.append(data[col])
    if not fields:
        raise HTTPException(status_code=400, detail="无更新字段")
    vals.append(emp_id)
    conn = hub._db()
    try:
        conn.execute(f"UPDATE employee_accounts SET {', '.join(fields)} WHERE employee_id = ?", vals)
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("account_updated", current_agent, {"employee_id": emp_id, **data})
    return {"status": "updated", "employee_id": emp_id}


# ── 1e 身份供给 · 部门目录（2026-09-20）────────────────────────────────────────
# 口径（用户 2026-09-20 认可）：department 表**只做目录 + 建员工时的默认值**，
# 权限判定仍走 employee_accounts.department / project_scope（CD-025 读时派生不动）——
# 刻意不引入第二真相源。与员工记录按 name join → 存量员工建部门后自动归位，零迁移。
_DEPT_PATCH_FIELDS = ("name", "description", "default_role_template")


@router.get("/api/v1/access/departments")
async def api_access_departments(request: Request = None,
                                  current_agent: str = Depends(get_current_agent)):
    """部门目录（附每部门员工统计）+「未纳管部门」兜底清单。

    未纳管 = 员工记录里出现、departments 表里没有同名的部门字符串
    （历史自由文本，或改名时未勾选同步员工）。显式返回，避免变成隐形数据。
    """
    _require_manager(request, current_agent)
    conn = hub._db()
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            "SELECT d.department_id, d.name, d.description, d.default_role_template,"
            " d.created_by, d.created_at,"
            " (SELECT COUNT(*) FROM employee_accounts e WHERE e.department = d.name)"
            "   AS employee_count,"
            " (SELECT COUNT(*) FROM employee_accounts e WHERE e.department = d.name"
            "   AND e.key_hash != '') AS key_issued,"
            " (SELECT COUNT(*) FROM employee_accounts e WHERE e.department = d.name"
            "   AND e.status = 'active') AS active_count,"
            " (SELECT COUNT(*) FROM agents a WHERE a.department = d.name) AS agent_count"
            " FROM departments d ORDER BY d.created_at DESC, d.name").fetchall()
        departments = [dict(r) for r in rows]
        managed = {r["name"] for r in rows}
        loose = [{"department": r["department"], "employee_count": r["n"]} for r in conn.execute(
            "SELECT department, COUNT(*) AS n FROM employee_accounts"
            " WHERE COALESCE(department, '') != '' GROUP BY department"
            " ORDER BY department").fetchall() if r["department"] not in managed]
    finally:
        conn.close()
    return {"status": "ok", "departments": departments, "count": len(departments),
            "unmanaged": loose, "templates": sorted(VALID_TEMPLATES)}


@router.post("/api/v1/access/departments")
async def api_access_departments_create(request: Request,
                                        current_agent: str = Depends(get_current_agent)):
    """新建部门（名称唯一；已存在 → 409，不覆盖）。"""
    _require_manager(request, current_agent)
    try:
        data = await request.json()
    except Exception:
        data = {}
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 必填")
    tpl = (data.get("default_role_template") or "staff").strip()
    if tpl not in VALID_TEMPLATES:
        raise HTTPException(status_code=400, detail=f"default_role_template 非法({tpl})")
    import uuid as _uuid

    conn = hub._db()
    try:
        exist = conn.execute("SELECT department_id FROM departments WHERE name = ?",
                             (name,)).fetchone()
        if exist:
            raise HTTPException(status_code=409, detail=f"部门已存在: {name}")
        dept_id = "dept-" + _uuid.uuid4().hex[:8]
        conn.execute(
            "INSERT INTO departments (department_id, name, description,"
            " default_role_template, created_by) VALUES (?,?,?,?,?)",
            (dept_id, name, data.get("description", "") or "", tpl, current_agent))
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("department_created", current_agent,
                         {"department_id": dept_id, "name": name, "default_role_template": tpl})
    return {"status": "created", "department_id": dept_id, "name": name}


@router.patch("/api/v1/access/departments/{dept_id}")
async def api_access_departments_patch(dept_id: str, request: Request,
                                       current_agent: str = Depends(get_current_agent)):
    """改部门名/描述/默认模板。改名可带 sync_employees=true 同步员工记录字段。

    不勾同步时，那些员工的 department 仍是旧名字 → 会出现在 list 的 unmanaged 清单里。
    """
    _require_manager(request, current_agent)
    try:
        data = await request.json()
    except Exception:
        data = {}
    _tpl = data.get("default_role_template")
    if _tpl and _tpl not in VALID_TEMPLATES:
        raise HTTPException(status_code=400, detail=f"default_role_template 非法({_tpl})")
    conn = hub._db()
    conn.row_factory = __import__("sqlite3").Row
    try:
        row = conn.execute("SELECT name FROM departments WHERE department_id = ?",
                           (dept_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="部门不存在")
        old_name = row["name"]
        new_name = ""
        if "name" in data:
            new_name = (data.get("name") or "").strip()
            if not new_name:
                raise HTTPException(status_code=400, detail="name 不能为空")
            if new_name != old_name:
                dup = conn.execute(
                    "SELECT 1 FROM departments WHERE name = ? AND department_id != ?",
                    (new_name, dept_id)).fetchone()
                if dup:
                    raise HTTPException(status_code=409, detail=f"部门已存在: {new_name}")
        fields, vals = [], []
        for col in ("description", "default_role_template"):
            if col in data:
                fields.append(f"{col} = ?")
                vals.append(data[col])
        if "name" in data:
            fields.append("name = ?")
            vals.append(new_name)
        if not fields:
            raise HTTPException(status_code=400, detail="无更新字段")
        vals.append(dept_id)
        conn.execute(f"UPDATE departments SET {', '.join(fields)} WHERE department_id = ?", vals)
        moved = 0
        if new_name and new_name != old_name and data.get("sync_employees"):
            cur = conn.execute("UPDATE employee_accounts SET department = ? WHERE department = ?",
                               (new_name, old_name))
            moved = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("department_updated", current_agent,
                         {"department_id": dept_id, "moved_employees": moved,
                          "name": data.get("name"), "sync_employees": bool(data.get("sync_employees"))})
    return {"status": "updated", "department_id": dept_id, "moved_employees": moved}


@router.delete("/api/v1/access/departments/{dept_id}")
async def api_access_departments_delete(dept_id: str, request: Request = None,
                                        current_agent: str = Depends(get_current_agent)):
    """删部门：**有员工即拒绝**（409，报还剩几人）；空部门才可删。

    员工本身只停用不删（status=disabled），保留「谁曾有过 key」的审计线索。
    """
    _require_manager(request, current_agent)
    conn = hub._db()
    conn.row_factory = __import__("sqlite3").Row
    try:
        row = conn.execute("SELECT name FROM departments WHERE department_id = ?",
                           (dept_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="部门不存在")
        name = row["name"]
        cnt = conn.execute("SELECT COUNT(*) AS c FROM employee_accounts WHERE department = ?",
                           (name,)).fetchone()["c"]
        if cnt:
            raise HTTPException(
                status_code=409,
                detail=f"部门下仍有 {cnt} 名员工，先迁走（改所属部门）或停用后再删")
        conn.execute("DELETE FROM departments WHERE department_id = ?", (dept_id,))
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("department_deleted", current_agent,
                         {"department_id": dept_id, "name": name})
    return {"status": "deleted", "department_id": dept_id, "name": name}
