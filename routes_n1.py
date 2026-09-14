"""星枢 Sync Hub — N1 全访问授权审批门（阶段1/1c，2026-08-30）

full_access=1 的 Agent 删除/破坏操作 → review_queue(pending, item_type='n1_delete')
→ manager 审批 → approved 执行删除 / rejected 拒绝。

默认关（fail-closed）：full_access=0 时无全访问能力，现有 role 门照旧。
复用 D6 review_queue 通用组件（禁仿制品）；授权/审批动作全部入 events 审计。
"""
import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from models import CONFIG
from hub_core import hub
from routes_common import NO_AUTH, get_current_agent

router = APIRouter()


def _n1_enqueue(agent_id: str, endpoint: str, params: dict) -> int:
    """删除操作入 review_queue(pending)。返回 queue id。"""
    detail = json.dumps(
        {"endpoint": endpoint, "params": params, "requester": agent_id},
        ensure_ascii=False,
    )
    conn = hub._db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO review_queue (item_type, doc_id, name, detail, level, status, source, created_at)"
        " VALUES ('n1_delete', ?, ?, ?, 'summary', 'pending', 'n1_gate', datetime('now'))",
        (endpoint, str(params.get("target") or params), detail),
    )
    conn.commit()
    qid = c.lastrowid
    conn.close()
    return qid


async def _n1_gate(current_agent: str, endpoint: str, params: dict):
    """删除/破坏操作审批门。

    返回 None = 放行（无全访问授权 / NO_AUTH 环境）；
    返回 dict = 拦截响应（202 pending_approval，操作已入审批队列）。
    """
    if NO_AUTH:
        return None
    info = hub.agents.get(current_agent, {})
    if not info.get("full_access"):
        return None
    qid = _n1_enqueue(current_agent, endpoint, params)
    await hub._log_event(
        "n1_delete_pending", current_agent,
        {"endpoint": endpoint, "target": str(params.get("target") or params), "queue_id": qid},
    )
    return {
        "status": "pending_approval",
        "queue_id": qid,
        "message": f"全访问操作 {endpoint} 已入审批队列 #{qid}，待主管审批后执行",
    }


async def _execute_n1_delete(detail: dict) -> dict:
    """审批通过后执行真正的删除（endpoint → 执行器映射，与各端点逻辑一致）。"""
    ep = detail.get("endpoint", "")
    p = detail.get("params", {}) or {}
    if ep == "automation_jobs":
        conn = hub._db()
        conn.execute(
            "DELETE FROM automation_jobs WHERE id = ? AND owner_agent_id = ?",
            (p.get("job_id"), p.get("owner", "")),
        )
        conn.commit()
        conn.close()
    elif ep == "integrations":
        from integrations import registry

        conn = hub._db()
        conn.execute(
            "UPDATE integrations_state SET enabled = 0, updated_at = datetime('now') WHERE name = ?",
            (p.get("name"),),
        )
        conn.commit()
        conn.close()
        registry._configured.pop(p.get("name"), None)
    elif ep == "knowledge":
        await hub.knowledge_delete(p.get("entry_id"))
    elif ep == "memory":
        await hub.delete_memory(p.get("memory_key"), p.get("agent_id"))
    elif ep == "shared_docs":
        from routes_shared import _ws

        ws_inst = _ws()
        if ws_inst is None:
            raise HTTPException(status_code=503, detail="workspace not ready")
        await ws_inst.delete_doc(p.get("doc_id"))
    elif ep == "team_members":
        await hub.remove_team_member(p.get("member_id"), p.get("owner", ""))
    elif ep == "chat_clear":
        from hub_agent_lc import SQLiteChatHistory

        SQLiteChatHistory(p.get("session_id", "default"), CONFIG.DB_PATH).clear()
    else:
        raise HTTPException(status_code=400, detail=f"未知 N1 删除端点: {ep}")
    return {"status": "executed", "endpoint": ep}


# ═══════════ 授权端点 ═══════════

@router.post("/api/v1/agents/full-access")
async def api_n1_full_access(request: Request,
                             current_agent: str = Depends(get_current_agent)):
    """显式授权/收回全访问（manager+，入审计）。body: {agent_id, enabled, reason?}"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可授权全访问")
    try:
        data = await request.json()
    except Exception:
        data = {}
    target = (data.get("agent_id") or "").strip()
    enabled = 1 if data.get("enabled") else 0
    reason = (data.get("reason") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="agent_id 必填")
    conn = hub._db()
    conn.execute("UPDATE agents SET full_access = ? WHERE agent_id = ?", (enabled, target))
    conn.commit()
    conn.close()
    if target in hub.agents:
        hub.agents[target]["full_access"] = enabled  # 内存态同步
    await hub._log_event(
        "n1_full_access", current_agent,
        {"agent_id": target, "enabled": bool(enabled), "reason": reason},
    )
    return {"status": "ok", "agent_id": target, "full_access": bool(enabled)}


# ═══════════ 审批队列与决策 ═══════════

@router.get("/api/v1/n1/reviews")
async def api_n1_reviews(status: str = "pending",
                         current_agent: str = Depends(get_current_agent)):
    """N1 删除审批队列（manager+）。query: ?status=pending|approved|rejected|空=全部"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可查看 N1 审批队列")
    conn = hub._db()
    conn.row_factory = sqlite3.Row
    if status:
        rows = conn.execute(
            "SELECT * FROM review_queue WHERE item_type = 'n1_delete' AND status = ?"
            " ORDER BY id DESC LIMIT 100", (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM review_queue WHERE item_type = 'n1_delete'"
            " ORDER BY id DESC LIMIT 100",
        ).fetchall()
    conn.close()
    return {"status": "ok", "reviews": [dict(r) for r in rows], "count": len(rows)}


@router.post("/api/v1/n1/reviews/{review_id}")
async def api_n1_review_decision(review_id: int, request: Request,
                                 current_agent: str = Depends(get_current_agent)):
    """N1 审批决策（manager+）。body: {decision: approved|rejected}
    approved → 执行 detail 中的删除动作；rejected → 拒绝（不执行）。"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可审批 N1")
    try:
        data = await request.json()
    except Exception:
        data = {}
    decision = data.get("decision", "")
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision 必须为 approved 或 rejected")
    conn = hub._db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM review_queue WHERE id = ? AND item_type = 'n1_delete'", (review_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="审批项不存在")
    if row["status"] != "pending":
        conn.close()
        raise HTTPException(status_code=409, detail=f"已处理（{row['status']}）")
    detail = json.loads(row["detail"] or "{}")
    if decision == "approved":
        result = await _execute_n1_delete(detail)
        conn.execute(
            "UPDATE review_queue SET status = 'approved', reviewed_at = datetime('now'),"
            " reviewed_by = ? WHERE id = ?", (current_agent, review_id),
        )
        conn.commit()
        conn.close()
        await hub._log_event(
            "n1_delete_approved", current_agent,
            {"queue_id": review_id, "endpoint": detail.get("endpoint")},
        )
        return {"status": "approved", "queue_id": review_id, "executed": result}
    conn.execute(
        "UPDATE review_queue SET status = 'rejected', reviewed_at = datetime('now'),"
        " reviewed_by = ? WHERE id = ?", (current_agent, review_id),
    )
    conn.commit()
    conn.close()
    await hub._log_event(
        "n1_delete_rejected", current_agent,
        {"queue_id": review_id, "endpoint": detail.get("endpoint")},
    )
    return {"status": "rejected", "queue_id": review_id}
