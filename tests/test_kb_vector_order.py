# -*- coding: utf-8 -*-
"""
CD-049: 知识 chunk 向量「先 upsert 后删差集」改序 + 失败显式化

用假 collection（记录调用顺序与内部状态，确定性优先，不依赖真 chromadb）：
  T5-1 顺序断言：upsert/add 出现在删除之前；删除 id 集合 == 旧 ids − 新 ids
  T5-2 无零 chunk 中间态：每个操作后回调检查该 entry chunk 数始终 ≥ 1
  T5-3 upsert 失败保留旧 chunk：不抛、降级返回 0、error 级日志
  T5-4 幂等：同一 entry 连续两次 → 无重复 id、chunk 数稳定、无残留
  T5-5 删旧不误删新：旧 3 chunk / 新 2 chunk → 最终只剩新 2 个
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub_mixins.knowledge import KnowledgeMixin


class FakeCollection:
    """记录 ops 调用序列 + 内存态 id→metadata；支持 get/upsert/add/delete。"""

    def __init__(self, initial_ids=(), entry_id="e1", on_op=None):
        self.store = {i: {"entry_id": entry_id, "layer": "knowledge"}
                      for i in initial_ids}
        self.ops = []
        self.deleted_ids = []
        self.on_op = on_op
        self.fail_write = False

    def _after(self, name):
        self.ops.append(name)
        if self.on_op:
            self.on_op(self)

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
        self._after("get")
        return {"ids": ids}

    def upsert(self, ids, embeddings, metadatas):
        if self.fail_write:
            raise RuntimeError("injected upsert failure")
        for i, m in zip(ids, metadatas):
            self.store[i] = dict(m)
        self._after("upsert")

    def add(self, ids, embeddings, metadatas):
        if self.fail_write:
            raise RuntimeError("injected add failure")
        for i, m in zip(ids, metadatas):
            self.store[i] = dict(m)
        self._after("add")

    def delete(self, where=None, ids=None):
        if ids is not None:
            self.deleted_ids.extend(ids)
            for i in ids:
                self.store.pop(i, None)
        else:
            eid = self._entry_id_of(where)
            doomed = [i for i, m in self.store.items()
                      if eid is None or m.get("entry_id") == eid]
            self.deleted_ids.extend(doomed)
            for i in doomed:
                self.store.pop(i, None)
        self._after("delete")


def _make_hub(collection):
    hub = KnowledgeMixin.__new__(KnowledgeMixin)
    hub._chroma_collection = collection

    def _model(texts):
        return [[0.0] * 8 for _ in texts]

    async def _fake_ensure():
        return _model

    hub._ensure_embedding_model = _fake_ensure
    return hub


def _entry(entry_id="e1"):
    return SimpleNamespace(entry_id=entry_id, title="t", category="c",
                           importance=0.5, content="正文内容")


def _patch_chunks(monkeypatch, n):
    """确定性替换 chunker：固定产出 n 个 chunk（piece_index 0..n-1）。"""
    def _fake_chunk_document(doc_id, content, embed_fn=None):
        return [{"piece_index": i, "content": f"seg{i}",
                 "chunk_hash": f"h{i}", "tokens": 10} for i in range(n)]
    monkeypatch.setattr("chunker.chunk_document", _fake_chunk_document)


def test_order_upsert_before_delete_and_diff(monkeypatch):
    """T5-1: 写操作在删除之前；删除集合 == 旧 ids − 新 ids。"""
    coll = FakeCollection(["kb:e1:0", "kb:e1:1", "kb:e1:2"])
    hub = _make_hub(coll)
    _patch_chunks(monkeypatch, 2)
    n = asyncio.run(hub._embed_knowledge_chunks("e1", _entry()))
    assert n == 2
    write_idx = min(i for i, op in enumerate(coll.ops)
                    if op in ("upsert", "add"))
    del_idx = [i for i, op in enumerate(coll.ops) if op == "delete"]
    assert del_idx, f"未发生差集删除: {coll.ops}"
    assert write_idx < del_idx[0], f"删除早于写入: {coll.ops}"
    assert set(coll.deleted_ids) == {"kb:e1:2"}, \
        f"删除集合应为旧−新差集，实际 {coll.deleted_ids}"


def test_no_zero_chunk_intermediate(monkeypatch):
    """T5-2: 任意操作后该 entry 的 chunk 数始终 ≥ 1（无零 chunk 中间态）。"""
    violations = []

    def on_op(coll):
        cnt = sum(1 for m in coll.store.values()
                  if m.get("entry_id") == "e1")
        if cnt < 1:
            violations.append(list(coll.ops))

    coll = FakeCollection(["kb:e1:0"], on_op=on_op)
    hub = _make_hub(coll)
    _patch_chunks(monkeypatch, 2)
    asyncio.run(hub._embed_knowledge_chunks("e1", _entry()))
    assert not violations, f"出现零 chunk 中间态: {violations}"


def test_upsert_failure_keeps_old_chunks(monkeypatch, caplog):
    """T5-3: upsert 抛错 → 旧 chunk 仍在、降级返回 0（不抛）、error 级日志。"""
    coll = FakeCollection(["kb:e1:0"])
    coll.fail_write = True
    hub = _make_hub(coll)
    _patch_chunks(monkeypatch, 2)
    with caplog.at_level(logging.ERROR):
        n = asyncio.run(hub._embed_knowledge_chunks("e1", _entry()))
    assert n == 0, f"写失败应降级返回 0，实际 {n}"
    assert "kb:e1:0" in coll.store, "写失败不应丢旧 chunk"
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs, "写失败应记 error 级日志"
    assert any("e1" in r.getMessage() for r in errs), \
        f"error 日志应含 entry_id: {[r.getMessage() for r in errs]}"


def test_idempotent_double_upsert(monkeypatch):
    """T5-4: 连续两次 → 无重复 id、chunk 数稳定、无残留。"""
    coll = FakeCollection()
    hub = _make_hub(coll)
    _patch_chunks(monkeypatch, 2)
    n1 = asyncio.run(hub._embed_knowledge_chunks("e1", _entry()))
    n2 = asyncio.run(hub._embed_knowledge_chunks("e1", _entry()))
    assert n1 == n2 == 2
    assert sorted(coll.store) == ["kb:e1:0", "kb:e1:1"], \
        f"幂等重入后应恰好 2 个 chunk，实际 {sorted(coll.store)}"


def test_delete_stale_not_new(monkeypatch):
    """T5-5: 旧 3 chunk / 新 2 chunk → 最终只剩新 2 个（不误删新 chunk）。"""
    coll = FakeCollection(["kb:e1:0", "kb:e1:1", "kb:e1:2"])
    hub = _make_hub(coll)
    _patch_chunks(monkeypatch, 2)
    asyncio.run(hub._embed_knowledge_chunks("e1", _entry()))
    assert sorted(coll.store) == ["kb:e1:0", "kb:e1:1"], \
        f"应只剩新 2 个 chunk，实际 {sorted(coll.store)}"
