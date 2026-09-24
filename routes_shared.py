"""星枢 Sync Hub — 共享工作区 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_shared")

import asyncio, json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from models import CONFIG, DisclosureLevel
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
    get_current_principal,
)
from routes_gateway import _log_read, _log_deny  # CD-054: 共享文档读审计复用网关 helper；CD-059(T18): 拒绝留痕

router = APIRouter()

# ═══ 共享工作区 (Shared Workspace) ═══
import shared_workspace as _sw


def _ws():
    """动态获取 workspace 单例（lifespan 后初始化）"""
    return _sw.workspace


def _is_hub_token_principal(principal) -> bool:
    """CD-094：principal 是否为 hub_token 部署级运维主体（Principal 对象或 dict 均兼容）。"""
    if not principal:
        return False
    if isinstance(principal, dict):
        return principal.get("auth_mode") == "hub_token"
    return getattr(principal, "auth_mode", "") == "hub_token"


# CD-104：判定下沉自 routes_ws（底座方向），watch 广播逐人组帧复用；准入行为一字不变
async def _shared_doc_ws_level(doc_id: str, agent_id: str, principal) -> DisclosureLevel:
    """主体对该共享文档的披露级别（CD-094 WS 门用）。

    fail-closed 收口：meta 不存在 → NONE；private 文档非成员（can_access 为假，
    与 REST 读出口第一道门同源）→ 恒 NONE，不进规则链。
    """
    if _is_hub_token_principal(principal):
        return DisclosureLevel.FULL
    ws_inst = _ws()
    meta = await ws_inst.get_doc_meta(doc_id) if ws_inst is not None else None
    if meta is None:
        return DisclosureLevel.NONE
    if (meta.get("visibility") or "team") == "private" \
            and not await ws_inst.can_access(doc_id, agent_id):
        return DisclosureLevel.NONE
    from disclosure import DisclosureEngine  # 延迟 import：避循环依赖
    return DisclosureEngine(hub).shared_doc_level(
        meta, agent_id,
        scope=getattr(principal, "scope", None) if principal else None,
        principal_known=principal is not None)


# 轻量 JSON watcher 集合：doc_id → {agent_id: (websocket, joined_at, principal)}（含 awareness）
_shared_watchers: dict[str, dict] = {}


async def _broadcast_shared_update(doc_id: str, event: dict):
    """向所有轻量 watcher 广播 JSON 事件（附带在线协作者列表 = awareness）。

    CD-104：按 watcher 逐人组帧——FULL/SUMMARY 原样（preview 前 100 字），
    METADATA 剥离 preview 为空串 + preview_stripped=true，NONE fail-closed 不推；
    判定抛异常同样 fail-closed 跳过。shared_presence（无 preview 键）不剥离。
    """
    import json as _json
    watchers = _shared_watchers.get(doc_id, {})
    dead = set()
    # 在线协作者（awareness）
    online = [aid for aid, entry in watchers.items() if aid and aid != "__anon__"]
    base = {**event, "online": online}
    has_preview = "preview" in event
    dumps_cache: dict = {}
    for aid, entry in list(watchers.items()):
        ws = entry[0]
        principal = entry[2] if len(entry) > 2 else None
        try:
            level = await _shared_doc_ws_level(doc_id, aid, principal)
        except Exception as _exc:
            logger.warning("CD-104 level judge failed, skip watcher %s@%s: %s",
                           aid, doc_id, _exc)
            continue
        if level == DisclosureLevel.NONE:
            continue
        stripped = has_preview and level == DisclosureLevel.METADATA
        if stripped not in dumps_cache:
            frame = dict(base)
            if stripped:
                frame["preview"] = ""
                frame["preview_stripped"] = True
            dumps_cache[stripped] = _json.dumps(frame)
        try:
            await ws.send_text(dumps_cache[stripped])
        except Exception:
            dead.add(aid)
    for aid in dead:
        watchers.pop(aid, None)
    _shared_watchers[doc_id] = watchers


def _doc_archived(doc_id: str) -> bool:
    """只看「已归档」这一态。不存在的 doc 返回 False —— 保持既有「不存在」语义不变
    （见 tests/test_ws_auth_matrix.py 对 /ws/shared/* 的成功用例）。"""
    import sqlite3 as _sq
    try:
        _c = _sq.connect(CONFIG.DB_PATH)
        _row = _c.execute("SELECT archived FROM shared_docs WHERE doc_id=?", (doc_id,)).fetchone()
        _c.close()
    except Exception:
        return False
    return bool(_row and _row[0])


async def _close_shared_watchers(doc_id: str, reason: str = "doc archived") -> int:
    """关闭该文档的全部轻量 watcher（归档 = 讨论结束，通道要一起关），返回关闭数。"""
    import json as _json
    watchers = _shared_watchers.pop(doc_id, {}) or {}
    msg = _json.dumps({"type": "shared_archived", "doc_id": doc_id, "reason": reason})
    for _aid, entry in list(watchers.items()):
        ws = entry[0]
        try:
            await ws.send_text(msg)
        except Exception:
            pass
        try:
            await ws.close(code=4404, reason=reason)
        except Exception:
            pass
    return len(watchers)


@router.get("/api/v1/shared/docs")
async def api_shared_list(current_agent: str = Depends(get_current_agent),
                          principal=Depends(get_current_principal)):
    """列出当前 agent 可见的共享文档（按 visibility 过滤）"""
    ws_inst = _ws()
    if ws_inst is None:
        return {"docs": []}
    docs = await ws_inst.list_docs(current_agent)
    # CD-094：level_cap=none 的 scoped key 连元数据列表也不给（fail-closed）；
    # 其余级别（含 metadata）可见列表——列表本就是元数据。
    _scope = getattr(principal, "scope", None) if principal else None
    if _scope and (_scope.get("level_cap") or "") == "none":
        _log_read(current_agent, principal, "shared", "", "", "none", 0, 0)
        return {"docs": []}
    # CD-054: 读审计落链（仅成功路径；_log_read 内部 except 不阻塞读取，D4）
    _log_read(current_agent, principal, "shared", "", "", "metadata", len(docs), 0)
    return {"docs": docs}


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
async def api_shared_get(doc_id: str, current_agent: str = Depends(get_current_agent),
                         principal=Depends(get_current_principal)):
    """获取文档内容（private 文档仅创建者+白名单可见）

    T17/CD-056 策略冻结（docs/api-error-policy.md）：先鉴权后取内容——
    非特权主体「不存在」与「无权」同一 403 + 同 detail（can_access 对不存在
    doc_id 返回 False），不可由 404/403 差异反推存在性；特权主体保留可区分
    语义（不存在 → 404 / 存在但无权 → 403），控制台排查不受影响。
    """
    ws_inst = _ws()
    if ws_inst is None:
        raise HTTPException(503, "workspace not ready")
    if not await ws_inst.can_access(doc_id, current_agent):
        from routes_common import principal_is_privileged
        if principal_is_privileged(principal) \
                and await ws_inst.get_doc_content(doc_id) is None:
            _log_deny(current_agent, principal, "shared", "", doc_id)
            raise HTTPException(404, "doc not found")
        # CD-059(T18): 非特权主体「不存在」与「无权」同一 403（T17 冻结），
        # 落行按实际返回记 denied，不为区分存在性再探一次
        _log_deny(current_agent, principal, "shared", "", doc_id)
        raise HTTPException(403, "无权访问该文档")
    content = await ws_inst.get_doc_content(doc_id)
    if content is None:
        _log_deny(current_agent, principal, "shared", "", doc_id)
        raise HTTPException(404, "doc not found")
    # CD-094（方案①）：读出口按主体披露级别剥离——级别 = min(主体判定
    # [8 规则链 + scoped key level_cap/data_domain], 文档密级[trust_level 映射])，
    # 判定逻辑收口在 disclosure.DisclosureEngine.shared_doc_level。
    # principal=None = NO_AUTH 开发态（无身份语义）→ 主体侧不判定，但文档密级仍生效。
    meta = await ws_inst.get_doc_meta(doc_id)
    if meta is None:
        # can_access 刚放行而元数据行已消失（并发删除竞态）——按不存在处理
        _log_deny(current_agent, principal, "shared", "", doc_id)
        raise HTTPException(404, "doc not found")
    from disclosure import DisclosureEngine
    if _is_hub_token_principal(principal):
        # hub_token 部署级运维主体（D1 不做 RBAC）：读出口不剥离，与上方
        # 「404/403 可区分」特权语义同源（控制台排查不受影响）。
        # 仅精确匹配 auth_mode=="hub_token"——scoped key（api_key 模式）永不命中，
        # manager/orchestrator 角色的普通 api_key 也不经此旁路（仍走规则链）。
        level = DisclosureLevel.FULL
    else:
        level = DisclosureEngine(hub).shared_doc_level(
            meta, current_agent,
            scope=getattr(principal, "scope", None) if principal else None,
            principal_known=principal is not None)
    if level == DisclosureLevel.NONE:
        # 与上方「无权」同一 403 + 同 detail（T17 冻结），不可由差异反推密级
        _log_deny(current_agent, principal, "shared", "", doc_id)
        raise HTTPException(403, "无权访问该文档")
    if level == DisclosureLevel.METADATA:
        # 只回元数据，无 content 键；审计记 granted_level=metadata + 剥离 1 段正文
        _log_read(current_agent, principal, "shared", "", doc_id, "metadata", 1, 1)
        return {"doc_id": doc_id, "title": meta.get("title") or "",
                "created_by": meta.get("created_by") or "",
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "block_count": meta.get("block_count", 0),
                "visibility": meta.get("visibility") or "team",
                "disclosure_level": "metadata"}
    if level == DisclosureLevel.SUMMARY:
        # 摘要级：正文前 200 字 + truncated 标记（与 _extract_by_level 的 200 字口径一致）
        _log_read(current_agent, principal, "shared", "", doc_id, "summary", 1, 1)
        return {"doc_id": doc_id, "content": content[:200],
                "truncated": len(content) > 200, "disclosure_level": "summary"}
    # CD-054: 读审计落链（成功路径；403/404 拒绝由上方 _log_deny 落 denied 行，CD-059）
    _log_read(current_agent, principal, "shared", "", doc_id, "full", 1, 0)
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
        _log_deny(current_agent, None, "shared", "", doc_id)
        raise HTTPException(404, "doc not found")
    vis = _row[1] or "team"
    if vis == "private" and _row[0] != current_agent:
        _log_deny(current_agent, None, "shared", "", doc_id)
        raise HTTPException(403, "仅创建者可归档私有文档")
    # 2026-09-22：归档 = 讨论结束 —— 先把房里的轻量 watcher 一起关掉再归档
    # （pycrdt 侧的连接由 delete_doc → close_doc_connections 关）
    await _close_shared_watchers(doc_id)
    ok = await ws_inst.delete_doc(doc_id)
    if not ok:
        _log_deny(current_agent, None, "shared", "", doc_id)
        raise HTTPException(404, "doc not found")
    return {"status": "deleted"}


@router.post("/api/v1/shared/docs/{doc_id}/unarchive")
async def api_shared_unarchive(doc_id: str, current_agent: str = Depends(get_current_agent)):
    """取消归档（2026-09-22）：archived 1 → 0，恢复后列表可见、可读、可再进房。

    权限与归档对称 —— 经同一 n1 审批门；private 文档仅创建者可恢复。
    房间按需懒加载，恢复时不预建 room。
    """
    from routes_n1 import _n1_gate
    _gate = await _n1_gate(current_agent, "shared_docs", {"doc_id": doc_id})
    if _gate:
        return _gate
    ws_inst = _ws()
    if ws_inst is None:
        raise HTTPException(503, "workspace not ready")
    import sqlite3 as _sq
    _c = _sq.connect(CONFIG.DB_PATH)
    _row = _c.execute(
        "SELECT created_by, visibility, archived FROM shared_docs WHERE doc_id=?", (doc_id,)
    ).fetchone()
    _c.close()
    if _row is None:
        _log_deny(current_agent, None, "shared", "", doc_id)
        raise HTTPException(404, "doc not found")
    vis = _row[1] or "team"
    if vis == "private" and _row[0] != current_agent:
        _log_deny(current_agent, None, "shared", "", doc_id)
        raise HTTPException(403, "仅创建者可恢复私有文档")
    ok = await ws_inst.restore_doc(doc_id)
    if not ok:
        _log_deny(current_agent, None, "shared", "", doc_id)
        raise HTTPException(409, "doc 未处于归档状态")
    return {"status": "restored", "doc_id": doc_id}


