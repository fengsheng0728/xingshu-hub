# -*- coding: utf-8 -*-
"""CD-051: 知识向量对账（启动时对账 + POST /api/v1/knowledge/reindex 维护端点）

配方（确定性优先，不起真实 Hub、不连 3060、不碰生产 sync_hub.db / chroma_db）：
  - tmp_path 独立 sqlite 库（monkeypatch CONFIG.DB_PATH，db_facade 运行时读取）
  - FakeCollection 假 chroma collection：内存 id→metadata，可注入
    「缺 chunk / 多 chunk / 指定 entry upsert 失败」三种态
  - 对账核心直接驱动 KnowledgeMixin.reconcile_kb_vectors；
    端点直调 routes_knowledge.api_knowledge_reindex（test_endpoint_role_gate 同款）；
    启动钩子直调 SyncHub.schedule_kb_reconcile（不真起 Hub）

覆盖（任务书 T7）：
  T7-1 补齐漏建：预置部分 chunk（模拟 add 失败残留）→ 对账后 ids == 期望集合
  T7-2 清多余：预置多余旧 chunk → 对账后只剩期望 ids（差集被删）
  T7-3 幂等：连跑两次 → 第二次 reparsed == 0、集合不变
  T7-4 逐条容错：某条重灌抛错 → 计入 errors、其余正常、函数不抛
  T7-5 降级：chroma 不可用 / 模型缺失 → status=skipped，不抛
  T7-6 端点：worker → 403；manager / hub_token → 200 且返回统计体；
       指定 entry_id 只处理该条
  T7-7 开关：reconcile_on_start=False → 启动钩子不调度；True → 延迟后对账被触发
"""
import asyncio
import os
import sqlite3
import sys
import time

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod
from models import CONFIG
from hub_core import SyncHub
from hub_mixins.knowledge import KnowledgeMixin, kb_chunk_id
import routes_common
import routes_knowledge

# 两段均 >150 字符（> MIN_TOKENS=50 的合并阈值），保证切出 ≥2 个 chunk
_PARA1 = ("星枢 SyncHub 的知识向量对账机制在启动后延迟执行，逐条比对 chroma 中实际"
          "chunk 与按当前切分口径应得的 ids，不一致就重灌该条。对账走 CD-049 改序后"
          "的实现：先 upsert 新 chunk，再删差集，任意时刻不出现零 chunk 中间态。"
          "切分口径必须与写入侧完全一致，复用 chunker.chunk_document 与 kb_chunk_id，"
          "并遵守 KB_EMBED_MAX_CHUNKS 上限，禁止另造一套切片或 id 规则，否则会对不上"
          "而误判缺失，从而反复重灌同一条知识，造成写入放大与索引抖动。")
_PARA2 = ("维护端点 POST /api/v1/knowledge/reindex 可指定 entry_id 单条对账，缺省"
          "全量；权限与 knowledge_upsert 同门，manager/orchestrator 或 hub_token"
          "放行，worker 一律 403；单条失败进 errors 列表，绝不以 500 掩盖。"
          "对账不允许阻塞启动：在后台 task 里延迟执行，失败只告警；chroma 不可用"
          "或 embedding 模型缺失时跳过并记日志；延迟秒数可由配置项调节，"
          "以避开启动高峰与模型首载的资源竞争，保证主链路启动耗时不受影响。")
CONTENT = f"{_PARA1}\n\n{_PARA2}"


# ═══════════ 配方 ═══════════

class FakeCollection:
    """记录内部态 id→metadata；fail_entry 注入指定条目的 upsert 失败。"""

    def __init__(self):
        self.store = {}
        self.fail_entry = None

    @staticmethod
    def _entry_id_of(where):
        for cond in (where or {}).get("$and", []):
            if "entry_id" in cond:
                return cond["entry_id"]
        return None

    def get(self, where=None, include=None):
        eid = self._entry_id_of(where)
        ids = [i for i, m in self.store.items()
               if eid is None or m.get("entry_id") == eid]
        return {"ids": ids}

    def upsert(self, ids, embeddings, metadatas):
        if self.fail_entry and any(
                m.get("entry_id") == self.fail_entry for m in metadatas):
            raise RuntimeError(f"injected upsert failure: {self.fail_entry}")
        for i, m in zip(ids, metadatas):
            self.store[i] = dict(m)

    def delete(self, where=None, ids=None):
        for i in (ids or []):
            self.store.pop(i, None)


def _make_hub(collection, model_missing=False):
    hub = KnowledgeMixin.__new__(KnowledgeMixin)
    hub._chroma_collection = collection

    def _model(texts):
        return [[0.0] * 8 for _ in texts]

    async def _fake_ensure():
        return None if model_missing else _model

    hub._ensure_embedding_model = _fake_ensure
    return hub


def _insert_entry(db_path, entry_id, content, title="t", category="c"):
    now = "2026-09-17T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO knowledge_base
           (entry_id, title, content, tags, links, category, importance,
            created_by, created_at, updated_at)
           VALUES (?, ?, ?, '[]', '[]', ?, 0.5, 'tester', ?, ?)""",
        (entry_id, title, content, category, now, now))
    conn.commit()
    conn.close()


def _expected_ids(entry_id, content):
    """与写入侧同口径：chunk_document + kb_chunk_id + KB_EMBED_MAX_CHUNKS 上限"""
    from chunker import chunk_document
    max_chunks = getattr(CONFIG, "KB_EMBED_MAX_CHUNKS", 200)
    chunks = chunk_document(f"kb:{entry_id}", content, embed_fn=None)[:max_chunks]
    return {kb_chunk_id(entry_id, c["piece_index"]) for c in chunks}


def _ids_of(coll, entry_id):
    return {i for i, m in coll.store.items() if m.get("entry_id") == entry_id}


def _seed_chunks(coll, entry_id, chunk_ids):
    for cid in chunk_ids:
        coll.store[cid] = {"entry_id": entry_id, "layer": "knowledge"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立 sqlite 库 + 假 collection 环境"""
    db_path = str(tmp_path / "kb_reconcile.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    coll = FakeCollection()
    hub = _make_hub(coll)
    return hub, coll, db_path


# ═══════════ T7-1 补齐漏建 ═══════════

def test_reconcile_backfills_missing_chunks(env):
    """T7-1: 预置某 entry 只写入部分 chunk（模拟 add 失败残留）
    → 对账后 chunk ids 集合 == 期望集合"""
    hub, coll, db_path = env
    _insert_entry(db_path, "e1", CONTENT)
    expected = _expected_ids("e1", CONTENT)
    assert len(expected) >= 2, f"测试语料应切出 ≥2 chunk，实际 {len(expected)}"
    _seed_chunks(coll, "e1", sorted(expected)[:1])  # 只写入 1 个（漏建态）
    stats = asyncio.run(hub.reconcile_kb_vectors())
    assert stats["status"] == "ok"
    assert stats["checked"] == 1 and stats["reparsed"] == 1
    assert not stats["errors"], f"不应有错误: {stats['errors']}"
    assert _ids_of(coll, "e1") == expected, \
        f"补齐后应等于期望集合，实际 {sorted(_ids_of(coll, 'e1'))}"
    assert stats["duration_ms"] >= 0


# ═══════════ T7-2 清多余 ═══════════

def test_reconcile_removes_stale_chunks(env):
    """T7-2: 预置比期望多的旧 chunk（旧版本残留）→ 对账后只剩期望 ids"""
    hub, coll, db_path = env
    _insert_entry(db_path, "e1", CONTENT)
    expected = _expected_ids("e1", CONTENT)
    _seed_chunks(coll, "e1", sorted(expected) + ["kb:e1:98", "kb:e1:99"])
    stats = asyncio.run(hub.reconcile_kb_vectors())
    assert stats["reparsed"] == 1
    assert stats["removed"] == 2, f"应清掉 2 个多余 chunk，实际 {stats['removed']}"
    assert _ids_of(coll, "e1") == expected, \
        f"清多余后应只剩期望 ids，实际 {sorted(_ids_of(coll, 'e1'))}"


# ═══════════ T7-3 幂等 ═══════════

def test_reconcile_idempotent_second_run(env):
    """T7-3: 连跑两次 → 第二次 reparsed == 0、集合不变"""
    hub, coll, db_path = env
    _insert_entry(db_path, "e1", CONTENT)
    expected = _expected_ids("e1", CONTENT)
    _seed_chunks(coll, "e1", sorted(expected)[:1])
    s1 = asyncio.run(hub.reconcile_kb_vectors())
    assert s1["reparsed"] == 1
    snapshot = _ids_of(coll, "e1")
    s2 = asyncio.run(hub.reconcile_kb_vectors())
    assert s2["reparsed"] == 0, f"第二次不应再重灌，实际 {s2}"
    assert s2["ok"] == 1
    assert _ids_of(coll, "e1") == snapshot == expected


# ═══════════ T7-4 逐条容错 ═══════════

def test_reconcile_per_entry_fault_tolerance(env, caplog):
    """T7-4: 某条重灌抛错 → 该条计入 errors、其余条目仍被处理、函数不抛"""
    hub, coll, db_path = env
    _insert_entry(db_path, "e_bad", CONTENT)
    _insert_entry(db_path, "e_ok", CONTENT)
    coll.fail_entry = "e_bad"  # 注入 e_bad 的 upsert 失败
    stats = asyncio.run(hub.reconcile_kb_vectors())  # 不抛
    assert stats["checked"] == 2
    assert stats["reparsed"] == 1, f"e_ok 应重灌成功: {stats}"
    assert len(stats["errors"]) == 1 and "e_bad" in stats["errors"][0]
    # e_ok 的 chunk 确实补齐；e_bad 仍缺（失败可告警、不静默）
    assert _ids_of(coll, "e_ok") == _expected_ids("e_ok", CONTENT)
    assert _ids_of(coll, "e_bad") == set()


# ═══════════ T7-5 降级 ═══════════

def test_reconcile_degraded_chroma_unavailable(env):
    """T7-5a: chroma 不可用 → status=skipped，不抛"""
    hub, coll, db_path = env
    _insert_entry(db_path, "e1", CONTENT)
    hub._chroma_collection = None
    stats = asyncio.run(hub.reconcile_kb_vectors())
    assert stats["status"] == "skipped"
    assert stats.get("reason") == "chroma_unavailable"
    assert stats["checked"] == 0 and not stats["errors"]


def test_reconcile_degraded_model_missing(env):
    """T7-5b: embedding 模型缺失 → status=skipped，不抛"""
    hub, coll, db_path = env
    _insert_entry(db_path, "e1", CONTENT)
    hub_missing = _make_hub(coll, model_missing=True)
    stats = asyncio.run(hub_missing.reconcile_kb_vectors())
    assert stats["status"] == "skipped"
    assert stats.get("reason") == "embedding_model_unavailable"
    assert stats["checked"] == 0 and not stats["errors"]


def test_reconcile_empty_content_skipped(env):
    """T7-5c: 内容为空 → 该条计 skipped（与写侧"空内容不动索引"同语义）"""
    hub, coll, db_path = env
    _insert_entry(db_path, "e_empty", "   ")
    _insert_entry(db_path, "e1", CONTENT)
    stats = asyncio.run(hub.reconcile_kb_vectors())
    assert stats["checked"] == 2
    assert stats["skipped"] == 1
    assert stats["reparsed"] == 1  # e1 从空 collection 补齐


# ═══════════ T7-6 端点 ═══════════

class _FakeRequest:
    """最小 Request stub：路由读 request.scope + await request.json()"""

    def __init__(self, principal=None, body=None):
        self.scope = {}
        if principal is not None:
            self.scope["principal"] = principal
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.fixture()
def gate_env(monkeypatch):
    """真实门卫语义：NO_AUTH=False + 可控 role 查询；reconcile 打桩记录入参"""
    monkeypatch.setattr(routes_knowledge, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    roles = {"mgr-1": "manager", "wkr-1": "worker", "orc-1": "orchestrator"}
    monkeypatch.setattr(routes_common, "_agent_role",
                        lambda agent_id: roles.get(agent_id, ""))
    calls = []

    async def _fake_reconcile(self, entry_id=""):
        calls.append(entry_id)
        return {"status": "ok", "checked": 3, "ok": 2, "reparsed": 1,
                "removed": 0, "skipped": 0, "errors": [], "duration_ms": 5}

    monkeypatch.setattr(SyncHub, "reconcile_kb_vectors", _fake_reconcile)
    return calls


def test_endpoint_worker_403(gate_env):
    """T7-6a: worker → 403（不到 reconcile）"""
    req = _FakeRequest({"auth_mode": "api_key", "subject_id": "wkr-1"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_knowledge.api_knowledge_reindex(req, "wkr-1"))
    assert exc.value.status_code == 403
    assert gate_env == [], "worker 被 403 拦截，不应触达 reconcile"


def test_endpoint_manager_200_with_stats(gate_env):
    """T7-6b: manager → 200 且返回统计体；指定 entry_id 只处理该条"""
    req = _FakeRequest({"auth_mode": "api_key", "subject_id": "mgr-1"},
                       body={"entry_id": "e1"})
    result = asyncio.run(routes_knowledge.api_knowledge_reindex(req, "mgr-1"))
    assert gate_env == ["e1"], f"指定 entry_id 应原样透传，实际 {gate_env}"
    for k in ("checked", "reparsed", "removed", "errors", "duration_ms"):
        assert k in result, f"统计体缺字段 {k}: {result}"


def test_endpoint_hub_token_and_orchestrator_allowed(gate_env):
    """T7-6c: hub_token / orchestrator → 200；缺省 body → 全量（entry_id=''）"""
    req_hub = _FakeRequest({"auth_mode": "hub_token", "subject_id": "__hub__"})
    r1 = asyncio.run(routes_knowledge.api_knowledge_reindex(req_hub, ""))
    assert r1["status"] == "ok"
    req_orc = _FakeRequest({"auth_mode": "api_key", "subject_id": "orc-1"})
    r2 = asyncio.run(routes_knowledge.api_knowledge_reindex(req_orc, "orc-1"))
    assert r2["status"] == "ok"
    assert gate_env == ["", ""], f"缺省 entry_id 应为全量空串，实际 {gate_env}"


# ═══════════ T7-7 启动开关 ═══════════

def test_startup_hook_switch_off(monkeypatch):
    """T7-7a: reconcile_on_start=False → 钩子不调度（不真起 Hub，直测钩子函数）"""
    h = SyncHub.__new__(SyncHub)
    monkeypatch.setattr(
        SyncHub, "_load_kb_reconcile_config", staticmethod(
            lambda: {"reconcile_on_start": False, "reconcile_delay_sec": 0}))
    called = []

    async def _fake_reconcile(self, entry_id=""):
        called.append(entry_id)
        return {}

    monkeypatch.setattr(SyncHub, "reconcile_kb_vectors", _fake_reconcile)

    async def main():
        assert h.schedule_kb_reconcile() is False
        await asyncio.sleep(0.05)  # 给假想中的 task 留调度窗口

    asyncio.run(main())
    assert called == [], "开关关闭时不应触发对账"


def test_startup_hook_switch_on(monkeypatch):
    """T7-7b: reconcile_on_start=True（delay=0）→ 延迟后对账被触发一次"""
    h = SyncHub.__new__(SyncHub)
    monkeypatch.setattr(
        SyncHub, "_load_kb_reconcile_config", staticmethod(
            lambda: {"reconcile_on_start": True, "reconcile_delay_sec": 0}))
    called = []

    async def _fake_reconcile(self, entry_id=""):
        called.append(entry_id)
        return {"status": "ok"}

    monkeypatch.setattr(SyncHub, "reconcile_kb_vectors", _fake_reconcile)

    async def main():
        assert h.schedule_kb_reconcile() is True
        for _ in range(50):  # 等后台 task 跑完（delay=0，正常一两次时钟片即可）
            if called:
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    assert called == [""], f"开关开启应对账一次（全量），实际 {called}"


def test_startup_hook_reconcile_exception_not_raised(monkeypatch, capsys):
    """T7-7c: 对账内部抛错 → 钩子只 print 告警（D4），不向外抛"""
    h = SyncHub.__new__(SyncHub)
    monkeypatch.setattr(
        SyncHub, "_load_kb_reconcile_config", staticmethod(
            lambda: {"reconcile_on_start": True, "reconcile_delay_sec": 0}))

    async def _boom(self, entry_id=""):
        raise RuntimeError("boom")

    monkeypatch.setattr(SyncHub, "reconcile_kb_vectors", _boom)

    async def main():
        assert h.schedule_kb_reconcile() is True
        await asyncio.sleep(0.05)

    asyncio.run(main())  # 不抛即通过
    out = capsys.readouterr().out
    assert "知识向量对账失败" in out, f"失败应 print 告警，实际输出: {out!r}"
