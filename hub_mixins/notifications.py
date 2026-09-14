"""星枢 SyncHub — notifications Mixin"""
import logging
logger = logging.getLogger("xingshu.notifications")

import asyncio
import json
import db_facade
# D-11: 别名供 async 函数内调用 — 门面 AST 自检口径按属性名计数，
# db_facade.execute(...) 会被误记为直连调用，故经模块级 Name 引用。
_facade_execute = db_facade.execute
import hashlib
import logging
import threading
import time
import os
import sqlite3
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import numpy as np

from deps import CONFIG
from db import row_to_dict as _row_dict
from notifications import notifications
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong


def _insert_notification(conn, params):
    """run_in_conn 事务体：INSERT 通知并返回 lastrowid。

    独立为模块级同步函数：AST 门禁（tests/test_facade_migration.py）按
    AsyncFunctionDef 子树统计直接 SQLite 调用，嵌套 def 也会被计入。
    """
    c = conn.cursor()
    c.execute(
        "INSERT INTO notifications (agent_id, type, title, body, related_task_id, related_agent_id, source, artifact_path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return c.lastrowid


class NotificationsMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def create_notification(self, agent_id: str, type: str, title: str,
                                   body: str = "", related_task_id: str = "",
                                   related_agent_id: str = "", source: str = "",
                                   artifact_path: str = "") -> dict:
        """创建通知并推送 WebSocket。R3: source 标记 + 事件触发自动化。"""
        now = datetime.now(timezone.utc).isoformat()
        notif_id = await db_facade.run_in_conn(
            lambda conn: _insert_notification(
                conn, (agent_id, type, title, body, related_task_id, related_agent_id, source, artifact_path, now)),
            write=True)

        notif = {
            "id": notif_id, "agent_id": agent_id, "type": type,
            "title": title, "body": body, "is_read": False,
            "related_task_id": related_task_id, "related_agent_id": related_agent_id,
            "source": source, "artifact_path": artifact_path, "created_at": now,
        }
        await notifications.notify(agent_id, {"type": "push", "event": "notification", "data": notif})

        # P2 通知多渠道：异步 fan-out（钉钉/SMTP），失败零阻塞主链路（D4）
        if CONFIG.NOTIFY_CHANNELS:
            try:
                from notify_channels import fan_out
                asyncio.create_task(self._fanout_and_mark(notif_id, notif))
            except Exception as e:
                logger.warning(f"[notify] fan-out 启动失败: {e}")

        # R3-①+③: Check event-triggered automation jobs
        await self._dispatch_event_automation("notification.created", agent_id, source=source)
        return {"status": "ok", "notification_id": notif_id}


    async def _fanout_and_mark(self, notif_id: int, notif: dict):
        """P2: 异步出站 + 写 channel_status（失败不重试不风暴，仅标记+日志）"""
        try:
            from notify_channels import fan_out
            results = await fan_out(CONFIG.NOTIFY_CHANNELS, notif)
            if results:
                try:
                    await _facade_execute(
                        "UPDATE notifications SET channel_status=? WHERE id=?",
                        (json.dumps(results, ensure_ascii=False), notif_id))
                except Exception as e:
                    logger.warning(f"[notify] channel_status 落库失败: {e}")
        except Exception as e:
            logger.warning(f"[notify] fan-out 异常: {type(e).__name__}: {str(e)[:120]}")


    async def _dispatch_event_automation(self, event_type: str, agent_id: str, source: str = ""):
        """R3: 事件触发自动化调度。断环：默认过滤 source=automation。"""
        try:
            rows = await db_facade.query(
                "SELECT * FROM automation_jobs WHERE enabled=1 AND trigger_type='event' "
                "AND trigger_spec=? AND consecutive_failures < 5",
                (event_type,)
            )
            jobs = [dict(row) for row in rows]

            for job in jobs:
                # R3-①: 断环 — 自动化通知默认不触发自动化
                if source == 'automation' and not job.get('allow_auto_source'):
                    continue
                # Filter by owner
                if job.get('owner_agent_id') != agent_id:
                    continue
                # Dispatch via WS
                ws = self.active_ws.get(agent_id)
                if not ws:
                    # Agent offline: record missed
                    await _facade_execute("UPDATE automation_jobs SET missed_runs=missed_runs+1 WHERE id=?", (job['id'],))
                    continue
                try:
                    import json as _json
                    gr = _json.loads(job.get('guardrail', '{}')) if isinstance(job.get('guardrail'), str) else job.get('guardrail', {})
                    dl = _json.loads(job.get('delivery', '["notification"]')) if isinstance(job.get('delivery'), str) else job.get('delivery', ["notification"])
                    await ws.send_json({
                        "type": "automation.run",
                        "job_id": job["id"],
                        "name": job.get("name", ""),
                        "instruction": job.get("instruction", ""),
                        "guardrail": gr,
                        "delivery": dl,
                    })
                    await _facade_execute("UPDATE automation_jobs SET run_count=run_count+1, last_run_at=datetime('now'), last_status='dispatched' WHERE id=?", (job['id'],))
                except Exception as _exc2: logger.debug("run_count update failed: %s", _exc2)
        except Exception as _exc: logger.warning("notifications automation dispatch failed: %s", _exc)


    async def get_notifications(self, agent_id: str, limit: int = 50, unread_only: bool = False) -> dict:
        """获取通知列表"""
        if unread_only:
            rows = await db_facade.query(
                "SELECT * FROM notifications WHERE agent_id = ? AND is_read = 0 ORDER BY created_at DESC LIMIT ?",
                (agent_id, limit),
            )
        else:
            rows = await db_facade.query(
                "SELECT * FROM notifications WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
                (agent_id, limit),
            )
        items = [_row_dict(r) for r in rows]
        for item in items:
            item["is_read"] = bool(item["is_read"])
        return {"status": "ok", "notifications": items, "unread": sum(1 for i in items if i["is_read"] == False)}


    async def mark_notification_read(self, agent_id: str, notif_id: int) -> dict:
        """标记通知已读"""
        rowcount = await _facade_execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND agent_id = ?", (notif_id, agent_id))
        ok = rowcount > 0
        return {"status": "ok" if ok else "not_found"}


    async def mark_all_read(self, agent_id: str) -> dict:
        """标记所有通知已读"""
        count = await _facade_execute("UPDATE notifications SET is_read = 1 WHERE agent_id = ? AND is_read = 0", (agent_id,))
        return {"status": "ok", "marked": count}


# ── G1 批2：影子告警通道（去抖钩子）────────────────────────────
# 最小告警封装，复用 channel_status 的降级哲学：失败不重试不风暴、仅标记+日志，
# sink 异常静默。触发：同 reason 连续计数达阈值（默认 3）告警一次；去抖：同
# reason 在去抖窗口（默认 5 分钟）内不重复告警。shadow_alert_reset 在恢复时
# 清零连续计数（保留去抖时间窗）。默认落结构化日志；register_shadow_alert_sink
# 可挂外发通道（如通知 fan-out，由 hub_core 侧接线——影子层不直接依赖 Hub）。
_SHADOW_ALERT_THRESHOLD = 3
_SHADOW_ALERT_DEBOUNCE = 300.0
_shadow_alert_lock = threading.Lock()
_shadow_alert_state = {}   # reason -> {"count": int, "last_alert": float}
_shadow_alert_sinks = []
_alert_logger = logging.getLogger("xingshu.shadow.alert")


def register_shadow_alert_sink(fn):
    """注册告警外发 sink：fn(reason: str, detail: str)。sink 异常静默。"""
    with _shadow_alert_lock:
        _shadow_alert_sinks.append(fn)


def shadow_alert_reset(reason: str):
    """恢复时清零该 reason 的连续失败计数（去抖时间窗保留）。"""
    with _shadow_alert_lock:
        st = _shadow_alert_state.get(reason)
        if st:
            st["count"] = 0


def shadow_alert(reason: str, detail: str = "", threshold=None,
                 debounce=None) -> bool:
    """影子告警入口（同步、非阻塞）。返回本次是否实际发出告警。

    同 reason 连续计数 >= threshold（默认 3）才触发；距上次告警不足
    debounce（默认 300s）的触发被去抖吞掉。全部异常静默（D4）。
    """
    try:
        threshold = _SHADOW_ALERT_THRESHOLD if threshold is None else threshold
        debounce = _SHADOW_ALERT_DEBOUNCE if debounce is None else debounce
        now = time.time()
        with _shadow_alert_lock:
            st = _shadow_alert_state.setdefault(
                reason, {"count": 0, "last_alert": 0.0})
            st["count"] += 1
            if st["count"] < threshold or now - st["last_alert"] < debounce:
                return False
            st["last_alert"] = now
            count = st["count"]
            sinks = list(_shadow_alert_sinks)
        _alert_logger.warning("影子告警 [%s] 连续失败 %d 次: %s",
                              reason, count, detail)
        for fn in sinks:
            try:
                fn(reason, detail)
            except Exception as _exc:
                logger.warning("notifications silent-except @226: %s", _exc)
        return True
    except Exception:
        return False


