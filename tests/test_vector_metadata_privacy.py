# -*- coding: utf-8 -*-
"""CD-048 向量 metadata 最小披露（裂缝2）验收测试 — T4 任务书（2026-09-17）

工程约定照抄 tests/test_vector_index_consistency.py：_MiniHub 最小宿主 + 临时库
（db.init_db 全量建表）+ 假 chroma collection（内存捕获 metadatas，确定性优先）；
检索路径（_chroma_search 的 where 语义）用真 chromadb.EphemeralClient
（与 tests/test_kb_unified_retrieval.py 同配方，真实验证 $ne 缺键语义）。

覆盖（任务书 §4）：
T4-1 metadata 键集合：写路径产出的 metadata 不含 content/summary、含 level，
    且与 _vector_metadata() 返回的键集合逐字一致
T4-2 三处一致：ingest 重建后某条 memory 的 metadata 键集合 == 写路径一致
T4-3 检索不受影响：_chroma_search 对同一条记忆返回的 content 来自 SQLite 回查、
    disclosure_level 判定不变；worker 查他人机密仍不可见（fail-closed）；
    旧向量兼容：带 content/summary 明文的历史向量仍正常回查取正文
T4-4 where 粗过滤：预置 level="none" 的向量 → 检索结果不含它
T4-5 补偿路径：hub_core._reindex_vector_sync 重灌出的 metadata 同样无明文、键集合一致
"""
import asyncio
import json
import os
import sqlite3
import sys
import uuid

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chromadb  # noqa: E402

import audit.memory_audit as memory_audit  # noqa: E402
import db as db_mod  # noqa: E402
import models  # noqa: E402
from disclosure import DisclosureEngine  # noqa: E402
from hub_core import SyncHub  # noqa: E402  （复用 _merge_trust/_trust_from_source/_reindex_vector_sync 真实现）
from hub_mixins.ingest import IngestMixin  # noqa: E402
from hub_mixins.memory import MemoryMixin, _vector_metadata  # noqa: E402
from models import MemoryEntry, SemanticSearchRequest  # noqa: E402

# CD-048 拍板的向量 metadata 键集合（最小披露：无 content/summary，新增 level）
VECTOR_METADATA_KEYS = {
    "owner", "key", "tags", "importance", "kind",
    "confidence", "source_type", "layer", "level",
}


def _emb(text: str):
    """确定性的内容相关合成向量（与 test_vector_index_consistency 同式）。"""
    seed = (sum(map(ord, text)) % 100) / 100.0
    return np.array([seed] * 8, dtype=np.float32).tolist()


class _FakeModel:
    """假 embedding 模型：encode 返回内容相关的确定向量。"""

    def encode(self, text):
        return np.array(_emb(text), dtype=np.float32)


class _CaptureChroma:
    """假 chroma collection：add/upsert/delete 捕获到内存 dict（metadatas 逐字保留）。

    delete 同时支持 ids= 与 where= 两种签名（rebuild 清空段用 where）。
    """

    def __init__(self):
        self.store = {}  # id -> {"embedding": list, "metadata": dict}
        self.calls = {"add": 0, "upsert": 0, "delete": 0}

    def add(self, ids, embeddings, metadatas):
        self.calls["add"] += 1
        for i, e, m in zip(ids, embeddings, metadatas):
            self.store[i] = {"embedding": list(e), "metadata": dict(m)}

    def upsert(self, ids, embeddings, metadatas):
        self.calls["upsert"] += 1
        for i, e, m in zip(ids, embeddings, metadatas):
            self.store[i] = {"embedding": list(e), "metadata": dict(m)}

    def delete(self, ids=None, where=None):
        self.calls["delete"] += 1
        if ids:
            for i in ids:
                self.store.pop(i, None)
        elif where is not None:
            self.store.clear()  # 测试场景 where 即"清空重建"，直接清空即可


def _policy_defaults():
    """与 hub_core._load_disclosure_policy 默认值对齐"""
    return {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
    }


class _MiniHub(MemoryMixin, IngestMixin):
    """最小宿主：MemoryMixin + IngestMixin 真实现，假模型/假审计，chroma 可换。"""

    _merge_trust = staticmethod(SyncHub._merge_trust)
    _trust_from_source = staticmethod(SyncHub._trust_from_source)

    def _reindex_vector_sync(self, payload):
        # 绑定 SyncHub 真实现（补偿重灌路径）
        return SyncHub._reindex_vector_sync(self, payload)

    def __init__(self, db_path, chroma=None):
        self._db_path = str(db_path)
        self.agents = {}
        self._shadow = None
        self._chroma_collection = chroma if chroma is not None else _CaptureChroma()
        self._memory_lock = asyncio.Lock()
        self._disclosure_policy = _policy_defaults()
        self.disclosure = DisclosureEngine(self)

    def _db(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    async def _ensure_embedding_model(self):
        return _FakeModel()

    async def _log_event(self, *args, **kwargs):
        return None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表）+ 临时审计目录 + hasher 重建 provider。"""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    monkeypatch.setattr(models.CONFIG, "EMBEDDING_PROVIDER", "hasher")
    monkeypatch.setattr(memory_audit, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(memory_audit, "AUDIT_FILE",
                        str(tmp_path / "audit" / "memory_pool.jsonl"))
    monkeypatch.setattr(memory_audit, "_rolling_chain", None)
    return {"db_path": db_path, "tmp_path": tmp_path}


def _real_chroma():
    """真 chroma 临时集合（每用例独立名，防 EphemeralClient 进程内共享存储）。"""
    client = chromadb.EphemeralClient()
    return client.get_or_create_collection(
        f"test_cd048_{uuid.uuid4().hex[:12]}", metadata={"hnsw:space": "cosine"})


def _insert_memory_row(db_path, memory_id, owner, content, disclosure_level="summary"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO memory_pool
           (memory_id, owner_agent_id, memory_key, content, summary, importance,
            tags, kind, confidence, source_type, disclosure_level, disclosure_scope,
            allowed_viewers, created_at, updated_at, trust_level, source_agent_id)
           VALUES (?, ?, ?, ?, ?, 1.0, '[]', 'fact', 1.0, 'user', ?, 'manager',
                   '[]', '2026-09-17T00:00:00+00:00', '2026-09-17T00:00:00+00:00',
                   'internal', ?)""",
        (memory_id, owner, memory_id[:8], content, content[:200],
         disclosure_level, owner),
    )
    conn.commit()
    conn.close()


# ═══════════ T4-1 metadata 键集合（写路径） ═══════════

def test_t4_1_write_path_metadata_keyset(env):
    expected = _vector_metadata("o", "k", '["t"]', 1.0, "fact", 0.9, "user", "summary")
    assert set(expected.keys()) == VECTOR_METADATA_KEYS, \
        f"_vector_metadata 键集合漂移: {sorted(expected.keys())}"
    assert "content" not in expected and "summary" not in expected
    assert expected["level"] == "summary"

    hub = _MiniHub(env["db_path"])
    r = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="最小披露键", content="客户偏好现代简约风格的橱柜")))
    assert r["status"] == "stored" and not r["locked"]
    doc = hub._chroma_collection.store.get(r["memory_id"])
    assert doc is not None, "写路径向量必须落索引"
    assert set(doc["metadata"]) == set(expected.keys()), \
        f"写路径 metadata 键集合必须与 _vector_metadata 逐字一致: {sorted(doc['metadata'])}"
    assert "content" not in doc["metadata"], "metadata 严禁留 content 明文"
    assert "summary" not in doc["metadata"], "metadata 严禁留 summary 明文"
    assert doc["metadata"]["level"] == "summary", "level 必须等于该记忆自身 disclosure_level"


# ═══════════ T4-2 三处一致：ingest 重建 == 写路径 ═══════════

def test_t4_2_ingest_rebuild_metadata_consistent(env):
    hub = _MiniHub(env["db_path"])
    r = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="重建一致键", content="安装后七天内可申请退换货")))
    assert r["status"] == "stored" and not r["locked"]
    mid = r["memory_id"]
    write_meta = dict(hub._chroma_collection.store[mid]["metadata"])

    # 假模型 8 维 ≠ hasher 384 维 → 该行 stale，重建必走 Chroma 重灌段
    out = asyncio.run(hub.rebuild_embeddings(requester="test"))
    assert out["status"] == "ok", f"重建失败: {out}"
    assert out["rebuilt_mem"] == 1, f"应重建 1 条: {out}"

    rebuilt_meta = hub._chroma_collection.store[mid]["metadata"]
    assert set(rebuilt_meta) == set(write_meta) == VECTOR_METADATA_KEYS, \
        f"重建 metadata 键集合必须与写路径一致: {sorted(rebuilt_meta)}"
    assert "content" not in rebuilt_meta and "summary" not in rebuilt_meta
    assert rebuilt_meta["level"] == write_meta["level"] == "summary"
    # 同值口径抽查：owner/key/layer 一致
    for k in ("owner", "key", "layer"):
        assert rebuilt_meta[k] == write_meta[k]


# ═══════════ T4-3 检索不受影响 + 旧向量兼容（真 chroma） ═══════════

def test_t4_3_search_content_still_from_sqlite(env):
    collection = _real_chroma()
    hub = _MiniHub(env["db_path"], chroma=collection)
    content = "客户张姐的橱柜订单尾款还有两万未结清，约定月底付款"
    r = asyncio.run(hub.store_memory(
        "owner-a", MemoryEntry(memory_key="检索回查键", content=content)))
    assert r["status"] == "stored" and not r["locked"]

    # 索引 metadata 已无明文（前提确认）
    got = collection.get(ids=[r["memory_id"]], include=["metadatas"])
    assert "content" not in got["metadatas"][0]

    query_emb = _FakeModel().encode(content).tolist()
    # owner 自查：规则 1 → FULL，正文必须等于 SQLite 全文（唯一来源）
    req = SemanticSearchRequest(query="尾款", requester_agent_id="owner-a", n_results=5)
    hits = asyncio.run(hub.disclosure._chroma_search(req, query_emb))
    assert len(hits) == 1
    assert hits[0]["content"] == content, \
        "返回正文必须来自 SQLite 回查（metadata 已无 content），且 FULL 级给全文"
    assert hits[0]["disclosure_level"] == "full"

    # 他人 worker 查：规则 7 无协作 → NONE → fail-closed 不可见
    hub.agents["owner-a"] = {"role": "worker", "managed_agents": [],
                             "disclosure_policy": {}, "department": "客服部"}
    hub.agents["worker-b"] = {"role": "worker", "managed_agents": [],
                              "disclosure_policy": {}, "department": "销售部"}
    req_b = SemanticSearchRequest(query="尾款", requester_agent_id="worker-b", n_results=5)
    hits_b = asyncio.run(hub.disclosure._chroma_search(req_b, query_emb))
    assert not hits_b, "worker 查他人机密必须 fail-closed 不可见"


def test_t4_3_legacy_plaintext_metadata_vector_compatible(env):
    """旧向量兼容（任务书 T4，只测不改）：历史向量 metadata 带 content/summary
    明文、无 level 键 → 检索仍正常回查 SQLite 取正文、披露判定不变。"""
    collection = _real_chroma()
    hub = _MiniHub(env["db_path"], chroma=collection)
    mid = "m" * 20
    content = "旧索引时代的记忆正文：浴室柜色号以合同附件为准"
    _insert_memory_row(env["db_path"], mid, "owner-a", content,
                       disclosure_level="full")
    # 灌一条旧格式向量：metadata 带 content[:500]/summary 明文、无 level、无 layer
    legacy_meta = {
        "owner": "owner-a", "key": mid[:8], "tags": "[]",
        "content": content[:500], "summary": content[:200],
        "importance": 1.0, "kind": "fact", "confidence": 1.0,
        "source_type": "user",
    }
    collection.add(ids=[mid], embeddings=[_emb(content)], metadatas=[legacy_meta])

    req = SemanticSearchRequest(query="浴室柜", requester_agent_id="owner-a", n_results=5)
    hits = asyncio.run(hub.disclosure._chroma_search(req, _emb(content)))
    assert len(hits) == 1, "带旧 metadata（且无 level 键）的向量必须仍被检索命中"
    assert hits[0]["content"] == content, "旧向量回查正文仍来自 SQLite，不受 metadata 明文影响"
    assert hits[0]["disclosure_level"] == "full", "披露级别判定不变"


# ═══════════ T4-4 where 粗过滤：level="none" 不命中（真 chroma） ═══════════

def test_t4_4_where_excludes_level_none(env):
    collection = _real_chroma()
    hub = _MiniHub(env["db_path"], chroma=collection)
    # 正常向量（level=summary）
    ok_mid = "n" * 20
    _insert_memory_row(env["db_path"], ok_mid, "owner-a", "正常的可见记忆内容",
                       disclosure_level="summary")
    collection.add(ids=[ok_mid], embeddings=[_emb("正常的可见记忆内容")],
                   metadatas=[_vector_metadata("owner-a", ok_mid[:8], "[]", 1.0,
                                               "fact", 1.0, "user", "summary")])
    # 历史脏数据：level="none" 的向量（NONE 级本不建向量，这是防残留）
    dirty_mid = "d" * 20
    _insert_memory_row(env["db_path"], dirty_mid, "owner-a", "敏感脏数据残留向量",
                       disclosure_level="none")
    collection.add(ids=[dirty_mid], embeddings=[_emb("敏感脏数据残留向量")],
                   metadatas=[_vector_metadata("owner-a", dirty_mid[:8], "[]", 1.0,
                                               "fact", 1.0, "user", "none")])

    req = SemanticSearchRequest(query="记忆", requester_agent_id="owner-a", n_results=10)
    hits = asyncio.run(hub.disclosure._chroma_search(req, _emb("记忆")))
    hit_ids = {h["memory_id"] for h in hits}
    assert ok_mid in hit_ids, "正常向量必须命中"
    assert dirty_mid not in hit_ids, "level=none 的脏向量必须被 where 粗过滤排除"


# ═══════════ T4-5 补偿路径：_reindex_vector_sync 重灌无明文 ═══════════

def test_t4_5_reindex_compensation_metadata_consistent(env):
    hub = _MiniHub(env["db_path"])
    r = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="补偿重灌键", content="补偿路径重灌的内容样本")))
    assert r["status"] == "stored" and not r["locked"]
    mid = r["memory_id"]
    write_meta = dict(hub._chroma_collection.store[mid]["metadata"])

    # 模拟索引丢失 + outbox 补偿重灌
    hub._chroma_collection.store.pop(mid)
    hub._reindex_vector_sync({"op": "upsert", "memory_id": mid})
    doc = hub._chroma_collection.store.get(mid)
    assert doc is not None, "补偿重灌后向量必须补上"
    assert set(doc["metadata"]) == set(write_meta) == VECTOR_METADATA_KEYS, \
        f"补偿重灌 metadata 键集合必须与写路径一致: {sorted(doc['metadata'])}"
    assert "content" not in doc["metadata"] and "summary" not in doc["metadata"]
    assert doc["metadata"]["level"] == "summary"
