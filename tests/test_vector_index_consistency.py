# -*- coding: utf-8 -*-
"""CD-046 向量索引一致性（裂缝1）验收测试 — T2 任务书（2026-09-17）

工程约定照抄 tests/test_outbox_audit_atomicity.py：_MiniHub 最小宿主 + 临时库
（db.init_db 全量建表）+ 假 chroma collection（内存 dict 记录调用，不依赖真
chromadb，确定性优先）。需要 embedding 时用假模型产出确定向量（内容相关的
合成向量，用于区分新旧内容的 embedding 是否真的被刷新）。

覆盖（任务书 T6）：
T6-1 原子性（先红）：注入 commit() 失败 → memory_pool 无行且假 collection 的
    add/upsert 调用次数为 0（旧代码在事务体内先写 chroma → 红）
T6-2 覆盖刷新（先红）：同 memory_key 写两次 → chroma 侧该 id 的 embedding 与
    metadata.content 均为第二次内容（旧代码覆盖分支完全不碰 chroma → 红）
T6-3 删除清理（先红）：写入后删除 → chroma 侧该 id 不存在（旧代码留孤儿向量 → 红）
T6-4 写失败补偿：collection.upsert 抛错 → 业务已提交 + event_outbox 有 1 条
    vector_index pending；换正常 collection 的消费者 drain → done 且向量与库一致
T6-5 重启 replay：预置 vector_index pending 行 → 新消费者 drain → done + 向量正确
T6-6 error 分支：消费者未注入 vector_fn 时消费 vector_index → 标 failed（不静默）
T6-7 回退门禁：_insert_new_memory_sync 函数体内无 _chroma_collection；memory.py
    全文的 _chroma_collection 只出现在提交后的向量执行函数内
T6-8 stats 契约：/api/v1/stats 返回体含 outbox 段且无 500（无消费者实例也可返回）
"""
import asyncio
import inspect
import json
import os
import sqlite3
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import audit.memory_audit as memory_audit  # noqa: E402
import db_facade  # noqa: E402
import models  # noqa: E402
from hub_core import SyncHub  # noqa: E402  （复用 _merge_trust/_trust_from_source/_reindex_vector_sync 真实现）
from hub_mixins.memory import MemoryMixin  # noqa: E402
from hub_mixins.outbox import OutboxConsumer, enqueue  # noqa: E402
from models import MemoryEntry  # noqa: E402

# 向量 metadata 键集合（CD-046 硬约束：与既有写路径完全一致，禁加密级字段）
VECTOR_METADATA_KEYS = {
    "owner", "key", "tags",
    "importance", "kind", "confidence", "source_type", "layer", "level",
}


def _emb(text: str):
    """确定性的内容相关合成向量：内容变 → 向量变（用于区分新旧 embedding）。"""
    seed = (sum(map(ord, text)) % 100) / 100.0
    return np.array([seed] * 8, dtype=np.float32).tolist()


class _FakeModel:
    """假 embedding 模型：encode 返回内容相关的确定向量。"""

    def encode(self, text):
        return np.array(_emb(text), dtype=np.float32)


class _FakeChroma:
    """假 chroma collection：add/upsert/delete 记录到内存 dict + 调用计数。"""

    def __init__(self):
        self.store = {}  # id -> {"embedding": list, "metadata": dict}
        self.calls = {"add": 0, "upsert": 0, "delete": 0}
        self.fail = set()  # 命中方法名即抛 RuntimeError（模拟 Chroma 写失败）

    def add(self, ids, embeddings, metadatas):
        self.calls["add"] += 1
        if "add" in self.fail:
            raise RuntimeError("fake chroma add boom")
        for i, e, m in zip(ids, embeddings, metadatas):
            self.store[i] = {"embedding": list(e), "metadata": dict(m)}

    def upsert(self, ids, embeddings, metadatas):
        self.calls["upsert"] += 1
        if "upsert" in self.fail:
            raise RuntimeError("fake chroma upsert boom")
        for i, e, m in zip(ids, embeddings, metadatas):
            self.store[i] = {"embedding": list(e), "metadata": dict(m)}

    def delete(self, ids):
        self.calls["delete"] += 1
        if "delete" in self.fail:
            raise RuntimeError("fake chroma delete boom")
        for i in ids:
            self.store.pop(i, None)


class _MiniHub(MemoryMixin):
    """最小 MemoryMixin 宿主：假 chroma + 假 embedding 模型，审计链路全保留。"""

    _merge_trust = staticmethod(SyncHub._merge_trust)
    _trust_from_source = staticmethod(SyncHub._trust_from_source)

    def _reindex_vector_sync(self, payload):
        # 延迟绑定到 SyncHub 真实现（先红阶段旧代码尚无该方法）
        return SyncHub._reindex_vector_sync(self, payload)

    def __init__(self, with_model=True):
        self.agents = {}
        self._shadow = None
        self._chroma_collection = _FakeChroma()
        self._memory_lock = asyncio.Lock()
        self._with_model = with_model

    async def _ensure_embedding_model(self):
        return _FakeModel() if self._with_model else None

    async def _log_event(self, *args, **kwargs):
        return None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表）+ 临时审计目录（memory_audit 模块级路径重定向）。"""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()
    monkeypatch.setattr(memory_audit, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(memory_audit, "AUDIT_FILE",
                        str(tmp_path / "audit" / "memory_pool.jsonl"))
    monkeypatch.setattr(memory_audit, "_rolling_chain", None)
    return {"db_path": db_path,
            "audit_file": tmp_path / "audit" / "memory_pool.jsonl"}


def _outbox_rows(db_path, event_type=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM event_outbox"
    params = ()
    if event_type:
        sql += " WHERE event_type=?"
        params = (event_type,)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]
    conn.close()
    return rows


def _enqueue_committed(db_path, event_type, payload):
    """独立连接 enqueue + commit（等价于一个已提交的业务事务）。"""
    conn = sqlite3.connect(db_path)
    enqueue(conn, event_type, payload)
    conn.commit()
    conn.close()


def _inject_commit_failure(monkeypatch):
    """照抄 CD-045 T6-2 的 wrapper 注入法：commit() 必抛 OperationalError。"""
    class _FailCommitConn:
        def __init__(self, real):
            self._real = real

        def cursor(self):
            return self._real.cursor()

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure (disk I/O)")

        def __getattr__(self, name):
            return getattr(self._real, name)

    async def _boom_run_in_conn(fn, *, db_path=None, write=False):
        conn = sqlite3.connect(db_path or models.CONFIG.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            return fn(_FailCommitConn(conn))
        finally:
            conn.close()  # 未 commit → close 即回滚

    monkeypatch.setattr(db_facade, "run_in_conn", _boom_run_in_conn)


# T6-1 原子性（先红）：commit 失败 → 库无行 且 chroma 从未被调用
def test_commit_failure_never_touches_chroma(env, monkeypatch):
    _inject_commit_failure(monkeypatch)
    hub = _MiniHub()
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(hub.store_memory(
            "agent-1", MemoryEntry(memory_key="向量原子性", content="commit 必败")))

    conn = sqlite3.connect(env["db_path"])
    n_pool = conn.execute(
        "SELECT COUNT(*) FROM memory_pool WHERE memory_key='向量原子性'").fetchone()[0]
    conn.close()
    assert n_pool == 0, "commit 失败 → memory_pool 不得有该行"
    assert hub._chroma_collection.calls["add"] == 0
    assert hub._chroma_collection.calls["upsert"] == 0, \
        "commit 失败 → chroma add/upsert 调用次数必须为 0（旧代码事务体内先写 chroma → 红）"
    assert hub._chroma_collection.store == {}, \
        "commit 失败 → 索引里不得留指向不存在数据的脏向量"


# T6-2 覆盖刷新（先红）：同 key 写两次 → chroma 侧 embedding/content 均为新内容
def test_conflict_overwrite_refreshes_vector(env):
    hub = _MiniHub()
    r1 = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="覆盖键", content="旧内容-甲")))
    assert r1["status"] == "stored" and not r1["locked"]
    mid = r1["memory_id"]
    assert mid in hub._chroma_collection.store

    r2 = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="覆盖键", content="新内容-乙")))
    assert r2["action"] == "conflict_overwrite"
    assert r2["memory_id"] == mid, "同 key 覆盖应复用原 memory_id"

    doc = hub._chroma_collection.store.get(mid)
    assert doc is not None, "覆盖后该 id 的向量必须仍在"
    assert "content" not in doc["metadata"] and "summary" not in doc["metadata"], \
        "CD-048：向量 metadata 不得含正文/摘要明文（内容权威来源=回查 SQLite）"
    assert doc["embedding"] == _emb("新内容-乙"), \
        "覆盖后 embedding 必须是新内容的向量（旧代码留旧向量 → 红）"
    assert set(doc["metadata"]) == VECTOR_METADATA_KEYS, \
        "向量 metadata 键集合不得变（不加不减）"


# T6-3 删除清理（先红）：删除后 chroma 内不存在该 id
def test_delete_memory_removes_vector(env):
    hub = _MiniHub()
    r = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="删除键", content="待删除内容")))
    assert r["status"] == "stored" and not r["locked"]
    mid = r["memory_id"]
    assert mid in hub._chroma_collection.store

    res = asyncio.run(hub.delete_memory("删除键", "agent-1"))
    assert res["status"] == "deleted"
    assert mid not in hub._chroma_collection.store, \
        "删除后 chroma 不得残留该 id 的孤儿向量（旧代码残留 → 红）"


# T6-4 写失败补偿：upsert 抛错 → 业务已提交 + vector_index pending；恢复后 drain 补齐
def test_vector_write_failure_enqueues_compensation(env):
    hub = _MiniHub()
    hub._chroma_collection.fail.add("upsert")  # 模拟 Chroma 写失败
    r = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="补偿键", content="chroma 故障时的写入")))
    assert r["status"] == "stored", "向量失败不得影响业务提交"

    conn = sqlite3.connect(env["db_path"])
    n_pool = conn.execute(
        "SELECT COUNT(*) FROM memory_pool WHERE memory_key='补偿键'").fetchone()[0]
    conn.close()
    assert n_pool == 1, "业务行必须已提交"
    assert r["memory_id"] not in hub._chroma_collection.store, "故障期间向量确实没写上"

    rows = _outbox_rows(env["db_path"], "vector_index")
    assert len(rows) == 1, "event_outbox 应有 1 条 vector_index 补偿事件"
    assert rows[0]["status"] == "pending"
    payload = json.loads(rows[0]["payload"])
    assert payload["op"] == "upsert"
    assert payload["memory_id"] == r["memory_id"]

    # 换成正常 collection 的消费者 drain → done 且向量与库一致（消费者从库内 blob 重灌）
    hub._chroma_collection.fail.clear()
    consumer = OutboxConsumer(env["db_path"], vector_fn=hub._reindex_vector_sync)
    consumer._drain_once()
    row = _outbox_rows(env["db_path"], "vector_index")[0]
    assert row["status"] == "done", f"补偿事件应消费成功，实际 {row}"

    doc = hub._chroma_collection.store.get(r["memory_id"])
    assert doc is not None, "补偿消费后向量必须补上（一条不丢）"
    assert "content" not in doc["metadata"] and "summary" not in doc["metadata"]
    assert doc["metadata"]["level"] == "summary", "CD-048：metadata 必须带 level 字段"
    assert set(doc["metadata"]) == VECTOR_METADATA_KEYS
    conn = sqlite3.connect(env["db_path"])
    blob = conn.execute("SELECT embedding FROM memory_pool WHERE memory_id=?",
                        (r["memory_id"],)).fetchone()[0]
    conn.close()
    assert doc["embedding"] == np.frombuffer(blob, dtype=np.float32).tolist(), \
        "补偿重灌的 embedding 必须来自库内 blob（不调模型）"


# T6-5 重启 replay：预置 vector_index pending 行 → 新消费者 drain → done + 向量正确
def test_restart_replay_vector_index(env):
    hub = _MiniHub()
    r = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="replay键", content="崩溃前入队未消费")))
    mid = r["memory_id"]
    # 模拟"向量丢失但库里行还在"的遗留现场 + 一条未消费的 pending 事件
    hub._chroma_collection.store.pop(mid)
    _enqueue_committed(env["db_path"], "vector_index",
                       {"op": "upsert", "memory_id": mid})
    pending = [x for x in _outbox_rows(env["db_path"], "vector_index")
               if x["status"] == "pending"]
    assert len(pending) == 1

    # 新建 OutboxConsumer = 等价于进程重启后 replay
    consumer = OutboxConsumer(env["db_path"], vector_fn=hub._reindex_vector_sync)
    consumer._drain_once()
    rows = _outbox_rows(env["db_path"], "vector_index")
    assert all(x["status"] == "done" for x in rows), rows
    doc = hub._chroma_collection.store.get(mid)
    assert doc is not None, "replay 后向量必须重建"
    assert "content" not in doc["metadata"] and "summary" not in doc["metadata"]
    assert doc["metadata"]["level"] == "summary", "CD-048：metadata 必须带 level 字段"
    assert set(doc["metadata"]) == VECTOR_METADATA_KEYS


# T6-6 error 分支：消费者未注入 vector_fn → 标 failed（不许静默跳过、不许 done）
def test_vector_event_without_vector_fn_marks_failed(env):
    _enqueue_committed(env["db_path"], "vector_index",
                       {"op": "upsert", "memory_id": "m-x"})
    consumer = OutboxConsumer(env["db_path"])  # 未注入 vector_fn
    consumer._drain_once()
    row = _outbox_rows(env["db_path"], "vector_index")[0]
    assert row["status"] == "failed", \
        f"未注入 vector_fn 时必须标 failed（不许静默/done），实际 {row['status']}"
    assert "vector_fn" in (row["last_error"] or ""), row["last_error"]
    assert consumer.stats_snapshot()["failed"] >= 1


# T6-7 回退门禁：_insert_new_memory_sync 无 _chroma_collection；
# memory.py 全文的 _chroma_collection 只在提交后的向量执行函数内
def test_no_chroma_reference_in_txn_guard():
    src = inspect.getsource(MemoryMixin._insert_new_memory_sync)
    assert "_chroma_collection" not in src, \
        "_insert_new_memory_sync 函数体内不得再出现 _chroma_collection"

    path = os.path.join(ROOT, "hub_mixins", "memory.py")
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    offenders = [i for i, ln in enumerate(lines) if "_chroma_collection" in ln]
    assert offenders, "memory.py 应仍有提交后向量执行函数持有 _chroma_collection"
    start = next(i for i, ln in enumerate(lines)
                 if "def _apply_vector_ops" in ln)
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("    async def ")
                or lines[i].startswith("    def ")), len(lines))
    assert all(start <= i < end for i in offenders), \
        f"_chroma_collection 只允许出现在 _apply_vector_ops（行 {start}-{end}）内，" \
        f"实际行号 {offenders}"


# T6-8 stats 契约：/api/v1/stats 返回体含 outbox 段且无 500（无消费者实例也可返回）
def test_stats_exposes_outbox_section(env, monkeypatch):
    import routes_dashboard
    from hub_core import hub

    # 有消费者实例：outbox 段给出 pending/done/failed/last_error/by_type
    _enqueue_committed(env["db_path"], "vector_index",
                       {"op": "upsert", "memory_id": "m-stats"})
    consumer = OutboxConsumer(env["db_path"])
    monkeypatch.setattr(hub, "_outbox_consumer", consumer)
    out = asyncio.run(routes_dashboard.api_stats())
    assert out["status"] == "ok"
    assert "outbox" in out, "stats 响应必须含 outbox 段"
    ob = out["outbox"]
    for key in ("pending", "done", "failed", "last_error", "by_type"):
        assert key in ob, f"outbox 段缺键 {key}: {ob}"
    assert ob["pending"] >= 1
    assert ob["by_type"].get("vector_index", 0) >= 1

    # 无消费者实例：outbox 段为 None，端点不许 500
    monkeypatch.setattr(hub, "_outbox_consumer", None)
    out = asyncio.run(routes_dashboard.api_stats())
    assert out["status"] == "ok"
    assert out["outbox"] is None, "无消费者实例时 outbox 段应给 null 而不是 500"
