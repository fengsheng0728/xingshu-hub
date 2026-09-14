"""P1: 任务依赖 DAG — T1-1 环检测 / T1-2 依赖门 / T1-5 向后兼容（先红后绿）

隔离：独立临时 DB（改 CONFIG.DB_PATH），测完恢复。fixture 提供 SyncHub 实例。
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import CONFIG, TaskStatus, TaskCreate  # noqa: E402
from hub_core import SyncHub  # noqa: E402


@pytest.fixture
def hub(monkeypatch):
    """独立临时 DB 的 Hub 实例（tasks 表含 depends_on 列）"""
    tmpdir = tempfile.mkdtemp(prefix="dag-test-")
    db_path = os.path.join(tmpdir, "dag.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, status TEXT, creator_agent_id TEXT,
        assigned_agent_id TEXT, description TEXT, required_capabilities TEXT,
        required_memories TEXT, disclosure_plan TEXT, current_phase INTEGER DEFAULT 1,
        priority INTEGER, result TEXT, created_at TEXT, updated_at TEXT,
        depends_on TEXT DEFAULT '[]', parent_task_id TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT,
        agent_id TEXT, payload TEXT, timestamp TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, type TEXT, title TEXT,
        body TEXT, related_task_id TEXT, related_agent_id TEXT, is_read INTEGER DEFAULT 0,
        created_at TEXT, source TEXT DEFAULT '', artifact_path TEXT DEFAULT '',
        channel_status TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS disclosure_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, from_agent_id TEXT,
        to_agent_id TEXT, memory_id TEXT, disclosed_level TEXT, disclosed_content TEXT,
        disclosed_at TEXT, reason TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_pool (
        memory_id TEXT PRIMARY KEY, owner_agent_id TEXT, memory_key TEXT,
        content TEXT, summary TEXT, embedding BLOB, importance REAL,
        tags TEXT, disclosure_level TEXT, disclosure_scope TEXT,
        allowed_viewers TEXT, created_at TEXT, access_count INTEGER DEFAULT 0,
        last_accessed TEXT, kind TEXT, source_session_id TEXT,
        confidence REAL, source_type TEXT, updated_at TEXT)""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)

    h = SyncHub()
    h.agents = {
        "dag-worker": {"agent_id": "dag-worker", "agent_name": "DAG测试",
                       "role": "worker", "capabilities": ["all"], "status": "online"},
    }
    yield h
    shutil.rmtree(tmpdir, ignore_errors=True)


def _mk(hub, task_id, desc="t", depends_on=None, creator="dag-worker"):
    import asyncio
    return asyncio.run(hub.create_task(TaskCreate(
        task_id=task_id, description=desc, creator_agent_id=creator,
        depends_on=depends_on or [])))


def _schedule(hub, task_id):
    """调度任务（自动匹配唯一在线 agent dag-worker → assigned）"""
    import asyncio
    return asyncio.run(hub.schedule_task(task_id))


def _start(hub, task_id, agent="dag-worker"):
    import asyncio
    return asyncio.run(hub.start_task(task_id, agent))


# ── T1-1 环检测 ──

def test_t11_cycle_rejected(hub):
    """A→B→C→A 环：create 阶段 400/error 拒绝并指明环路径"""
    _mk(hub, "A")
    _mk(hub, "B", depends_on=["A"])
    r = _mk(hub, "C", depends_on=["B"])
    assert r.get("status") == "created", f"C 创建失败: {r}"
    # 构造 A→B→C→A：用 update 把 A 的 depends_on 改成 [C] → 应环检测拒绝
    r3 = asyncio.run(hub.update_task("A", "A 更新", "dag-worker", depends_on=["C"]))
    assert r3.get("status") == "error", f"环应被拒绝: {r3}"
    err = json.dumps(r3, ensure_ascii=False)
    assert "环" in err or "依赖" in err, f"应指明环/依赖问题: {err}"


def test_t11_self_dep_rejected(hub):
    """自依赖：A 依赖 A 自身 → 拒绝"""
    r = _mk(hub, "self", depends_on=["self"])
    assert r.get("status") != "ok", f"自依赖应被拒绝: {r}"


def test_t11_missing_dep_rejected(hub):
    """依赖不存在的任务 id → 拒绝"""
    r = _mk(hub, "orphan", depends_on=["no-such-task"])
    assert r.get("status") != "ok", f"依赖不存在应被拒绝: {r}"


# ── T1-2 依赖门 ──

def test_t12_start_blocked_by_pending_dep(hub):
    """依赖未完成 start → 拒绝 + 返回缺失依赖清单"""
    _mk(hub, "A")
    _mk(hub, "B", depends_on=["A"])
    _schedule(hub, "A")
    _schedule(hub, "B")  # B 已分配（依赖门在 start 时生效）
    r = _start(hub, "B")
    assert r.get("status") == "error", f"依赖未完成应拒绝: {r}"
    missing = json.dumps(r, ensure_ascii=False)
    assert "A" in missing, f"应返回缺失依赖清单: {missing}"


def test_t12_start_blocked_by_failed_dep(hub):
    """依赖 failed → 拒绝（fail-closed D3）"""
    _mk(hub, "A")
    _mk(hub, "B", depends_on=["A"])
    _schedule(hub, "A")
    _schedule(hub, "B")
    _start(hub, "A")
    asyncio.run(hub.fail_task("A", "dag-worker", "测试失败"))
    r = _start(hub, "B")
    assert r.get("status") == "error", f"failed 依赖应拒绝: {r}"


def test_t12_start_blocked_by_cancelled_dep(hub):
    """依赖 cancelled → 拒绝（fail-closed D3）"""
    _mk(hub, "A")
    _mk(hub, "B", depends_on=["A"])
    _schedule(hub, "A")
    _schedule(hub, "B")
    asyncio.run(hub.cancel_task("A", "dag-worker"))
    r = _start(hub, "B")
    assert r.get("status") == "error", f"cancelled 依赖应拒绝: {r}"


def test_t12_start_ok_after_dep_complete(hub):
    """依赖完成后 start 成功；blocked_by 从 [A] 变 []"""
    _mk(hub, "A")
    _mk(hub, "B", depends_on=["A"])
    _schedule(hub, "A")
    _schedule(hub, "B")
    _start(hub, "A")
    asyncio.run(hub.complete_task("A", "dag-worker", "done"))
    r = _start(hub, "B")
    assert r.get("status") == "started", f"依赖完成后应可 start: {r}"


# ── T1-5 向后兼容 ──

def test_t15_legacy_task_no_depends_on(hub):
    """无 depends_on 的旧任务创建/启动/完成全流程不变"""
    r = _mk(hub, "legacy")  # 不传 depends_on
    assert r.get("status") in ("created", "ok"), f"旧任务创建失败: {r}"
    _schedule(hub, "legacy")
    s = _start(hub, "legacy")
    assert s.get("status") == "started", f"旧任务 start 失败: {s}"
    c = asyncio.run(hub.complete_task("legacy", "dag-worker", "ok"))
    assert c.get("status") == "completed", f"旧任务 complete 失败: {c}"
