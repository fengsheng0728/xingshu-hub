"""
星枢 Sync Hub — 通知系统
"""
import json
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List  # D-7: Dict/List 原靠 models 通配导入泄漏，现显式导入
from fastapi import WebSocket

logger = logging.getLogger("xingshu.notifications")

# ============ 通知系统（Phase 2B） ============

class NotificationManager:
    """WebSocket 通知管理器：维护 agent_id → [WebSocket] 映射"""

    # CD-084：同一 source（notify_send:<agent_id>）死信落库最小间隔秒数。
    # 通知发送失败可能高频（断连重连风暴），不节流会把死信表刷爆。
    DEAD_LETTER_THROTTLE_SEC = 60

    def __init__(self):
        self.connections: Dict[str, List[WebSocket]] = {}
        # per-agent 发送锁：同一 agent 的多个并发写入者（notify 直发 /
        # WS handler 内直发 / 其他路由直发）复用同一把锁串行化，防 WS 帧交错
        self._send_locks: Dict[str, asyncio.Lock] = {}
        # CD-084：发送失败落死信的节流台账（source → 上次落库 time.time()）
        self._dead_letter_last: Dict[str, float] = {}

    async def connect(self, agent_id: str, ws: WebSocket):
        if agent_id not in self.connections:
            self.connections[agent_id] = []
        self.connections[agent_id].append(ws)

    def disconnect(self, agent_id: str, ws: WebSocket):
        if agent_id in self.connections:
            self.connections[agent_id] = [w for w in self.connections[agent_id] if w != ws]

    def send_lock(self, agent_id: str) -> asyncio.Lock:
        """获取某 agent 的发送锁（routes_ws handler 直发等外部写入方复用同一把锁）。"""
        lock = self._send_locks.get(agent_id)
        if lock is None:
            lock = self._send_locks[agent_id] = asyncio.Lock()
        return lock

    async def _record_send_failure(self, agent_id: str, payload, exc, frame: str):
        """CD-084：WS 发送失败落死信账本（不吞原语义：断连清理照常进行）。

        节流：同 source（notify_send:<agent_id>）DEAD_LETTER_THROTTLE_SEC 秒内
        已落过则跳过，防通知高频失败刷爆死信表。payload 预览截断，
        遵守 record_dead_letter 的 2000 字上限（helper 自身也会再截断一次）。
        """
        source = f"notify_send:{agent_id}"
        now = time.time()
        if now - self._dead_letter_last.get(source, 0) < self.DEAD_LETTER_THROTTLE_SEC:
            return
        self._dead_letter_last[source] = now
        try:
            from db import record_dead_letter  # 局部导入：本模块被早期加载，避免循环
            await asyncio.to_thread(
                record_dead_letter, source, "notification",
                {"agent_id": agent_id, "frame": frame,
                 "payload": str(payload)[:1800]},
                f"{type(exc).__name__}: {exc}")
        except Exception as _exc:
            # 死信落库自身失败只 debug，绝不反噬发送主流程
            logger.debug("notifications silent-except(_record_send_failure): %s", _exc)

    async def safe_send(self, agent_id: str, payload: dict) -> int:
        """持 per-agent 锁向该 agent 的全部连接推送；断连清理语义与原 notify 一致。

        返回投递成功的连接数（int）：agent 无连接或全部连接发送失败返回 0，
        调用方可据此判定「零投递」并按失败处理（死信落账/节流语义不变）。
        """
        async with self.send_lock(agent_id):
            delivered = 0
            if agent_id in self.connections:
                dead = []
                for ws in self.connections[agent_id]:
                    try:
                        await ws.send_json(payload)
                        delivered += 1
                    except Exception as exc:
                        dead.append(ws)
                        logger.warning("notify_send 失败 agent=%s frame=json: %s: %s",
                                       agent_id, type(exc).__name__, exc)
                        await self._record_send_failure(agent_id, payload, exc, "json")
                for ws in dead:
                    self.disconnect(agent_id, ws)
            return delivered

    async def safe_send_text(self, agent_id: str, text: str) -> int:
        """safe_send 的文本帧变体（CD-098）：envelope 序列化串等 str 载荷
        也走同一把 per-agent 锁，防文本帧与 JSON 帧跨写入方交错。

        返回投递成功的连接数（int），语义同 safe_send。
        """
        async with self.send_lock(agent_id):
            delivered = 0
            if agent_id in self.connections:
                dead = []
                for ws in self.connections[agent_id]:
                    try:
                        await ws.send_text(text)
                        delivered += 1
                    except Exception as exc:
                        dead.append(ws)
                        logger.warning("notify_send 失败 agent=%s frame=text: %s: %s",
                                       agent_id, type(exc).__name__, exc)
                        await self._record_send_failure(agent_id, text, exc, "text")
                for ws in dead:
                    self.disconnect(agent_id, ws)
            return delivered

    async def notify(self, agent_id: str, notification: dict):
        """向指定 Agent 的所有 WebSocket 连接推送通知"""
        await self.safe_send(agent_id, notification)

    async def broadcast_dashboard(self, event: dict):
        """向所有 dashboard 观察者广播事件"""
        await self.notify("__dashboard__", event)


notifications = NotificationManager()
