"""星枢 Sync Hub — 控制台/工作台数据 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
from fastapi import APIRouter, Depends

from hub_core import hub
from routes_common import get_current_agent, get_current_agent_optional

router = APIRouter()


@router.get("/api/v1/dashboard")
async def api_dashboard(current_agent: str = Depends(get_current_agent_optional)):
    """监控数据接口，按请求者角色过滤可见范围（可选认证）"""
    return await hub.get_dashboard_data(current_agent)


@router.get("/api/v1/agent/workspace")
async def api_agent_workspace(current_agent: str = Depends(get_current_agent)):
    """Agent 端工作台：返回当前 Agent 的任务、通知、团队摘要"""
    return await hub.get_agent_workspace(current_agent)
