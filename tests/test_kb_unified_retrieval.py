# -*- coding: utf-8 -*-
"""K-1: 知识库检索统一 collection（方案 B）— 写侧/读侧/兼容/披露/降级全链路测试

配方：tmp_path 独立 sqlite 库（monkeypatch CONFIG.DB_PATH，db_facade 运行时读取）
+ chromadb.EphemeralClient（不落盘、不碰生产 chroma_db）+ FakeHub 装配真实
KnowledgeMixin / DisclosureEngine 方法。不起真实端口、不碰生产 sync_hub.db。

覆盖（任务书 K-1 T5 九条）：
 1. id 空间不冲突（kb:{entry_id}:{i} vs memory_id）
 2. 知识条目入向量（layer=knowledge，向量数 == chunk 数）
 3. 回查分流（memory→memory_pool / knowledge→knowledge_base，不静默丢）
 4. 向后兼容（不传 layer 时 memory 命中字段结构与改动前一致）
 5. 旧向量兼容（metadata 无 layer 键 → 按 memory 处理，不报错）
 6. 披露不放松（披露引擎判 NONE 的知识命中不得返回，fail-closed）
 7. 降级不回归（collection=None → degraded=true + 不抛异常）
 9. metadata 无 None（chromadb 不接受 None）
（第 8 条 seed 幂等在 tests/test_seed_kb.py）
"""
import asyncio
import json
import os
import sqlite3
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chromadb

import db as db_mod
from db import get_embedding_provider
from models import CONFIG, KnowledgeEntry, SemanticSearchRequest
from disclosure import DisclosureEngine
from hub_mixins.knowledge import KnowledgeMixin

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HASHER = get_embedding_provider("hasher", n_features=384)

# memory 命中返回体的字段集（改动前现状，T5-4 回归断言锚点）
MEMORY_HIT_FIELDS = {"memory_id", "owner", "disclosure_level", "content",
                     "tags", "importance", "similarity"}


def _policy_defaults():
    """与 hub_core._load_disclosure_policy 默认值对齐"""
    return {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
    }


class FakeHub(KnowledgeMixin):
    """装配真实 KnowledgeMixin/DisclosureEngine 需要的最小 hub 面。

    _enqueue_write 同步直写 tmp 库（替代缓冲队列，测试同步可见）。
    """

    def __init__(self, db_path, collection, chroma_client):
        self._db_path = str(db_path)
        self._chroma_client = chroma_client
        self._chroma_collection = collection
        self._embedding_model = HASHER
        self.agents = {}
        self._disclosure_policy = _policy_defaults()
        self._wiki_sync_pending = False
        self.traces = []

    def _db(self):
        return sqlite3.connect(self._db_path)

    def _enqueue_write(self, kind, payload):
        """缓冲队列的同步等价物：knowledge upsert 直写 tmp 库"""
        if kind == "upsert":
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                """INSERT OR REPLACE INTO knowledge_base
                   (entry_id, title, content, tags, links, category, importance,
                    created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (payload["entry_id"], payload["title"], payload["content"],
                 payload["tags_json"], payload["links_json"], payload["category"],
                 payload["importance"], payload["created_by"],
                 payload["created_at"], payload["updated_at"]),
            )
            conn.commit()
            conn.close()
        return "queued"

    def _record_trace(self, action, agent_id, title, entry_id):
        self.traces.append((action, agent_id, title, entry_id))

    async def _ensure_embedding_model(self):
        return self._embedding_model


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立 sqlite + ephemeral chroma 环境"""
    db_path = tmp_path / "kb_test.db"
    monkeypatch.setattr(CONFIG, "DB_PATH", str(db_path))
    db_mod.init_db()
    client = chromadb.EphemeralClient()
    # EphemeralClient 同名集合在进程内可能共享底层存储 → 每用例独立集合名
    collection = client.get_or_create_collection(
        f"test_kb_{uuid.uuid4().hex[:12]}", metadata={"hnsw:space": "cosine"})
    hub = FakeHub(db_path, collection, client)
    engine = DisclosureEngine(hub)
    return hub, engine, collection


def _run(coro):
    return asyncio.run(coro)


def _encode(text):
    return HASHER.encode(text).tolist()


def _insert_memory(db_path, memory_id, owner, content, disclosure_level="summary"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO memory_pool
           (memory_id, owner_agent_id, memory_key, content, summary, importance,
            tags, kind, confidence, source_type, disclosure_level, disclosure_scope,
            allowed_viewers, created_at, updated_at, trust_level, source_agent_id)
           VALUES (?, ?, ?, ?, ?, 1.0, '[]', 'fact', 1.0, 'user', ?, 'manager',
                   '[]', '2026-09-16T00:00:00+00:00', '2026-09-16T00:00:00+00:00',
                   'internal', ?)""",
        (memory_id, owner, memory_id[:8], content, content[:200],
         disclosure_level, owner),
    )
    conn.commit()
    conn.close()


def _add_memory_vector(collection, memory_id, owner, content, with_layer=True):
    """按 memory.py 写侧 metadata 键集灌一条 memory 向量（with_layer=False 模拟旧残留）"""
    meta = {
        "owner": owner, "key": memory_id[:8], "tags": "[]",
        "content": content[:500], "summary": content[:200],
        "importance": 1.0, "kind": "fact", "confidence": 1.0,
        "source_type": "user",
    }
    if with_layer:
        meta["layer"] = "memory"
    collection.add(ids=[memory_id], embeddings=[_encode(content)], metadatas=[meta])


def _knowledge_entry(entry_id, title, content, category="process", created_by=""):
    return KnowledgeEntry(entry_id=entry_id, title=title, content=content,
                          tags=[], links=[], category=category, importance=1.0,
                          created_by=created_by)


# ═══════════ 1. id 空间不冲突 ═══════════

def test_kb_chunk_id_space_no_collision(env):
    """kb:{entry_id}:{i} 与 memory_id（sha256 hex）不会重复"""
    from hub_mixins.knowledge import kb_chunk_id
    ids = {kb_chunk_id("entry1", i) for i in range(5)}
    assert all(i.startswith("kb:") for i in ids)
    # memory_id 是 20 位 hex（memory.py _insert_new_memory_sync）——不可能带 "kb:" 前缀
    import hashlib, time
    mem_id = hashlib.sha256(f"a:k:{time.time()}".encode()).hexdigest()[:20]
    assert not mem_id.startswith("kb:")
    assert mem_id not in ids


# ═══════════ 2. 知识条目入向量 ═══════════

def test_knowledge_upsert_writes_vectors(env):
    hub, engine, collection = env
    content = ("客户有权在安装后7天内申请退换货。色差问题属于质量问题，免费更换。\n\n"
               "退换货需保留原包装与配件，人为损坏不在免费更换范围。" * 3)
    entry = _knowledge_entry("kb-test-001", "退换货政策", content, category="policy")
    r = _run(hub.knowledge_upsert(entry))
    assert r["status"] == "ok"

    got = collection.get(where={"layer": "knowledge"}, include=["metadatas"])
    assert got["ids"], "知识条目未写入向量集合"
    assert all(m.get("entry_id") == "kb-test-001" for m in got["metadatas"])

    # 向量数 == chunk 数（同一 chunker 口径复算）
    from chunker import chunk_document
    n_chunks = len(chunk_document("kb:kb-test-001", content, embed_fn=None))
    assert len(got["ids"]) == n_chunks

    # knowledge_base 落库行存在（回查分流的前提）
    conn = sqlite3.connect(str(hub._db_path))
    row = conn.execute(
        "SELECT title FROM knowledge_base WHERE entry_id='kb-test-001'").fetchone()
    conn.close()
    assert row and row[0] == "退换货政策"


# ═══════════ 9. metadata 无 None ═══════════

def test_knowledge_metadata_no_none(env):
    hub, engine, collection = env
    entry = _knowledge_entry("kb-test-002", "橱柜色差处理流程",
                             "确认色差程度。安排上门复尺。判断是否批次问题。",
                             category="process")
    _run(hub.knowledge_upsert(entry))
    got = collection.get(where={"layer": "knowledge"}, include=["metadatas"])
    assert got["ids"]
    for m in got["metadatas"]:
        for k, v in m.items():
            assert v is not None, f"metadata[{k}] 为 None（chromadb 不接受）"


# ═══════════ 3. 回查分流（核心：不静默丢知识命中） ═══════════

def test_routing_memory_and_knowledge_hits(env):
    hub, engine, collection = env
    # memory 轨：一条记忆（layer=memory）
    _insert_memory(hub._db_path, "m" * 20, "req-agent", "客户预算是三万，倾向季度付款")
    _add_memory_vector(collection, "m" * 20, "req-agent", "客户预算是三万，倾向季度付款")
    # knowledge 轨：一条知识（经真实写侧入向量）
    entry = _knowledge_entry("kb-test-003", "客户接待流程",
                             "客户进店后先确认预算与风格偏好，再安排设计师上门复尺。")
    _run(hub.knowledge_upsert(entry))

    hub.agents["req-agent"] = {"role": "orchestrator", "managed_agents": [],
                               "disclosure_policy": {}, "department": ""}
    req = SemanticSearchRequest(query="客户", requester_agent_id="req-agent",
                                n_results=10)
    hits = _run(engine._chroma_search(req, _encode("客户")))
    assert hits, "应同时命中 memory 与 knowledge"

    mem_hits = [h for h in hits if h["memory_id"] == "m" * 20]
    kb_hits = [h for h in hits if h.get("origin") == "knowledge"]
    assert mem_hits, "memory 命中丢失"
    assert kb_hits, "knowledge 命中被静默丢弃（回查落空不得 continue 掉整类）"
    assert kb_hits[0]["entry_id"] == "kb-test-003"
    assert kb_hits[0]["title"] == "客户接待流程"
    # memory 命中不带 origin 字段（结构不变）
    assert "origin" not in mem_hits[0]


# ═══════════ 4. 向后兼容：不传 layer 时 memory 命中结构不变 ═══════════

def test_backward_compat_memory_hit_shape(env):
    hub, engine, collection = env
    _insert_memory(hub._db_path, "n" * 20, "req-agent", "今天完成了任务看板的前端开发")
    _add_memory_vector(collection, "n" * 20, "req-agent", "今天完成了任务看板的前端开发")

    hub.agents["req-agent"] = {"role": "worker", "managed_agents": [],
                               "disclosure_policy": {}, "department": ""}
    req = SemanticSearchRequest(query="看板", requester_agent_id="req-agent",
                                n_results=5)
    assert getattr(req, "layer", "") == "", "layer 默认必须为空（不过滤）"
    hits = _run(engine._chroma_search(req, _encode("看板")))
    assert len(hits) == 1
    assert set(hits[0].keys()) == MEMORY_HIT_FIELDS, \
        f"memory 命中字段集漂移: {sorted(hits[0].keys())}"


# ═══════════ 5. 旧向量兼容（无 layer 键 → 按 memory 处理） ═══════════

def test_legacy_vector_without_layer_treated_as_memory(env):
    hub, engine, collection = env
    # 模拟现存 5 条残留向量：metadata 无 layer 键
    _insert_memory(hub._db_path, "o" * 20, "req-agent", "旧维度内容残留向量")
    _add_memory_vector(collection, "o" * 20, "req-agent", "旧维度内容残留向量",
                       with_layer=False)

    hub.agents["req-agent"] = {"role": "worker", "managed_agents": [],
                               "disclosure_policy": {}, "department": ""}
    req = SemanticSearchRequest(query="旧维度", requester_agent_id="req-agent",
                                n_results=5)
    hits = _run(engine._chroma_search(req, _encode("旧维度")))
    assert len(hits) == 1, "无 layer 键的旧向量必须按 memory 回查返回"
    assert hits[0]["memory_id"] == "o" * 20


# ═══════════ 6. 披露不放松（fail-closed） ═══════════

def test_knowledge_hit_published_visible_to_worker(env):
    """CD-033A（2026-09-17 用户拍板）：已发布知识对全员可见，但只到 SUMMARY 级。

    语义变更记录：本用例原名 test_knowledge_hit_disclosure_none_filtered，断言「worker-b 查不到
    worker-a 的知识」——即"发布内容对非协作 worker 全不可见"的旧死路径。用户 2026-09-17 拍板
    「企业已发布内容（知识层/手写页）对全员可见到摘要级」后该断言被取代：现断言 worker-b 能看到，
    但级别必须是 SUMMARY（不得给 FULL 全文）；编排者对照组照旧可见。
    """
    hub, engine, collection = env
    entry = _knowledge_entry("kb-test-004", "内部折扣底线",
                             "橱柜单品最低折扣为八五折，低于此折扣需店长审批。",
                             category="policy", created_by="worker-a")
    _run(hub.knowledge_upsert(entry))

    hub.agents["worker-a"] = {"role": "worker", "managed_agents": [],
                              "disclosure_policy": {}, "department": "销售部"}
    hub.agents["worker-b"] = {"role": "worker", "managed_agents": [],
                              "disclosure_policy": {}, "department": "客服部"}
    hub.agents["boss"] = {"role": "orchestrator", "managed_agents": [],
                          "disclosure_policy": {}, "department": ""}

    # worker-b 查 worker-a 的知识：规则 7 本判 NONE → r4_published_public 链尾提升 → SUMMARY
    req_b = SemanticSearchRequest(query="折扣", requester_agent_id="worker-b",
                                  n_results=5)
    hits_b = [h for h in _run(engine._chroma_search(req_b, _encode("折扣")))
              if h.get("origin") == "knowledge"]
    assert hits_b, "CD-033A：已发布知识应对全员（含非协作 worker）可见到摘要级"
    assert all(h.get("disclosure_level") == "summary" for h in hits_b), \
        "已发布知识只应给到 SUMMARY（不得给 FULL 全文）"

    # orchestrator 查：规则 6 全局可见 → 返回（对照组，证明不是整类被丢）
    req_o = SemanticSearchRequest(query="折扣", requester_agent_id="boss",
                                  n_results=5)
    hits_o = _run(engine._chroma_search(req_o, _encode("折扣")))
    assert [h for h in hits_o if h.get("origin") == "knowledge"], \
        "orchestrator 应能看到知识命中"


# ═══════════ 7. 降级不回归（chroma 不可用） ═══════════

def test_degraded_when_collection_none(env):
    hub, engine, collection = env
    hub._chroma_collection = None
    hub._chroma_client = None  # _ensure_embedding_model 随之返回 None
    _insert_memory(hub._db_path, "p" * 20, "req-agent", "量子计算在金融风控中的应用")

    hub.agents["req-agent"] = {"role": "worker", "managed_agents": [],
                               "disclosure_policy": {}, "department": ""}
    req = SemanticSearchRequest(query="金融风控", requester_agent_id="req-agent",
                                n_results=5)
    res = _run(engine.semantic_search(req))
    assert res.get("degraded") is True, f"collection=None 必须走降级: {res}"
    assert res["total"] > 0, "降级 SQLite 关键词应命中"
    assert "memories" in res


# ═══════════ 写侧降级：chroma 不可用时知识写入不阻塞 ═══════════

def test_knowledge_upsert_degrades_when_chroma_none(env):
    hub, engine, collection = env
    hub._chroma_collection = None
    entry = _knowledge_entry("kb-test-005", "无向量环境写入",
                             "chroma 不可用时应正常落库，不写向量。")
    r = _run(hub.knowledge_upsert(entry))
    assert r["status"] == "ok", "chroma 不可用不得让知识写入失败"
    conn = sqlite3.connect(str(hub._db_path))
    row = conn.execute(
        "SELECT entry_id FROM knowledge_base WHERE entry_id='kb-test-005'").fetchone()
    conn.close()
    assert row, "knowledge_base 落库必须成功"


# ═══════════ 更新语义：同 entry_id 重写不翻倍 ═══════════

def test_knowledge_update_replaces_old_chunks(env):
    hub, engine, collection = env
    e1 = _knowledge_entry("kb-test-006", "标题", "第一版内容。确认色差程度。安排复尺。")
    _run(hub.knowledge_upsert(e1))
    n1 = collection.count()
    assert n1 > 0
    e2 = _knowledge_entry("kb-test-006", "标题", "第二版内容完全不同。质保期两年整。")
    _run(hub.knowledge_upsert(e2))
    got = collection.get(where={"layer": "knowledge"}, include=["metadatas"])
    # CD-052（2026-09-19，Hermes 验收裁决同步）：索引 metadata 已按最小披露**去正文**
    # （删 content 键、改为 piece_index；正文一律回查 knowledge_base 重切）。
    # 故本条不再从 metadata 读正文，改为「按与写侧同口径重切库内 content」还原每段正文，
    # 断言语义一字不变：更新后旧 chunk 必须已被删除（索引里只剩第二版内容）。
    from chunker import chunk_document
    _conn = sqlite3.connect(str(hub._db_path))
    _row = _conn.execute(
        "SELECT content FROM knowledge_base WHERE entry_id='kb-test-006'").fetchone()
    _conn.close()
    _live = {c["piece_index"]: c["content"]
             for c in chunk_document("kb:kb-test-006", _row[0], embed_fn=None)}
    contents = []
    for m in got["metadatas"]:
        _pi = m.get("piece_index")
        assert _pi is not None, "CD-052 后 metadata 必须带 piece_index（回查定位用）"
        contents.append(_live.get(int(_pi), ""))
    assert not any("第一版" in c for c in contents), "更新后旧 chunk 必须先删再写"
    assert all("第二版" in c or "质保" in c for c in contents)
