"""星枢 Sync Hub — 集成层 API（§七 门框）

连接器列表/配置/测试连接/手动拉取/webhook 入站/出站拦截。
- 管理端点（configure/delete/outbound）：manager/orchestrator 角色门
- webhook：scoped key 鉴权（scope.endpoints 前缀匹配 /api/v1/integrations/{name}/webhook）
- 集成数据一律走附录 E 管道，trust_level="external"（taint）
"""
from fastapi import APIRouter, Depends, HTTPException

from hub_core import hub
from routes_common import NO_AUTH, get_current_agent
from integrations.registry import ConnectorRegistry
from integrations.base import HubEvent, config_fingerprint

router = APIRouter()

# 模块级注册表（目录扫描发现连接器；hub._db 为连接工厂）
registry = ConnectorRegistry(hub._db)


def _require_manager(current_agent: str):
    """与 /api/v1/keys 一致的角色门：仅 manager/orchestrator"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可管理集成")


@router.get("/api/v1/integrations")
async def api_integrations_list(current_agent: str = Depends(get_current_agent)):
    """⑩页列表：连接器（名称/状态/最近同步/记录数），配置掩码出参"""
    return {"status": "ok", "connectors": registry.list_status()}


@router.post("/api/v1/integrations/{name}/configure")
async def api_integrations_configure(name: str, body: dict,
                                     current_agent: str = Depends(get_current_agent)):
    """配置连接器（密钥字段 AES-GCM 加密落库；掩码 *** 保留旧值）"""
    _require_manager(current_agent)
    try:
        r = registry.configure(
            name,
            cfg=body.get("config") or {},
            field_mapping=body.get("field_mapping"),
            enabled=body.get("enabled", True),
            pull_interval_min=(int(body["pull_interval_min"])
                               if body.get("pull_interval_min") is not None else None))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await hub._log_event("integration_configured", current_agent, {
        "connector": name, "config_fp": config_fingerprint(body.get("config") or {}),
        "enabled": r["enabled"]})
    return r


@router.delete("/api/v1/integrations/{name}")
async def api_integrations_disable(name: str,
                                   current_agent: str = Depends(get_current_agent)):
    from routes_n1 import _n1_gate
    _gate = await _n1_gate(current_agent, "integrations", {"name": name})
    if _gate:
        return _gate
    """禁用连接器（保留配置与历史状态）"""
    _require_manager(current_agent)
    row = registry._get_row(name)
    if not row:
        raise HTTPException(status_code=404, detail="连接器未配置")
    with hub._db() as conn:
        c = conn.cursor()
        c.execute("UPDATE integrations_state SET enabled = 0, updated_at = datetime('now') WHERE name = ?",
                  (name,))
        conn.commit()
    registry._configured.pop(name, None)
    await hub._log_event("integration_disabled", current_agent, {"connector": name})
    return {"status": "disabled", "name": name}


@router.post("/api/v1/integrations/{name}/test")
async def api_integrations_test(name: str,
                                current_agent: str = Depends(get_current_agent)):
    """测试连接（⑩页按钮）"""
    return registry.test_connection(name)


@router.post("/api/v1/integrations/{name}/pull")
async def api_integrations_pull(name: str, body: dict = None,
                                current_agent: str = Depends(get_current_agent)):
    """手动拉取（body.full=true 全量）。结果走附录 E 管道。"""
    try:
        return await registry.run_pull(hub, name, full=bool((body or {}).get("full")))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/v1/integrations/{name}/webhook")
async def api_integrations_webhook(name: str, payload: dict,
                                   current_agent: str = Depends(get_current_agent)):
    """入站 webhook。scoped key 用 endpoints 前缀限定到本路径即完成授权。"""
    try:
        return await registry.run_webhook(hub, name, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/v1/integrations/{name}/outbound")
async def api_integrations_outbound(name: str, body: dict,
                                    current_agent: str = Depends(get_current_agent)):
    """出站（§7.4）：写外部系统 = 高危 → 审批门拦截 + 审计（门框期不真正下发）"""
    _require_manager(current_agent)
    event = HubEvent(event_type=body.get("event_type", ""),
                     payload=body.get("payload") or {})
    return await registry.run_outbound(hub, name, event, current_agent)


@router.get("/api/v1/integrations/meta/available")
async def api_integrations_available(current_agent: str = Depends(get_current_agent)):
    """已发现的连接器类型（含未配置）— "添加连接器"向导数据源"""
    return {"status": "ok", "available": registry.available()}
