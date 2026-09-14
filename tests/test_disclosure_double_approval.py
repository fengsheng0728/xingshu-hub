# -*- coding: utf-8 -*-
"""
双人审（FULL + 机密词）单元测试 — 并行任务书 B
覆盖：
  1. FULL 级 + 命中机密词 → 第一人 approve 后仍为 pending_second（未生效）
  2. 第二人（不同审批人）approve → FULL 生效
  3. 同一人二次 approve 无效（防自审）
  4. deny 任意阶段直接拒绝（pending / pending_second）
  5. 普通级别（非 FULL 或未命中机密词）维持现有单审
  6. TTL 对 pending_second 同样生效（短 TTL 参数化）
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
from db import init_db
from hub_core import SyncHub

SECRET_WORD = "合同价"  # sensitivity.DEFAULT_SECRET_KEYWORDS 内置机密词


def _mk_hub(tmpdir, monkeypatch):
    tmpdb = os.path.join(tmpdir, "test.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    init_db()
    hub = SyncHub()
    return hub


def _insert_task(hub, task_id, plan_level="full", creator="creator-a", worker="worker-1"):
    plan = {"phases": [
        {"phase": 1, "level": "summary"},
        {"phase": 2, "level": plan_level},
    ]}
    now = datetime.now(timezone.utc).isoformat()
    conn = hub._db()
    c = conn.cursor()
    hub._ensure_double_approval_columns(c)
    c.execute(
        """INSERT INTO tasks
           (task_id, status, creator_agent_id, assigned_agent_id, description,
            disclosure_plan, current_phase, created_at, updated_at)
           VALUES (?, 'in_progress', ?, ?, '双人审测试任务', ?, 1, ?, ?)""",
        (task_id, creator, worker, json.dumps(plan, ensure_ascii=False), now, now),
    )
    conn.commit()
    conn.close()


def _insert_memory(hub, mem_id, owner="creator-a", content="普通工作记录"):
    now = datetime.now(timezone.utc).isoformat()
    conn = hub._db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO memory_pool
           (memory_id, owner_agent_id, memory_key, content, summary, importance,
            tags, disclosure_level, allowed_viewers, created_at, updated_at)
           VALUES (?, ?, 'k', ?, '摘要', 1.0, '[]', 'summary', '[]', ?, ?)""",
        (mem_id, owner, content, now, now),
    )
    conn.commit()
    conn.close()


def _insert_request(hub, request_id, task_id, agent_id="worker-1", new_phase=2,
                    status="pending", created_at=None, first_approver=None):
    created = created_at or datetime.now(timezone.utc).isoformat()
    conn = hub._db()
    c = conn.cursor()
    hub._ensure_double_approval_columns(c)
    if first_approver is None:
        c.execute(
            """INSERT INTO disclosure_requests
               (request_id, task_id, agent_id, reason, new_phase, status, created_at)
               VALUES (?, ?, ?, '需要完整内容', ?, ?, ?)""",
            (request_id, task_id, agent_id, new_phase, status, created),
        )
    else:
        c.execute(
            """INSERT INTO disclosure_requests
               (request_id, task_id, agent_id, reason, new_phase, status, created_at, first_approver)
               VALUES (?, ?, ?, '需要完整内容', ?, ?, ?, ?)""",
            (request_id, task_id, agent_id, new_phase, status, created, first_approver),
        )
    conn.commit()
    conn.close()


def _get_request(hub, request_id):
    conn = hub._db()
    c = conn.cursor()
    c.execute("SELECT * FROM disclosure_requests WHERE request_id = ?", (request_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _get_task_phase(hub, task_id):
    conn = hub._db()
    c = conn.cursor()
    c.execute("SELECT current_phase FROM tasks WHERE task_id = ?", (task_id,))
    phase = c.fetchone()["current_phase"]
    conn.close()
    return phase


def _approve(hub, request_id, approver):
    return asyncio.run(hub.approve_disclosure_request(request_id, approver))


def _deny(hub, request_id, approver, reason=""):
    return asyncio.run(hub.deny_disclosure_request(request_id, approver, reason))


# ═══════════════════════════════
# ① FULL + 机密词 → 第一人后仍 pending_second
# ═══════════════════════════════

def test_full_secret_first_approver_enters_pending_second(tmp_path, monkeypatch):
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-1", plan_level="full")
    _insert_memory(hub, "m-dbl-1", content=f"本季度{SECRET_WORD}明细已归档")
    _insert_request(hub, "r-dbl-1", "t-dbl-1")

    r = _approve(hub, "r-dbl-1", "mgr-a")
    assert r["status"] == "pending_second"
    assert r["first_approver"] == "mgr-a"

    req = _get_request(hub, "r-dbl-1")
    assert req["status"] == "pending_second"
    assert req["first_approver"] == "mgr-a"
    # 披露未生效：任务阶段未提升
    assert _get_task_phase(hub, "t-dbl-1") == 1


# ═══════════════════════════════
# ② 第二人 approve → FULL 生效
# ═══════════════════════════════

def test_second_approver_completes_full(tmp_path, monkeypatch):
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-2", plan_level="full")
    _insert_memory(hub, "m-dbl-2", content=f"{SECRET_WORD}谈判底线")
    _insert_request(hub, "r-dbl-2", "t-dbl-2")

    r1 = _approve(hub, "r-dbl-2", "mgr-a")
    assert r1["status"] == "pending_second"

    r2 = _approve(hub, "r-dbl-2", "mgr-b")
    assert r2["status"] == "approved"

    req = _get_request(hub, "r-dbl-2")
    assert req["status"] == "approved"
    assert req["resolved_by"] == "mgr-b"
    assert req["first_approver"] == "mgr-a"
    # 披露生效：任务阶段提升到 2
    assert _get_task_phase(hub, "t-dbl-2") == 2


# ═══════════════════════════════
# ③ 同一人二次 approve 无效（防自审）
# ═══════════════════════════════

def test_same_approver_second_approve_rejected(tmp_path, monkeypatch):
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-3", plan_level="full")
    _insert_memory(hub, "m-dbl-3", content=f"内含{SECRET_WORD}")
    _insert_request(hub, "r-dbl-3", "t-dbl-3")

    r1 = _approve(hub, "r-dbl-3", "mgr-a")
    assert r1["status"] == "pending_second"

    r2 = _approve(hub, "r-dbl-3", "mgr-a")
    assert r2["status"] == "error"
    assert "自审" in r2["error"]

    req = _get_request(hub, "r-dbl-3")
    assert req["status"] == "pending_second"  # 状态未被推进
    assert _get_task_phase(hub, "t-dbl-3") == 1


# ═══════════════════════════════
# ④ deny 任意阶段直接拒绝
# ═══════════════════════════════

def test_deny_at_pending(tmp_path, monkeypatch):
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-4a", plan_level="full")
    _insert_memory(hub, "m-dbl-4a", content=f"{SECRET_WORD}相关")
    _insert_request(hub, "r-dbl-4a", "t-dbl-4a")

    r = _deny(hub, "r-dbl-4a", "mgr-a", "敏感内容不予披露")
    assert r["status"] == "denied"
    assert _get_request(hub, "r-dbl-4a")["status"] == "denied"
    assert _get_task_phase(hub, "t-dbl-4a") == 1


def test_deny_at_pending_second(tmp_path, monkeypatch):
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-4b", plan_level="full")
    _insert_memory(hub, "m-dbl-4b", content=f"{SECRET_WORD}相关")
    _insert_request(hub, "r-dbl-4b", "t-dbl-4b")

    r1 = _approve(hub, "r-dbl-4b", "mgr-a")
    assert r1["status"] == "pending_second"

    r2 = _deny(hub, "r-dbl-4b", "mgr-b", "二审否决")
    assert r2["status"] == "denied"
    req = _get_request(hub, "r-dbl-4b")
    assert req["status"] == "denied"
    assert req["resolved_by"] == "mgr-b"
    assert _get_task_phase(hub, "t-dbl-4b") == 1


# ═══════════════════════════════
# ⑤ 普通级别维持现有单审
# ═══════════════════════════════

def test_non_full_level_single_approval(tmp_path, monkeypatch):
    """目标级别非 FULL（summary）即使命中机密词也维持单审"""
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-5a", plan_level="summary")
    _insert_memory(hub, "m-dbl-5a", content=f"{SECRET_WORD}相关")
    _insert_request(hub, "r-dbl-5a", "t-dbl-5a")

    r = _approve(hub, "r-dbl-5a", "mgr-a")
    assert r["status"] == "approved"
    assert _get_request(hub, "r-dbl-5a")["status"] == "approved"
    assert _get_task_phase(hub, "t-dbl-5a") == 2


def test_full_without_secret_word_single_approval(tmp_path, monkeypatch):
    """目标 FULL 但未命中机密词 → 维持单审"""
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-5b", plan_level="full")
    _insert_memory(hub, "m-dbl-5b", content="普通工作记录，不含敏感信息")
    _insert_request(hub, "r-dbl-5b", "t-dbl-5b")

    r = _approve(hub, "r-dbl-5b", "mgr-a")
    assert r["status"] == "approved"
    assert _get_task_phase(hub, "t-dbl-5b") == 2


# ═══════════════════════════════
# ⑥ TTL 对 pending_second 生效
# ═══════════════════════════════

def test_ttl_expires_pending_second(tmp_path, monkeypatch):
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-6", plan_level="full")
    _insert_memory(hub, "m-dbl-6", content=f"{SECRET_WORD}相关")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _insert_request(hub, "r-dbl-6", "t-dbl-6", status="pending_second",
                    created_at=old, first_approver="mgr-a")

    expired = asyncio.run(hub._expire_stale_disclosures(ttl_hours=1.0))
    assert expired == 1
    req = _get_request(hub, "r-dbl-6")
    assert req["status"] == "rejected"
    assert req["resolved_by"] == "system"

    # 过期后 approve 不再生效
    r = _approve(hub, "r-dbl-6", "mgr-b")
    assert r["status"] == "error"
    assert _get_task_phase(hub, "t-dbl-6") == 1


def test_ttl_keeps_fresh_pending_second(tmp_path, monkeypatch):
    """未超 TTL 的 pending_second 不被回收"""
    hub = _mk_hub(str(tmp_path), monkeypatch)
    _insert_task(hub, "t-dbl-6b", plan_level="full")
    _insert_memory(hub, "m-dbl-6b", content=f"{SECRET_WORD}相关")
    _insert_request(hub, "r-dbl-6b", "t-dbl-6b", status="pending_second",
                    first_approver="mgr-a")

    expired = asyncio.run(hub._expire_stale_disclosures(ttl_hours=24.0))
    assert expired == 0


# ═══════════════════════════════
# ⑦ 判定异常 fail-closed（2026-09-02 验收修正）
# ═══════════════════════════════

def test_judge_failure_fail_closed(tmp_path, monkeypatch):
    """sensitivity 判定异常 → 拒绝升级，状态保持 pending，不降级单审（fail-closed）"""
    import hub_mixins.disclosure_ops as dop

    hub = _mk_hub(str(tmp_path), monkeypatch)

    def boom(text):
        raise RuntimeError("判定引擎不可用")

    monkeypatch.setattr(dop, "_sensitivity_classify", boom)

    _insert_task(hub, "t-dbl-7", plan_level="full")
    _insert_memory(hub, "m-dbl-7", content=f"{SECRET_WORD}相关")
    _insert_request(hub, "r-dbl-7", "t-dbl-7")

    r = _approve(hub, "r-dbl-7", "mgr-a")
    assert r["status"] == "error"
    assert "fail-closed" in r.get("error", "")
    req = _get_request(hub, "r-dbl-7")
    assert req["status"] == "pending"  # 未被放行、未提升
    assert _get_task_phase(hub, "t-dbl-7") == 1
