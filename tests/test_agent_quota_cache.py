# -*- coding: utf-8 -*-
"""CD-040（2026-09-14）：O4 配额快照缓存 —— 每请求同步查 agent_quotas 改进程内快照。

原实现：`_agent_quota_ok()` 对每个已认证请求 `sqlite3.connect` + SELECT agent_quotas，
同步跑在事件循环里（与写缓冲 BEGIN IMMEDIATE 争锁）。
现：`routes_common.agent_quotas_snapshot()` 内存快照（TTL 30s，过期经 to_thread 单飞刷新），
写点（POST /api/v1/agents/quota）调用 invalidate_agent_quotas() 立即失效。
"""
import asyncio
import os
import sqlite3

import pytest

import routes
import routes_common as rc


def _mk_db(tmp_path, rows):
    p = os.path.join(str(tmp_path), "hub.db")
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE agent_quotas (agent_id TEXT PRIMARY KEY, qps_limit REAL, "
        "mode TEXT, window_sec REAL, burst INTEGER)"
    )
    for r in rows:
        c.execute("INSERT INTO agent_quotas VALUES (?, ?, ?, ?, ?)", r)
    c.commit()
    c.close()
    return p


@pytest.fixture(autouse=True)
def _reset_snapshot():
    rc.invalidate_agent_quotas()
    rc._QUOTA_SNAPSHOTS.clear()
    routes._agent_quota_hits.clear() if hasattr(routes, "_agent_quota_hits") else None
    yield
    rc.invalidate_agent_quotas()
    rc._QUOTA_SNAPSHOTS.clear()


def test_snapshot_loaded_once(tmp_path, monkeypatch):
    """TTL 内多次取快照只读库一次（原实现每请求一次）。"""
    db = _mk_db(tmp_path, [("a1", 50.0, "reject", 1.0, 3)])
    monkeypatch.setattr(rc.CONFIG, "DB_PATH", db)
    calls = {"n": 0}
    real = rc._load_agent_quotas_sync

    def wrapped():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(rc, "_load_agent_quotas_sync", wrapped)
    s1 = asyncio.run(rc.agent_quotas_snapshot())
    s2 = asyncio.run(rc.agent_quotas_snapshot())
    s3 = asyncio.run(rc.agent_quotas_snapshot())
    assert calls["n"] == 1, "TTL 内应只读库 1 次，实测 %d" % calls["n"]
    assert s1["a1"] == (50.0, "reject", 1.0, 3)
    assert s2 is s3 or s2 == s3


def test_invalidate_forces_reload(tmp_path, monkeypatch):
    """写点失效：invalidate_agent_quotas() 后下一次强制重载。"""
    db = _mk_db(tmp_path, [("a1", 50.0, "alert_only", 1.0, 3)])
    monkeypatch.setattr(rc.CONFIG, "DB_PATH", db)
    calls = {"n": 0}
    real = rc._load_agent_quotas_sync

    def wrapped():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(rc, "_load_agent_quotas_sync", wrapped)
    asyncio.run(rc.agent_quotas_snapshot())
    n = calls["n"]
    rc.invalidate_agent_quotas()
    asyncio.run(rc.agent_quotas_snapshot())
    assert calls["n"] == n + 1, "失效后必须重载"


def test_missing_table_is_empty_snapshot(tmp_path, monkeypatch):
    """表缺失/异常 → 空快照（等价旧行为「无行即不限流」），不抛异常。"""
    db = os.path.join(str(tmp_path), "empty.db")
    sqlite3.connect(db).close()
    monkeypatch.setattr(rc.CONFIG, "DB_PATH", db)
    assert asyncio.run(rc.agent_quotas_snapshot()) == {}


def test_quota_semantics_preserved(tmp_path, monkeypatch):
    """语义不变：无行 → 放行；mode=reject 超限 → 拦；mode=alert_only 超限 → 放行。"""
    db = _mk_db(tmp_path, [("rejector", 1.0, "reject", 1.0, 0),
                           ("alerts", 1.0, "alert_only", 1.0, 0)])
    monkeypatch.setattr(rc.CONFIG, "DB_PATH", db)

    async def _run():
        scope = {"client": ("127.0.0.1", 1), "path": "/api/v1/knowledge"}
        # 无行 agent → 放行
        assert await routes._agent_quota_ok(scope, "unknown-agent", "/api/v1/knowledge") is True
        # reject：limit = max(1, 1.0*1.0) = 1，第 2 次超限 → False
        r1 = await routes._agent_quota_ok(scope, "rejector", "/api/v1/knowledge")
        r2 = await routes._agent_quota_ok(scope, "rejector", "/api/v1/knowledge")
        # alert_only：超限仍放行
        a1 = await routes._agent_quota_ok(scope, "alerts", "/api/v1/knowledge")
        a2 = await routes._agent_quota_ok(scope, "alerts", "/api/v1/knowledge")
        return r1, r2, a1, a2

    r1, r2, a1, a2 = asyncio.run(_run())
    assert r1 is True and r2 is False, "mode=reject 超限必须返回 False（拦）"
    assert a1 is True and a2 is True, "mode=alert_only 超限仍放行"
