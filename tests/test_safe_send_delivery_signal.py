# -*- coding: utf-8 -*-
"""终审修复：safe_send 吞失败信号 —— 回归测试

背景：CD-098 把 active_ws 直发收口到 notifications.safe_send/safe_send_text
（per-agent 发送锁）后，safe_send 清完死连接仍正常返回，调用方丢失
「发送失败」信号。修复 = safe_send/safe_send_text 返回投递成功连接数（int，
无连接或全部死亡返回 0），四个调用点判 0 按失败处理：

1. routes_sessions handoff：恢复「离线/投递失败直接报错」契约（400）；
2. routes_automation retry / manual_run：返回 ok:false + 原因，run_count 不虚增；
3. automation_scheduler 派发：失败不推进 next_run_at、last_status 记
   'dispatch_failed'，任务不再静默跳过整个周期；
4. hub_mixins/notifications 事件触发派发：改走 safe_send 判 0，对齐离线语义
   记 missed_runs，run_count 只在真实投递成功时 +1。

配方同 test_module5_delivery：CONFIG.DB_PATH monkeypatch 到 tmp_path +
db.init_db()；全局 notifications 单例用独立 agent_id 并于 finally 清理，
防跨用例污染（死信节流台账按 source=<agent> 隔离）。
"""
import asyncio
import os
import sqlite3
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as db_mod  # noqa: E402
from models import CONFIG  # noqa: E402
from notifications import NotificationManager, notifications  # noqa: E402


class _DeadWS:
    """发送必失败的 WS 替身（死连接场景）。"""

    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        raise RuntimeError("ws broken")

    async def send_text(self, text):
        raise RuntimeError("ws broken")


class _LiveWS:
    """记录帧的可用 WS 替身。"""

    def __init__(self):
        self.frames = []

    async def send_json(self, payload):
        self.frames.append(payload)

    async def send_text(self, text):
        self.frames.append(text)


@pytest.fixture()
def tmp_hub(monkeypatch, tmp_path):
    """tmp 库 + routes.app + 全局 hub（同 test_module5_delivery 配方）。"""
    from db import init_db
    from routes import app, hub

    db_path = str(tmp_path / "sig.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    monkeypatch.setattr("routes.NO_AUTH", True, raising=False)
    monkeypatch.setattr("routes_automation.NO_AUTH", True, raising=False)
    init_db()
    return {"db_path": db_path, "app": app, "hub": hub}


def _insert_agent(db_path, agent_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO agents (agent_id, agent_name, role, api_key, status)"
        " VALUES (?, ?, ?, ?, ?)",
        (agent_id, agent_id, "worker", f"key-{agent_id}", "online"))
    conn.commit()
    conn.close()


def _job_row(db_path, job_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute(
            "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def _attach_ws(hub, agent_id, ws):
    """模拟 WS 已认证接入：active_ws + notifications 推送池双注册（routes_ws 口径）。"""
    hub.active_ws[agent_id] = ws

    async def _conn():
        await notifications.connect(agent_id, ws)
    asyncio.run(_conn())


def _detach_ws(hub, agent_id):
    hub.active_ws.pop(agent_id, None)
    notifications.connections.pop(agent_id, None)


# ── 1. safe_send / safe_send_text 返回投递成功数 ──

def test_safe_send_returns_delivered_count(tmp_hub):
    """无连接 / 全死 / 部分死三种情形：返回成功连接数；死连接清理语义不变。"""
    mgr = NotificationManager()  # 独立实例：节流台账干净
    live, dead = _LiveWS(), _DeadWS()

    async def _flow():
        # agent 无连接 → 0
        assert await mgr.safe_send("sig-nobody", {"x": 1}) == 0
        assert await mgr.safe_send_text("sig-nobody", "t") == 0

        # 全死 → 0，死连接被清理
        await mgr.connect("sig-dead", _DeadWS())
        assert await mgr.safe_send("sig-dead", {"x": 2}) == 0
        assert mgr.connections.get("sig-dead") == []

        # 一死一活 → 1，活连接收到帧、死连接被清理
        await mgr.connect("sig-mixed", live)
        await mgr.connect("sig-mixed", dead)
        assert await mgr.safe_send("sig-mixed", {"x": 3}) == 1
        assert mgr.connections.get("sig-mixed") == [live]

        # 文本帧变体同口径
        assert await mgr.safe_send_text("sig-mixed", "env") == 1

    asyncio.run(_flow())
    assert {"x": 3} in live.frames and "env" in live.frames


# ── 2. handoff：死连接恢复「离线直接报错」契约 ──

def test_handoff_dead_connection_returns_400(tmp_hub):
    """active_ws 有死连接（在线判空通过）但投递零成功 → 400「不在线」，
    不再报 ok；活连接对照组 → 200 且帧送达。"""
    from starlette.testclient import TestClient

    hub = tmp_hub["hub"]
    _insert_agent(tmp_hub["db_path"], "sig-alice")
    _insert_agent(tmp_hub["db_path"], "sig-bob")
    client = TestClient(tmp_hub["app"])
    body = {"from_agent_id": "sig-alice", "to_agent_id": "sig-bob",
            "local_session_id": 1, "title": "t", "summary": "s",
            "key_facts": [], "messages": [{"role": "user", "content": "hi"}]}

    _attach_ws(hub, "sig-bob", _DeadWS())
    try:
        r = client.post("/api/v1/sessions/handoff?agent_id=sig-alice", json=body)
        assert r.status_code == 400, f"死连接应报 400: {r.status_code} {r.text}"
        assert "不在线" in (r.json().get("detail") or ""), r.json()
    finally:
        _detach_ws(hub, "sig-bob")

    # 对照：活连接 → 200 + 帧送达
    live = _LiveWS()
    _attach_ws(hub, "sig-bob", live)
    try:
        r = client.post("/api/v1/sessions/handoff?agent_id=sig-alice", json=body)
        assert r.status_code == 200, r.text
        assert any(f.get("type") == "session.handoff" for f in live.frames)
    finally:
        _detach_ws(hub, "sig-bob")


# ── 3. retry / manual_run：死连接返回 ok:false，run_count 不虚增 ──

def test_retry_and_manual_run_dead_connection_ok_false(tmp_hub):
    from starlette.testclient import TestClient

    hub = tmp_hub["hub"]
    _insert_agent(tmp_hub["db_path"], "sig-retry")
    client = TestClient(tmp_hub["app"])
    r = client.post("/api/v1/automation/jobs?agent_id=sig-retry",
                    json={"instruction": "每晚汇总", "name": "sig汇总"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    _attach_ws(hub, "sig-retry", _DeadWS())
    try:
        r = client.post("/api/v1/automation/missed/retry?agent_id=sig-retry",
                        json={"job_id": job_id})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False and "deliver" in r.json()["error"], r.json()

        r = client.post(f"/api/v1/automation/jobs/{job_id}/run?agent_id=sig-retry")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False and "deliver" in r.json()["error"], r.json()
    finally:
        _detach_ws(hub, "sig-retry")

    row = _job_row(tmp_hub["db_path"], job_id)
    assert row["run_count"] == 0, "零投递不得虚增 run_count"

    # 对照：活连接 → ok:true + run_count +1
    live = _LiveWS()
    _attach_ws(hub, "sig-retry", live)
    try:
        r = client.post(f"/api/v1/automation/jobs/{job_id}/run?agent_id=sig-retry")
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert any(isinstance(f, dict) and f.get("type") == "automation.run"
                   for f in live.frames)
    finally:
        _detach_ws(hub, "sig-retry")
    assert _job_row(tmp_hub["db_path"], job_id)["run_count"] == 1


# ── 4. scheduler：派发失败不推进 next_run，last_status 记失败 ──

def test_scheduler_dead_connection_does_not_advance_next_run(tmp_hub):
    """active_ws 有死连接时：safe_send_text 返回 0 → last_status='dispatch_failed'、
    next_run_at 保持原值（下 tick 重投本周期）、run_count 不增——
    不再静默跳过整个调度周期。"""
    import datetime as _dt

    from routes_automation import automation_scheduler

    hub = tmp_hub["hub"]
    db_path = tmp_hub["db_path"]
    _insert_agent(db_path, "sig-sched")

    past = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(
        "INSERT INTO automation_jobs (name, trigger_type, trigger_spec, schedule_kind,"
        " instruction, owner_agent_id, enabled, next_run_at)"
        " VALUES ('sig周期任务', 'schedule', '60', 'every', '巡检', 'sig-sched', 1, ?)",
        (past,))
    job_id = c.lastrowid
    conn.commit()
    conn.close()

    _attach_ws(hub, "sig-sched", _DeadWS())

    async def _run():
        task = asyncio.create_task(automation_scheduler(hub))
        try:
            # scheduler 起始 sleep(5)，轮询等待首个 tick 完成派发
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                row = _job_row(db_path, job_id)
                if row["last_status"] == "dispatch_failed":
                    break
                await asyncio.sleep(0.2)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_run())
    finally:
        _detach_ws(hub, "sig-sched")

    row = _job_row(db_path, job_id)
    assert row["last_status"] == "dispatch_failed", f"应记失败状态: {row}"
    assert row["next_run_at"] == past, f"派发失败不得推进 next_run: {row['next_run_at']}"
    assert row["run_count"] == 0, "零投递不得虚增 run_count"


# ── 5. 事件触发派发：run_count 只在真实投递成功时 +1 ──

def test_event_dispatch_dead_connection_no_run_count_inflation(tmp_hub):
    """hub_mixins 事件触发自动化：死连接 → 对齐离线语义记 missed_runs，
    run_count 不虚增、last_status 不记 dispatched；活连接对照 → run_count+1。"""
    hub = tmp_hub["hub"]
    db_path = tmp_hub["db_path"]
    _insert_agent(db_path, "sig-evt")

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(
        "INSERT INTO automation_jobs (name, trigger_type, trigger_spec, instruction,"
        " owner_agent_id, enabled, allow_auto_source)"
        " VALUES ('sig事件任务', 'event', 'notification.created', '处理通知',"
        " 'sig-evt', 1, 1)")
    job_id = c.lastrowid
    conn.commit()
    conn.close()

    # 死连接：run_count 不虚增，记 missed_runs
    _attach_ws(hub, "sig-evt", _DeadWS())
    try:
        asyncio.run(hub.create_notification("sig-evt", "automation", "t1",
                                            source="test"))
    finally:
        _detach_ws(hub, "sig-evt")
    row = _job_row(db_path, job_id)
    assert row["run_count"] == 0, f"零投递不得虚增 run_count: {row}"
    assert row["missed_runs"] == 1, f"应对齐离线语义记 missed: {row}"
    assert row["last_status"] != "dispatched", f"不得伪记 dispatched: {row}"

    # 活连接对照：投递成功 → run_count+1 + dispatched + 帧送达
    live = _LiveWS()
    _attach_ws(hub, "sig-evt", live)
    try:
        asyncio.run(hub.create_notification("sig-evt", "automation", "t2",
                                            source="test"))
    finally:
        _detach_ws(hub, "sig-evt")
    row = _job_row(db_path, job_id)
    assert row["run_count"] == 1, f"真实投递应 +1: {row}"
    assert row["last_status"] == "dispatched", row
    assert any(isinstance(f, dict) and f.get("type") == "automation.run"
               for f in live.frames)
