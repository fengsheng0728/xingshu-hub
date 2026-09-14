"""星枢 Sync Hub — 联邦快照/拉取 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_federation")

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

@router.get("/api/v1/federation/snapshot/{kind}")
async def api_fed_snapshot(kind: str,
                              current_agent: str = Depends(get_current_agent)):
    """N6a：导出数据快照供配对 Hub 拉取（主 Hub 侧）。kind: agents|memory|knowledge|wiki。
    安全：agents 快照排除 api_key 相关列。"""
    from federation_sync import export_snapshot
    return export_snapshot(CONFIG.DB_PATH, kind)


@router.post("/api/v1/federation/pull")
async def api_fed_pull(request: Request,
                          current_agent: str = Depends(get_current_agent)):
    """N6a：备 Hub 手动触发从所有配对 Hub 拉取单向同步。
    body: {kinds?: [agents,memory,knowledge,wiki]} 默认全部。"""
    from federation_sync import pull_from_peer
    body = {}
    try:
        body = await request.json()
    except Exception as _exc:
        logger.warning("routes_federation silent-except @41: %s", _exc)
    kinds = body.get("kinds") or ["agents", "memory", "knowledge", "wiki"]
    conn = sqlite3.connect(CONFIG.DB_PATH)
    peers = conn.execute("SELECT * FROM team_members WHERE revoked_at IS NULL").fetchall()
    conn.close()
    results = []
    for p in peers:
        r = pull_from_peer(CONFIG.DB_PATH, dict(p), kinds)
        results.append(r)
    try:
        from audit_chain import AuditChain
        AuditChain(CONFIG.DB_PATH).append(
            "federation_pull", "audit_log", current_agent,
            {"peers": len(peers), "kinds": kinds})
    except Exception as _exc:
        logger.warning("routes_federation silent-except @56: %s", _exc)
    return {"status": "ok", "pulled": results}


