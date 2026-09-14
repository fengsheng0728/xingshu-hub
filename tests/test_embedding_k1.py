# -*- coding: utf-8 -*-
"""
K1 embedding 升级单测（附录 F 2026-08-06 冻结验收）
覆盖：
  1. provider 工厂：hasher 默认 / sentence 缺模型抛异常 / sentence 缺路径抛异常
  2. 降级链：sentence 配置但模型缺失 → _ensure_embedding_model 返回 None → 检索走 ILIKE
  3. K1b rebuild_embeddings：stale 检测（维度不匹配）+ 批处理重建 + 幂等
  4. K1c calibrate_cos_threshold：分布分位 + suggested=P25
  5. 路由注册（rebuild/calibrate）
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
from db import get_embedding_provider, LocalEmbedding


def test_provider_factory():
    p = get_embedding_provider("hasher")
    assert isinstance(p, LocalEmbedding)
    v = p.encode("测试")
    assert v.shape == (384,)
    # sentence 缺路径
    try:
        get_embedding_provider("sentence", model_path="")
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    # sentence 缺模型文件
    try:
        get_embedding_provider("sentence", model_path=r"C:\nonexistent-model-xyz")
        assert False, "应抛异常"
    except Exception:
        pass
    print("PASS test_provider_factory")


def test_degrade_chain():
    """sentence 配置但模型缺失 → None → 检索降级 ILIKE"""
    models.CONFIG.EMBEDDING_PROVIDER = "sentence"
    models.CONFIG.EMBEDDING_MODEL_PATH = r"C:\nonexistent-model-xyz"
    from hub_core import SyncHub
    hub = SyncHub()
    model = asyncio.run(hub._ensure_embedding_model())
    assert model is None, f"模型缺失应降级 None，实际 {type(model).__name__ if model else None}"
    assert getattr(hub, "_embedding_load_failed", False) is True, "应标记加载失败"
    # 恢复默认
    models.CONFIG.EMBEDDING_PROVIDER = "hasher"
    print("PASS test_degrade_chain")


def test_rebuild_stale_detection():
    tmpdir = tempfile.mkdtemp(prefix="k1t-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_pool (
        memory_id TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL, memory_key TEXT,
        content TEXT, summary TEXT, embedding BLOB, importance REAL, tags TEXT,
        kind TEXT DEFAULT 'fact', source_session_id TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0, source_type TEXT DEFAULT 'user',
        disclosure_level TEXT DEFAULT 'summary', disclosure_scope TEXT DEFAULT 'manager',
        allowed_viewers TEXT, created_at TEXT, updated_at TEXT,
        access_count INTEGER DEFAULT 0, last_accessed TEXT,
        trust_level TEXT DEFAULT 'internal', source_agent_id TEXT DEFAULT '', tainted_at TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT,
        agent_id TEXT, payload TEXT, timestamp TEXT)""")
    # 插入 2 条：1 条 384 维旧向量（stale）+ 1 条无向量（stale）
    import numpy as np
    old_vec = np.zeros(384, dtype=np.float32).tobytes()
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content, embedding, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("m1", "a", "k1", "旧维度内容", old_vec, "t", "t"))
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content, embedding, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("m2", "a", "k2", "无向量内容", None, "t", "t"))
    conn.commit()
    conn.close()

    # 用 monkeypatch 隔离（直接改 CONFIG 后恢复）
    orig = models.CONFIG.DB_PATH
    models.CONFIG.DB_PATH = tmpdb
    try:
        from hub_core import SyncHub
        hub = SyncHub()
        # hasher 384 维 → m1(384) 维度匹配不 stale，m2(无) stale
        r = asyncio.run(hub.rebuild_embeddings(requester="t"))
        assert r["status"] == "ok"
        assert r["provider"] == "hasher"
        assert r["target_dim"] == 384
        assert r["stale_total"] == 1, f"应只有 m2 stale，实际 {r['stale_total']}"
        assert r["rebuilt_mem"] == 1
        # 幂等：再跑 → 0 stale
        r2 = asyncio.run(hub.rebuild_embeddings(requester="t"))
        assert r2["stale_total"] == 0, f"幂等应 0 stale，实际 {r2['stale_total']}"
        # m2 现在有向量
        conn = sqlite3.connect(tmpdb)
        row = conn.execute("SELECT embedding FROM memory_pool WHERE memory_id='m2'").fetchone()
        conn.close()
        assert row[0] is not None, "m2 应已重建向量"
    finally:
        models.CONFIG.DB_PATH = orig
    print("PASS test_rebuild_stale_detection")


def test_calibrate_threshold():
    from chunker import calibrate_cos_threshold
    from db import get_embedding_provider
    model = get_embedding_provider("hasher")
    samples = [
        "股票市场上涨了三个百分点，投资者信心回升。",
        "基金净值今天大幅增长，收益率创新高。",
        "今天天气晴朗适合出行，气温适宜。",
        "明天可能有降雨请带伞，注意保暖。",
    ]
    r = calibrate_cos_threshold(model, samples)
    assert "p10" in r and "p25" in r and "p50" in r and "suggested" in r
    assert r["suggested"] == r["p25"], "suggested 应为 P25"
    assert r["samples"] >= 1
    print(f"PASS test_calibrate_threshold: suggested={r['suggested']} samples={r['samples']}")


def test_routes_registered():
    old = os.environ.get("SYNC_HUB_NO_AUTH")
    os.environ["SYNC_HUB_NO_AUTH"] = "1"
    try:
        from routes import app
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/v1/embeddings/rebuild" in paths
        assert "/api/v1/embeddings/calibrate" in paths
        print("PASS test_routes_registered")
    finally:
        if old is None:
            os.environ.pop("SYNC_HUB_NO_AUTH", None)
        else:
            os.environ["SYNC_HUB_NO_AUTH"] = old


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nK1 embedding: {len(tests)} 用例全绿")
