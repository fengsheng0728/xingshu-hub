"""星枢 Sync Hub — 披露审批 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
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

@router.post("/api/v1/disclosure/approve")
async def api_approve_disclosure(
    request_id: str,
    approver_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """店长批准披露升级请求"""
    if not NO_AUTH and current_agent != approver_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作")
    return await hub.approve_disclosure_request(request_id, approver_id)


@router.post("/api/v1/disclosure/deny")
async def api_deny_disclosure(
    request_id: str,
    approver_id: str,
    deny_reason: str = "",
    current_agent: str = Depends(get_current_agent),
):
    """店长拒绝披露升级请求"""
    if not NO_AUTH and current_agent != approver_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作")
    return await hub.deny_disclosure_request(request_id, approver_id, deny_reason)


