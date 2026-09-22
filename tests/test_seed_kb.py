# -*- coding: utf-8 -*-
"""K-1/T3+T5(8): scripts/seed_kb.py 核心函数测试 — 解析 / 派生 id / 幂等

不起真实端口、不打 HTTP：直接测 build_entries/parse_front_matter/entry_id_from_filename，
幂等用 FakeHub（复用 test_kb_unified_retrieval 的配方）跑两遍 knowledge_upsert，
断言 knowledge_base 行数与 chroma 向量数不翻倍。
"""
import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.seed_kb import (build_entries, entry_id_from_filename,
                             parse_front_matter)
from tests.test_kb_unified_retrieval import FakeHub, _insert_memory  # noqa: F401

import chromadb
import db as db_mod
from models import CONFIG, KnowledgeEntry

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS_DIR = os.path.join(REPO_ROOT, "corpus")


def _run(coro):
    return asyncio.run(coro)


# ═══════════ front-matter 解析 ═══════════

def test_parse_front_matter_basic():
    text = "---\ntitle: 标题\ncategory: faq\ntags: [a, b, c]\nsource: X.md#L1-L2\n---\n\n正文内容\n"
    meta, body = parse_front_matter(text)
    assert meta["title"] == "标题"
    assert meta["category"] == "faq"
    assert meta["tags"] == ["a", "b", "c"]
    assert meta["source"] == "X.md#L1-L2"
    assert body.strip() == "正文内容"


def test_parse_front_matter_rejects_missing():
    with pytest.raises(ValueError):
        parse_front_matter("# 没有 front-matter\n正文")
    with pytest.raises(ValueError):
        parse_front_matter("---\n坏行没有冒号\n---\n正文")


def test_entry_id_derivation_stable_and_ascii():
    assert entry_id_from_filename("faq-01-why-not-wechat.md") == "kb-faq-01-why-not-wechat"
    # 稳定：同文件名多次派生一致（不用随机/时间戳）
    assert entry_id_from_filename("a-b.md") == entry_id_from_filename("a-b.md")
    with pytest.raises(ValueError):
        entry_id_from_filename("中文文件名.md")


# ═══════════ corpus 全量构建 ═══════════

def test_build_entries_from_real_corpus():
    entries = build_entries(CORPUS_DIR)
    assert 20 <= len(entries) <= 40, f"样本语料目标 20-40 篇，实测 {len(entries)}"
    ids = [e["entry_id"] for e in entries]
    assert len(ids) == len(set(ids)), "entry_id 不得重复"
    for e in entries:
        assert e["title"] and e["category"] and e["tags"] and e["source"]
        assert e["content"], f"{e['entry_id']} 正文为空"
    categories = {e["category"] for e in entries}
    # 任务书要求至少覆盖 4 类：制度/流程、产品与参数、售后政策、客户常见问答
    assert {"process", "product", "policy", "faq"} <= categories
    # 连跑两次构建结果完全一致（确定性）
    assert build_entries(CORPUS_DIR) == entries


# ═══════════ T5-8: seed 幂等（核心函数跑两遍，行数/向量数不变） ═══════════

@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "seed_test.db"
    monkeypatch.setattr(CONFIG, "DB_PATH", str(db_path))
    db_mod.init_db()
    client = chromadb.EphemeralClient()
    import uuid
    collection = client.get_or_create_collection(
        f"test_seed_{uuid.uuid4().hex[:12]}", metadata={"hnsw:space": "cosine"})
    return FakeHub(db_path, collection, client), collection


def _counts(hub, collection):
    conn = sqlite3.connect(str(hub._db_path))
    kb_rows = conn.execute("SELECT COUNT(*) FROM knowledge_base").fetchone()[0]
    dc_rows = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
    conn.close()
    return kb_rows, dc_rows, collection.count()


def _apply_entries(hub, entries):
    for e in entries:
        entry = KnowledgeEntry(entry_id=e["entry_id"], title=e["title"],
                               content=e["content"], tags=e["tags"], links=[],
                               category=e["category"], importance=1.0,
                               created_by="seed_kb")
        r = _run(hub.knowledge_upsert(entry))
        assert r["status"] == "ok"


def test_seed_idempotent_two_runs(env):
    hub, collection = env
    entries = build_entries(CORPUS_DIR)

    _apply_entries(hub, entries)
    kb1, dc1, v1 = _counts(hub, collection)
    assert kb1 == len(entries)
    assert v1 > 0, "首跑后应有知识向量"

    _apply_entries(hub, entries)  # 第二遍：全量重复
    kb2, dc2, v2 = _counts(hub, collection)

    assert kb2 == kb1, "knowledge_base 行数不得翻倍"
    assert dc2 == dc1 == 0, "document_chunks 不经过本管道，必须始终为 0"
    assert v2 == v1, "chroma 向量数不得翻倍（chunk id 确定性 + 写前先删旧 chunk）"
