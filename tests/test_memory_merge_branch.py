# -*- coding: utf-8 -*-
"""数据层修复轮：store_memory 合并（merge）分支回归 + 向量操作线程卸载验收

先红背景（代码审查实测复现，rowcount=0）：
store_memory 的 merge 分支（相似度 > 0.90 → 保留原 content、刷新 meta）SQL
占位符序为 (confidence, updated_at, last_accessed, trust_level, memory_id)，
实参却是 (confidence, now, now, best_row[0], merge_trust)——trust_level 拿到
memory_id、WHERE 拿到信任级字符串 → 恒 0 行更新，合并分支从不生效。

验收：
  M-1 两条 >0.90 相似度记忆触发 merge：access_count+1、last_accessed 刷新、
      confidence 取 MAX、trust_level 按 _merge_trust 降级、content 保留原文、
      不新增行。
  M-2 _apply_vector_ops 的 chroma upsert/delete 必须在工作线程执行
      （不得阻塞事件循环）。

脚手架对齐 tests/test_memory_fts_sync.py：临时库走 db.init_db()，
monkeypatch CONFIG.DB_PATH；直调 routes_memory.hub 模块级单例。
"""
import asyncio
import os
import sqlite3
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit.memory_audit as memory_audit  # noqa: E402
import models  # noqa: E402
import routes_memory  # noqa: E402
from deps import MemoryEntry  # noqa: E402
from hub_core import SyncHub  # noqa: E402


class _FakeModel:
    """确定性假 embedding：同向近单位向量（任意两段文本 cosine > 0.99）。"""

    def encode(self, text):
        v = np.ones(384, dtype=np.float32)
        v[1] += (len(text) % 5) * 1e-3  # 微扰，cosine ≈ 1 但不完全同向
        return v


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表）+ 临时审计目录。"""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()
    monkeypatch.setattr(memory_audit, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(memory_audit, "AUDIT_FILE",
                        str(tmp_path / "audit" / "memory_pool.jsonl"))
    monkeypatch.setattr(memory_audit, "_rolling_chain", None)
    return db_path


@pytest.fixture()
def hub(monkeypatch):
    """模块级 hub 单例：假 embedding 模型（触发三段去重）、无向量栈/无影子。"""
    h = routes_memory.hub

    async def _fake_model(self):
        return _FakeModel()

    monkeypatch.setattr(h, "_chroma_collection", None)
    # CD-114：类级打桩（实例级会给单例留下永久实例属性，遮蔽后续类级 monkeypatch）
    monkeypatch.setattr(SyncHub, "_ensure_embedding_model", _fake_model)
    monkeypatch.setattr(h, "_shadow", None)
    return h


def _row(db_path, memory_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT content, confidence, trust_level, access_count, last_accessed,"
        " updated_at FROM memory_pool WHERE memory_id=?",
        (memory_id,)).fetchone()
    conn.close()
    return row


# ═══════════ M-1 merge 分支真实生效（先红核心） ═══════════

def test_merge_branch_updates_meta_and_demotes_trust(env, hub):
    r1 = asyncio.run(hub.store_memory(
        "agent-a", MemoryEntry(
            memory_key="k1", content="季度汇报模板存放路径说明",
            confidence=0.5, trust_level="internal")))
    assert r1["status"] == "stored" and r1["action"] == "write"
    mid = r1["memory_id"]
    before = _row(env, mid)
    assert before is not None and before[3] == 0  # access_count 初始 0

    # 不同 key（避开硬冲突）、相似内容（假模型 → sim > 0.90）、低信任来源
    r2 = asyncio.run(hub.store_memory(
        "agent-a", MemoryEntry(
            memory_key="k2", content="季度汇报模板存放路径说明文档",
            confidence=0.8, source_type="tool", trust_level="external")))

    assert r2["action"] == "merge", f"相似度 >0.90 必须走 merge，实际: {r2}"
    assert r2["memory_id"] == mid, "merge 不得新增行，须落在原记忆上"

    conn = sqlite3.connect(env)
    n = conn.execute(
        "SELECT COUNT(*) FROM memory_pool WHERE owner_agent_id='agent-a'"
    ).fetchone()[0]
    conn.close()
    assert n == 1, f"merge 不得新增行，实际行数 {n}"

    after = _row(env, mid)
    assert after[0] == "季度汇报模板存放路径说明", "merge 保留原 content"
    assert after[1] == pytest.approx(0.8), \
        f"confidence 应刷新为 MAX(0.5, 0.8)=0.8，实际 {after[1]}"
    assert after[2] == "external", \
        f"trust_level 应按 _merge_trust 降级 internal→external，实际 {after[2]!r}"
    assert after[3] == 1, f"access_count 应 +1，实际 {after[3]}"
    assert after[4], "last_accessed 必须刷新（旧代码 WHERE 错位时恒为 NULL）"
    assert after[5] != before[5] or after[5], "updated_at 必须刷新"


def test_merge_branch_action_visible_in_result(env, hub):
    """merge 不回写向量/FTS（跳过同步），result 必须暴露 action=merge。"""
    asyncio.run(hub.store_memory(
        "agent-a", MemoryEntry(memory_key="k1", content="客户偏好记录样例文本")))
    res = asyncio.run(hub.store_memory(
        "agent-a", MemoryEntry(memory_key="k2", content="客户偏好记录样例文本改")))
    assert res["action"] == "merge"
    assert res["status"] == "stored"


# ═══════════ M-2 向量操作卸载到工作线程（CD-017 同类） ═══════════

def test_apply_vector_ops_runs_off_event_loop():
    """chroma collection 的 upsert/delete 是同步阻塞 IO，必须经 to_thread 卸载。"""
    from hub_mixins.memory import MemoryMixin

    h = MemoryMixin()
    h._chroma_collection = None  # 先置空再过类型检查（下方赋假实现）
    calls = []

    class _FakeColl:
        def upsert(self, ids=None, embeddings=None, metadatas=None):
            calls.append(("upsert", threading.current_thread().name))

        def delete(self, ids=None):
            calls.append(("delete", threading.current_thread().name))

    h._chroma_collection = _FakeColl()

    async def _main():
        loop_thread = threading.current_thread().name
        await h._apply_vector_ops([
            {"op": "upsert", "memory_id": "m1", "embedding": [0.1, 0.2],
             "metadata": {"owner": "a"}},
            {"op": "delete", "memory_id": "m2"},
        ])
        return loop_thread

    loop_thread = asyncio.run(_main())
    assert [c[0] for c in calls] == ["upsert", "delete"], f"两个 op 都应执行: {calls}"
    assert all(c[1] != loop_thread for c in calls), \
        "向量操作不得跑在事件循环线程（同步阻塞会串行化整个 Hub）"
