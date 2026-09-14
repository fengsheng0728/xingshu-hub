# -*- coding: utf-8 -*-
"""tests/test_cleanup_readlog.py — CD-021：gateway_read_log 保留策略

背景：gateway_read_log 只增不删（每次网关读 +1 行），events 表有清理而它没有。
修复：_run_cleanup 增加读审计保留清理（默认 RETENTION_READLOG_DAYS=90 天，
独立 try——表缺失/异常不连累 events/memory/tasks 清理）。

覆盖：
① 超过保留期的旧行被删（created_at 是 datetime('now') 无 T 格式，cutoff 同格式）
② 保留期内新行不受影响
③ 表不存在时清理静默跳过（不抛异常、不影响其它清理）
"""
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub_core import hub
from models import CONFIG


def _make_db(db_path: str, with_readlog: bool = True):
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, agent_id TEXT, payload TEXT, timestamp TEXT)""")
    conn.execute("""CREATE TABLE memory_pool (
        memory_id TEXT PRIMARY KEY, owner_agent_id TEXT, kind TEXT,
        content TEXT, disclosure_level TEXT DEFAULT 'summary',
        importance REAL DEFAULT 0, created_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, description TEXT, status TEXT,
        updated_at TEXT DEFAULT (datetime('now')))""")
    if with_readlog:
        conn.execute("""CREATE TABLE gateway_read_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester TEXT NOT NULL, auth_mode TEXT DEFAULT '',
            scope_json TEXT DEFAULT '', kind TEXT NOT NULL,
            query TEXT DEFAULT '', target TEXT DEFAULT '',
            granted_level TEXT DEFAULT '', item_count INTEGER DEFAULT 0,
            stripped_chunks INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    conn.close()


def _iso_now_minus(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _sql_now_minus(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM gateway_read_log").fetchone()[0]
    conn.close()
    return n


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cleanup.db")
    _make_db(db_path)
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    yield db_path


def _insert(db_path: str, created_at: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO gateway_read_log (requester, kind, created_at)"
        " VALUES ('probe', 'memory', ?)", (created_at,))
    conn.commit()
    conn.close()


def test_cleanup_removes_expired_keeps_fresh(env):
    """超过 90 天保留期的旧行被删,保留期内新行不动。"""
    db = env
    _insert(db, _sql_now_minus(95))   # 95 天前 → 删
    _insert(db, _sql_now_minus(3))    # 3 天前 → 留
    _insert(db, _sql_now_minus(89))   # 89 天前(边界内)→ 留
    asyncio.run(hub._run_cleanup())
    assert _count(db) == 2, "95 天旧行应被清理,89/3 天行保留"


def test_cleanup_boundary_exact_90_days(env):
    """满 90 天(created_at < cutoff,严格小于):90 天整再早 3 秒的行必删。"""
    db = env
    ts = (datetime.now(timezone.utc) - timedelta(days=90, seconds=3)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    _insert(db, ts)
    asyncio.run(hub._run_cleanup())
    assert _count(db) == 0, "满 90 天的行应被清理"


def test_cleanup_missing_table_silent(tmp_path, monkeypatch):
    """gateway_read_log 表不存在(老库):清理静默跳过,不抛异常。"""
    db_path = str(tmp_path / "old.db")
    _make_db(db_path, with_readlog=False)  # 老库:无 gateway_read_log 表
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    # 不抛异常即通过(events/memory/tasks 清理正常执行)
    asyncio.run(hub._run_cleanup())
