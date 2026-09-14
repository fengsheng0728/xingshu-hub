"""星枢 SyncHub — tasks Mixin"""
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

from deps import TASK_TRANSITIONS, TaskCreate, TaskStatus
from db import row_to_dict as _row_dict
from notifications import notifications
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong
import db_facade

class TasksMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def _validate_dependencies(self, task_id: str, depends_on: list) -> Optional[dict]:
        """P1 DAG: 依赖存在性 + 环检测（DFS）。合法返回 None，非法返回 error dict。"""
        if not depends_on:
            return None
        # 自依赖
        if task_id in depends_on:
            return {"status": "error", "error": f"自依赖: 任务 {task_id} 不能依赖自身",
                    "cycle": [task_id, task_id]}

        # 存在性
        for dep in depends_on:
            if not await db_facade.query_one("SELECT 1 FROM tasks WHERE task_id = ?", (dep,)):
                return {"status": "error", "error": f"依赖任务不存在: {dep}", "missing": dep}

        # 环检测：从每个依赖 DFS，若回到 task_id 或依赖内部成环则拒绝
        async def dfs(node: str, path: list) -> Optional[list]:
            if node == task_id and path:
                return path + [task_id]
            if node in path:
                return path + [node]
            row = await db_facade.query_one("SELECT depends_on FROM tasks WHERE task_id = ?", (node,))
            if not row:
                return None
            for sub in json.loads(row["depends_on"] or "[]"):
                r = await dfs(sub, path + [node])
                if r:
                    return r
            return None

        cycle = None
        for dep in depends_on:
            r = await dfs(dep, [])
            if r:
                cycle = r
                break
        if cycle:
            return {"status": "error", "error": f"依赖环: {' → '.join(cycle)}",
                    "cycle": cycle}
        return None


    def _blocked_by(self, depends_on: list) -> list:
        """P1 DAG: 计算未完成依赖（blocked_by 计算字段）。返回缺失依赖 id 列表。
        单次批量 IN 查询（原实现每依赖新建一次连接）。"""
        deps = list(depends_on or [])
        if not deps:
            return []
        placeholders = ",".join("?" * len(deps))
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT task_id, status FROM tasks WHERE task_id IN ({placeholders})",
                deps).fetchall()
        done = {r["task_id"] for r in rows
                if r["status"] == TaskStatus.COMPLETED.value}
        return [d for d in deps if d not in done]


    async def create_task(self, task: TaskCreate) -> dict:
        """创建调度任务（P1: 支持 depends_on，环检测 + 依赖存在性校验）"""
        async with self._task_lock:
            now = datetime.now(timezone.utc).isoformat()

            # P1 DAG: 环检测 + 依赖存在性（先于写入）
            dep_check = await self._validate_dependencies(task.task_id, task.depends_on)
            if dep_check is not None:
                return dep_check

            # P2: parent_task_id 校验 — 存在性 + 非自指（防孤儿/自环）
            parent_id = (task.parent_task_id or "").strip()
            if parent_id:
                if parent_id == task.task_id:
                    return {"status": "error", "error": "任务不能作为自己的子任务"}
                prow = await db_facade.query_one("SELECT task_id FROM tasks WHERE task_id = ?", (parent_id,))
                if not prow:
                    return {"status": "error", "error": f"父任务 {parent_id} 不存在"}

            disclosure_plan = task.disclosure_plan or {
                "phases": [
                    {"phase": 1, "trigger": "task_accepted", "level": "metadata"},
                    {"phase": 2, "trigger": "task_started", "level": "summary"},
                    {"phase": 3, "trigger": "task_blocked", "level": "full"},
                ]
            }

            rows_affected = await db_facade.execute(
                """
                INSERT OR IGNORE INTO tasks
                (task_id, status, creator_agent_id, description, required_capabilities,
                 required_memories, disclosure_plan, priority, created_at, updated_at,
                 depends_on, parent_task_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    "pending",
                    task.creator_agent_id or "system",
                    task.description,
                    json.dumps(task.required_capabilities),
                    json.dumps(task.required_memories),
                    json.dumps(disclosure_plan),
                    task.priority,
                    now,
                    now,
                    json.dumps(task.depends_on),
                    parent_id or None,
                ),
            )

            if rows_affected == 0:
                return {"status": "exists", "task_id": task.task_id, "message": "任务已存在"}

            await self._log_event("task_create", task.creator_agent_id or "system", {
                "task_id": task.task_id,
                "priority": task.priority,
            })

            return {"status": "created", "task_id": task.task_id,
                    "disclosure_plan": disclosure_plan}


    async def schedule_task(self, task_id: str) -> dict:
        """
        调度任务：匹配 Agent → 渐进式披露（第一阶段）
        """
        async with self._task_lock:
            row = await db_facade.query_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            task = _row_dict(row) if row else None

            if not task:
                return {"status": "error", "message": "Task not found"}

            # 1. 匹配能力
            required_caps = json.loads(task["required_capabilities"] or "[]")
            candidates = []

            for agent_id, info in self.agents.items():
                if info.get("status") != "online":
                    continue
                agent_caps = info.get("capabilities", [])
                # 如果是 orchestrator 调度，考虑 managed_agents 范围
                creator = task.get("creator_agent_id", "")
                creator_info = self.agents.get(creator, {})
                if creator_info.get("role") in ("manager", "orchestrator"):
                    managed = creator_info.get("managed_agents", [])
                    if managed and agent_id not in managed:
                        continue
                if all(cap in agent_caps for cap in required_caps):
                    candidates.append(agent_id)

            if not candidates:
                return {"status": "no_candidate", "required_caps": required_caps}

            # 按最近心跳时间选最活跃的
            candidates.sort(
                key=lambda aid: self.agents[aid].get("last_heartbeat", ""),
                reverse=True,
            )
            best_candidate = candidates[0]

            # 2. 第一阶段披露
            disclosed = await self._execute_disclosure_phase(
                task_id=task_id, agent_id=best_candidate, phase=1
            )

            await db_facade.execute(
                """
                    UPDATE tasks SET assigned_agent_id = ?, status = 'assigned',
                        current_phase = 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                (best_candidate, datetime.now(timezone.utc).isoformat(), task_id),
            )

            # 通知被调度的 Agent
            if best_candidate in self.active_ws:
                await self.active_ws[best_candidate].send_json({
                    "msg_type": "task_assigned",
                    "task_id": task_id,
                    "description": task["description"][:80] + "...",
                    "disclosure_phase": 1,
                    "available_memories": disclosed["count"],
                })

            # P7: 持久化通知
            await self.create_notification(
                best_candidate, "task_assigned",
                f"新任务: {task['description'][:40]}",
                body=task["description"],
                related_task_id=task_id,
            )

            # P2B: 通知被分配者
            await notifications.notify(best_candidate, {
                "type": "task_assigned",
                "task_id": task_id,
                "description": task["description"][:80] + "...",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await notifications.broadcast_dashboard({
                "type": "task_assigned",
                "task_id": task_id,
                "assigned_to": best_candidate,
            })

            return {
                "status": "scheduled",
                "task_id": task_id,
                "assigned_to": best_candidate,
                "disclosure_phase": 1,
                "disclosed_memories": disclosed,
            }


    async def _execute_disclosure_phase(
        self, task_id: str, agent_id: str, phase: int
    ) -> dict:
        """执行披露阶段 — 委托给 DisclosureEngine"""
        return await self.disclosure._execute_disclosure_phase(task_id, agent_id, phase)


    async def _validate_transition(self, task_id: str, new_status: TaskStatus) -> dict:
        """校验状态转换是否合法（P6）"""
        row = await db_facade.query_one("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
        if not row:
            return {"valid": False, "error": "任务不存在"}
        current = TaskStatus(row["status"])
        if new_status not in TASK_TRANSITIONS.get(current, []):
            return {
                "valid": False,
                "error": f"非法状态转换: {current.value} → {new_status.value}",
                "current_status": current.value,
            }
        return {"valid": True, "current_status": current.value}


    async def start_task(self, task_id: str, agent_id: str) -> dict:
        """开始执行任务: assigned → in_progress（P6；P1: 依赖门 fail-closed）"""
        async with self._task_lock:
            check = await self._validate_transition(task_id, TaskStatus.IN_PROGRESS)
            if not check["valid"]:
                return {"status": "error", **check}

            # P1 DAG: 依赖门 — 依赖中任一非 completed → 拒绝 + 返回缺失清单
            row = await db_facade.query_one("SELECT depends_on FROM tasks WHERE task_id = ?", (task_id,))
            if row and row["depends_on"]:
                deps = json.loads(row["depends_on"] or "[]")
                missing = await db_facade.run_sync(self._blocked_by, deps)
                if missing:
                    return {"status": "error",
                            "error": f"依赖未完成: {missing}",
                            "blocked_by": missing}

            row = await db_facade.query_one(
                "SELECT assigned_agent_id FROM tasks WHERE task_id = ?",
                (task_id,)
            )
            if not row or row["assigned_agent_id"] != agent_id:
                return {"status": "error", "error": "无权操作：不是任务的执行者"}

            now = datetime.now(timezone.utc).isoformat()
            await db_facade.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (TaskStatus.IN_PROGRESS.value, now, task_id),
            )
            return {"status": "started", "task_id": task_id, "new_status": TaskStatus.IN_PROGRESS.value}


    async def complete_task(self, task_id: str, agent_id: str, result: str = "") -> dict:
        """完成任务: in_progress → completed（P6）"""
        async with self._task_lock:
            check = await self._validate_transition(task_id, TaskStatus.COMPLETED)
            if not check["valid"]:
                return {"status": "error", **check}

            row = await db_facade.query_one(
                "SELECT assigned_agent_id FROM tasks WHERE task_id = ?",
                (task_id,)
            )
            if not row or row["assigned_agent_id"] != agent_id:
                return {"status": "error", "error": "无权操作：不是任务的执行者"}

            # P2: 父完成门 — 子任务未全部完成时父任务不可 complete（fail-closed）
            sub_row = await db_facade.query_one(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS done FROM tasks WHERE parent_task_id = ?", (task_id,))
            sub_total = sub_row["total"] or 0
            sub_done = sub_row["done"] or 0
            if sub_total > 0 and sub_done < sub_total:
                pending = [r0["task_id"] for r0 in await db_facade.query(
                    "SELECT task_id FROM tasks WHERE parent_task_id = ? AND status != 'completed'", (task_id,))]
                return {"status": "error", "error": f"还有 {sub_total - sub_done} 个子任务未完成",
                        "pending_subtasks": pending}

            now = datetime.now(timezone.utc).isoformat()
            await db_facade.execute(
                "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE task_id = ?",
                (TaskStatus.COMPLETED.value, result, now, task_id),
            )

            await self._log_event("task_complete", agent_id, {"task_id": task_id, "result": result})

            # P2B: 通知 creator
            row2 = await db_facade.query_one("SELECT creator_agent_id FROM tasks WHERE task_id = ?", (task_id,))
            if row2:
                creator_id = row2["creator_agent_id"]
                await notifications.notify(creator_id, {
                    "type": "task_completed",
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "result": result[:200],
                    "timestamp": now,
                })
                await notifications.broadcast_dashboard({
                    "type": "task_completed",
                    "task_id": task_id,
                    "agent_id": agent_id,
                })

            return {"status": "completed", "task_id": task_id, "result": result}


    async def get_subtasks(self, task_id: str) -> dict:
        """P2: 子任务列表 + 完成聚合（父任务拆解视图）"""
        rows = await db_facade.query(
            "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY updated_at DESC",
            (task_id,))
        total = len(rows)
        done = sum(1 for r in rows if r["status"] == "completed")
        return {"status": "ok", "task_id": task_id, "total": total,
                "completed_count": done, "subtasks": [dict(r) for r in rows]}


    def _subtask_summary(self, task_id: str) -> dict:
        """P2: 单任务子任务聚合（GET /tasks 列表附注用）"""
        conn = self._db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS done "
                "FROM tasks WHERE parent_task_id = ?", (task_id,)).fetchone()
        finally:
            conn.close()
        return {"total": row["total"] or 0, "completed": row["done"] or 0}


    async def fail_task(self, task_id: str, agent_id: str, reason: str = "") -> dict:

        """任务失败: in_progress → failed（P6）"""
        async with self._task_lock:
            check = await self._validate_transition(task_id, TaskStatus.FAILED)
            if not check["valid"]:
                return {"status": "error", **check}

            row = await db_facade.query_one(
                "SELECT assigned_agent_id FROM tasks WHERE task_id = ?",
                (task_id,)
            )
            if not row or row["assigned_agent_id"] != agent_id:
                return {"status": "error", "error": "无权操作：不是任务的执行者"}

            now = datetime.now(timezone.utc).isoformat()
            await db_facade.execute(
                "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE task_id = ?",
                (TaskStatus.FAILED.value, reason, now, task_id),
            )

            await self._log_event("task_fail", agent_id, {"task_id": task_id, "reason": reason})

            # P2B: 通知 creator
            row2 = await db_facade.query_one("SELECT creator_agent_id FROM tasks WHERE task_id = ?", (task_id,))
            if row2:
                creator_id = row2["creator_agent_id"]
                await notifications.notify(creator_id, {
                    "type": "task_failed",
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "reason": reason[:200],
                    "timestamp": now,
                })
                await notifications.broadcast_dashboard({
                    "type": "task_failed",
                    "task_id": task_id,
                    "agent_id": agent_id,
                })

            return {"status": "failed", "task_id": task_id, "reason": reason}


    async def update_task(self, task_id: str, description: str, agent_id: str,
                          depends_on: Optional[list] = None) -> dict:
        """更新任务描述/依赖（看板编辑；P1: 支持 depends_on 更新 + 环检测）"""
        if not description or not description.strip():
            return {"status": "error", "error": "description 必填"}
        async with self._task_lock:
            # P1: 更新依赖时环检测
            if depends_on is not None:
                dep_check = await self._validate_dependencies(task_id, depends_on)
                if dep_check is not None:
                    return dep_check
            if depends_on is not None:
                updated = await db_facade.execute(
                    "UPDATE tasks SET description=?, depends_on=?, updated_at=? WHERE task_id=?",
                    (description.strip(), json.dumps(depends_on),
                     datetime.now(timezone.utc).isoformat(), task_id),
                )
            else:
                updated = await db_facade.execute(
                    "UPDATE tasks SET description=?, updated_at=? WHERE task_id=?",
                    (description.strip(), datetime.now(timezone.utc).isoformat(), task_id),
                )
            if not updated:
                return {"status": "error", "error": f"任务不存在: {task_id}"}
            await self._log_event("task_updated", agent_id, {"task_id": task_id})
            return {"status": "ok", "task_id": task_id}


    async def cancel_task(self, task_id: str, agent_id: str) -> dict:
        """取消任务: 任意非终态 → cancelled（P6）"""
        async with self._task_lock:
            check = await self._validate_transition(task_id, TaskStatus.CANCELLED)
            if not check["valid"]:
                return {"status": "error", **check}

            row = await db_facade.query_one(
                "SELECT creator_agent_id FROM tasks WHERE task_id = ?",
                (task_id,)
            )

            # 只允许 creator 或 orchestrator 取消
            agent_info = self.agents.get(agent_id, {})
            is_orchestrator = agent_info.get("role") == "orchestrator"
            is_creator = row and row["creator_agent_id"] == agent_id

            if not is_creator and not is_orchestrator:
                return {"status": "error", "error": "无权取消：不是任务创建者或调度者"}

            now = datetime.now(timezone.utc).isoformat()
            await db_facade.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (TaskStatus.CANCELLED.value, now, task_id),
            )

            await self._log_event("task_cancel", agent_id, {"task_id": task_id})
            return {"status": "cancelled", "task_id": task_id}


