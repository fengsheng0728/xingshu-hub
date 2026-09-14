"""星枢 Sync Hub — 记忆池 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import asyncio, json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from deps import DisclosureRequest, MemoryBatchOp, MemoryEntry, SemanticSearchRequest
from models import CONFIG
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
    get_current_principal,
)

router = APIRouter()

@router.post("/api/v1/memory/batch")
async def api_batch_memory(
    agent_id: str,
    operations: List[MemoryBatchOp],
    current_agent: str = Depends(get_current_agent),
):
    """H3: 批量记忆操作——一次 HTTP 读写多条"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    results = []
    for op in operations:
        try:
            if op.action == "store":
                entry = MemoryEntry(memory_key=op.memory_key or str(uuid.uuid4())[:8],
                                    content=op.content or "",
                                    kind=op.kind or "fact",
                                    source_type=op.source_type or "agent")
                r = await hub.store_memory(agent_id, entry)
                results.append({"ok": True, "key": r.get("key", op.memory_key)})
            elif op.action == "delete":
                r = await hub.delete_memory(op.memory_key, agent_id)
                results.append({"ok": True, "deleted": op.memory_key})
            elif op.action == "search":
                r = await hub.search_memory(agent_id, op.query or "", op.limit or 10)
                results.append({"ok": True, "results": r})
            else:
                results.append({"ok": False, "error": f"Unknown action: {op.action}"})
        except Exception as e:
            results.append({"ok": False, "error": str(e)})
    return {"results": results}


@router.post("/api/v1/memory/store")
async def api_store_memory(
    agent_id: str,
    memory: MemoryEntry,
    current_agent: str = Depends(get_current_agent),
):
    """
    Agent 写入自己的记忆池。
    【不广播，不通知任何人】—— 写入隔离的核心保证。
    """
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    return await hub.store_memory(agent_id, memory)


@router.delete("/api/v1/memory/{memory_key}")
async def api_delete_memory(
    memory_key: str,
    agent_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """Agent 删除自己的记忆"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    from routes_n1 import _n1_gate
    _gate = await _n1_gate(current_agent, "memory",
                           {"memory_key": memory_key, "agent_id": agent_id})
    if _gate:
        return _gate
    return await hub.delete_memory(memory_key, agent_id)


@router.get("/api/v1/memory/{memory_key}/versions")
async def api_memory_versions(
    memory_key: str,
    agent_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """获取记忆版本历史"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    return await hub.get_memory_versions(memory_key, agent_id)


@router.post("/api/v1/memory/{memory_key}/rollback")
async def api_memory_rollback(
    memory_key: str,
    agent_id: str,
    version_id: int,
    current_agent: str = Depends(get_current_agent),
):
    """回滚记忆到指定版本"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    return await hub.rollback_memory(memory_key, version_id, agent_id)


@router.post("/api/v1/memory/disclose")
async def api_disclose(
    req: DisclosureRequest,
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """
    按需披露查询。

    根据请求者的身份、与目标 Agent 的关系、任务需求以及记忆自身的策略，
    决定披露级别：NONE → METADATA → SUMMARY → FULL
    """
    if not NO_AUTH and current_agent != req.requester_agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {req.requester_agent_id}")
    scope = principal.scope if principal else None
    return await hub.request_disclosure(req, scope=scope)


@router.post("/api/v1/memory/semantic_search")
async def api_semantic_search(
    req: SemanticSearchRequest,
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """
    语义搜索（基于 ChromaDB + sentence-transformers）。

    将查询文本转为向量，在 ChromaDB 中搜索语义相似的记忆，
    返回结果受披露策略控制（与 disclosure 引擎一致）。
    """
    if not NO_AUTH and current_agent != req.requester_agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {req.requester_agent_id}")
    scope = principal.scope if principal else None
    return await hub.semantic_search(req, scope=scope)


class MemorySearchRequest(PydanticBase):
    query: str
    agent_id: str
    kind: list = PydanticField(default=["fact", "todo"])
    top_k: int = 5
    min_confidence: float = 0.6


@router.post("/api/v1/memory/search")
async def api_memory_search(
    req: MemorySearchRequest,
    current_agent: str = Depends(get_current_agent),
):
    if not NO_AUTH and current_agent != req.agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份检索 {req.agent_id}")
    return await hub.memory_search(req)


@router.get("/api/v1/memory")
async def api_get_memories(
    agent_id: str,
    kind: str = "",
    current_agent: str = Depends(get_current_agent),
):
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份查看 {agent_id}")
    return hub.get_memories(agent_id, kind)


