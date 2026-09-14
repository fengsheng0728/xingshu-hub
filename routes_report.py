"""星枢 Sync Hub — 日报 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_report")

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

@router.get("/api/v1/report/daily")
async def api_daily_report():
    """生成日报：今日统计 + LLM 摘要"""
    stats = await asyncio.to_thread(_daily_report_stats_sync)

    today = stats["date"]
    task_stats = stats["tasks"]["by_status"]
    tasks_done = stats["tasks"]["done"]
    tasks_failed = stats["tasks"]["failed"]
    tasks_active = stats["tasks"]["active"]
    mems_today = stats["memories"]
    disc_today = stats["disclosures"]["total"]
    disc_pending = stats["disclosures"]["pending"]
    online = stats["agents"]["online"]
    total = stats["agents"]["total"]
    top_tags = [(t["tag"], t["count"]) for t in stats["top_tags"]]
    kb_count = stats["knowledge_base"]

    # LLM 摘要
    summary = None
    if hub_agent.is_configured():
        try:
            from hub_agent import _get_company_name
            cn = _get_company_name()
            prompt = f"""生成今日星枢 Hub 日报摘要（{cn}客服团队）。

数据：
- 完成任务: {tasks_done} 个
- 失败任务: {tasks_failed} 个
- 进行中: {tasks_active} 个
- 今日记忆: {mems_today} 条
- 披露请求: {disc_today} 个（{disc_pending} 个待审批）
- 在线 Agent: {online}/{total}
- 高频话题: {', '.join(f'{t}({c}次)' for t,c in top_tags) if top_tags else '无'}
- 知识库条目: {kb_count} 条

用 3-5 句话总结，语气像给店长汇报，简洁有力。指出需要关注的问题。"""
            import httpx
            cfg = hub_agent._get_config()
            async with httpx.AsyncClient(timeout=20.0) as client:
                base = cfg.get("api_base") or "https://api.deepseek.com/v1"
                headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
                payload = {"model": cfg.get("model", "deepseek-chat"), "messages": [{"role": "user", "content": prompt}], "temperature": 0.5, "max_tokens": 300}
                resp = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
                if resp.status_code == 200:
                    summary = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            summary = f"(AI 摘要生成失败: {e})"

    return {"status": "ok", "stats": stats, "summary": summary}


def _daily_report_stats_sync() -> dict:
    """api_daily_report 的同步查询段（经 asyncio.to_thread 在线程池执行）。

    只含 sqlite 同步查询与 stats 组装；LLM 摘要（await client.post）保持在 async 壳内。
    """
    conn = sqlite3.connect(CONFIG.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 任务统计
    c.execute("SELECT status, COUNT(*) as cnt FROM tasks WHERE updated_at LIKE ? GROUP BY status", (f"{today}%",))
    task_stats = {row["status"]: row["cnt"] for row in c.fetchall()}
    tasks_done = task_stats.get("completed", 0)
    tasks_failed = task_stats.get("failed", 0)
    tasks_active = task_stats.get("in_progress", 0) + task_stats.get("assigned", 0)

    # 今日记忆
    c.execute("SELECT COUNT(*) FROM memory_pool WHERE created_at LIKE ?", (f"{today}%",))
    mems_today = c.fetchone()[0]

    # 披露请求
    c.execute("SELECT COUNT(*) FROM disclosure_requests WHERE created_at LIKE ?", (f"{today}%",))
    disc_today = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM disclosure_requests WHERE created_at LIKE ? AND status = 'pending'", (f"{today}%",))
    disc_pending = c.fetchone()[0]

    # Agent 状态
    online = sum(1 for a in hub.agents.values() if a.get("status") == "online")
    total = len(hub.agents)

    # 高频标签
    c.execute("SELECT tags FROM memory_pool WHERE created_at LIKE ? AND tags != '' AND tags IS NOT NULL", (f"{today}%",))
    tag_counts = {}
    for (tags_str,) in c.fetchall():
        try:
            for tag in json.loads(tags_str):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        except Exception as _exc:
            logger.debug("routes_report silent-except @114: %s", _exc)
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # 知识库
    c.execute("SELECT COUNT(*) FROM knowledge_base")
    kb_count = c.fetchone()[0]

    conn.close()

    stats = {
        "date": today,
        "tasks": {"done": tasks_done, "failed": tasks_failed, "active": tasks_active, "by_status": task_stats},
        "memories": mems_today,
        "disclosures": {"total": disc_today, "pending": disc_pending},
        "agents": {"online": online, "total": total},
        "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        "knowledge_base": kb_count,
    }
    return stats


