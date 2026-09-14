"""星枢 Sync Hub — 企业知识库 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
from fastapi import APIRouter, Depends, HTTPException

from models import KnowledgeEntry
from hub_core import hub, hub_agent
from routes_common import NO_AUTH, get_current_agent

router = APIRouter()


# ============ 企业知识库智能工具 API ============

@router.post("/api/v1/knowledge/auto-complete")
async def api_knowledge_auto_complete(req: dict):
    """AI 自动补全知识条目"""
    title = req.get("title", "")
    if not title:
        raise HTTPException(status_code=400, detail="缺少标题")
    return await hub_agent.auto_complete_knowledge(title)


@router.get("/api/v1/knowledge/from-memories")
async def api_knowledge_from_memories(limit: int = 10):
    """从记忆池提取高频主题建议"""
    return await hub_agent.extract_knowledge_from_memories(limit)


# ============ 企业知识库 API ============

@router.post("/api/v1/knowledge")
async def api_knowledge_upsert(
    entry: KnowledgeEntry,
    current_agent: str = Depends(get_current_agent),
):
    """创建/更新知识条目（仅 orchestrator 可写）"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可编辑知识库")
    entry.created_by = current_agent or entry.created_by
    return await hub.knowledge_upsert(entry)


@router.get("/api/v1/knowledge")
async def api_knowledge_list(current_agent: str = Depends(get_current_agent)):
    """获取全部知识条目"""
    return await hub.knowledge_get()


@router.get("/api/v1/knowledge/{entry_id}")
async def api_knowledge_get(
    entry_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """获取单个知识条目"""
    try:
        return await hub.knowledge_get(entry_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="知识条目不存在")


@router.delete("/api/v1/knowledge/{entry_id}")
async def api_knowledge_delete(
    entry_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """删除知识条目（仅 orchestrator）"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") != "orchestrator":
            raise HTTPException(status_code=403, detail="仅店长可删除")
    from routes_n1 import _n1_gate
    _gate = await _n1_gate(current_agent, "knowledge", {"entry_id": entry_id})
    if _gate:
        return _gate
    return await hub.knowledge_delete(entry_id)


@router.get("/api/v1/knowledge/graph/data")
async def api_knowledge_graph(current_agent: str = Depends(get_current_agent)):
    """知识图谱数据（节点 + 边）。N4: 按 requester 权限过滤（NONE 级节点隐藏）。"""
    return await hub.knowledge_graph(requester=current_agent)
