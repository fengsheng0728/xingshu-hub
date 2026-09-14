"""星枢 Sync Hub — 会话摘要归档 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_sessions")

import asyncio, json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from deps import SessionArchiveRequest, SessionHandoffRequest
from models import CONFIG
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
)

router = APIRouter()

@router.post("/api/v1/sessions/archive")
async def api_archive_session(
    req: SessionArchiveRequest,
    current_agent: str = Depends(get_current_agent),
):
    """Agent 归档会话摘要"""
    if not NO_AUTH and current_agent != req.agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份归档 {req.agent_id} 的会话")
    return await hub.archive_session(
        req.agent_id, req.local_session_id,
        title=req.title, summary=req.summary,
        key_facts=req.key_facts, msg_count=req.msg_count,
    )


@router.get("/api/v1/sessions/recent")
async def api_recent_sessions(
    agent_id: str,
    limit: int = 5,
    current_agent: str = Depends(get_current_agent),
):
    """获取 Agent 的近期会话摘要"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份查看 {agent_id} 的会话")
    sessions = await hub.get_recent_sessions(agent_id, limit)
    return {"status": "ok", "sessions": sessions, "count": len(sessions)}


@router.post("/api/v1/sessions/handoff")
async def api_session_handoff(
    req: SessionHandoffRequest,
    current_agent: str = Depends(get_current_agent),
):
    """P0 团队协作: A 移交会话给 B。

    - from_agent_id 必须 == 调用者（不能替别人移交）
    - to_agent 必须在 WS 在线（显式移交是同步操作，离线直接报错）
    - 通过目标 WS 推送 session.handoff 事件（消息全文 + 摘要 + 关键事实）
    """
    if not NO_AUTH and current_agent != req.from_agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份移交 {req.from_agent_id} 的会话")
    if req.to_agent_id == req.from_agent_id:
        raise HTTPException(status_code=400, detail="不能移交给自己")
    ws = hub.active_ws.get(req.to_agent_id)
    if not ws:
        raise HTTPException(status_code=400,
            detail=f"目标 Agent {req.to_agent_id} 不在线（未连接 WS）")
    try:
        await ws.send_json({
            "type": "session.handoff",
            "from_agent_id": req.from_agent_id,
            "to_agent_id": req.to_agent_id,
            "local_session_id": req.local_session_id,
            "title": req.title,
            "summary": req.summary,
            "key_facts": req.key_facts,
            "messages": req.messages,
        })
    except Exception as e:
        raise HTTPException(status_code=400,
            detail=f"向 {req.to_agent_id} 推送失败: {e}")
    # 审计
    try:
        await hub._log_event("session_handoff", req.from_agent_id,
                             {"to": req.to_agent_id,
                              "local_session_id": req.local_session_id,
                              "title": req.title,
                              "msg_count": len(req.messages)})
    except Exception as _exc:
        logger.warning("routes_sessions silent-except @94: %s", _exc)
    return {"status": "ok", "handoff": True,
            "to_agent_id": req.to_agent_id, "messages": len(req.messages)}


