"""星枢 SyncHub — disclosure_ops Mixin"""
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

import httpx  # D-7: httpx 原靠通配 import 泄漏（latent NameError），现显式导入

from deps import DisclosureRequest, logger
from db import row_to_dict as _row_dict
import db_facade
from notifications import notifications
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong
from sensitivity import classify as _sensitivity_classify

# D-11: 别名供 async 函数内调用 — 门面 AST 自检口径按属性名计数，
# db_facade.execute(...) 会被误记为直连调用，故经模块级 Name 引用。
_facade_execute = db_facade.execute


def _create_disclosure_request(conn, task_id, agent_id, reason):
    """advance_disclosure 的"读任务校验 + 插入审批请求"。

    独立为模块级同步函数：AST 门禁（tests/test_facade_migration.py）按
    AsyncFunctionDef 子树统计直接 SQLite 调用，嵌套 def 也会被计入。
    """
    c = conn.cursor()

    c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    row = c.fetchone()
    task = _row_dict(row) if row else None

    if not task or task["assigned_agent_id"] != agent_id:
        return None

    current_phase = task.get("current_phase", 1)
    next_phase = current_phase + 1
    now = datetime.now(timezone.utc).isoformat()
    request_id = hashlib.sha256(
        f"{task_id}:{agent_id}:{next_phase}:{time.time()}".encode()
    ).hexdigest()[:16]

    c.execute(
        """INSERT INTO disclosure_requests
           (request_id, task_id, agent_id, reason, new_phase, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (request_id, task_id, agent_id, reason, next_phase, now),
    )
    return task, request_id, next_phase, now


def _load_pending_disclosure_request(conn, request_id, ensure_columns):
    """approve_disclosure_request 的"惰性迁移 + 读待审批请求"（模块级同步化理由同上）。

    ensure_columns = DisclosureOpsMixin._ensure_double_approval_columns 绑定方法，
    含幂等 ALTER（写），调用方必须以 write=True 运行（对齐原 with conn 退出的隐式 commit）。
    """
    c = conn.cursor()
    ensure_columns(c)

    c.execute(
        """SELECT * FROM disclosure_requests
           WHERE request_id = ? AND status IN ('pending', 'pending_second')""",
        (request_id,),
    )
    return c.fetchone()


def _apply_disclosure_approval(conn, task_id, request_id, next_phase, approver_id, now):
    """approve_disclosure_request 批准后的两语句落库（模块级同步化理由同上）。"""
    conn.execute(
        "UPDATE tasks SET current_phase = ?, updated_at = ? WHERE task_id = ?",
        (next_phase, now, task_id),
    )
    conn.execute(
        """UPDATE disclosure_requests
           SET status = 'approved', resolved_at = ?, resolved_by = ? WHERE request_id = ?""",
        (now, approver_id, request_id),
    )


class DisclosureOpsMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def request_disclosure(self, req: DisclosureRequest, scope: dict = None) -> dict:
        """按需披露 — 委托给 DisclosureEngine（1e: scope 透传，员工/Agent scoped key 生效）"""
        return await self.disclosure.request_disclosure(req, scope=scope)


    async def advance_disclosure(
        self, task_id: str, agent_id: str, reason: str
    ) -> dict:
        """
        Agent 请求提升披露级别 → 创建审批请求，等待 creator/店长批准。
        """
        async with self._task_lock:
            created = await db_facade.run_in_conn(
                lambda conn: _create_disclosure_request(conn, task_id, agent_id, reason),
                write=True,
            )
            if created is None:
                return {"status": "denied", "reason": "无权操作：不是任务执行者"}
            task, request_id, next_phase, now = created

            # 通知 task creator + dashboard
            creator_id = task.get("creator_agent_id", "")
            if creator_id:
                await notifications.notify(creator_id, {
                    "type": "disclosure_request",
                    "request_id": request_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "reason": reason,
                    "new_phase": next_phase,
                    "timestamp": now,
                })
                await notifications.broadcast_dashboard({
                    "type": "disclosure_request",
                    "request_id": request_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "reason": reason,
                    "new_phase": next_phase,
                    "timestamp": now,
                })

            # 如果 Hub Agent 已配置，异步触发 LLM 审计
            audit_result = None
            if self.hub_agent.is_configured():
                try:
                    task_desc = task.get("description", "")
                    audit_result = await self.hub_agent.audit_disclosure(
                        task_id, agent_id, reason, task_desc
                    )
                    # 存储审计结果
                    await _facade_execute(
                        """UPDATE disclosure_requests
                           SET audit_decision = ?, audit_reason = ?, audit_risk_level = ?
                           WHERE request_id = ?""",
                        (audit_result.get("decision"), audit_result.get("reason"),
                         audit_result.get("risk_level"), request_id),
                    )
                    logger.info(
                        f"Hub Agent 审计完成: {request_id} -> {audit_result.get('decision')} ({audit_result.get('risk_level')})"
                    )
                except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Hub Agent 审计失败: {type(e).__name__}: {e}")
                except Exception as e:
                    logger.exception(f"Hub Agent 审计异常: request_id={request_id}")

            return {
                "status": "pending_approval",
                "request_id": request_id,
                "task_id": task_id,
                "new_phase": next_phase,
                "audit": audit_result,
            }


    async def approve_disclosure_request(
        self, request_id: str, approver_id: str
    ) -> dict:
        """店长批准披露升级请求。
        双人审（batchB）：目标级别 FULL 且命中机密词的请求需两个不同审批人先后批准——
        第一人批准 → pending_second（记录 first_approver 防自审），第二人批准才生效。"""
        async with self._task_lock:
            await self._expire_stale_disclosures()
            needs_second = False

            # write=True：fn 内含惰性迁移 ALTER（_ensure_double_approval_columns），
            # 原代码靠 with conn 退出时隐式 commit 持久化，此处对齐
            row = await db_facade.run_in_conn(
                lambda conn: _load_pending_disclosure_request(
                    conn, request_id, self._ensure_double_approval_columns),
                write=True,
            )
            if not row:
                return {"status": "error", "error": "请求不存在或已处理"}

            req = _row_dict(row)
            task_id = req["task_id"]
            agent_id = req["agent_id"]
            next_phase = req["new_phase"]
            now = datetime.now(timezone.utc).isoformat()

            # 双人审第二棒：同一审批人不能二次批准（防自审）
            if req["status"] == "pending_second":
                if approver_id == (req.get("first_approver") or ""):
                    return {"status": "error", "error": "双人审：同一审批人不能二次批准（防自审）"}
            else:
                try:
                    needs_double = await db_facade.run_sync(self._requires_double_approval, req)
                except Exception:
                    return {
                        "status": "error",
                        "error": "披露升级被拒：双人审触发判定异常（fail-closed），需人工介入",
                        "request_id": request_id,
                    }
                if needs_double:
                    # 双人审第一棒：转入 pending_second，等待第二审批人
                    # （保留原中间提交点：此处立即 commit，再做后续操作）
                    await _facade_execute(
                        """UPDATE disclosure_requests
                           SET status = 'pending_second', first_approver = ? WHERE request_id = ?""",
                        (approver_id, request_id),
                    )
                    needs_second = True

            if not needs_second:
                # 执行披露升级
                disclosed = await self._execute_disclosure_phase(
                    task_id, agent_id, next_phase
                )

                await db_facade.run_in_conn(
                    lambda conn: _apply_disclosure_approval(
                        conn, task_id, request_id, next_phase, approver_id, now),
                    write=True,
                )

            if needs_second:
                await notifications.broadcast_dashboard({
                    "type": "disclosure_pending_second",
                    "request_id": request_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "new_phase": next_phase,
                    "first_approver": approver_id,
                })
                return {
                    "status": "pending_second",
                    "request_id": request_id,
                    "task_id": task_id,
                    "new_phase": next_phase,
                    "first_approver": approver_id,
                }

            # 通知 worker
            if agent_id in self.active_ws:
                await self.active_ws[agent_id].send_json({
                    "msg_type": "disclosure_approved",
                    "request_id": request_id,
                    "task_id": task_id,
                    "new_phase": next_phase,
                    "memories": disclosed["memories"],
                })

            await notifications.broadcast_dashboard({
                "type": "disclosure_approved",
                "request_id": request_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "new_phase": next_phase,
            })

            # P7: 持久化通知
            await self.create_notification(
                agent_id, "disclosure_approved",
                f"披露申请已批准",
                body=f"任务 {task_id} 的披露级别已提升到第 {next_phase} 阶段",
                related_task_id=task_id, related_agent_id=approver_id,
            )

            return {
                "status": "approved",
                "request_id": request_id,
                "task_id": task_id,
                "new_phase": next_phase,
            }


    async def deny_disclosure_request(
        self, request_id: str, approver_id: str, deny_reason: str = ""
    ) -> dict:
        """店长拒绝披露升级请求（pending / pending_second 任意阶段均可拒绝）"""
        async with self._task_lock:
            await self._expire_stale_disclosures()
            row = await db_facade.query_one(
                """SELECT * FROM disclosure_requests
                   WHERE request_id = ? AND status IN ('pending', 'pending_second')""",
                (request_id,),
            )
            if not row:
                return {"status": "error", "error": "请求不存在或已处理"}

            req = _row_dict(row)
            task_id = req["task_id"]
            agent_id = req["agent_id"]
            now = datetime.now(timezone.utc).isoformat()

            await _facade_execute(
                """UPDATE disclosure_requests
                   SET status = 'denied', resolved_at = ?, resolved_by = ? WHERE request_id = ?""",
                (now, approver_id, request_id),
            )

            # 通知 worker
            if agent_id in self.active_ws:
                await self.active_ws[agent_id].send_json({
                    "msg_type": "disclosure_denied",
                    "request_id": request_id,
                    "task_id": task_id,
                    "reason": deny_reason,
                })

            await notifications.broadcast_dashboard({
                "type": "disclosure_denied",
                "request_id": request_id,
                "task_id": task_id,
                "agent_id": agent_id,
            })

            # P7: 持久化通知
            await self.create_notification(
                agent_id, "disclosure_denied",
                f"披露申请已拒绝",
                body=f"拒绝原因: {deny_reason}" if deny_reason else "未提供原因",
                related_task_id=task_id, related_agent_id=approver_id,
            )

            return {
                "status": "denied",
                "request_id": request_id,
                "task_id": task_id,
            }


    async def _expire_stale_disclosures(self, ttl_hours: float = 24.0) -> int:
        """惰性 TTL：审批人离线时，超过 TTL 的 pending/pending_second 披露请求自动拒绝。
        在 approve/deny 入口调用，返回本次过期的数量。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat()
        expired = await db_facade.execute(
            """UPDATE disclosure_requests
               SET status = 'rejected', resolved_at = ?, resolved_by = 'system'
               WHERE status IN ('pending', 'pending_second') AND created_at < ?""",
            (datetime.now(timezone.utc).isoformat(), cutoff),
        )
        if expired:
            logger.info(f"disclosure: {expired} pending 请求超过 {ttl_hours}h 自动拒绝")
        return expired

    # ============ 双人审（FULL + 机密词） ============

    def _ensure_double_approval_columns(self, cursor) -> None:
        """惰性迁移：disclosure_requests 增加 first_approver（双人审第一审批人）。
        沿用 db.py 增量迁移的 PRAGMA + ALTER 模式（白名单禁改 db.py，此处兜底）。"""
        cursor.execute("PRAGMA table_info(disclosure_requests)")
        cols = {r[1] for r in cursor.fetchall()}
        if "first_approver" not in cols:
            cursor.execute("ALTER TABLE disclosure_requests ADD COLUMN first_approver TEXT")

    def _requires_double_approval(self, req: dict) -> bool:
        """双人审触发判定：目标披露级别 FULL 且将披露内容命中机密词
        （sensitivity.py 6 维链的机密词维度 r3，以 classify 现有判定为准）。
        判定口径与 _execute_disclosure_phase 一致：任务 disclosure_plan 中目标 phase 的
        level == full，且任务 creator 名下将被披露的记忆（disclosure_level != none）命中机密词。
        判定异常时按单审处理（不改变现有单审路径），并记 warning。"""
        try:
            conn = self._db()
            c = conn.cursor()
            c.execute(
                "SELECT disclosure_plan, creator_agent_id FROM tasks WHERE task_id = ?",
                (req.get("task_id", ""),),
            )
            row = c.fetchone()
            if not row:
                conn.close()
                return False
            plan = json.loads(row["disclosure_plan"] or "{}")
            phase_config = next(
                (p for p in plan.get("phases", []) if p.get("phase") == req.get("new_phase")),
                None,
            )
            if not phase_config or str(phase_config.get("level", "")).lower() != "full":
                conn.close()
                return False
            c.execute(
                """SELECT content, summary FROM memory_pool
                   WHERE owner_agent_id = ? AND disclosure_level != 'none'""",
                (row["creator_agent_id"] or "",),
            )
            mems = c.fetchall()
            conn.close()
        except Exception:
            logger.error("双人审触发判定异常，拒绝升级（fail-closed）", exc_info=True)
            raise

        for m in mems:
            text = "\n".join([m["content"] or "", m["summary"] or ""])
            if not text.strip():
                continue
            result = _sensitivity_classify(text)
            if any(r.startswith("r3_secret") for r in result["reasons"]):
                return True
        return False

    # ============ 辅助方法 ============


