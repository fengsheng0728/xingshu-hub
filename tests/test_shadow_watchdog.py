# -*- coding: utf-8 -*-
"""影子批2 单测 — 看门狗 + write_file 返回值 + stats 可见性 + 失败落账 + 告警去抖

覆盖（任务书 K 验收）：
① 看门狗检测 worker 线程死亡并重启（monkeypatch 杀线程模拟）
② write_file 返回 False → 失败路径 attempts+1（分干 vault / 主干 index 两路）
③ stats 端点返回含新键（watchdog_restarts 等 + pending_incomplete）
④ failed 批落审计记录（audit_log entry_type=shadow_batch_failed）
⑤ 告警去抖（连续失败只告警一次 / 窗口内不重复 / reset 语义 / sink 异常静默）
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_trunk import DataTrunk
from hub_mixins.shadow import ShadowWriter, _PENDING_MAX_ATTEMPTS

_DATE = "2026-09-02"


def _dt(tmp_path):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": True,
                           "wiki": True, "shared": True},
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    return dt


def _writer(tmp_path, watchdog_interval=2.0, audit_db_path="", pending_db_path=None):
    dt = _dt(tmp_path)
    return ShadowWriter(dt, audit_db_path=audit_db_path,
                        pending_db_path=pending_db_path or str(tmp_path / "pending.db"),
                        watchdog_interval=watchdog_interval)


def _mem(mid, content="客户不吃辣"):
    return {"memory_id": mid, "owner": "agent-1", "memory_key": "客户偏好",
            "content": content, "trust": "internal", "level": "summary",
            "tags": ["偏好"], "date": _DATE}


def _rows(db_path, where="1=1"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM shadow_pending WHERE {where} ORDER BY id")]
    conn.close()
    return rows


def _make_audit_db(path):
    """建 audit_log 表（与 db.py 正式 DDL 同构，审计链写入需要）。"""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_type TEXT NOT NULL DEFAULT '',
        ref_table TEXT DEFAULT '',
        ref_id TEXT DEFAULT '',
        payload TEXT DEFAULT '',
        prev_hash TEXT NOT NULL DEFAULT '',
        entry_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT)""")
    conn.commit()
    conn.close()


# ① 看门狗检测 worker 线程死亡并重启
def test_watchdog_restarts_dead_worker(tmp_path):
    w = _writer(tmp_path, watchdog_interval=0.1)
    orig_worker = w._worker
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker 意外死亡（模拟）")
        orig_worker()  # 第二次（看门狗重启后）恢复正常 worker 循环

    w._worker = flaky  # start() 绑定 target=self._worker → 首个线程启动即死
    w.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if w.stats["watchdog_restarts"] >= 1 and w._thread is not None \
                    and w._thread.is_alive():
                break
            time.sleep(0.05)
        assert w.stats["watchdog_restarts"] >= 1, "看门狗未检测到线程死亡"
        assert w._thread is not None and w._thread.is_alive(), "重启后 worker 应存活"
        # 重启后新 submit 正常镜像（看门狗恢复的是真功能不是空转）
        w.submit("memory", _mem("wd1"))
        md = None
        deadline = time.time() + 10
        while time.time() < deadline:
            md = w.dt.branch_repo().read_at(f"vault/memory/{_DATE}/wd1.md")
            if md is not None:
                break
            time.sleep(0.1)
        assert md is not None and "客户不吃辣" in md
    finally:
        w.stop()
    # 停止后看门狗不再重启
    assert w._watchdog is None and w._thread is None


# ② write_file 返回 False → 失败路径 attempts+1（分干 vault 写）
def test_branch_write_file_false_counts_failure(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", _mem("wf1"))
    w.dt.branch_repo().write_file = Mock(return_value=False)
    w._drain_once()
    rows = _rows(str(tmp_path / "pending.db"))
    assert rows[0]["attempts"] == 1, "write_file=False 应计入失败路径 attempts+1"
    assert rows[0]["status"] == "pending"  # 行仍在，等 replay
    assert w.stats["failures"] >= 1
    assert w.stats["last_failure_reason"] == "item_write"
    # index 未写入该 id（vault 写失败即中断本条）
    idx = w.dt.trunk.read_at("index/memory.jsonl") or ""
    assert "wf1" not in idx


# ②b write_file 返回 False → 失败路径（主干 index 写）
def test_trunk_index_write_false_counts_failure(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", _mem("wf2"))
    w.dt.trunk.write_file = Mock(return_value=False)
    w._drain_once()
    rows = _rows(str(tmp_path / "pending.db"))
    assert rows[0]["attempts"] == 1
    assert rows[0]["status"] == "pending"
    assert w.stats["failures"] >= 1


# ③ stats 端点返回含新键 + pending 未完成计数
def test_stats_endpoint_exposes_new_keys(tmp_path, monkeypatch):
    w = _writer(tmp_path)
    w.submit("memory", _mem("s1"))
    fake_hub_core = types.ModuleType("hub_core")
    fake_hub_core.hub = SimpleNamespace(_shadow=w)
    sys.modules.pop("routes_maintenance", None)
    monkeypatch.setitem(sys.modules, "hub_core", fake_hub_core)
    import routes_maintenance

    res = asyncio.run(routes_maintenance.api_shadow_stats())
    assert res["enabled"] is True
    for k in ("submitted", "flushed", "failures", "last_flush_at",
              "pending_replayed", "pending_failed", "pending_insert_failed",
              "watchdog_restarts", "last_failure_at", "last_failure_reason"):
        assert k in res["stats"], f"stats 缺键 {k}"
    assert res["stats"]["submitted"] == 1
    assert res["pending_incomplete"] == 1  # 未 flush → 未完成计数 1
    assert res["queue_depth"] == 1
    assert res["daemon_alive"] is False  # 未 start
    w._drain_once()
    res2 = asyncio.run(routes_maintenance.api_shadow_stats())
    assert res2["pending_incomplete"] == 0, "flush 后 pending 未完成计数应归零"
    assert res2["stats"]["flushed"] == 1


# ③b stats 端点：影子未启用时占位返回（端点永远可用，D4）
def test_stats_endpoint_shadow_disabled(monkeypatch):
    fake_hub_core = types.ModuleType("hub_core")
    fake_hub_core.hub = SimpleNamespace(_shadow=None)
    sys.modules.pop("routes_maintenance", None)
    monkeypatch.setitem(sys.modules, "hub_core", fake_hub_core)
    import routes_maintenance

    res = asyncio.run(routes_maintenance.api_shadow_stats())
    assert res["enabled"] is False
    assert res["stats"] is None
    assert res["pending_incomplete"] == 0


# ④ failed 批落审计记录（attempts 超限标 failed → audit_log）
def test_failed_batch_lands_in_audit(tmp_path):
    db = str(tmp_path / "hub.db")
    _make_audit_db(db)
    w = _writer(tmp_path, audit_db_path=db, pending_db_path=db)
    w.submit("memory", _mem("a1"))
    w._flush_batch = Mock(side_effect=RuntimeError("git 故障"))
    for _ in range(_PENDING_MAX_ATTEMPTS):
        w._drain_once()
        w._replay_pending()  # 失败批留表重入队（等价重启后 replay）
    rows = _rows(db)
    assert rows[0]["status"] == "failed"
    assert rows[0]["attempts"] == _PENDING_MAX_ATTEMPTS
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    recs = [dict(r) for r in conn.execute(
        "SELECT * FROM audit_log WHERE entry_type='shadow_batch_failed'")]
    conn.close()
    assert len(recs) >= 1, "failed 批应落 audit_log（shadow_batch_failed）"
    rec = recs[0]
    assert rec["ref_table"] == "shadow_pending"
    assert rec["prev_hash"] and rec["entry_hash"], "审计记录应上行级哈希链"
    payload = json.loads(rec["payload"])
    assert payload["failed"] >= 1
    assert payload["max_attempts"] == _PENDING_MAX_ATTEMPTS
    assert payload["pending_ids"], "审计 payload 应含 failed 行 id"


# ⑤ 告警去抖：连续失败只告警一次 / 窗口内不重复 / reset 语义
def test_alert_debounce():
    from hub_mixins import notifications as notif
    notif._shadow_alert_state.clear()
    sent = []
    notif.register_shadow_alert_sink(lambda reason, detail: sent.append(reason))
    try:
        assert notif.shadow_alert("t-flush") is False   # 连续 1，未达阈值
        assert notif.shadow_alert("t-flush") is False   # 连续 2
        assert notif.shadow_alert("t-flush") is True    # 连续 3 → 触发
        assert notif.shadow_alert("t-flush") is False   # 去抖窗口内不重复
        assert notif.shadow_alert("t-flush") is False
        assert sent == ["t-flush"], "连续失败应只告警一次"
        # 去抖窗口过后可再次告警
        assert notif.shadow_alert("t-flush", debounce=0.0) is True
        assert sent == ["t-flush", "t-flush"]
        # reset 清零连续计数（去抖窗口保留）
        notif.shadow_alert_reset("t-flush")
        assert notif.shadow_alert("t-flush", debounce=0.0) is False  # 计数回 1
    finally:
        notif._shadow_alert_sinks.clear()
        notif._shadow_alert_state.clear()


# ⑤b 告警 sink 异常静默（不阻塞主链路，D4）
def test_alert_sink_exception_silent():
    from hub_mixins import notifications as notif
    notif._shadow_alert_state.clear()

    def bad_sink(reason, detail):
        raise RuntimeError("sink 故障")

    notif.register_shadow_alert_sink(bad_sink)
    try:
        assert notif.shadow_alert("t-bad", threshold=1) is True  # 不抛异常
    finally:
        notif._shadow_alert_sinks.clear()
        notif._shadow_alert_state.clear()


# 回归：影子关（enabled=false）看门狗不启动、全 no-op
def test_disabled_no_watchdog_noop(tmp_path):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=False,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={},
    )
    dt = DataTrunk(cfg)
    w = ShadowWriter(dt, pending_db_path=str(tmp_path / "pending.db"))
    w.start()
    assert w._thread is None and w._watchdog is None
    w.submit("memory", _mem("x1"))
    assert w.stats["submitted"] == 0
    w.stop()
