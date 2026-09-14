"""星枢 Sync Hub — 任务调度 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import asyncio, json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from deps import TaskCreate
from models import CONFIG
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
)

router = APIRouter()

@router.get("/api/v1/tasks")
async def api_list_tasks(
    status: str = "all",
    current_agent: str = Depends(get_current_agent),
):
    """任务列表（P1: 附带 blocked_by 计算字段）。status: all|pending|assigned|in_progress|completed|cancelled"""
    conn = hub._db()
    try:
        if status and status != "all":
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC LIMIT 200",
                (status,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 200").fetchall()
        # P2: 一次聚合所有子任务分布(父任务附 subtask_summary, 看板分组用)
        sub_agg = {}
        for sr in conn.execute(
                "SELECT parent_task_id, status FROM tasks WHERE parent_task_id IS NOT NULL").fetchall():
            pid = sr["parent_task_id"]
            d = sub_agg.setdefault(pid, {"total": 0, "completed": 0})
            d["total"] += 1
            if sr["status"] == "completed":
                d["completed"] += 1
    finally:
        conn.close()
    tasks = []
    for row in rows:
        t = dict(row)
        deps = json.loads(t.get("depends_on") or "[]")
        t["blocked_by"] = hub._blocked_by(deps)
        if t["task_id"] in sub_agg:
            t["subtask_summary"] = sub_agg[t["task_id"]]
        tasks.append(t)
    return {"tasks": tasks}


@router.post("/api/v1/tasks/create")
async def api_create_task(
    task: TaskCreate,
    current_agent: str = Depends(get_current_agent),
):
    if not NO_AUTH and task.creator_agent_id and current_agent != task.creator_agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份创建 {task.creator_agent_id} 的任务")
    return await hub.create_task(task)


@router.post("/api/v1/tasks/{task_id}/schedule")
async def api_schedule_task(
    task_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """调度任务，匹配 Agent，执行第一阶段渐进披露"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        role = info.get("role", "worker")
        if role not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403,
                detail=f"Forbidden: {current_agent} 角色 {role} 无权执行此操作")
    return await hub.schedule_task(task_id)


@router.post("/api/v1/tasks/{task_id}/advance")
async def api_advance_disclosure(
    task_id: str,
    agent_id: str,
    reason: str = "",
    current_agent: str = Depends(get_current_agent),
):
    """
    Agent 请求提升披露级别。

    场景：任务执行中信息不足 → 申请升级 → 获得更多上下文
    """
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    return await hub.advance_disclosure(task_id, agent_id, reason)


@router.post("/api/v1/tasks/{task_id}/start")
async def api_start_task(
    task_id: str,
    agent_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """开始执行任务: assigned → in_progress"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    return await hub.start_task(task_id, agent_id)


@router.post("/api/v1/tasks/{task_id}/complete")
async def api_complete_task(
    task_id: str,
    agent_id: str,
    result: str = "",
    current_agent: str = Depends(get_current_agent),
):
    """完成任务: in_progress → completed"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    return await hub.complete_task(task_id, agent_id, result)


@router.post("/api/v1/tasks/{task_id}/fail")
async def api_fail_task(
    task_id: str,
    agent_id: str,
    reason: str = "",
    current_agent: str = Depends(get_current_agent),
):
    """任务失败: in_progress → failed"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    return await hub.fail_task(task_id, agent_id, reason)


@router.post("/api/v1/tasks/{task_id}/cancel")
async def api_cancel_task(
    task_id: str,
    agent_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """取消任务: 任意非终态 → cancelled"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    return await hub.cancel_task(task_id, agent_id)


@router.post("/api/v1/tasks/{task_id}/update")
async def api_update_task(
    task_id: str,
    description: str,
    depends_on: Optional[str] = None,
    current_agent: str = Depends(get_current_agent),
):
    """更新任务描述/依赖（看板编辑；P1: depends_on 可选 JSON 数组字符串，如 "[taskA]"）"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        role = info.get("role", "worker")
        if role not in ("manager", "orchestrator", "worker"):
            raise HTTPException(status_code=403,
                detail=f"Forbidden: {current_agent} 角色 {role} 无权执行此操作")
    deps = None
    if depends_on is not None:
        try:
            deps = json.loads(depends_on)
            if not isinstance(deps, list):
                raise ValueError("depends_on 须为 JSON 数组")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"depends_on 格式错误: {e}")
    result = await hub.update_task(task_id, description, current_agent, depends_on=deps)
    return result


@router.get("/api/v1/tasks/{task_id}/subtasks")
async def api_task_subtasks(
    task_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """P2: 子任务列表 + 完成聚合（父任务拆解视图）"""
    return await hub.get_subtasks(task_id)


