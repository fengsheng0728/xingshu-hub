# -*- coding: utf-8 -*-
"""CD-047/CD-050 影子三洞 + 开关观测 + 变更点联动 验收测试 — T3 任务书（2026-09-17）

工程约定照抄 tests/test_vector_index_consistency.py：_MiniHub 最小宿主 + 临时库
（db.init_db 全量建表）+ 假影子（记录 submit 调用、可注入失败）+ 假 chroma
（内存 dict 记录调用）。影子真 git 侧由 test_shadow_pending.py 等既有套件覆盖，
本文件聚焦"事件驱动"链路：事务内事件行 → outbox 消费者 → 影子 submit。

覆盖（任务书 T6）：
T6-1 写记忆 → event_outbox 有 1 条 shadow_mirror（只记 id 不打包快照）；
    drain 后假影子收到 1 次 submit("memory", payload)，payload 与库内当前值一致
T6-2 先红：shadow.submit 抛异常 → 事件行留 pending（attempts+1），换正常影子
    再 drain → 镜像补上（旧代码 except: pass 后无任何痕迹 → 红）
T6-3 先红：写→写（覆盖归档旧版）→rollback → 假影子收到第三次 submit，
    content == 回滚后内容（旧代码 rollback 完全无 submit → 红）
T6-4 rollback 后 vector_index 事件入队且被消费（假 chroma 的 embedding 与
    库内 blob 一致、metadata.content 为回滚后内容）——CD-050 的另一半
T6-5 跨天：库内 updated_at 是昨天，重镜像 payload 的 date 用库内日期
    （不用 datetime.now()，否则跨天再生成一份 vault 档案）
T6-6 _record_origins 注入失败 → 不抛 + stats["origins_failed"]>=1 + WARNING
    日志；既有 collect_origins（从 index/.commits.jsonl 重建）仍可用
T6-7 开关跳过可观测：disabled → skipped_disabled+1；kind 关闭 →
    skipped_kind[kind]+1；/api/v1/maintenance/shadow-stats 透出
    enabled/switches/skipped_* 且不 500
T6-8 回退门禁：memory.py 无 _shadow.submit(；outbox.py 有 shadow_mirror
    分发分支；hub_core.py 有 _shadow_mirror_sync
"""
import asyncio
import inspect
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import audit.memory_audit as memory_audit  # noqa: E402
import db_facade  # noqa: E402
import models  # noqa: E402
from data_trunk import DataTrunk  # noqa: E402
from hub_core import SyncHub  # noqa: E402  （复用 _merge_trust/_trust_from_source/_shadow_mirror_sync/_reindex_vector_sync 真实现）
from hub_mixins.memory import MemoryMixin  # noqa: E402
from hub_mixins.outbox import OutboxConsumer, enqueue  # noqa: E402
from hub_mixins.shadow import ShadowWriter, collect_origins  # noqa: E402
from models import MemoryEntry  # noqa: E402


def _emb(text: str):
    """确定性的内容相关合成向量（与 test_vector_index_consistency 同款）。"""
    seed = (sum(map(ord, text)) % 100) / 100.0
    return np.array([seed] * 8, dtype=np.float32).tolist()


class _FakeModel:
    def encode(self, text):
        return np.array(_emb(text), dtype=np.float32)


class _FakeChroma:
    """假 chroma collection：upsert/delete 记录到内存 dict + 调用计数。"""

    def __init__(self):
        self.store = {}
        self.calls = {"upsert": 0, "delete": 0}

    def upsert(self, ids, embeddings, metadatas):
        self.calls["upsert"] += 1
        for i, e, m in zip(ids, embeddings, metadatas):
            self.store[i] = {"embedding": list(e), "metadata": dict(m)}

    def delete(self, ids):
        self.calls["delete"] += 1
        for i in ids:
            self.store.pop(i, None)


class _FakeShadow:
    """假影子：记录 submit 调用；fail=True 时抛错（模拟队列满/进程崩溃）。"""

    def __init__(self):
        self.calls = []  # [(kind, payload)]
        self.fail = False

    def submit(self, kind, payload):
        if self.fail:
            raise RuntimeError("fake shadow queue full")
        self.calls.append((kind, dict(payload)))


class _MiniHub(MemoryMixin):
    """最小 MemoryMixin 宿主：假影子 + 假 chroma + 假 embedding 模型。"""

    _merge_trust = staticmethod(SyncHub._merge_trust)
    _trust_from_source = staticmethod(SyncHub._trust_from_source)

    def _shadow_mirror_sync(self, payload):
        # 延迟绑定到 SyncHub 真实现（先红阶段旧代码尚无该方法）
        return SyncHub._shadow_mirror_sync(self, payload)

    def _reindex_vector_sync(self, payload):
        return SyncHub._reindex_vector_sync(self, payload)

    def __init__(self):
        self.agents = {}
        self._shadow = _FakeShadow()
        self._chroma_collection = _FakeChroma()
        self._memory_lock = asyncio.Lock()

    async def _ensure_embedding_model(self):
        return _FakeModel()

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


def _pool_row(db_path, memory_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM memory_pool WHERE memory_id=?",
                       (memory_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _enqueue_committed(db_path, event_type, payload):
    """独立连接 enqueue + commit（等价于一个已提交的业务事务）。"""
    conn = sqlite3.connect(db_path)
    enqueue(conn, event_type, payload)
    conn.commit()
    conn.close()


def _consumer(db_path, hub):
    """构造消费者。先红兼容：旧代码 OutboxConsumer 无 shadow_fn 形参，
    探测签名后再注入（先红时事件分发分支不存在，影子永不收到 submit）。"""
    kw = {"vector_fn": hub._reindex_vector_sync}
    if "shadow_fn" in inspect.signature(OutboxConsumer.__init__).parameters:
        kw["shadow_fn"] = hub._shadow_mirror_sync
    return OutboxConsumer(db_path, **kw)


def _drain(db_path, hub):
    _consumer(db_path, hub)._drain_once()


def _store(hub, key, content, **kw):
    return asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key=key, content=content, **kw)))


# T6-1 写记忆 → shadow_mirror 事件（只记 id）；drain 后 submit payload 与库一致
def test_store_enqueues_shadow_mirror_and_payload_matches_db(env):
    hub = _MiniHub()
    r = _store(hub, "影子键", "客户怕吵", tags=["偏好"])
    assert r["status"] == "stored"

    rows = _outbox_rows(env["db_path"], "shadow_mirror")
    assert len(rows) == 1, "写记忆后 event_outbox 应有 1 条 shadow_mirror"
    payload = json.loads(rows[0]["payload"])
    assert payload == {"kind": "memory", "memory_id": r["memory_id"]}, \
        f"事件只记 id 不打包快照，实际 {payload}"

    _drain(env["db_path"], hub)
    row = _outbox_rows(env["db_path"], "shadow_mirror")[0]
    assert row["status"] == "done"

    assert len(hub._shadow.calls) == 1, "drain 后假影子应收到 1 次 submit"
    kind, sp = hub._shadow.calls[0]
    assert kind == "memory"
    db_row = _pool_row(env["db_path"], r["memory_id"])
    assert sp["memory_id"] == r["memory_id"]
    assert sp["owner"] == "agent-1"
    assert sp["memory_key"] == "影子键"
    assert sp["content"] == "客户怕吵"
    assert sp["trust"] == db_row["trust_level"]
    assert sp["level"] == db_row["disclosure_level"]
    assert sp["tags"] == ["偏好"]
    assert sp["date"] == db_row["updated_at"][:10], \
        "date 必须取库内 updated_at[:10]（不要用 datetime.now()）"


# T6-2 先红：submit 抛异常 → 事件行留 pending；恢复后 drain 补上（一条不丢）
def test_shadow_submit_failure_event_survives_and_replays(env):
    hub = _MiniHub()
    hub._shadow.fail = True  # 模拟队列满/崩溃：submit 抛异常
    r = _store(hub, "崩溃窗口", "submit 必败时的写入")
    assert r["status"] == "stored", "影子故障不得影响业务提交"

    rows = _outbox_rows(env["db_path"], "shadow_mirror")
    assert len(rows) == 1, \
        "写记忆后 event_outbox 应有 1 条 shadow_mirror" \
        "（旧代码 except: pass 后无任何痕迹、数据永久不进影子 → 红）"

    _drain(env["db_path"], hub)
    row = _outbox_rows(env["db_path"], "shadow_mirror")[0]
    assert row["status"] == "pending", "submit 失败 → 事件留 pending 而非丢弃"
    assert row["attempts"] == 1
    assert "RuntimeError" in (row["last_error"] or "")
    assert hub._shadow.calls == [], "故障期间影子确实没收到"

    # 换成正常影子再 drain → 镜像补上
    hub._shadow.fail = False
    _drain(env["db_path"], hub)
    row = _outbox_rows(env["db_path"], "shadow_mirror")[0]
    assert row["status"] == "done"
    assert len(hub._shadow.calls) == 1
    assert hub._shadow.calls[0][1]["content"] == "submit 必败时的写入"


def _write_two_versions(hub, key, v1, v2, db_path=None):
    """写两版（第二写走 conflict_overwrite 归档 v1），返回 memory_id。

    db_path 传入时每写一版先 drain 一次——消费侧读库内最新值，不 drain 就
    覆盖会让两版事件都读到 v2（这是设计语义，不是 bug）。
    """
    r1 = _store(hub, key, v1)
    if db_path:
        _drain(db_path, hub)
    r2 = _store(hub, key, v2)
    assert r2["action"] == "conflict_overwrite"
    assert r2["memory_id"] == r1["memory_id"]
    if db_path:
        _drain(db_path, hub)
    return r1["memory_id"]


def _version_id_of(db_path, key, content):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT id FROM memory_versions WHERE memory_key=? AND content=?",
        (key, content)).fetchone()
    conn.close()
    assert row, f"memory_versions 应有 {content} 的归档"
    return row[0]


# T6-3 先红：rollback → 假影子收到重镜像 submit，content == 回滚后内容
def test_rollback_triggers_remirror_with_current_content(env):
    hub = _MiniHub()
    mid = _write_two_versions(hub, "回滚键", "回滚内容-一", "回滚内容-二",
                              db_path=env["db_path"])
    assert [c[1]["content"] for c in hub._shadow.calls] == \
        ["回滚内容-一", "回滚内容-二"]

    vid = _version_id_of(env["db_path"], "回滚键", "回滚内容-一")
    res = asyncio.run(hub.rollback_memory("回滚键", vid, "agent-1"))
    assert res["status"] == "rolled_back"
    db_row = _pool_row(env["db_path"], mid)
    assert db_row["content"] == "回滚内容-一", "库内已回滚到旧版本"

    _drain(env["db_path"], hub)
    assert len(hub._shadow.calls) == 3, \
        "rollback 变更点必须触发重镜像（旧代码完全无第三次 submit → 红）"
    kind, sp = hub._shadow.calls[2]
    assert kind == "memory"
    assert sp["memory_id"] == mid
    assert sp["content"] == "回滚内容-一", \
        "重镜像 payload 必须是库内当前值（回滚后内容），不是提交时刻旧快照"


# T6-4 rollback → vector_index 事件入队且被消费（embedding 与库内 blob 一致）
def test_rollback_enqueues_vector_reindex(env):
    hub = _MiniHub()
    mid = _write_two_versions(hub, "向量回滚键", "向量内容-一", "向量内容-二")
    before = hub._chroma_collection.calls["upsert"]

    vid = _version_id_of(env["db_path"], "向量回滚键", "向量内容-一")
    res = asyncio.run(hub.rollback_memory("向量回滚键", vid, "agent-1"))
    assert res["status"] == "rolled_back"

    rows = [r for r in _outbox_rows(env["db_path"], "vector_index")
            if json.loads(r["payload"]).get("memory_id") == mid]
    assert len(rows) == 1, \
        "rollback 内容变了必须登记 vector_index 重灌事件（CD-050；旧代码无 → 红）"
    assert json.loads(rows[0]["payload"])["op"] == "upsert"

    _drain(env["db_path"], hub)
    assert _outbox_rows(env["db_path"], "vector_index")[0]["status"] == "done"
    assert hub._chroma_collection.calls["upsert"] == before + 1, \
        "rollback 后向量必须重灌一次"
    doc = hub._chroma_collection.store.get(mid)
    assert doc is not None
    # CD-048 起向量 metadata 不再携带正文（去明文）→ 内容新鲜度改为「库内当前值 + 向量取库内 blob」两面证明
    assert "content" not in doc["metadata"] and "summary" not in doc["metadata"], \
        "CD-048：向量 metadata 不得含正文/摘要明文"
    assert doc["metadata"]["level"] == "summary", "CD-048：metadata 必须带 level 字段"
    db_row = _pool_row(env["db_path"], mid)
    assert db_row["content"] == "向量内容-一", \
        "库内当前值必须是回滚后内容（重灌正是按库内值做的）"
    assert doc["embedding"] == np.frombuffer(
        db_row["embedding"], dtype=np.float32).tolist(), \
        "重灌 embedding 必须来自库内 blob（不调模型）"


# T6-5 跨天：库内 updated_at 是昨天 → 重镜像 date 用库内日期
def test_remirror_uses_db_date_not_today(env):
    hub = _MiniHub()
    r = _store(hub, "跨天键", "昨天写入今天重镜像")
    mid = r["memory_id"]
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(env["db_path"])
    conn.execute("UPDATE memory_pool SET updated_at=? WHERE memory_id=?",
                 (yesterday, mid))
    conn.commit()
    conn.close()

    # 模拟变更点触发的重镜像事件（等价 rollback 入队的那条）
    _enqueue_committed(env["db_path"], "shadow_mirror",
                       {"kind": "memory", "memory_id": mid})
    _drain(env["db_path"], hub)

    assert len(hub._shadow.calls) == 2, "写入 1 次 + 重镜像 1 次"
    sp = hub._shadow.calls[1][1]
    assert sp["date"] == yesterday[:10], \
        "重镜像 date 必须取库内 updated_at[:10]（用今天会再生成一份 vault 档案）"
    assert sp["date"] != datetime.now(timezone.utc).isoformat()[:10]


# T6-6 _record_origins 失败可观测 + collect_origins 文件回源仍可用
def test_record_origins_failure_observable(tmp_path, monkeypatch, caplog):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": True,
                           "wiki": True, "shared": True},
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    w = ShadowWriter(dt, pending_db_path=str(tmp_path / "pending.db"))

    def _boom_head(self):
        raise RuntimeError("head_hash boom")

    monkeypatch.setattr(type(w.dt.trunk), "head_hash", _boom_head)
    with caplog.at_level(logging.WARNING, logger="xingshu.shadow"):
        w._record_origins(  # 不得抛异常（降级语义不变）
            {"default": [{"id": "m-orig", "kind": "memory",
                          "path": "vault/memory/2026-09-17/m-orig.md"}]},
            "测试批次-锚点")
    assert w.stats.get("origins_failed", 0) >= 1, \
        "锚点登记失败必须计 stats[origins_failed]"
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "锚点登记失败" in r.getMessage()]
    assert warns, "必须有 WARNING 日志"
    assert "RuntimeError" in warns[0].getMessage(), "warning 须含异常类型"
    assert "测试批次-锚点" in warns[0].getMessage(), "warning 须含批次"

    # 既有 collect_origins（从 index/.commits.jsonl 纯文件重建）仍可用
    monkeypatch.undo()  # 恢复 head_hash（collect_origins 不依赖它，保险起见）
    root = w.dt.trunk.root
    os.makedirs(os.path.join(root, "index"), exist_ok=True)
    with open(os.path.join(root, "index", "memory.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({
            "kind": "memory", "id": "m-orig", "owner": "agent-1",
            "branch": "default", "path": "vault/memory/2026-09-17/m-orig.md",
            "date": "2026-09-17"}, ensure_ascii=False) + "\n")
    with open(os.path.join(root, "index", ".commits.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": "2026-09-17T10:00:00+08:00", "commit": "abc123",
            "branches": {"default": "def456"}, "ids": ["m-orig"],
            "batch": "测试批次"}, ensure_ascii=False) + "\n")
    out = collect_origins(w.dt, ids=["m-orig"])
    assert out["m-orig"]["path"] == "vault/memory/2026-09-17/m-orig.md"
    assert out["m-orig"]["trunk_commit"] == "abc123"
    assert out["m-orig"]["branch_commit"] == "def456"


# T6-7 开关跳过可观测 + 端点透出 enabled/switches/skipped_*
def test_submit_skip_counting_and_stats_endpoint(monkeypatch):
    # 全局 disabled → skipped_disabled +1，无任何投递
    dt_off = SimpleNamespace(enabled=False, shadow={})
    w_off = ShadowWriter(dt_off)
    w_off.submit("memory", {"memory_id": "m-skip"})
    assert w_off.queue_depth() == 0
    assert w_off.stats.get("skipped_disabled") == 1
    snap = w_off.stats_snapshot()
    assert snap["enabled"] is False
    assert "switches" in snap, "stats_snapshot 必须带 switches 明细"
    assert snap["stats"]["skipped_disabled"] == 1

    # 单 kind 关闭 → skipped_kind[kind] +1
    dt_on = SimpleNamespace(enabled=True, shadow={"memory": True})
    w_on = ShadowWriter(dt_on)
    w_on.submit("knowledge", {"entry_id": "k-skip"})
    assert w_on.queue_depth() == 0
    assert w_on.stats["skipped_kind"]["knowledge"] == 1
    snap_on = w_on.stats_snapshot()
    assert snap_on["enabled"] is True
    assert snap_on["switches"] == {"memory": True, "knowledge": False,
                                   "wiki": False, "shared": False}

    # 端点：真实 writer → 透出 enabled/switches/skipped_*；None → 占位不 500
    import routes_maintenance
    from hub_core import hub as global_hub
    monkeypatch.setattr(global_hub, "_shadow", w_on)
    out = asyncio.run(routes_maintenance.api_shadow_stats())
    assert out["enabled"] is True
    assert out["switches"]["memory"] is True
    assert out["stats"]["skipped_kind"]["knowledge"] == 1

    monkeypatch.setattr(global_hub, "_shadow", None)
    out = asyncio.run(routes_maintenance.api_shadow_stats())
    assert out["enabled"] is False
    assert "switches" in out, "影子未构造的占位响应也要带 switches 键"


# T6-8 回退门禁：提交路径全走事件；outbox 有 shadow_mirror 分发；core 有同步方法
def test_regression_guard_event_driven_mirror():
    with open(os.path.join(ROOT, "hub_mixins", "memory.py"),
              encoding="utf-8") as f:
        memory_src = f.read()
    assert "_shadow.submit(" not in memory_src, \
        "hub_mixins/memory.py 不得再出现 _shadow.submit(（提交路径全部改走事件）"

    with open(os.path.join(ROOT, "hub_mixins", "outbox.py"),
              encoding="utf-8") as f:
        outbox_src = f.read()
    assert '"shadow_mirror"' in outbox_src, \
        "hub_mixins/outbox.py 必须有 shadow_mirror 分发分支"

    with open(os.path.join(ROOT, "hub_core.py"), encoding="utf-8") as f:
        core_src = f.read()
    assert "_shadow_mirror_sync" in core_src, \
        "hub_core.py 必须有 _shadow_mirror_sync（消费侧读库内最新值）"
