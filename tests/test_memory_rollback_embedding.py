# -*- coding: utf-8 -*-
"""T32 / CD-053②：rollback 必须重算 embedding（防「新正文 + 旧向量」错位）

背景：rollback 的恢复 UPDATE 原先只改 content/summary/confidence/updated_at，
`embedding` 列仍是**被覆盖那一版**的 blob；而 `vector_index` 消费侧按**库内 blob** 重灌
（`hub_core._reindex_vector_sync`）→ 回滚后向量对的是旧内容（检索命中错位向量）。

冻结口径（Hermes 接手实现）：回滚时**同步重算** embedding 并同事务写回；
算不出（无模型 / 超时 / 异常 / 目标内容为空）时**写 NULL**——宁可暂时没有向量，
也不用错位向量；结果以 `memory_rollback` 事件的 `embedding` 字段留痕。

先红：改动前本文件 E-1 必失败（库内 embedding == 被覆盖版 v2 的向量）。
不 spawn Hub、不绑端口。
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from models import CONFIG  # noqa: E402
from hub_mixins.memory import MemoryMixin  # noqa: E402

V1 = "第一版内容：客户张三的偏好是浅色玻璃"
V2 = "第二版内容：客户张三改成了深色哑光"
MID = "m-t32-0001"
AGENT = "agent-t32"
KEY = "t32-key"


def _vec(text):
    """可辨识的假向量（同文本必得同向量；不同文本必不同）。"""
    return [float(len(text)), float(sum(map(ord, text)) % 997), 1.0, 0.0]


def _blob(text):
    return np.array(_vec(text), dtype=np.float32).tobytes()


class _StubModel:
    def __init__(self, fail=False):
        self.fail = fail

    def encode(self, text):
        if self.fail:
            raise RuntimeError("simulated encode failure")
        return _vec(text)


class _RollbackHub(MemoryMixin):
    """最小 rollback 宿主：无影子、无向量栈（collection=None → _apply_vector_ops 跳过）。"""

    def __init__(self, model):
        self.agents = {}
        self.data_trunk = None
        self._shadow = None
        self._chroma_collection = None
        self._model = model
        self.events = []

    async def _ensure_embedding_model(self):
        return self._model

    async def _log_event(self, kind, agent_id, payload):
        self.events.append((kind, payload))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    import db as db_mod
    db_mod.init_db()
    con = sqlite3.connect(db_path)
    # 现状库：正文 = v2（"覆盖版"），embedding = v2 的向量（这就是错位源）
    con.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content, summary,"
        " confidence, embedding, disclosure_level, tags) VALUES (?,?,?,?,?,?,?,?,?)",
        (MID, AGENT, KEY, V2, V2[:50], 1.0, _blob(V2), "summary", "[]"))
    # 目标历史版本：v1
    con.execute(
        "INSERT INTO memory_versions (memory_id, memory_key, version, content, summary, confidence)"
        " VALUES (?,?,?,?,?,?)", (MID, KEY, 1, V1, V1[:50], 1.0))
    con.commit()
    con.close()
    return db_path, _RollbackHub(_StubModel())


def _db_embedding(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT content, embedding FROM memory_pool WHERE memory_id=?", (MID,)).fetchone()
    finally:
        con.close()


def _ver_id(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT id FROM memory_versions WHERE memory_key=? AND content=?", (KEY, V1)).fetchone()[0]
    finally:
        con.close()


# ═══════════ E-1 先红核心：回滚后向量必须换成目标版本的 embedding ═══════════

def test_e1_rollback_recomputes_embedding(env):
    db_path, hub = env
    res = asyncio.run(hub.rollback_memory(KEY, _ver_id(db_path), AGENT))
    assert res["status"] == "rolled_back"
    content, emb = _db_embedding(db_path)
    assert content == V1, f"正文应回滚为 v1，实际: {content[:20]}"
    assert emb is not None, "回滚后不得让向量为空（本次有模型，应重算成功）"
    assert emb == _blob(V1), "库内 embedding 必须是**目标版本 v1** 的向量（改动前：仍是 v2 的）"
    assert emb != _blob(V2), "不得残留被覆盖版 v2 的向量"


# ═══════════ E-2 留痕：memory_rollback 事件带 embedding 状态 ═══════════

def test_e2_event_records_embedding_state(env):
    db_path, hub = env
    asyncio.run(hub.rollback_memory(KEY, _ver_id(db_path), AGENT))
    kinds = [k for k, _ in hub.events]
    assert "memory_rollback" in kinds
    payload = [p for k, p in hub.events if k == "memory_rollback"][0]
    assert payload.get("embedding") == "recomputed", f"应留痕 recomputed，实际 {payload.get('embedding')}"


# ═══════════ E-3 无模型 → 写 NULL（不错位），不抛异常 ═══════════

def test_e3_no_model_writes_null_not_stale(env):
    db_path, hub = env
    hub._model = None
    res = asyncio.run(hub.rollback_memory(KEY, _ver_id(db_path), AGENT))
    assert res["status"] == "rolled_back"
    content, emb = _db_embedding(db_path)
    assert content == V1
    assert emb is None, "无模型时必须写 NULL —— 绝不能留下 v2 的错位向量"
    payload = [p for k, p in hub.events if k == "memory_rollback"][0]
    assert payload.get("embedding") == "unavailable"


# ═══════════ E-4 重算异常 → 同样写 NULL + 告警 ═══════════

def test_e4_encode_failure_writes_null(env, caplog):
    import logging
    db_path, hub = env
    hub._model = _StubModel(fail=True)
    with caplog.at_level(logging.WARNING, logger="deps"):
        res = asyncio.run(hub.rollback_memory(KEY, _ver_id(db_path), AGENT))
    assert res["status"] == "rolled_back"
    _, emb = _db_embedding(db_path)
    assert emb is None, "重算失败必须写 NULL，不得沿用旧向量"


# ═══════════ E-5 既有联动不回归：shadow_mirror / vector_index 事件仍在 ═══════════

def test_e5_existing_events_still_enqueued(env):
    db_path, hub = env
    asyncio.run(hub.rollback_memory(KEY, _ver_id(db_path), AGENT))
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT event_type, payload FROM event_outbox ORDER BY id").fetchall()
    finally:
        con.close()
    types = [r[0] for r in rows]
    assert "vector_index" in types, "vector_index 事件必须仍在（消费侧按**新** blob 重灌）"
    # 新 blob 已入库 → 消费侧重灌拿到的就是 v1 的向量
    _, emb = _db_embedding(db_path)
    assert emb == _blob(V1)
