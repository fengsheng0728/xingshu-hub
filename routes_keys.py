"""星枢 Sync Hub — Scoped Keys API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import asyncio, json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from models import CONFIG
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
)

router = APIRouter()

@router.post("/api/v1/keys")
async def api_keys_create(request: Request, current_agent: str = Depends(get_current_agent)):
    """创建 scoped API key（仅 manager/orchestrator）— 创建审批

    body: {"agent_id": str, "scope": {"endpoints": [...], "data_domain": [...], "level_cap": "summary"},
           "expires_at": str(可选, ISO)}
    返回: key_id + key(明文仅此一次)
    """
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可创建 scoped key")
    try:
        data = await request.json()
    except Exception:
        data = {}
    target = data.get("agent_id", "") or current_agent
    scope = data.get("scope") or {}
    expires = data.get("expires_at", "") or ""
    from key_scopes import get_store
    r = get_store().create(target, scope, created_by=current_agent, expires_at=expires)
    await hub._log_event("key_created", current_agent, {
        "key_id": r["key_id"], "agent_id": target, "scope": r["scope"]})
    return {"status": "created", "key_id": r["key_id"], "key": r["key"],
            "agent_id": target, "scope": r["scope"]}


@router.delete("/api/v1/keys/{key_id}")
async def api_keys_revoke(key_id: str, current_agent: str = Depends(get_current_agent)):
    """吊销 scoped key（仅 manager/orchestrator）— 60s 内全端点失效"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可吊销 key")
    from key_scopes import get_store
    ok = get_store().revoke(key_id)
    if not ok:
        raise HTTPException(404, "key 不存在或已吊销")
    await hub._log_event("key_revoked", current_agent, {"key_id": key_id})
    return {"status": "revoked", "key_id": key_id}


@router.get("/api/v1/keys")
async def api_keys_list(current_agent: str = Depends(get_current_agent)):
    """列 scoped keys（调用画像：last_used_at/call_count；仅 manager/orchestrator）"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可查看 keys")
    from key_scopes import get_store
    keys = get_store().list_keys()
    await hub._log_event("keys_listed", current_agent, {"count": len(keys)})
    return {"status": "ok", "keys": keys, "count": len(keys)}


