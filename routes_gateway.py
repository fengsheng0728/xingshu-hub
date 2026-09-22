"""星枢 Sync Hub — 网关读取端点（阶段2/01，2026-08-30）

统一读取入口：Agent/员工检索与读取 Hub 数据必经此口。
链路：认证(principal) → 检索(semantic/memory/doc) → 段落剥离(chunk 级) → 读审计落链。

- kind=semantic: 语义检索，复用 disclosure.semantic_search（1e scope 剥离已生效）
- kind=memory: 关键词检索，复用 hub.search_memory + scope
- kind=doc: 文档段落读取，document_chunks 按 requester 披露级别剥离（sensitivity.chunk_level 打标体系）
- P2 交付2：响应条目附可选 origin 字段（真相源定位：git 路径 + commit hash；读取仍走 SQLite）
全部请求落 gateway_read_log（读审计：谁/哪把 key/看了什么/给到哪级/剥离了多少）。
"""
import logging
logger = logging.getLogger("xingshu.routes_gateway")

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from hub_core import hub
from models import DisclosureLevel, SemanticSearchRequest
from routes_common import NO_AUTH, get_current_agent, get_current_principal

router = APIRouter()

_VALID_KINDS = ("semantic", "memory", "doc")


# ============ CD-058：MCP 主体身份注入（contextvars） ============
# FastMCP 1.26.0 工具是模块级函数，拿不到 request → 主体经 contextvar 同 task
# 传播（call_tool 直调不过线程池，T20 探针实测确认）。helper 放本模块是为避免
# routes.py ↔ mcp_server 循环导入（mcp_server 已函数内 import 本模块 _log_read）。
import contextvars as _contextvars

_mcp_principal_var: "_contextvars.ContextVar" = _contextvars.ContextVar(
    "mcp_principal", default=None)


def set_mcp_principal(principal) -> None:
    """TokenAuthMiddleware 认证成功路径调用：把 Principal 注入 contextvar。"""
    _mcp_principal_var.set(principal)


def get_mcp_principal():
    """MCP 工具读取当前请求主体；未注入（无认证上下文）→ None（fail-closed）。"""
    return _mcp_principal_var.get()


class GatewayReadRequest(BaseModel):
    kind: str = "semantic"
    query: str = ""
    target_agent_id: str = ""
    doc_id: str = ""
    n_results: int = 10
    required_level: str = ""


def _log_read(requester: str, principal, kind: str, query: str, target: str,
              granted_level: str, item_count: int, stripped: int,
              auth_mode_override: str = "") -> None:
    """读审计落链：gateway_read_log 表 + events 事件。同步执行（调用方保证非热路径）。

    auth_mode_override（CD-054 甲，T15）：无主体调用方（如 MCP 工具，分辨不出
    调用者身份）显式指定 auth_mode 落链；默认空串时沿用 principal.auth_mode，
    默认路径行为与旧版逐字一致。"""
    import sqlite3
    from models import CONFIG

    scope_json = json.dumps(principal.scope if principal else None, ensure_ascii=False)
    auth_mode = auth_mode_override or (getattr(principal, "auth_mode", "") if principal else "")
    try:
        conn = sqlite3.connect(CONFIG.DB_PATH)
        conn.execute(
            "INSERT INTO gateway_read_log (requester, auth_mode, scope_json, kind, query,"
            " target, granted_level, item_count, stripped_chunks)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (requester, auth_mode,
             scope_json, kind, query[:200], target[:200],
             granted_level, item_count, stripped),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # 读审计失败不阻塞读取（D4 可用性优先）


def _log_deny(requester: str, principal, kind: str, query: str = "",
              target: str = "", auth_mode_override: str = "") -> None:
    """读被拒留痕（T18 · CD-059）：403/404 拒绝落 gateway_read_log，
    granted_level="denied"、item_count=0、stripped_chunks=0；kind 沿用端点既有值。

    auth_mode 只记类别（hub_token/api_key/anonymous-tool/空串），严禁凭据明文；
    principal 兼容 Principal 对象或其 to_dict() 字典（TokenAuthMiddleware 注入
    request.scope["principal"] 的是 dict）。query/target 沿用 [:200] 截断。
    D4：审计失败不阻塞响应，但失败留 logger.warning 一行（含 kind/target/
    异常类型，不含凭据）——禁止静默 pass。"""
    import sqlite3
    from models import CONFIG

    try:
        if isinstance(principal, dict):
            scope = principal.get("scope")
            auth_mode = auth_mode_override or principal.get("auth_mode", "")
        else:
            scope = principal.scope if principal else None
            auth_mode = auth_mode_override or (
                getattr(principal, "auth_mode", "") if principal else "")
        conn = sqlite3.connect(CONFIG.DB_PATH)
        conn.execute(
            "INSERT INTO gateway_read_log (requester, auth_mode, scope_json, kind, query,"
            " target, granted_level, item_count, stripped_chunks)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (requester, auth_mode,
             json.dumps(scope, ensure_ascii=False), kind,
             (query or "")[:200], (target or "")[:200], "denied", 0, 0),
        )
        conn.commit()
        conn.close()
    except Exception as _exc:
        logger.warning("_log_deny 落行失败 kind=%s target=%s err=%s",
                       kind, str(target)[:80], type(_exc).__name__)


# 条目 id 字段优先级（memory_id=记忆/语义，chunk_id=doc 段落，
# entry_id=知识库，doc_id=共享文档/wiki 父档，id=兜底）
_ORIGIN_ID_KEYS = ("memory_id", "chunk_id", "entry_id", "doc_id", "id")


def _attach_origin(items) -> None:
    """P2 交付2：给读取结果条目附「真相源定位」origin 字段（git 路径 + commit hash）。

    读取仍走 SQLite（影子期）；origin 是可选新字段——data-trunk 未启用、
    条目未被影子镜像（历史存量）、或查询失败 → 不附加，存量请求完全兼容。
    任何异常静默（D4：读取主链路不依赖影子）。
    """
    try:
        dt = getattr(hub, "data_trunk", None)
        if dt is None or not getattr(dt, "enabled", False):
            return
        ids = []
        for it in items:
            if not isinstance(it, dict) or "origin" in it:
                continue
            for k in _ORIGIN_ID_KEYS:
                if it.get(k):
                    ids.append(str(it[k]))
                    break
        if not ids:
            return
        from hub_mixins.shadow import collect_origins
        origins = collect_origins(dt, getattr(hub, "_shadow", None), ids)
        if not origins:
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            for k in _ORIGIN_ID_KEYS:
                v = it.get(k)
                if v and str(v) in origins:
                    it["origin"] = origins[str(v)]
                    break
    except Exception as _exc:
        logger.debug("routes_gateway silent-except(_attach_origin): %s", _exc)


@router.post("/api/v1/gateway/read")
async def api_gateway_read(
    req: GatewayReadRequest,
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """统一网关读取。body: {kind, query?, target_agent_id?, doc_id?, n_results?, required_level?}"""
    if req.kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"kind 必须为 {'/'.join(_VALID_KINDS)}")
    scope = principal.scope if principal else None
    requester = current_agent

    # ── 语义检索（scope 剥离已由 disclose_for_principal 完成）──
    if req.kind == "semantic":
        if not req.query.strip():
            raise HTTPException(status_code=400, detail="query 必填")
        result = await hub.semantic_search(
            SemanticSearchRequest(
                query=req.query,
                requester_agent_id=requester,
                n_results=max(1, min(req.n_results or 10, 50)),
                filter_owner=req.target_agent_id or None,
            ),
            scope=scope,
        )
        memories = result.get("memories", [])
        granted = str(result.get("level", ""))
        _attach_origin(memories)
        _log_read(requester, principal, "semantic", req.query, req.target_agent_id,
                  granted, len(memories), 0)
        return {"status": "ok", "kind": "semantic", "requester": requester,
                "level": granted, "memories": memories, "degraded": result.get("degraded", False)}

    # ── 关键词检索（memory search + scope）──
    if req.kind == "memory":
        if not req.query.strip():
            raise HTTPException(status_code=400, detail="query 必填")
        from routes_memory import MemorySearchRequest

        target = req.target_agent_id or requester
        memories = await hub.memory_search(MemorySearchRequest(
            query=req.query, agent_id=target, top_k=max(1, min(req.n_results or 10, 50)),
        ))
        # scope 剥离：逐条走 disclose_for_principal（规则 1 自查 FULL 生效，与 semantic 同语义）
        kept = []
        items = memories.get("results") or [] if isinstance(memories, dict) else (memories or [])
        for m in items:
            allowed = hub.disclosure.disclose_for_principal(
                memory=m, requester=requester, task={},
                required_level=DisclosureLevel.SUMMARY, scope=scope,
            )
            item_rank = _rank(m.get("disclosure_level") or "metadata")
            if _rank(allowed) >= item_rank:
                kept.append(m)
        stripped = max(0, len(items) - len(kept))
        _attach_origin(kept)
        _log_read(requester, principal, "memory", req.query, target, "", len(kept), stripped)
        return {"status": "ok", "kind": "memory", "requester": requester,
                "memories": kept, "stripped": stripped}

    # ── 文档段落读取（chunk 级段落剥离）──
    if req.kind == "doc":
        if not req.doc_id:
            raise HTTPException(status_code=400, detail="doc_id 必填")
        import sqlite3
        from models import CONFIG

        conn = sqlite3.connect(CONFIG.DB_PATH)
        conn.row_factory = sqlite3.Row
        chunks = conn.execute(
            "SELECT * FROM document_chunks WHERE parent_doc_id = ? ORDER BY piece_index", (req.doc_id,),
        ).fetchall()
        conn.close()
        if not chunks:
            _log_deny(requester, principal, "doc", req.query, req.doc_id)
            raise HTTPException(status_code=404, detail="文档不存在或无分块")

        # requester 允许级别：disclose_for_principal 判定（用文档首 chunk 代表）
        first = dict(chunks[0])
        mem = {
            "owner_agent_id": first.get("source_agent_id") or requester,
            "disclosure_level": first.get("disclosure_level") or "summary",
            "department": first.get("department", ""),
            "owner_level": first.get("owner_level", ""),
            "allowed_viewers": first.get("allowed_viewers", ""),
        }
        allowed = hub.disclosure.disclose_for_principal(
            memory=mem, requester=requester, task={},
            required_level=req.required_level or DisclosureLevel.SUMMARY,
            scope=scope,
        )
        allow_rank = _rank(allowed)
        kept = []
        stripped = 0
        for ch in chunks:
            ch_d = dict(ch)
            if _rank(ch_d.get("disclosure_level") or "none") <= allow_rank:
                kept.append({"chunk_id": ch_d["chunk_id"], "piece_index": ch_d["piece_index"],
                             "content": ch_d.get("content", ""), "disclosure_level": ch_d.get("disclosure_level")})
            else:
                stripped += 1
        _attach_origin(kept)
        _log_read(requester, principal, "doc", req.query, req.doc_id, str(allowed), len(kept), stripped)
        return {"status": "ok", "kind": "doc", "requester": requester, "doc_id": req.doc_id,
                "allowed_level": str(allowed), "chunks": kept, "total_chunks": len(chunks),
                "stripped_chunks": stripped}


def _rank(level: str) -> int:
    return {"none": 0, "metadata": 1, "summary": 2, "full": 3}.get((level or "").lower(), 0)
