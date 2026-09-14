"""星枢 Sync Hub — 知识加工管线（chunks/embeddings/sensitivity/entities）API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
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

@router.post("/api/v1/chunks/search")
async def api_chunks_search(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """检索文档 chunk（披露过滤 + 防拼接滑窗）

    body: {"query": str, "doc_id": str(可选), "limit": int(默认10)}
    返回: results(chunk_id/piece_index/disclosure_level/content) + parent_hint(布尔) + degraded
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    query = data.get("query", "") or ""
    doc_id = data.get("doc_id", "") or ""
    limit = int(data.get("limit", 10) or 10)
    if limit < 1 or limit > 100:
        limit = 10
    from disclosure import DisclosureEngine
    engine = DisclosureEngine(hub)
    return await engine.search_chunks(
        requester=current_agent, query=query, doc_id=doc_id, limit=limit,
    )


@router.post("/api/v1/chunks/ingest")
async def api_chunks_ingest(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """数据汇入：切割 → 敏感度打标 → 幂等去重 → 落库 → 三路分流

    body: {"doc_id": str, "content": str, "kind": "fact"(可选),
          "source_agent_id": str(可选), "trust_level": str(可选)}
    返回: chunks/inserted/skipped_hash/locked_none/locked/disclosure_level
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    doc_id = data.get("doc_id", "") or ""
    content = data.get("content", "") or ""
    if not doc_id or not content:
        raise HTTPException(400, "doc_id 和 content 必填")
    kind = data.get("kind", "fact") or "fact"
    source_agent_id = data.get("source_agent_id", "") or current_agent
    trust_level = data.get("trust_level", "") or "trusted"
    owner_role = (hub.agents.get(current_agent, {}) or {}).get("role", "worker")
    return await hub.ingest_chunks(
        doc_id=doc_id, content=content, source_agent_id=source_agent_id,
        kind=kind, trust_level=trust_level, owner_role=owner_role,
    )


@router.post("/api/v1/chunks/reclassify")
async def api_chunks_reclassify(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """E.7 级别变更重判定：重跑分类器，级别变化入审计 + 图谱/wiki 同步

    body: {"doc_id": str(可选，空=全量重判)}
    返回: changed + details
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    doc_id = data.get("doc_id", "") or ""
    return await hub.reclassify_chunks(doc_id=doc_id, requester=current_agent)


@router.post("/api/v1/embeddings/rebuild")
async def api_embeddings_rebuild(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """K1b 旧向量作废 + 全量重建（换模型后调用，E.7 同款批处理）

    body: {"batch_size": int(可选, 默认100)}
    返回: provider/target_dim/rebuilt_mem/stale_total
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    batch_size = int(data.get("batch_size", 100) or 100)
    return await hub.rebuild_embeddings(requester=current_agent, batch_size=batch_size)


@router.post("/api/v1/embeddings/calibrate")
async def api_embeddings_calibrate(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """K1c cos 阈值标定（换 embedding 模型后重新测量切点阈值）

    body: {"samples": [str, ...]} — 至少 2 段代表语料
    返回: p10/p25/p50/suggested（建议阈值 = P25）
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    samples = data.get("samples") or []
    if len(samples) < 2:
        raise HTTPException(400, "至少提供 2 段代表语料用于标定")
    from db import get_embedding_provider
    from chunker import calibrate_cos_threshold
    provider = CONFIG.EMBEDDING_PROVIDER or "hasher"
    try:
        if provider == "sentence":
            model = get_embedding_provider("sentence", model_path=CONFIG.EMBEDDING_MODEL_PATH)
        else:
            model = get_embedding_provider("hasher", n_features=384)
    except Exception as e:
        raise HTTPException(500, f"模型加载失败: {e}")
    result = calibrate_cos_threshold(model, samples)
    result["provider"] = provider
    return result


@router.get("/api/v1/sensitivity/words")
async def api_sensitivity_words(
    current_agent: str = Depends(get_current_agent),
):
    """查看当前生效的机密词库（仅 manager/orchestrator）

    返回: {"words": [...], "source": "dir|file|default", "count": N}
    """
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可查看机密词库")
    from sensitivity import _load_secret_keywords, _SECRET_WORDS_DIR, _SECRET_WORDS_FILE
    words = _load_secret_keywords()
    source = "dir" if (_SECRET_WORDS_DIR and os.path.isdir(_SECRET_WORDS_DIR)) else (
        "file" if (_SECRET_WORDS_FILE and os.path.exists(_SECRET_WORDS_FILE)) else "default")
    await hub._log_event("sensitivity_words_viewed", current_agent, {"count": len(words), "source": source})
    return {"words": words, "source": source, "count": len(words)}


@router.post("/api/v1/sensitivity/words/reload")
async def api_sensitivity_words_reload(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """词库更新后重载 + 触发 E.7 重判定（仅 manager/orchestrator）

    body: {"reclassify": bool(可选, 默认true)} — 重载后是否全量重判定存量 chunk
    返回: {"words": N, "source": "...", "reclassified": {changed, ...} | "skipped"}
    """
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可更新机密词库")
    try:
        data = await request.json()
    except Exception:
        data = {}
    import importlib
    import sensitivity as _sens_mod
    importlib.reload(_sens_mod)  # 重新读取 env/文件（词库文件更新后生效）
    words = _sens_mod._load_secret_keywords()
    await hub._log_event("sensitivity_words_reloaded", current_agent, {"count": len(words)})
    result = {"words": len(words), "source": "reloaded"}
    if data.get("reclassify", True):
        rc = await hub.reclassify_chunks(requester=current_agent)  # 全量重判定
        result["reclassified"] = rc
    else:
        result["reclassified"] = "skipped"
    return result


@router.get("/api/v1/entities/review")
async def api_entity_review_list(
    status: str = "pending",
    current_agent: str = Depends(get_current_agent),
):
    """实体审查队列列表（查看需 manager/orchestrator——实体本身是敏感情报）

    query: ?status=pending|approved|rejected|（空=全部）
    """
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可查看实体审查队列")
    return await hub.list_entity_reviews(status=status or "")


@router.post("/api/v1/entities/review/{review_id}")
async def api_entity_review_decision(
    review_id: int,
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """审查放行/拒绝实体（仅 manager/orchestrator）

    body: {"decision": "approved"|"rejected"}
    approved → 进 knowledge_base（图谱节点，N4 披露过滤自动生效）
    """
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可审查实体")
    try:
        data = await request.json()
    except Exception:
        data = {}
    decision = data.get("decision", "")
    if decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision 必须为 approved 或 rejected")
    return await hub.review_entity(review_id, decision, reviewer=current_agent)


