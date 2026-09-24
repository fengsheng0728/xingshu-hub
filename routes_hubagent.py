"""星枢 Sync Hub — Hub Agent（LLM 披露审计引擎）API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import asyncio, json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from deps import DisclosureRules, HubAgentConfig
from models import CONFIG
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
    get_current_principal, principal_is_privileged, require_role,
)

router = APIRouter()

@router.post("/api/v1/hub-agent/configure")
async def api_hub_agent_configure(cfg: HubAgentConfig,
                                  current_agent: str = Depends(get_current_agent)):
    """配置 LLM provider（仅 manager/orchestrator）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH,
                 roles=("manager", "orchestrator"),
                 detail="仅主管/店长可配置 Hub Agent")
    return hub_agent.configure(cfg)


@router.post("/api/v1/hub-agent/disclosure-rules")
async def api_hub_agent_set_rules(rules: DisclosureRules,
                                  current_agent: str = Depends(get_current_agent)):
    """设置披露审计规则（仅 manager/orchestrator）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH,
                 roles=("manager", "orchestrator"),
                 detail="仅主管/店长可设置披露审计规则")
    return hub_agent.set_disclosure_rules(rules)


@router.get("/api/v1/hub-agent/config")
async def api_hub_agent_get_config(raw: bool = False,
                                   current_agent: str = Depends(get_current_agent),
                                   principal=Depends(get_current_principal)):
    """获取当前配置（默认脱敏 api_key；?raw=true 仅特权主体返回原始值，非特权仍得脱敏版）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH,
                 roles=("manager", "orchestrator"),
                 detail="仅主管/店长可查看 Hub Agent 配置")
    cfg = hub_agent._get_config()
    if not (raw and principal_is_privileged(principal)) and cfg.get("api_key"):
        cfg["api_key"] = cfg["api_key"][:8] + "..." + cfg["api_key"][-4:] if len(cfg["api_key"]) > 12 else "***"
    return cfg


@router.post("/api/v1/hub-agent/test")
async def api_hub_agent_test(current_agent: str = Depends(get_current_agent)):
    """测试 LLM 连通性（仅 manager/orchestrator）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH,
                 roles=("manager", "orchestrator"),
                 detail="仅主管/店长可测试 Hub Agent 连通性")
    return await hub_agent.test_connection()


@router.post("/api/v1/hub-agent/audit/{request_id}")
async def api_hub_agent_audit(request_id: str,
                              current_agent: str = Depends(get_current_agent)):
    """手动对指定披露请求执行 LLM 审计（仅 manager/orchestrator）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH,
                 roles=("manager", "orchestrator"),
                 detail="仅主管/店长可触发 Hub Agent 审计")
    conn = sqlite3.connect(CONFIG.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM disclosure_requests WHERE request_id = ?", (request_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="请求不存在")
    req = dict(row)
    return await hub_agent.audit_disclosure(
        task_id=req["task_id"],
        agent_id=req["agent_id"],
        reason=req.get("reason", ""),
    )


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


@router.post("/api/v1/hub-agent/chat")
async def api_hub_agent_chat(req: ChatRequest,
                             current_agent: str = Depends(get_current_agent)):
    """Hub Agent 对话（仅 manager/orchestrator）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH,
                 roles=("manager", "orchestrator"),
                 detail="仅主管/店长可使用 Hub Agent 对话")
    msg = req.message
    session_id = req.session_id
    if not msg:
        raise HTTPException(status_code=400, detail="缺少 message")
    if not hasattr(hub_agent, "_lc_chain"):
        # D-5 3-5a: langchain 缺失（未装向量栈）→ 明确降级响应，不抛 500 堆栈
        try:
            from hub_agent_lc import create_hub_agent, HubTools, SQLiteChatHistory
            from langchain_openai import ChatOpenAI
        except ImportError:
            return {"status": "degraded",
                    "error": "LLM 栈未安装（langchain 缺失），请 pip install -r requirements-vector.txt"}
        cfg = hub_agent._get_config()
        if not cfg.get("api_key"):
            return {"status": "error", "error": "Hub Agent 未配置 LLM"}
        llm = ChatOpenAI(
            model=cfg.get("model", "deepseek-chat"),
            api_key=cfg["api_key"],
            base_url=cfg.get("api_base") or "https://api.deepseek.com/v1",
            temperature=float(cfg.get("temperature", "0.3")),
        )
        tools = HubTools(hub, CONFIG.DB_PATH)
        tools_list = [
            tools.query_memories, tools.query_knowledge, tools.list_agents,
            tools.list_tasks, tools.approve_disclosure, tools.deny_disclosure,
            tools.create_task, tools.get_stats,
        ]
        from hub_agent import _get_company_name
        company_name = _get_company_name()
        hub_agent._lc_chain, hub_agent._tool_map = create_hub_agent(llm, tools_list, CONFIG.DB_PATH, company_name)
    try:
        from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
        from hub_agent_lc import SQLiteChatHistory
        history = SQLiteChatHistory(session_id, CONFIG.DB_PATH)
        old_count = len(history.messages)
        # Build messages: history + new user message
        messages = list(history.messages) + [HumanMessage(content=msg)]

        # Agent loop: invoke → execute tools → repeat (max 5 rounds)
        max_rounds = 5
        for round_num in range(max_rounds):
            # 第一轮用原始 msg 作 input，后续轮用增量上下文
            if round_num == 0:
                chat_history = list(history.messages)
                user_input = msg
            else:
                chat_history = messages[:]
                user_input = "继续处理"

            result = await asyncio.to_thread(
                hub_agent._lc_chain.invoke,
                {"chat_history": chat_history, "input": user_input}
            )
            messages.append(result)
            if not hasattr(result, 'tool_calls') or not result.tool_calls:
                break
            for tc in result.tool_calls:
                tool = hub_agent._tool_map.get(tc['name'])
                if tool:
                    try:
                        tool_out = await asyncio.to_thread(tool.invoke, tc['args'])
                    except Exception as e:
                        tool_out = f"工具错误: {e}"
                else:
                    tool_out = f"未知工具: {tc['name']}"
                messages.append(ToolMessage(content=str(tool_out), tool_call_id=tc['id']))

        # Only save new messages (avoid clear+re-add data loss risk)
        new_messages = messages[old_count:]
        if new_messages:
            history.add_messages(new_messages)

        final = messages[-1]
        reply = final.content if hasattr(final, 'content') and final.content else "(已完成)"
        return {"status": "ok", "reply": reply}
    except ImportError:
        # D-5 3-5a: langchain 缺失（未装向量栈）→ 明确降级响应
        return {"status": "degraded",
                "error": "LLM 栈未安装（langchain 缺失），请 pip install -r requirements-vector.txt"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/api/v1/hub-agent/chat/clear")
async def api_hub_agent_clear_history(req: dict,
                                       current_agent: str = Depends(get_current_agent)):
    """清空对话历史（仅 manager/orchestrator）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH,
                 roles=("manager", "orchestrator"),
                 detail="仅主管/店长可清空 Hub Agent 对话历史")
    from routes_n1 import _n1_gate
    _gate = await _n1_gate(current_agent, "chat_clear",
                           {"session_id": req.get("session_id", "default")})
    if _gate:
        return _gate
    # D-5 3-5a: langchain 缺失（未装向量栈）→ 明确降级响应，不抛 500 堆栈
    try:
        from hub_agent_lc import SQLiteChatHistory
    except ImportError:
        return {"status": "degraded",
                "error": "LLM 栈未安装（langchain 缺失），请 pip install -r requirements-vector.txt"}
    SQLiteChatHistory(req.get("session_id", "default"), CONFIG.DB_PATH).clear()
    return {"status": "ok"}


@router.get("/api/v1/hub-agent/chat/history")
async def api_hub_agent_chat_history(session_id: str = "default",
                                     current_agent: str = Depends(get_current_agent)):
    """获取对话历史（仅 manager/orchestrator）"""
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH,
                 roles=("manager", "orchestrator"),
                 detail="仅主管/店长可查看 Hub Agent 对话历史")
    # D-5 3-5a: langchain 缺失（未装向量栈）→ 明确降级响应，不抛 500 堆栈
    try:
        from hub_agent_lc import SQLiteChatHistory
    except ImportError:
        return {"status": "degraded",
                "error": "LLM 栈未安装（langchain 缺失），请 pip install -r requirements-vector.txt"}
    history = SQLiteChatHistory(session_id, CONFIG.DB_PATH)
    msgs = [{"role": "user" if m.type == "human" else "agent", "content": m.content} for m in history.messages]
    return {"status": "ok", "messages": msgs}


