"""星枢 SyncHub — dashboard Mixin"""
import asyncio
import json
import hashlib
import time
import os
import sqlite3
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import numpy as np

from db import row_to_dict as _row_dict
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong

class DashboardMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def get_dashboard_data(self, requester_id: str = "") -> dict:
        """获取监控面板数据，按请求者角色过滤可见范围"""
        return await asyncio.to_thread(self._get_dashboard_data_sync, requester_id)

    def _get_dashboard_data_sync(self, requester_id: str = "") -> dict:
        """get_dashboard_data 的同步查询主体（经 asyncio.to_thread 在线程池执行，避免阻塞事件循环）"""
        req_info = self.agents.get(requester_id, {})
        # 无认证时（管理端/开发模式）视为 orchestrator，拥有全部操作权限
        req_role = req_info.get("role", "orchestrator" if not requester_id else "worker")
        managed = req_info.get("managed_agents", [])

        with self._db() as conn:
            c = conn.cursor()

            # ── 构建可见 Agent ID 集合 ──
            if not requester_id or req_role == "orchestrator":
                visible_agents = None  # 全部可见
            elif req_role == "manager":
                visible_agents = {requester_id} | set(managed)
            else:  # worker
                # worker 只能看到自己 + 上级(manager/orchestrator)
                visible_agents = {requester_id}
                for aid, info in self.agents.items():
                    if info.get("role") in ("manager", "orchestrator"):
                        if requester_id in info.get("managed_agents", []):
                            visible_agents.add(aid)

            # ── Agent 统计 ──
            if visible_agents is None:
                c.execute("SELECT COUNT(*) FROM agents WHERE status = 'online'")
                online = c.fetchone()[0]
                c.execute("SELECT COUNT(*) FROM agents")
                total = c.fetchone()[0]
            else:
                placeholders = ",".join("?" * len(visible_agents))
                c.execute(
                    f"SELECT COUNT(*) FROM agents WHERE status = 'online' AND agent_id IN ({placeholders})",
                    list(visible_agents),
                )
                online = c.fetchone()[0]
                c.execute(
                    f"SELECT COUNT(*) FROM agents WHERE agent_id IN ({placeholders})",
                    list(visible_agents),
                )
                total = c.fetchone()[0]

            # ── 记忆统计 ──
            if visible_agents is None:
                c.execute("SELECT COUNT(*) FROM memory_pool")
            else:
                c.execute(
                    f"SELECT COUNT(*) FROM memory_pool WHERE owner_agent_id IN ({placeholders})",
                    list(visible_agents),
                )
            memories = c.fetchone()[0]

            # ── 任务统计 ──
            if visible_agents is None:
                c.execute("SELECT COUNT(*) FROM tasks")
                tasks_total = c.fetchone()[0]
                c.execute("SELECT COUNT(*) FROM tasks WHERE status = 'pending'")
                pending = c.fetchone()[0]
                c.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status")
                by_status = {row["status"]: row["cnt"] for row in c.fetchall()}
                c.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 30")
                task_list = [_row_dict(row) for row in c.fetchall()]
                for _t in task_list:
                    _t["blocked_by"] = self._blocked_by(json.loads(_t.get("depends_on") or "[]"))
            else:
                placeholders = ", ".join("?" * len(visible_agents))
                params = list(visible_agents)
                c.execute(
                    f"SELECT COUNT(*) FROM tasks WHERE creator_agent_id IN ({placeholders}) OR assigned_agent_id IN ({placeholders})",
                    params + params,
                )
                tasks_total = c.fetchone()[0]
                c.execute(
                    f"SELECT COUNT(*) FROM tasks WHERE status = 'pending' AND (creator_agent_id IN ({placeholders}) OR assigned_agent_id IN ({placeholders}))",
                    params + params,
                )
                pending = c.fetchone()[0]
                c.execute(
                    f"SELECT status, COUNT(*) as cnt FROM tasks WHERE creator_agent_id IN ({placeholders}) OR assigned_agent_id IN ({placeholders}) GROUP BY status",
                    params + params,
                )
                by_status = {row["status"]: row["cnt"] for row in c.fetchall()}
                c.execute(
                    f"SELECT * FROM tasks WHERE creator_agent_id IN ({placeholders}) OR assigned_agent_id IN ({placeholders}) ORDER BY updated_at DESC LIMIT 30",
                    params + params,
                )
                task_list = [_row_dict(row) for row in c.fetchall()]
                for _t in task_list:
                    _t["blocked_by"] = self._blocked_by(json.loads(_t.get("depends_on") or "[]"))

            # ── 披露统计 ──
            if visible_agents is None:
                c.execute("SELECT COUNT(*) FROM disclosure_log")
                disclosures = c.fetchone()[0]
                c.execute("SELECT * FROM disclosure_log ORDER BY disclosed_at DESC LIMIT 20")
                disclosure_rows = c.fetchall()
            else:
                placeholders = ",".join("?" * len(visible_agents))
                params = list(visible_agents)
                c.execute(
                    f"SELECT COUNT(*) FROM disclosure_log WHERE from_agent_id IN ({placeholders}) OR to_agent_id IN ({placeholders})",
                    params + params,
                )
                disclosures = c.fetchone()[0]
                c.execute(
                    f"SELECT * FROM disclosure_log WHERE from_agent_id IN ({placeholders}) OR to_agent_id IN ({placeholders}) ORDER BY disclosed_at DESC LIMIT 20",
                    params + params,
                )
                disclosure_rows = c.fetchall()

            # ── Agent 列表 ──
            if visible_agents is None:
                c.execute("SELECT * FROM agents ORDER BY last_heartbeat DESC")
            else:
                c.execute(
                    f"SELECT * FROM agents WHERE agent_id IN ({placeholders}) ORDER BY last_heartbeat DESC",
                    list(visible_agents),
                )
            agents = []
            for row in c.fetchall():
                agents.append({
                    "agent_id": row["agent_id"],
                    "agent_name": row["agent_name"],
                    "department": row["department"] or "",
                    "role": row["role"],
                    "status": row["status"],
                    "capabilities": json.loads(row["capabilities"] or "[]"),
                    "managed_agents": json.loads(row["managed_agents"] or "[]"),
                })

            recent_disclosures = []
            for row in disclosure_rows:
                recent_disclosures.append({
                    "from": row["from_agent_id"],
                    "to": row["to_agent_id"],
                    "level": row["disclosed_level"],
                    "time": row["disclosed_at"],
                    "reason": row["reason"],
                })

            # ── 待审批披露请求 ──
            c2 = conn.cursor()
            c2.execute(
                "SELECT request_id, task_id, agent_id, reason, new_phase, status, created_at,"
                " audit_decision, audit_reason, audit_risk_level"
                " FROM disclosure_requests WHERE status = 'pending' ORDER BY created_at DESC LIMIT 20"
            )
            pending_disclosures = [_row_dict(row) for row in c2.fetchall()]

        return {
            "viewer_role": req_role,
            "viewer_agent_id": requester_id,
            "agents": {"online": online, "total": total, "list": agents},
            "memories": {"total": memories},
            "pending_disclosures": pending_disclosures,
            "tasks": {
                "total": tasks_total,
                "pending": pending,
                "by_status": by_status,
                "list": task_list,
            },
            "disclosures": {"total": disclosures, "recent": recent_disclosures},
        }


    async def get_agent_workspace(self, agent_id: str) -> dict:
        """Agent 端专用：返回当前 Agent 的任务、通知、团队摘要"""
        with self._db() as conn:
            c = conn.cursor()

            # -- Agent info --
            c.execute("SELECT agent_id, agent_name, role, department FROM agents WHERE agent_id = ?", (agent_id,))
            agent_row = c.fetchone()
            if not agent_row:
                return {"error": "agent not found"}

            agent_info = dict(agent_row)

            # -- Tasks assigned to this agent --
            c.execute("""
                SELECT * FROM tasks
                WHERE assigned_agent_id = ? OR creator_agent_id = ?
                ORDER BY updated_at DESC LIMIT 30
            """, (agent_id, agent_id))
            tasks = [{k: row[k] for k in row.keys()} for row in c.fetchall()]

            # P2: tasks carry subtask_summary (kanban decompose badge data source)
            sub_agg = {}
            for sr in c.execute(
                    "SELECT parent_task_id, status FROM tasks WHERE parent_task_id IS NOT NULL").fetchall():
                pid = sr["parent_task_id"]
                d = sub_agg.setdefault(pid, {"total": 0, "completed": 0})
                d["total"] += 1
                if sr["status"] == "completed":
                    d["completed"] += 1
            for t in tasks:
                if t.get("task_id") in sub_agg:
                    t["subtask_summary"] = sub_agg[t["task_id"]]

            # -- Notifications for this agent (persistent notifications + disclosure requests) --
            c.execute("""
                SELECT * FROM notifications
                WHERE agent_id = ?
                ORDER BY created_at DESC LIMIT 50
            """, (agent_id,))
            notifications = []
            for row in c.fetchall():
                r = {k: row[k] for k in row.keys()}
                notifications.append({
                    "id": r.get("id"),
                    "type": r.get("type", ""),
                    "event": "delivery" if r.get("artifact_path") else r.get("type", ""),
                    "title": r.get("title", ""),
                    "message": r.get("body", ""),
                    "body": r.get("body", ""),
                    "artifact_path": r.get("artifact_path", ""),
                    "related_task_id": r.get("related_task_id", ""),
                    "related_agent_id": r.get("related_agent_id", ""),
                    "is_read": bool(r.get("is_read", 0)),
                    "source": r.get("source", ""),
                    "created_at": r.get("created_at", ""),
                })

            # -- Pending disclosure requests from this agent --
            c.execute("""
                SELECT * FROM disclosure_requests
                WHERE agent_id = ? AND status IN ('pending', 'approved', 'denied')
                ORDER BY created_at DESC LIMIT 20
            """, (agent_id,))
            for row in c.fetchall():
                r = {k: row[k] for k in row.keys()}
                status_label = {"pending": "待审批", "approved": "已批准", "denied": "已拒绝"}.get(r.get("status", ""), r.get("status", ""))
                notifications.append({
                    "type": "disclosure",
                    "event": "disclosure",
                    "title": "披露申请",
                    "message": f"披露申请 {status_label}: {r.get('reason', '')}",
                    "task_id": r.get("task_id", ""),
                    "status": r.get("status", ""),
                    "audit_decision": r.get("audit_decision", ""),
                    "audit_reason": r.get("audit_reason", ""),
                    "created_at": r.get("created_at", ""),
                })

            # -- Team summaries (public memories from same department) --
            dept = agent_info.get("department", "")
            team_summaries = []
            if dept:
                c.execute("""
                    SELECT mp.*, a.agent_name FROM memory_pool mp
                    JOIN agents a ON mp.owner_agent_id = a.agent_id
                    WHERE a.department = ? AND mp.owner_agent_id != ?
                    AND mp.disclosure_level IN ('summary', 'full')
                    ORDER BY mp.created_at DESC LIMIT 10
                """, (dept, agent_id))
                team_summaries = [{k: row[k] for k in row.keys()} for row in c.fetchall()]

        return {
            "agent": agent_info,
            "tasks": tasks,
            "notifications": notifications,
            "team_summaries": team_summaries,
        }

    # ============ 写入缓冲（批量落库） ============


