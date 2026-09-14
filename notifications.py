"""
星枢 Sync Hub — 通知系统
"""
import json
import asyncio
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List  # D-7: Dict/List 原靠 models 通配导入泄漏，现显式导入
from fastapi import WebSocket

# ============ 通知系统（Phase 2B） ============

class NotificationManager:
    """WebSocket 通知管理器：维护 agent_id → [WebSocket] 映射"""

    def __init__(self):
        self.connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, agent_id: str, ws: WebSocket):
        if agent_id not in self.connections:
            self.connections[agent_id] = []
        self.connections[agent_id].append(ws)

    def disconnect(self, agent_id: str, ws: WebSocket):
        if agent_id in self.connections:
            self.connections[agent_id] = [w for w in self.connections[agent_id] if w != ws]

    async def notify(self, agent_id: str, notification: dict):
        """向指定 Agent 的所有 WebSocket 连接推送通知"""
        if agent_id in self.connections:
            dead = []
            for ws in self.connections[agent_id]:
                try:
                    await ws.send_json(notification)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.disconnect(agent_id, ws)

    async def broadcast_dashboard(self, event: dict):
        """向所有 dashboard 观察者广播事件"""
        await self.notify("__dashboard__", event)


notifications = NotificationManager()