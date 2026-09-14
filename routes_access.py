"""星枢 Sync Hub — 访问与权限 API（U3 新增）

账号视图（agents 身份表）+ 例外与租约视图：
- 例外 = 已批准的披露申请（disclosure_requests status='approved'）
- 租约 = 带 expires_at 的 scoped key（agent_keys），7 天内到期倒计时
S1-SMB 账号体系（阶段 1e）落地前，账号 = 已注册 Agent 身份。
"""
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from hub_core import hub
from routes_common import NO_AUTH, get_current_agent

router = APIRouter()


def _require_manager(current_agent: str):
    """与 /api/v1/keys 一致的角色门：仅 manager/orchestrator"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可访问")


@router.get("/api/v1/access/accounts")
async def api_access_accounts(current_agent: str = Depends(get_current_agent)):
    """账号表：已注册 Agent 身份（S1-SMB 落地前的现实映射）"""
    _require_manager(current_agent)
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
async def api_access_exceptions(current_agent: str = Depends(get_current_agent)):
    """例外与租约：
    - exceptions: 已批准的披露申请（例外必须被阳光晒到；收回语义随 1e 落地）
    - expiring_keys: 7 天内到期的 scoped key（到期倒计时）
    """
    _require_manager(current_agent)
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


def _gen_employee_key():
    """生成员工 key 明文（SHA256 落库，同 S1K 规范不存明文）；明文仅返回一次"""
    import hashlib
    import secrets

    plain = "emp_" + secrets.token_hex(24)
    return plain, hashlib.sha256(plain.encode("utf-8")).hexdigest()


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
async def api_access_accounts_employees(current_agent: str = Depends(get_current_agent)):
    """员工账号表（1e）：模板/部门/状态/租约/key 是否已签发"""
    _require_manager(current_agent)
    conn = hub._db()
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            "SELECT employee_id, name, email, role_template, department, project_scope,"
            " status, created_at, lease_expires_at,"
            " CASE WHEN key_hash != '' THEN 1 ELSE 0 END AS has_key"
            " FROM employee_accounts ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return {"status": "ok", "accounts": [dict(r) for r in rows], "count": len(rows)}


@router.post("/api/v1/access/accounts/import")
async def api_access_accounts_import(request: Request, current_agent: str = Depends(get_current_agent)):
    """CSV 批量导入员工账号(姓名,邮箱,模板)。幂等: email 存在则更新模板，坏行拒绝并回报告。"""
    _require_manager(current_agent)
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
    _require_manager(current_agent)
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
    plain, h = _gen_employee_key()
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
    return {"status": "created", "employee_id": emp_id, "key": plain}


@router.post("/api/v1/access/accounts/{emp_id}/key")
async def api_access_accounts_key(emp_id: str, request: Request,
                                  current_agent: str = Depends(get_current_agent)):
    """补签员工 key（明文仅一次）；已有 key 自动覆盖（吊销+重签一步）"""
    _require_manager(current_agent)
    conn = hub._db()
    try:
        row = conn.execute(
            "SELECT employee_id FROM employee_accounts"
            " WHERE employee_id = ? AND status = 'active'", (emp_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="账号不存在或已禁用")
        plain, h = _gen_employee_key()
        conn.execute("UPDATE employee_accounts SET key_hash = ? WHERE employee_id = ?", (h, emp_id))
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("account_key_issued", current_agent, {"employee_id": emp_id})
    return {"status": "issued", "employee_id": emp_id, "key": plain}


@router.post("/api/v1/access/accounts/{emp_id}/revoke")
async def api_access_accounts_revoke(emp_id: str,
                                     current_agent: str = Depends(get_current_agent)):
    """吊销员工 key（清 key_hash，立即失效）"""
    _require_manager(current_agent)
    conn = hub._db()
    try:
        conn.execute("UPDATE employee_accounts SET key_hash = '' WHERE employee_id = ?", (emp_id,))
        conn.commit()
    finally:
        conn.close()
    await hub._log_event("account_key_revoked", current_agent, {"employee_id": emp_id})
    return {"status": "revoked", "employee_id": emp_id}


@router.patch("/api/v1/access/accounts/{emp_id}")
async def api_access_accounts_patch(emp_id: str, request: Request,
                                    current_agent: str = Depends(get_current_agent)):
    """改模板/部门/项目域/状态/租约（role_template 变更 → 下次认证 scope 现算生效）"""
    _require_manager(current_agent)
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
