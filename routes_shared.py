"""星枢 Sync Hub — 共享工作区 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
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

# ═══ 共享工作区 (Shared Workspace) ═══
import shared_workspace as _sw


def _ws():
    """动态获取 workspace 单例（lifespan 后初始化）"""
    return _sw.workspace


# 轻量 JSON watcher 集合：doc_id → {agent_id: (websocket, joined_at)}（含 awareness）
_shared_watchers: dict[str, dict] = {}


async def _broadcast_shared_update(doc_id: str, event: dict):
    """向所有轻量 watcher 广播 JSON 事件（附带在线协作者列表 = awareness）"""
    import json as _json
    watchers = _shared_watchers.get(doc_id, {})
    dead = set()
    # 在线协作者（awareness）
    online = [aid for aid, (ws, _j) in watchers.items() if aid and aid != "__anon__"]
    event = {**event, "online": online}
    msg = _json.dumps(event)
    for aid, (ws, _j) in list(watchers.items()):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(aid)
    for aid in dead:
        watchers.pop(aid, None)
    _shared_watchers[doc_id] = watchers


@router.get("/api/v1/shared/docs")
async def api_shared_list(current_agent: str = Depends(get_current_agent)):
    """列出当前 agent 可见的共享文档（按 visibility 过滤）"""
    ws_inst = _ws()
    if ws_inst is None:
        return {"docs": []}
    return {"docs": await ws_inst.list_docs(current_agent)}


@router.post("/api/v1/shared/docs")
async def api_shared_create(request: Request, current_agent: str = Depends(get_current_agent)):
    """创建共享文档"""
    ws_inst = _ws()
    if ws_inst is None:
        raise HTTPException(503, "workspace not ready")
    data = await request.json()
    title = data.get("title", "Untitled")
    visibility = data.get("visibility", "team")
    allowed_agents = data.get("allowed_agents") or []
    return await ws_inst.create_doc(title, current_agent, visibility, allowed_agents)


@router.get("/api/v1/shared/docs/{doc_id}")
async def api_shared_get(doc_id: str, current_agent: str = Depends(get_current_agent)):
    """获取文档内容（private 文档仅创建者+白名单可见）"""
    ws_inst = _ws()
    if ws_inst is None:
        raise HTTPException(503, "workspace not ready")
    content = await ws_inst.get_doc_content(doc_id)
    if content is None:
        raise HTTPException(404, "doc not found")
    if not await ws_inst.can_access(doc_id, current_agent):
        raise HTTPException(403, "无权访问该文档")
    return {"doc_id": doc_id, "content": content}


@router.post("/api/v1/shared/docs/{doc_id}/blocks")
async def api_shared_append(
    doc_id: str,
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """向文档追加 block（同时通过 CRDT 广播）"""
    ws_inst = _ws()
    if ws_inst is None:
        raise HTTPException(503, "workspace not ready")
    data = await request.json()
    text = data.get("text", "")
    if not text:
        raise HTTPException(400, "text required")
    if not await ws_inst.can_access(doc_id, current_agent):
        raise HTTPException(403, "无权编辑该文档")
    ok = await ws_inst.append_block(doc_id, text, current_agent)
    if not ok:
        raise HTTPException(404, "doc not found")
    # 广播给轻量 watcher（前端实时刷新）
    asyncio.create_task(_broadcast_shared_update(doc_id, {
        "type": "shared_update",
        "doc_id": doc_id,
        "agent_id": current_agent,
        "preview": text[:100]
    }))
    return {"status": "ok"}


@router.delete("/api/v1/shared/docs/{doc_id}")
async def api_shared_delete(doc_id: str, current_agent: str = Depends(get_current_agent)):
    from routes_n1 import _n1_gate
    _gate = await _n1_gate(current_agent, "shared_docs", {"doc_id": doc_id})
    if _gate:
        return _gate
    """归档文档"""
    ws_inst = _ws()
    if ws_inst is None:
        raise HTTPException(503, "workspace not ready")
    import sqlite3 as _sq
    _c = _sq.connect(CONFIG.DB_PATH)
    _row = _c.execute("SELECT created_by, visibility FROM shared_docs WHERE doc_id=?", (doc_id,)).fetchone()
    _c.close()
    if _row is None:
        raise HTTPException(404, "doc not found")
    vis = _row[1] or "team"
    if vis == "private" and _row[0] != current_agent:
        raise HTTPException(403, "仅创建者可归档私有文档")
    ok = await ws_inst.delete_doc(doc_id)
    if not ok:
        raise HTTPException(404, "doc not found")
    return {"status": "deleted"}


