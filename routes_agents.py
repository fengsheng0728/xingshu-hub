"""星枢 Sync Hub — Agent 注册/心跳/配额 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_agents")

import asyncio, hashlib, hmac, json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from deps import AgentRegistration
from models import CONFIG
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    invalidate_agent_quotas,  # CD-040: 配额变更后清快照
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
)

router = APIRouter()

@router.get("/api/v1/agents/quota")
async def api_agent_quotas(current_agent: str = Depends(get_current_agent)):
    """O4：查询全部 Agent 配额配置。"""
    conn = sqlite3.connect(CONFIG.DB_PATH)
    rows = conn.execute("SELECT agent_id, qps_limit, mode, window_sec, burst FROM agent_quotas ORDER BY agent_id").fetchall()
    conn.close()
    return {"quotas": [dict(zip(("agent_id", "qps_limit", "mode", "window_sec", "burst"), r)) for r in rows]}


@router.post("/api/v1/agents/quota")
async def api_set_agent_quota(request: Request,
                                current_agent: str = Depends(get_current_agent)):
    """O4：设置某 Agent 配额。body: {agent_id, qps_limit?, mode?, burst?}
    mode 取值 reject|throttle|alert_only。"""
    body = await request.json()
    agent_id = (body.get("agent_id") or "").strip()
    if not agent_id:
        return {"ok": False, "error": "agent_id 必填"}
    mode = body.get("mode", "alert_only")
    if mode not in ("reject", "throttle", "alert_only"):
        return {"ok": False, "error": f"mode 非法: {mode}"}
    qps = float(body.get("qps_limit", 50))
    burst = int(body.get("burst", 3))
    conn = sqlite3.connect(CONFIG.DB_PATH)
    conn.execute(
        "INSERT INTO agent_quotas (agent_id, qps_limit, mode, window_sec, burst, updated_at) VALUES (?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(agent_id) DO UPDATE SET qps_limit=?, mode=?, burst=?, updated_at=datetime('now')",
        (agent_id, qps, mode, 1.0, burst, qps, mode, burst))
    conn.commit()
    invalidate_agent_quotas()  # CD-040：配额写入后立即失效内存快照（不必等 TTL）
    conn.close()
    try:
        from audit_chain import AuditChain
        AuditChain(CONFIG.DB_PATH).append(
            "agent_quota_set", "audit_log", agent_id,
            {"actor": current_agent, "qps_limit": qps, "mode": mode, "burst": burst})
    except Exception as _exc:
        logger.warning("routes_agents silent-except @58: %s", _exc)
    return {"ok": True, "agent_id": agent_id, "qps_limit": qps, "mode": mode}


def _check_reregister_credential(request: Request, agent_id: str) -> None:
    """T0-2 防重注册身份窃取（open/guarded 统一，路由层判定）。

    agent_id 已存在于 agents 表时，请求必须出示有效凭据，否则 403：
      有效凭据 = Authorization Bearer 值 == 该 agent 现有 api_key 或 == CONFIG.HUB_TOKEN
      （常时比较 hmac.compare_digest，按 UTF-8 bytes 比较避免非 ASCII 抛 TypeError）
    agent_id 不存在 → 放行（新号注册/引导维持现状语义：
      open 无凭据可建号；guarded 由 hub_core.register 拒签分支兜底 403）。
    已存在且凭据有效 → 放行（幂等重引导，api_key 不变）。

    T1-2（2026-09-09）：已迁移库（api_key_hash 列存在）按 sha256(token) 与
    api_key_hash 常时比对——库内不存明文，比对前必须先 hash。
    未迁移老库（无 hash 列）降级旧明文比对（兼容窗口）。
    """
    conn = sqlite3.connect(CONFIG.DB_PATH)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        if "api_key_hash" in cols:
            row = conn.execute("SELECT api_key_hash FROM agents WHERE agent_id = ?",
                               (agent_id,)).fetchone()
        else:
            row = conn.execute("SELECT api_key FROM agents WHERE agent_id = ?",
                               (agent_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return  # 新号注册/引导
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    ok = False
    if token:
        tok = token.encode("utf-8")
        stored = (row[0] or "")
        if "api_key_hash" in cols:
            if stored and hmac.compare_digest(
                    hashlib.sha256(tok).hexdigest().encode("utf-8"),
                    stored.encode("utf-8")):
                ok = True
        elif stored and hmac.compare_digest(tok, stored.encode("utf-8")):
            ok = True
        if not ok and CONFIG.HUB_TOKEN and hmac.compare_digest(
                tok, CONFIG.HUB_TOKEN.encode("utf-8")):
            ok = True
    if not ok:
        raise HTTPException(
            status_code=403,
            detail="agent_id 已注册，重注册需出示该 agent 的 api_key 或 hub_token")


@router.post("/api/v1/agents/register")
async def api_register(request: Request, agent: AgentRegistration):
    # T0-2: 已存在 agent_id 无凭据重注册 → 403（防窃 key）
    _check_reregister_credential(request, agent.agent_id)
    reg = await hub.register(agent)
    # OGA: 方法层 error(如 guarded 拒签 code=403)转 HTTP 状态码, open 成功路径结构不变
    if isinstance(reg, dict) and reg.get("status") == "error":
        raise HTTPException(status_code=int(reg.get("code", 400)),
                            detail=reg.get("detail", "registration failed"))
    return reg


@router.post("/api/v1/agents/bootstrap")
async def api_bootstrap(request: Request, agent: AgentRegistration):
    """H1: 合并连接初始化——一次返回 register+workspace+config+sessions+automation"""
    # T0-2: 已存在 agent_id 无凭据重引导 → 403（防窃 key）
    _check_reregister_credential(request, agent.agent_id)
    # 1. Register
    reg = await hub.register(agent)
    # OGA: register 失败(guarded 拒签等)直接中断, 不跑 workspace/sessions
    if isinstance(reg, dict) and reg.get("status") == "error":
        raise HTTPException(status_code=int(reg.get("code", 400)),
                            detail=reg.get("detail", "registration failed"))
    api_key = reg.get("api_key", "")
    # 2. Workspace
    ws = await hub.get_agent_workspace(agent.agent_id)
    # 3. Hub agent config
    cfg = hub_agent._get_config()
    if cfg and cfg.get("api_key"):
        cfg["api_key"] = cfg["api_key"][:8] + "..."  # 脱敏后由 Agent 端自己持有
    # 4. Recent sessions
    sessions = await hub.get_recent_sessions(agent.agent_id, limit=5)
    # 5. Missed automation runs
    conn = sqlite3.connect(CONFIG.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT SUM(missed_runs) as total FROM automation_jobs WHERE owner_agent_id=? AND missed_runs > 0", (agent.agent_id,))
    total_row = c.fetchone()
    missed_total = total_row[0] if total_row and total_row[0] else 0
    c.execute("SELECT id, name, missed_runs FROM automation_jobs WHERE owner_agent_id=? AND missed_runs > 0", (agent.agent_id,))
    missed_jobs = [{"id": r["id"], "name": r["name"], "missed_runs": r["missed_runs"]} for r in c.fetchall()]
    conn.close()
    missed = {"total_missed": missed_total, "jobs": missed_jobs}
    return {
        "status": "bootstrapped",
        "agent_id": agent.agent_id,
        "api_key": api_key,
        "workspace": ws,
        "config": cfg,
        "sessions": sessions,
        "automation_missed": missed,
    }


@router.post("/api/v1/agents/{agent_id}/heartbeat")
async def api_heartbeat(agent_id: str, current_agent: str = Depends(get_current_agent)):
    return await hub.heartbeat(agent_id)


