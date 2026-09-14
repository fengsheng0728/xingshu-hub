# -*- coding: utf-8 -*-
"""shadow_pending WAL 单测 — G1 批1 影子双写崩溃一致性

覆盖（任务书 G 验收）：
① submit 后 pending 行存在
② flush 成功后软标记 done
③ 模拟 flush 失败（monkeypatch _flush_batch 抛错）→ attempts+1 且行仍在
④ 重试超 N 次（3）标记 failed 并计 stats
⑤ 启动 replay：pending 行重新入队并镜像成功
⑥ 幂等：重复 replay 不产生重复 index 行（同 id 去重）
⑦ db.py SCHEMA_VERSION=6 正式迁移建表 + user_version 同步
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_trunk import DataTrunk
from hub_mixins.shadow import ShadowWriter, _PENDING_MAX_ATTEMPTS

_DATE = "2026-09-02"


def _dt(tmp_path, switches=None):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW=switches or {"memory": True, "knowledge": True,
                                       "wiki": True, "shared": True},
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    return dt


def _writer(tmp_path, start=False, switches=None):
    dt = _dt(tmp_path, switches)
    w = ShadowWriter(dt, pending_db_path=str(tmp_path / "pending.db"))
    if start:
        w.start()
    return w


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


# ① submit 后 pending 行存在
def test_submit_inserts_pending_row(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", _mem("p1"))
    rows = _rows(str(tmp_path / "pending.db"))
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "memory"
    assert r["status"] == "pending"
    assert r["attempts"] == 0
    assert r["created_at"]
    payload = json.loads(r["payload"])
    assert payload["memory_id"] == "p1"
    assert payload["content"] == "客户不吃辣"  # 完整 payload 落库（replay 要重放）
    # 队列条目携带 pending_id
    kind, _, pid = w._q[0]
    assert kind == "memory" and pid == r["id"]


# ② flush 成功后软标记 done
def test_flush_marks_pending_done(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", _mem("p2"))
    w.submit("knowledge", {"entry_id": "k2", "title": "t", "content": "c",
                           "created_by": "b", "tags": [], "date": _DATE})
    w._drain_once()
    rows = _rows(str(tmp_path / "pending.db"))
    assert len(rows) == 2
    assert all(r["status"] == "done" for r in rows)
    assert w.stats["flushed"] == 2


# ③ 模拟 flush 失败 → attempts+1 且行仍在（不再静默丢弃）
def test_flush_failure_attempts_plus_one(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", _mem("p3"))
    w._flush_batch = Mock(side_effect=RuntimeError("boom"))
    w._drain_once()
    rows = _rows(str(tmp_path / "pending.db"))
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"  # 行仍在，未标 done
    assert rows[0]["attempts"] == 1
    assert w.stats["failures"] >= 1


# ④ 重试超 N 次标记 failed 并计 stats
def test_attempts_exceed_marks_failed(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", _mem("p4"))
    w._flush_batch = Mock(side_effect=RuntimeError("boom"))
    for _ in range(_PENDING_MAX_ATTEMPTS):
        w._drain_once()
        w._replay_pending()  # 失败批留表，重入队再走一轮（等价于重启后 replay）
    rows = _rows(str(tmp_path / "pending.db"))
    assert rows[0]["status"] == "failed"
    assert rows[0]["attempts"] == _PENDING_MAX_ATTEMPTS
    assert w.stats["pending_failed"] == 1
    # failed 行不再被 replay
    w._replay_pending()
    assert w.queue_depth() == 0


# ⑤ 启动 replay：pending 行重新入队并镜像成功
def test_start_replays_pending(tmp_path):
    w1 = _writer(tmp_path)  # 不 start，submit 后不 flush —— 模拟崩溃
    w1.submit("memory", _mem("r1", "重启前写入"))
    assert _rows(str(tmp_path / "pending.db"))[0]["status"] == "pending"
    w1._pend_close()
    # 新进程（新 ShadowWriter 实例）启动 → replay → 镜像补齐
    w2 = ShadowWriter(w1.dt, pending_db_path=str(tmp_path / "pending.db"))
    w2.start()
    try:
        md = None
        for _ in range(100):  # 攒批 0.5s，轮询最多 10s
            md = w2.dt.branch_repo().read_at(f"vault/memory/{_DATE}/r1.md")
            if md is not None:
                break
            time.sleep(0.1)
        assert md is not None and "重启前写入" in md
        assert w2.stats["pending_replayed"] >= 1
        # 软标记 done 在批 commit 之后，轮询等其落库
        done = False
        for _ in range(100):
            if _rows(str(tmp_path / "pending.db"))[0]["status"] == "done":
                done = True
                break
            time.sleep(0.1)
        assert done, "replay 后 pending 行应软标记 done"
    finally:
        w2.stop()


# ⑥ 幂等：重复 replay 不产生重复 index 行
def test_replay_idempotent_no_dup_index(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", _mem("i1"))
    w._drain_once()  # 正常 flush，行已 done
    # 人为再造一条同 payload 的 pending（如崩溃时标 done 失败残留），强制 replay 两次
    pid = w._pending_insert("memory", _mem("i1"))
    assert pid
    w._replay_pending()
    w._replay_pending()
    w._drain_once()
    idx = w.dt.trunk.read_at("index/memory.jsonl")
    n = len([ln for ln in idx.splitlines()
             if ln.strip() and json.loads(ln).get("id") == "i1"])
    assert n == 1, f"同 id 重放应去重,实际 {n} 行"
    assert w.dt.branch_repo().read_at(f"vault/memory/{_DATE}/i1.md") is not None


# ⑦ db.py 正式迁移：SCHEMA_VERSION=6 建 shadow_pending + user_version 同步
def test_schema_v6_shadow_pending_migration():
    import models
    from db import SCHEMA_VERSION
    assert SCHEMA_VERSION == 6, "db.py SCHEMA_VERSION 应为 6"
    tmp = tempfile.mkdtemp(prefix="g1-")
    db = os.path.join(tmp, "t.db")
    old = models.CONFIG.DB_PATH
    models.CONFIG.DB_PATH = db
    try:
        import db as dbmod
        dbmod.CONFIG.DB_PATH = db
        dbmod.init_db()
        conn = sqlite3.connect(db)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(shadow_pending)")]
        conn.close()
        assert ver == 6, f"user_version {ver} 应 == 6"
        for col in ("id", "kind", "payload", "created_at", "status", "attempts"):
            assert col in cols, f"shadow_pending 缺列 {col}"
        dbmod.init_db()  # 幂等：再跑一次不崩
    finally:
        models.CONFIG.DB_PATH = old
        dbmod.CONFIG.DB_PATH = old
        shutil.rmtree(tmp, ignore_errors=True)


# 回归：无 pending_db_path 时行为与旧版一致（全 no-op 于 DB 侧）
def test_no_pending_db_path_degrades(tmp_path):
    dt = _dt(tmp_path)
    w = ShadowWriter(dt)  # 不传 pending_db_path，audit_db_path 也为空
    w.start()
    try:
        w.submit("memory", _mem("n1"))
        w._drain_once()
        assert w.stats["submitted"] == 1
        assert w.stats["flushed"] == 1
        assert w.stats["failures"] == 0
        assert w.dt.branch_repo().read_at(f"vault/memory/{_DATE}/n1.md") is not None
    finally:
        w.stop()
