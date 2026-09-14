"""星枢 Sync Hub — 通知 / 私聊 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from hub_core import hub
from routes_common import NO_AUTH, get_current_agent

router = APIRouter()


@router.post("/api/v1/messages/send")
async def api_dm_send(req: dict, current_agent: str = Depends(get_current_agent)):
    """P3: Agent 私聊发送 — to 在线 → WS direct_message 推送; 离线 → 落通知(上线可见, 不丢消息)"""
    to_agent = (req.get("to_agent_id") or "").strip()
    content = (req.get("content") or "").strip()
    if not to_agent or not content:
        raise HTTPException(status_code=400, detail="to_agent_id 和 content 必填")
    if to_agent == current_agent:
        raise HTTPException(status_code=400, detail="不能给自己发私聊")
    now = datetime.now(timezone.utc).isoformat()

    conn = hub._db()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO messages (from_agent_id, to_agent_id, content, is_read, created_at) VALUES (?,?,?,0,?)",
                  (current_agent, to_agent, content, now))
        msg_id = c.lastrowid
        conn.commit()
        # 在线: WS direct_message 直推
        ws = hub.active_ws.get(to_agent)
        if ws:
            try:
                await ws.send_json({"type": "direct_message", "message_id": msg_id,
                                    "from_agent_id": current_agent, "to_agent_id": to_agent,
                                    "content": content, "created_at": now})
                return {"status": "sent", "message_id": msg_id,
                        "to_agent_id": to_agent, "from_agent_id": current_agent, "delivered": "ws"}
            except Exception:
                pass  # 推送失败降级为通知
        # 离线/推送失败: 落通知(上线可见)
        c.execute("INSERT INTO notifications (agent_id, type, title, body, related_agent_id, created_at) "
                  "VALUES (?,?,?,?,?,datetime('now','localtime'))",
                  (to_agent, "direct_message", "私聊", f"来自 {current_agent}: {content[:200]}", current_agent))
        conn.commit()
        return {"status": "queued", "message_id": msg_id,
                "to_agent_id": to_agent, "from_agent_id": current_agent, "delivered": "notification"}
    finally:
        conn.close()


@router.get("/api/v1/messages")
async def api_dm_list(agent_id: str, current_agent: str = Depends(get_current_agent)):
    """P3: 私聊消息列表（双向, 时间升序）"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
                            detail=f"Forbidden: 不能以 {current_agent} 身份查看 {agent_id} 的私聊")
    conn = hub._db()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE from_agent_id = ? OR to_agent_id = ? ORDER BY created_at ASC",
            (agent_id, agent_id)).fetchall()
    finally:
        conn.close()
    return {"messages": [dict(r) for r in rows]}


@router.get("/api/v1/notifications")
async def api_get_notifications(
    agent_id: str,
    limit: int = 50,
    unread_only: bool = False,
    current_agent: str = Depends(get_current_agent),
):
    """获取通知列表"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await hub.get_notifications(agent_id, limit=limit, unread_only=unread_only)


@router.post("/api/v1/notifications/{notif_id}/read")
async def api_mark_read(
    notif_id: int,
    agent_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """标记单条通知已读"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await hub.mark_notification_read(agent_id, notif_id)


@router.post("/api/v1/notifications/read-all")
async def api_mark_all_read(
    agent_id: str,
    current_agent: str = Depends(get_current_agent),
):
    """标记所有通知已读"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await hub.mark_all_read(agent_id)


@router.post("/api/v1/notifications/create")
async def api_create_notification(
    agent_id: str,
    type: str = "info",
    title: str = "",
    body: str = "",
    related_task_id: str = "",
    related_agent_id: str = "",
    source: str = "",
    artifact_path: str = "",
    current_agent: str = Depends(get_current_agent),
):
    """创建通知并 WebSocket 推送。source 标记触发自动化。

    Query params: agent_id, type, title, body, related_task_id, related_agent_id, source, artifact_path
    """
    return await hub.create_notification(
        agent_id=agent_id, type=type, title=title, body=body,
        related_task_id=related_task_id, related_agent_id=related_agent_id,
        source=source, artifact_path=artifact_path,
    )
