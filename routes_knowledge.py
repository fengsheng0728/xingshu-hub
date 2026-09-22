"""星枢 Sync Hub — 企业知识库 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
from fastapi import APIRouter, Depends, HTTPException, Request

from models import KnowledgeEntry
from hub_core import hub, hub_agent
from routes_common import (
    NO_AUTH, get_current_agent, get_current_principal, principal_is_privileged,
)
from routes_common import require_role  # CD-074（hub_token 放行的 canonical 角色门）
from routes_gateway import _log_read, _log_deny  # CD-052: knowledge 读端点复用网关读审计 helper；CD-059(T18): 拒绝留痕

router = APIRouter()


# ============ CD-052 知识读出口最小披露（方案 A） ============

def _kb_level_for(created_by: str, requester: str, principal) -> str:
    """条目级定级：created_by == requester（r1 自己）→ full；
    principal_is_privileged（hub_token / manager / orchestrator）→ full；
    其余（含主体不可判定）→ summary（fail-closed，前 200 字）。
    零权限语义放大：不新增/放宽任何门，身份门与错误码原样。"""
    if requester and created_by and created_by == requester:
        return "full"
    if principal_is_privileged(principal):
        return "full"
    return "summary"


def _kb_strip_entry(entry: dict, level: str) -> dict:
    """按实际级别剥离正文并挂 level 标记（不改动任何既有键）。

    summary → 前 200 字（CD-033A 定档精度）；doc: 前缀条目在 KB 里本就是
    ≤300 字摘要（hub_mixins/knowledge._upsert_doc_entry）→ 摘要级原样返回。"""
    entry["level"] = level
    if level == "summary":
        if str(entry.get("entry_id") or "").startswith("doc:"):
            return entry
        content = entry.get("content") or ""
        entry["content"] = content[:200] + "..." if len(content) > 200 else content
    return entry


# ============ 企业知识库智能工具 API ============

@router.post("/api/v1/knowledge/auto-complete")
async def api_knowledge_auto_complete(
    req: dict,
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """AI 自动补全知识条目（仅 manager/orchestrator —— CD-054 T19 费用/可用性面收编）"""
    if not NO_AUTH:
        try:
            # CD-074：canonical 角色门（hub_token 放行）；本端点另有读审计拒绝留痕
            require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH, detail="仅主管/店长可使用知识自动补全")
        except HTTPException:
            _log_deny(current_agent, principal, "knowledge", "", "auto-complete")
            raise
    title = req.get("title", "")
    if not title:
        raise HTTPException(status_code=400, detail="缺少标题")
    return await hub_agent.auto_complete_knowledge(title)


@router.get("/api/v1/knowledge/from-memories")
async def api_knowledge_from_memories(
    limit: int = 10,
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """从记忆池提取高频主题建议（CD-054 T19：按主体过滤 —— 特权→全员统计，
    非特权→只统计 owner_agent_id == current_agent 的记忆，无主体→fail-closed
    空 suggestions）+ 读审计"""
    privileged = principal_is_privileged(principal)
    result = await hub_agent.extract_knowledge_from_memories(
        limit, requester=current_agent, privileged=privileged)
    suggestions = result.get("suggestions") or []
    level = "full" if privileged else "summary"
    result["level"] = level  # 键只加不删
    _log_read(current_agent, principal, "knowledge", "", "from-memories",
              level, len(suggestions), 0 if privileged else len(suggestions))
    return result


# ============ 企业知识库 API ============

@router.post("/api/v1/knowledge")
async def api_knowledge_upsert(
    entry: KnowledgeEntry,
    current_agent: str = Depends(get_current_agent),
):
    """创建/更新知识条目（仅 orchestrator 可写）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH, roles=("manager", "orchestrator",), detail="仅主管/店长可编辑知识库")
    entry.created_by = current_agent or entry.created_by
    return await hub.knowledge_upsert(entry)


@router.get("/api/v1/knowledge")
async def api_knowledge_list(
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """获取全部知识条目（CD-052：按主体逐条定级剥离，summary=前 200 字）"""
    result = await hub.knowledge_get()
    entries = result.get("entries") or []
    stripped = 0
    out = []
    for e in entries:
        lv = _kb_level_for(e.get("created_by") or "", current_agent, principal)
        if lv == "summary":
            stripped += 1
        out.append(_kb_strip_entry(e, lv))
    result["entries"] = out
    try:
        _log_read(current_agent, principal, "knowledge", "", "",
                  "full" if stripped == 0 else "summary",
                  len(out), stripped)
    except Exception:
        pass  # 读审计失败不阻塞读取（D4）
    return result


@router.get("/api/v1/knowledge/{entry_id}")
async def api_knowledge_get(
    entry_id: str,
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """获取单个知识条目（CD-052：按主体定级剥离，summary=前 200 字）"""
    try:
        result = await hub.knowledge_get(entry_id)
    except KeyError:
        _log_deny(current_agent, principal, "knowledge", "", entry_id)
        raise HTTPException(status_code=404, detail="知识条目不存在")
    entry = result.get("entry") or {}
    lv = _kb_level_for(entry.get("created_by") or "", current_agent, principal)
    result["entry"] = _kb_strip_entry(entry, lv)
    try:
        _log_read(current_agent, principal, "knowledge", "", entry_id,
                  lv, 1, 1 if lv == "summary" else 0)
    except Exception:
        pass  # 读审计失败不阻塞读取（D4）
    return result


@router.delete("/api/v1/knowledge/{entry_id}")
async def api_knowledge_delete(
    entry_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """删除知识条目（仅 orchestrator）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH, roles=("orchestrator",), detail="仅店长可删除")
    from routes_n1 import _n1_gate
    _gate = await _n1_gate(current_agent, "knowledge", {"entry_id": entry_id})
    if _gate:
        return _gate
    return await hub.knowledge_delete(entry_id)


@router.post("/api/v1/knowledge/reindex")
async def api_knowledge_reindex(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """CD-051: 知识向量对账（手动/巡检触发，与启动对账同一套 reconcile 逻辑）

    body: {"entry_id": str（可选；缺省全量逐条对账）}
    权限：与 knowledge_upsert 同门 — manager/orchestrator 角色或 hub_token；
    worker → 403（T15 principal_is_privileged 同款判定，fail-closed）。
    返回: checked/ok/reparsed/removed/skipped/errors/duration_ms；
    单条失败进 errors 列表，整体不 500。
    """
    if not NO_AUTH and not principal_is_privileged(request.scope.get("principal")):
        # CD-059(T18): 403 拒绝落读审计 denied 行；principal 在此为
        # TokenAuthMiddleware 注入的 dict，_log_deny 兼容取类别 auth_mode；
        # body 尚未解析，target 记空串
        _log_deny(current_agent, request.scope.get("principal"), "knowledge", "", "")
        raise HTTPException(
            status_code=403,
            detail="需要 manager/orchestrator 角色或 hub_token 才能触发知识向量对账",
        )
    try:
        data = await request.json()
    except Exception:
        data = {}
    entry_id = (data.get("entry_id") or "") if isinstance(data, dict) else ""
    return await hub.reconcile_kb_vectors(entry_id=entry_id)


@router.get("/api/v1/knowledge/graph/data")
async def api_knowledge_graph(
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """知识图谱数据（节点 + 边）。N4: 按 requester 权限过滤（NONE 级节点隐藏）。
    CD-054(T19)：fail-closed + 回填过滤 + 已发布对齐（见 knowledge_graph），补读审计。"""
    result = await hub.knowledge_graph(requester=current_agent)
    nodes = result.get("nodes") or []
    _log_read(current_agent, principal, "knowledge", "", "graph",
              "full" if not current_agent else "summary",
              len(nodes), int(result.get("hidden") or 0))
    return result
