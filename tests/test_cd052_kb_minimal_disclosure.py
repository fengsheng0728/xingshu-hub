# -*- coding: utf-8 -*-
"""T13 · CD-052 方案A：知识层正文最小披露 + 分层回查 + 索引闭环 验收测试（2026-09-19）

存储与索引侧（对齐 CD-048 memory 层同款手法）：

S-1 metadata 无明文：真实临时 chroma 目录写入一条知识（正文含唯一哨兵串）
    → 该 chunk 的 metadata 不含 content 键、含 piece_index；键集合精确匹配
S-2 静态字节搜零命中（核心证据）：写入后遍历临时 chroma 目录下所有非 .bin
    文件逐字节搜哨兵串 → 命中数必须为 0；反向对照：同一哨兵串在
    knowledge_base.content 里必须能搜到（证明不是哨兵没写进去）
S-3 回查正文逐字相等：检索命中返回的正文 == 按写侧同口径重切 KB content
    后第 piece_index 段的正文（owner 自查 FULL 级）
S-4 doc: 分流：doc: 前缀条目正文取自 document_chunks 且按 chunk 级密级剥离；
    document_chunks 无对应行 → 该命中被丢弃（fail-closed 返回 None）
S-5 重建窗口 fail-closed：置位重建标记期间发起检索 → 返回降级标记
    （degraded + degraded_reason=index_rebuilding）、不含任何正文
S-6 孤儿哨兵（用户修正 1）：注入一条 DB 无对应行的知识 chunk →
    (a) rebuild_embeddings 后该 id 不存在（清库重建语义可断言）；
    (b) reconcile_kb_vectors 反向对账后该 id 被删且 orphan_removed >= 1
S-7 删除补索引（用户修正 2）：knowledge_delete 后该 entry 的 chunk 在索引里
    不存在；collection 抛错时 logger.error 被调用（caplog）且返回契约仍为
    {"status": "deleted"}

配方（确定性优先，不起真实 Hub、不连 3060、不碰生产 sync_hub.db / chroma_db）：
  - tmp_path 独立 sqlite 库（monkeypatch CONFIG.DB_PATH，db_facade 运行时读取，
    db.init_db() 建全 schema）
  - chromadb.PersistentClient 落在 tmp_path 独立目录（S-2 需要真实落盘文件
    做字节搜；每用例独立目录 + 独立集合名）
  - _MiniHub 装配真实 KnowledgeMixin / IngestMixin / DisclosureEngine 方法，
    _enqueue_write 同步直写 tmp 库（替代缓冲队列，测试同步可见）
"""
import asyncio
import os
import sqlite3
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chromadb  # noqa: E402

import db as db_mod  # noqa: E402
from db import get_embedding_provider  # noqa: E402
from models import CONFIG, KnowledgeEntry, SemanticSearchRequest  # noqa: E402
from disclosure import DisclosureEngine  # noqa: E402
from hub_mixins.knowledge import KnowledgeMixin, kb_chunk_id  # noqa: E402
from hub_mixins.ingest import IngestMixin  # noqa: E402

HASHER = get_embedding_provider("hasher", n_features=384)

# 唯一哨兵串：只应出现在 knowledge_base.content，绝不应出现在 chroma 目录任何文件
SENTINEL = "CD052哨兵串-明文绝不可入索引-7f3a9cE1"

# metadata 键集合新契约（CD-052 T1：删 content、加 piece_index）
KB_METADATA_KEYS = {"layer", "entry_id", "title", "source_type",
                    "importance", "chunk_hash", "piece_index"}

# 两段均 >150 字符（> MIN_TOKENS=50 的合并阈值，保证切出 ≥2 个 chunk）
_PARA1 = ("星枢知识层的正文最小披露要求检索索引里不存任何明文片段，命中后"
          "按 entry_id 与 piece_index 回查 SQLite 权威全文重切得到对应段落，"
          "切片口径必须与写入侧完全一致，复用 chunker.chunk_document 与"
          "kb_chunk_id，并遵守 KB_EMBED_MAX_CHUNKS 上限，禁止另造一套切片或"
          "id 规则，否则回查会对不上段落而 fail-closed 丢弃命中，导致检索"
          "结果静默变少且难以排查，必须以告警与对账双重手段兜底观测。")
_PARA2 = ("重建窗口期间检索一律 fail-closed，绝不回退到任何仍含明文的旧路径；"
          "重建走清库重建语义，先清空整集合再按 DB 现存条目重灌，孤儿 chunk"
          "天然不残留；删除条目时先删向量后删行，删除失败必须告警不许静默；"
          "对账要补反向清扫，chroma 有而 DB 无的 chunk 一律删除并计数，"
          "保证索引与主数据双向一致，不留任何漂移死角与历史包袱。")
CONTENT = f"{_PARA1}{SENTINEL}\n\n{_PARA2}"


def _policy_defaults():
    """与 hub_core._load_disclosure_policy 默认值对齐"""
    return {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
    }


class _MiniHub(KnowledgeMixin, IngestMixin):
    """装配真实 KnowledgeMixin/IngestMixin/DisclosureEngine 需要的最小 hub 面。"""

    def __init__(self, db_path, collection):
        self._db_path = str(db_path)
        self._chroma_collection = collection
        self._embedding_model = HASHER
        self.agents = {}
        self._disclosure_policy = _policy_defaults()
        self._wiki_sync_pending = False
        self.traces = []

    def _db(self):
        return sqlite3.connect(self._db_path)

    def _enqueue_write(self, kind, payload):
        """缓冲队列的同步等价物：upsert/delete 直写 tmp 库"""
        conn = sqlite3.connect(self._db_path)
        if kind == "upsert":
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
        elif kind == "delete":
            conn.execute("DELETE FROM knowledge_base WHERE entry_id = ?",
                         (payload["entry_id"],))
        conn.commit()
        conn.close()

    def _record_trace(self, action, agent_id, title, entry_id):
        self.traces.append((action, agent_id, title, entry_id))

    async def _ensure_embedding_model(self):
        return self._embedding_model

    async def _log_event(self, *args, **kwargs):
        pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立 sqlite 库 + 临时目录真 chroma（PersistentClient 真实落盘）"""
    db_path = str(tmp_path / "cd052.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    chroma_dir = tmp_path / "chroma"
    client = chromadb.PersistentClient(path=str(chroma_dir))
    collection = client.get_or_create_collection(
        f"cd052_{uuid.uuid4().hex[:12]}", metadata={"hnsw:space": "cosine"})
    hub = _MiniHub(db_path, collection)
    engine = DisclosureEngine(hub)
    return SimpleNamespace(hub=hub, engine=engine, coll=collection,
                           db_path=db_path, chroma_dir=str(chroma_dir))


def _entry(entry_id, content, title="测试知识", created_by="owner-a"):
    return KnowledgeEntry(entry_id=entry_id, title=title, content=content,
                          tags=[], links=[], category="process", importance=1.0,
                          created_by=created_by)


def _kb_metas(coll, entry_id):
    got = coll.get(where={"$and": [{"layer": "knowledge"}, {"entry_id": entry_id}]},
                   include=["metadatas"])
    return got["ids"], got["metadatas"]


def _expected_piece(entry_id, content, piece_index):
    """与写入侧同口径重切，取 piece_index 段正文"""
    from chunker import chunk_document
    max_chunks = getattr(CONFIG, "KB_EMBED_MAX_CHUNKS", 200)
    chunks = chunk_document(f"kb:{entry_id}", content, embed_fn=None)[:max_chunks]
    for ch in chunks:
        if ch["piece_index"] == piece_index:
            return ch["content"]
    return None


def _insert_doc_chunk(db_path, doc_id, piece_index, content, summary,
                      disclosure_level="summary", source_agent_id="uploader"):
    now = "2026-09-19T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO document_chunks
           (chunk_id, parent_doc_id, piece_index, content, summary,
            source_agent_id, trust_level, tainted_at, disclosure_level,
            sensitivity_score, chunk_hash, kind, pii_hits, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'trusted', '', ?, 0.0, ?, 'fact', '[]', ?, ?)""",
        (f"{doc_id}-c{piece_index}", doc_id, piece_index, content, summary,
         source_agent_id, disclosure_level, f"hash-{doc_id}-{piece_index}",
         now, now),
    )
    conn.commit()
    conn.close()


# ═══════════ S-1 metadata 无明文 + piece_index ═══════════

def test_s1_metadata_no_plaintext_has_piece_index(env):
    asyncio.run(env.hub.knowledge_upsert(_entry("kb-s1", CONTENT)))
    ids, metas = _kb_metas(env.coll, "kb-s1")
    assert ids, "知识条目未写入向量集合"
    for m in metas:
        assert "content" not in m, f"metadata 不得含正文键 content: {sorted(m.keys())}"
        assert "piece_index" in m, f"metadata 必须含 piece_index（回查定位段）: {sorted(m.keys())}"
        assert isinstance(m["piece_index"], int)
        assert set(m.keys()) == KB_METADATA_KEYS, \
            f"metadata 键集合漂移: {sorted(m.keys())}"


# ═══════════ S-2 静态字节搜零命中（核心证据） ═══════════

def test_s2_static_byte_search_zero_hit(env):
    asyncio.run(env.hub.knowledge_upsert(_entry("kb-s2", CONTENT)))
    ids, _ = _kb_metas(env.coll, "kb-s2")
    assert ids, "知识条目未写入向量集合"

    # 反向对照：哨兵串确实写进了 knowledge_base.content（证明不是没写进去）
    conn = sqlite3.connect(env.db_path)
    row = conn.execute(
        "SELECT content FROM knowledge_base WHERE entry_id = 'kb-s2'").fetchone()
    conn.close()
    assert row and SENTINEL in row[0], "反向对照失败：哨兵串未落入 knowledge_base.content"

    # 正向：遍历临时 chroma 目录下所有非 .bin 文件，逐字节搜哨兵串 → 必须 0 命中
    needle = SENTINEL.encode("utf-8")
    hits = []
    for root, _dirs, files in os.walk(env.chroma_dir):
        for f in files:
            if f.endswith(".bin"):
                continue  # .bin 是 HNSW 向量二进制（float32），无文本
            path = os.path.join(root, f)
            with open(path, "rb") as fh:
                if needle in fh.read():
                    hits.append(path)
    assert hits == [], f"静态攻击命中：哨兵串出现在 chroma 目录文件 {hits}"


# ═══════════ S-3 回查正文逐字相等 ═══════════

def test_s3_hit_content_exact_rechunk(env):
    asyncio.run(env.hub.knowledge_upsert(_entry("kb-s3", CONTENT)))
    ids, metas = _kb_metas(env.coll, "kb-s3")
    assert len(ids) >= 2, f"测试语料应切出 ≥2 chunk，实际 {len(ids)}"
    env.hub.agents["owner-a"] = {"role": "worker", "managed_agents": [],
                                 "disclosure_policy": {}, "department": ""}
    req = SemanticSearchRequest(query="披露", requester_agent_id="owner-a",
                                n_results=5)
    # 逐 chunk 验证：owner 自查（规则 1 FULL）→ 正文与重切段落逐字相等
    for mem_id, meta in zip(ids, metas):
        hit = asyncio.run(
            env.engine._knowledge_hit(mem_id, dict(meta), 0.0, req))
        assert hit is not None, f"owner 自查命中不得被丢弃: {mem_id}"
        assert hit["disclosure_level"] == "full", f"owner 自查应为 FULL: {hit}"
        expected = _expected_piece("kb-s3", CONTENT, meta["piece_index"])
        assert expected, f"重切无 piece_index={meta['piece_index']} 段"
        assert hit["content"] == expected, \
            f"回查正文与重切段落不一致: {hit['content']!r} != {expected!r}"


# ═══════════ S-4 doc: 分流（document_chunks + chunk 级密级） ═══════════

def test_s4_doc_prefix_reads_document_chunks(env):
    doc_content = "文档型段落正文哨兵-doc段-content-必须来自document_chunks表而非索引"
    doc_summary = "文档型段落摘要级内容"
    _insert_doc_chunk(env.db_path, "demo-doc", 0, doc_content, doc_summary,
                      disclosure_level="summary")
    env.hub.agents["boss"] = {"role": "orchestrator", "managed_agents": [],
                              "disclosure_policy": {}, "department": ""}
    req = SemanticSearchRequest(query="文档", requester_agent_id="boss",
                                n_results=5)
    meta = {"layer": "knowledge", "entry_id": "doc:demo-doc", "piece_index": 0}
    hit = asyncio.run(env.engine._knowledge_hit("kb:doc:demo-doc:0", meta, 0.0, req))
    assert hit is not None, "doc: 命中应回查 document_chunks 成功"
    # orchestrator 判定 FULL，但 chunk 存储级 summary → min 后 SUMMARY → 给摘要
    assert hit["disclosure_level"] == "summary", f"应按 chunk 存储级剥离: {hit}"
    assert hit["content"] == doc_summary, \
        f"摘要级应给 document_chunks.summary: {hit['content']!r}"
    assert doc_content not in hit["content"], "摘要级不得漏 chunk 全文"


def test_s4_doc_prefix_missing_chunk_fail_closed(env):
    """document_chunks 无对应行 → 命中被丢弃（fail-closed 返回 None）"""
    env.hub.agents["boss"] = {"role": "orchestrator", "managed_agents": [],
                              "disclosure_policy": {}, "department": ""}
    req = SemanticSearchRequest(query="文档", requester_agent_id="boss",
                                n_results=5)
    meta = {"layer": "knowledge", "entry_id": "doc:ghost-doc", "piece_index": 0}
    hit = asyncio.run(env.engine._knowledge_hit("kb:doc:ghost-doc:0", meta, 0.0, req))
    assert hit is None, "document_chunks 无行时必须 fail-closed 丢弃命中"


# ═══════════ S-5 重建窗口 fail-closed ═══════════

def test_s5_rebuild_window_fail_closed(env):
    asyncio.run(env.hub.knowledge_upsert(_entry("kb-s5", CONTENT)))
    env.hub.agents["owner-a"] = {"role": "worker", "managed_agents": [],
                                 "disclosure_policy": {}, "department": ""}
    req = SemanticSearchRequest(query="披露", requester_agent_id="owner-a",
                                n_results=5)
    env.hub._index_rebuilding = True
    try:
        res = asyncio.run(env.engine.semantic_search(req))
        assert res.get("degraded") is True, f"重建窗口必须降级: {res}"
        assert res.get("degraded_reason") == "index_rebuilding", \
            f"降级原因必须明确为 index_rebuilding: {res}"
        assert res["memories"] == [], f"重建窗口不得返回任何正文: {res}"
        assert SENTINEL not in str(res), "重建窗口响应不得含明文"
        # _chroma_search / _knowledge_hit 直接调用同样 fail-closed
        assert asyncio.run(env.engine._chroma_search(req, [0.1] * 384)) == []
        ids, metas = _kb_metas(env.coll, "kb-s5")
        assert asyncio.run(env.engine._knowledge_hit(
            ids[0], dict(metas[0]), 0.0, req)) is None
    finally:
        env.hub._index_rebuilding = False
    # 窗口结束后检索恢复（标记可复位）
    res2 = asyncio.run(env.engine.semantic_search(req))
    assert not res2.get("degraded"), f"窗口结束后不应再降级: {res2}"
    assert res2["memories"], "窗口结束后检索应恢复"


# ═══════════ S-6 孤儿哨兵（修正 1） ═══════════

def _inject_orphan(coll, entry_id="e-ghost"):
    coll.add(ids=[kb_chunk_id(entry_id, 0)],
             embeddings=[HASHER.encode("孤儿").tolist()],
             metadatas=[{"layer": "knowledge", "entry_id": entry_id,
                         "title": "幽灵", "source_type": "process",
                         "importance": 0.5, "chunk_hash": "ghost", "piece_index": 0}])


def test_s6a_rebuild_purges_orphan(env):
    """rebuild_embeddings 清库重建：DB 无对应行的 chunk 重建后不存在"""
    asyncio.run(env.hub.knowledge_upsert(_entry("e-live", CONTENT)))
    _inject_orphan(env.coll)
    ghost_id = kb_chunk_id("e-ghost", 0)
    assert ghost_id in env.coll.get(ids=[ghost_id])["ids"], "孤儿注入失败"
    result = asyncio.run(env.hub.rebuild_embeddings(requester="test"))
    assert result["status"] == "ok", f"重建失败: {result}"
    assert env.coll.get(ids=[ghost_id])["ids"] == [], \
        "清库重建后孤儿 chunk 必须不存在"
    live_ids, _ = _kb_metas(env.coll, "e-live")
    assert live_ids, "存活条目的 chunk 重建后必须存在"
    # 重建窗口标记必须复位（try/finally 语义）
    assert getattr(env.hub, "_index_rebuilding", False) is False


def test_s6b_reconcile_reverse_sweep(env):
    """reconcile 反向对账：chroma 有、DB 无 → 删孤儿且 orphan_removed >= 1"""
    asyncio.run(env.hub.knowledge_upsert(_entry("e-live", CONTENT)))
    _inject_orphan(env.coll)
    ghost_id = kb_chunk_id("e-ghost", 0)
    stats = asyncio.run(env.hub.reconcile_kb_vectors())
    assert stats["status"] == "ok", f"对账失败: {stats}"
    assert stats.get("orphan_removed", 0) >= 1, \
        f"反向对账应至少清掉 1 个孤儿: {stats}"
    assert env.coll.get(ids=[ghost_id])["ids"] == [], \
        "反向对账后孤儿 chunk 必须被删"
    live_ids, _ = _kb_metas(env.coll, "e-live")
    assert live_ids, "存活条目的 chunk 不得被误删"


# ═══════════ S-7 删除补索引（修正 2） ═══════════

def test_s7_delete_purges_vectors(env):
    asyncio.run(env.hub.knowledge_upsert(_entry("kb-s7", CONTENT)))
    ids, _ = _kb_metas(env.coll, "kb-s7")
    assert ids, "知识条目未写入向量集合"
    result = asyncio.run(env.hub.knowledge_delete("kb-s7"))
    assert result == {"status": "deleted"}, f"返回契约不得变: {result}"
    left, _ = _kb_metas(env.coll, "kb-s7")
    assert left == [], f"删除后索引里不得残留该 entry 的 chunk: {left}"
    conn = sqlite3.connect(env.db_path)
    row = conn.execute(
        "SELECT entry_id FROM knowledge_base WHERE entry_id = 'kb-s7'").fetchone()
    conn.close()
    assert row is None, "DB 行必须同步删除"


def test_s7_delete_vector_failure_logged_not_silent(env, caplog):
    """collection 抛错 → logger.error 被调用（不静默）且返回契约不变"""
    asyncio.run(env.hub.knowledge_upsert(_entry("kb-s7b", CONTENT)))

    class _BoomCollection:
        def delete(self, **kwargs):
            raise RuntimeError("injected chroma delete failure")

    env.hub._chroma_collection = _BoomCollection()
    import logging
    with caplog.at_level(logging.ERROR):
        result = asyncio.run(env.hub.knowledge_delete("kb-s7b"))
    assert result == {"status": "deleted"}, f"返回契约不得变: {result}"
    assert any("kb-s7b" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.ERROR), \
        "向量删除失败必须 logger.error（不许静默）"


def test_s7_delete_chroma_none_warns(env, caplog):
    """_chroma_collection is None → 跳过 + logger.warning；DB 行仍删"""
    asyncio.run(env.hub.knowledge_upsert(_entry("kb-s7c", CONTENT)))
    env.hub._chroma_collection = None
    import logging
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(env.hub.knowledge_delete("kb-s7c"))
    assert result == {"status": "deleted"}
    assert any("kb-s7c" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING), \
        "chroma 不可用时跳过删向量必须 logger.warning"
    conn = sqlite3.connect(env.db_path)
    row = conn.execute(
        "SELECT entry_id FROM knowledge_base WHERE entry_id = 'kb-s7c'").fetchone()
    conn.close()
    assert row is None, "chroma 不可用时 DB 行仍须删除"
