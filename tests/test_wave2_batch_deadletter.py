# -*- coding: utf-8 -*-
"""修复波二（2026-09-24）配套测试：

1. CD-091 REST 半边：POST /api/v1/memory/batch 的 search 分支改走真实方法
   hub.memory_search(req)（旧代码调不存在的 hub.search_memory，恒 ok:false 静默吞）；
   per-item 异常补日志且不影响其他 item。
2. CD-084 死信接入：自动化连失败 ≥5 停用落死信（source=automation_job:<id>），
   retry 端点重启闭环；notify 发送失败落死信（source=notify_send:<agent_id>）且节流。

配方同 test_dead_letters / test_module5_delivery：CONFIG.DB_PATH monkeypatch 到
tmp_path + db.init_db()；端点直调或 TestClient；NO_AUTH=1 由 conftest 保证。
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as db_mod  # noqa: E402
import routes_maintenance  # noqa: E402
import routes_memory  # noqa: E402
from hub_mixins.memory import MemoryMixin  # noqa: E402
from models import CONFIG, MemoryBatchOp  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """空库：init_db 建全 schema（含 dead_letters），CONFIG.DB_PATH 指向 tmp。"""
    db_path = str(tmp_path / "wave2.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


def _fetch(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class _FakeReq:
    """最小 Request 替身：require_ops_privilege 只读 .scope['principal']。"""

    def __init__(self, principal=None):
        self.scope = {"principal": principal}


# ── CD-091 REST 半边：batch search ──

def test_batch_search_calls_memory_search_with_request(fresh_db, monkeypatch):
    """接线断言：batch search 必须构造 MemorySearchRequest 调 hub.memory_search
    （op.limit → top_k、op.kind 单值 → 单元素 list、agent_id 来自端点参数）。"""
    from hub_core import hub

    seen = {}

    async def _fake(self, req):
        seen["req"] = req
        return {"results": [{"memory_key": "k9", "content": "x"}],
                "total": 1, "degraded": True, "degraded_reason": "keyword_fallback"}

    # CD-114：类级打桩（实例级会给单例留下永久实例属性，遮蔽后续类级 monkeypatch）
    monkeypatch.setattr(MemoryMixin, "memory_search", _fake)
    ops = [MemoryBatchOp(action="search", query="例会", kind="todo", limit=7)]
    r = asyncio.run(routes_memory.api_batch_memory(
        "agent-w2", ops, current_agent="agent-w2"))

    req = seen.get("req")
    assert isinstance(req, routes_memory.MemorySearchRequest), "必须走 MemorySearchRequest"
    assert req.agent_id == "agent-w2" and req.query == "例会"
    assert req.top_k == 7 and req.kind == ["todo"]
    # 响应结构不变：{"results": [...]} 外壳 + per-item {"ok", "results"}
    item = r["results"][0]
    assert item["ok"] is True
    assert item["results"]["total"] == 1
    assert item["results"]["results"][0]["memory_key"] == "k9"


def test_batch_search_returns_real_rows(fresh_db):
    """真链路：batch store 写入后 batch search 真实命中（旧代码此用例恒 ok:false）。"""
    ops = [
        MemoryBatchOp(action="store", memory_key="w2k1",
                      content="星枢周会纪要：每周一上午同步进度"),
        MemoryBatchOp(action="search", query="周会", limit=5),
    ]
    r = asyncio.run(routes_memory.api_batch_memory(
        "agent-w2", ops, current_agent="agent-w2"))
    assert r["results"][0]["ok"] is True
    item = r["results"][1]
    assert item["ok"] is True, f"search 分支不许再走 except：{item}"
    rows = item["results"]["results"]
    assert any("周会" in (row.get("content") or "") for row in rows), rows


def test_batch_item_failure_isolated_and_logged(fresh_db, monkeypatch, caplog):
    """某 item 异常：其余 item 不受影响，且有带 item 索引+异常类型的 warning 日志。"""
    from hub_core import hub

    async def _boom(self, req):
        raise RuntimeError("search exploded")

    # CD-114：类级打桩（实例级会给单例留下永久实例属性，遮蔽后续类级 monkeypatch）
    monkeypatch.setattr(MemoryMixin, "memory_search", _boom)
    ops = [
        MemoryBatchOp(action="store", memory_key="w2k2", content="甲"),
        MemoryBatchOp(action="search", query="x"),
        MemoryBatchOp(action="store", memory_key="w2k3", content="乙"),
    ]
    with caplog.at_level(logging.WARNING, logger="xingshu.routes_memory"):
        r = asyncio.run(routes_memory.api_batch_memory(
            "agent-w2", ops, current_agent="agent-w2"))

    assert [i["ok"] for i in r["results"]] == [True, False, True]
    assert "search exploded" in r["results"][1]["error"]
    msgs = [rec.getMessage() for rec in caplog.records
            if rec.name == "xingshu.routes_memory" and rec.levelno >= logging.WARNING]
    assert any("op #1" in m and "RuntimeError" in m for m in msgs), msgs
    # 前后两条 store 真实落库
    keys = {row[0] for row in _fetch(
        fresh_db, "SELECT memory_key FROM memory_pool WHERE owner_agent_id='agent-w2'")}
    assert {"w2k2", "w2k3"} <= keys


# ── CD-084：自动化连失败停用 → 死信 → retry 重启闭环 ──

@pytest.fixture()
def tmp_hub(monkeypatch, tmp_path):
    """同 test_module5_delivery：tmp 库 + routes.app + 注册测试 Agent。"""
    from db import init_db
    from routes import app, hub  # noqa: F401

    db_path = str(tmp_path / "wave2_auto.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    monkeypatch.setattr("routes.NO_AUTH", True, raising=False)
    monkeypatch.setattr("routes_automation.NO_AUTH", True, raising=False)
    init_db()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO agents (agent_id, agent_name, role, api_key, status)"
        " VALUES (?, ?, ?, ?, ?)",
        ("agent-5", "测试员", "worker", "key-5", "online"))
    conn.commit()
    conn.close()
    return {"db_path": db_path, "app": app}


def test_automation_disable_lands_dead_letter_and_retry_reenables(tmp_hub):
    """连失败 5 次 → 任务停用 + 落死信（payload 含 job_id/title/连失败次数）；
    retry 端点对 automation_job:<id> 重置连失败计数并重新启用，死信标记已处理。"""
    from starlette.testclient import TestClient

    client = TestClient(tmp_hub["app"])
    r = client.post("/api/v1/automation/jobs?agent_id=agent-5",
                    json={"instruction": "每晚汇总", "name": "w2汇总"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    for i in range(5):
        rr = client.post("/api/v1/automation/runs?agent_id=agent-5",
                         json={"job_id": job_id, "name": "w2汇总", "status": "failed",
                               "result_summary": f"boom{i}"})
        assert rr.status_code == 200, rr.text

    enabled, fails = _fetch(
        tmp_hub["db_path"],
        "SELECT enabled, consecutive_failures FROM automation_jobs WHERE id=?",
        (job_id,))[0]
    assert enabled == 0 and fails == 5, "连失败 5 次应已停用"

    letters = _fetch(
        tmp_hub["db_path"],
        "SELECT * FROM dead_letters WHERE source=?", (f"automation_job:{job_id}",))
    assert len(letters) == 1, "停用瞬间应恰好落一行死信"
    letter = dict(letters[0])
    assert letter["kind"] == "automation_alert"
    payload = json.loads(letter["payload_json"])
    assert payload["job_id"] == job_id
    assert payload["title"] == "w2汇总"
    assert payload["consecutive_failures"] == 5

    # retry 闭环：重置连失败计数 + 重新启用 + 死信标记已处理
    res = asyncio.run(routes_maintenance.api_dead_letter_retry(
        letter["id"], _FakeReq({"auth_mode": "hub_token"}), current_agent="mgr"))
    assert res["ok"] and res["action"] == "automation_job_reenabled"
    enabled, fails = _fetch(
        tmp_hub["db_path"],
        "SELECT enabled, consecutive_failures FROM automation_jobs WHERE id=?",
        (job_id,))[0]
    assert enabled == 1 and fails == 0
    retried = _fetch(tmp_hub["db_path"],
                     "SELECT retried FROM dead_letters WHERE id=?",
                     (letter["id"],))[0][0]
    assert retried == 1


# ── CD-084：notify 发送失败落死信 + 节流 ──

class _DeadWS:
    """发送必失败的 WS 替身。"""

    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        raise RuntimeError("ws broken")

    async def send_text(self, text):
        raise RuntimeError("ws broken")


def test_notify_failure_lands_throttled_dead_letter(fresh_db):
    """发送失败 → 落死信（source=notify_send:<agent_id>，断连清理语义保留）；
    同 source 60 秒内已落过则跳过（节流），过窗后可再落。"""
    from notifications import NotificationManager

    mgr = NotificationManager()  # 独立实例：节流台账干净，不污染全局单例

    async def _flow():
        await mgr.connect("agent-n", _DeadWS())
        await mgr.safe_send("agent-n", {"hello": 1})
    asyncio.run(_flow())

    async def _flow():
        # 节流：60 秒内同 source 再失败不再落行
        await mgr.connect("agent-n", _DeadWS())
        await mgr.safe_send("agent-n", {"hello": 2})

        # 过窗后可再落（直接回拨台账时间，不动全局 time）
        mgr._dead_letter_last["notify_send:agent-n"] -= (
            mgr.DEAD_LETTER_THROTTLE_SEC + 1)
        await mgr.connect("agent-n", _DeadWS())
        await mgr.safe_send("agent-n", {"hello": 3})

        # 文本帧变体同样落账（frame=text；不同 agent → 不同 source，不受节流影响）
        await mgr.connect("agent-t", _DeadWS())
        await mgr.safe_send_text("agent-t", "env-json")
    asyncio.run(_flow())

    rows = _fetch(fresh_db,
                  "SELECT * FROM dead_letters WHERE source='notify_send:agent-n'"
                  " ORDER BY id")
    # 节流断言：hello:2 在窗口内被跳过未落行；hello:1 / hello:3 各落一行
    assert len(rows) == 2, "窗口内 1 行 + 过窗后 1 行（hello:2 被节流跳过）"
    letter = dict(rows[0])
    assert letter["kind"] == "notification"
    assert "ws broken" in letter["error"]
    payload = json.loads(letter["payload_json"])
    assert payload["agent_id"] == "agent-n" and payload["frame"] == "json"
    assert "hello': 1" in payload["payload"]
    assert "hello': 3" in json.loads(dict(rows[1])["payload_json"])["payload"]
    # 断连清理语义保留：死连接已被移除
    assert mgr.connections.get("agent-n") == []

    # 文本帧变体（frame=text）
    rows = _fetch(fresh_db,
                  "SELECT * FROM dead_letters WHERE source='notify_send:agent-t'")
    assert len(rows) == 1
    assert json.loads(dict(rows[0])["payload_json"])["frame"] == "text"
