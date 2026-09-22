# -*- coding: utf-8 -*-
"""T10 · CD-054（memory 组）：memory 读端点补读审计 验收测试（2026-09-19）

memory 组 5 个读端点（routes_memory.py）补 _log_read（复用 routes_gateway 既有
helper，落 gateway_read_log）的验收：

M-1 GET /api/v1/memory 成功 → gateway_read_log 恰好多 1 行
    （requester / kind='memory' / target=agent_id / item_count == 返回条数）
M-2 POST /api/v1/memory/search → 多 1 行（query / target / item_count）
M-3 GET /api/v1/memory/{key}/versions → 多 1 行（item_count == len(versions)）
M-4 POST /api/v1/memory/semantic_search → 多 1 行
    （monkeypatch hub.semantic_search 返回固定结果，不真连 chroma）
M-5 403 路径落 denied 行（语义随 CD-059 变更——旧断言「403 不落行」已废，非回归）：
current_agent != agent_id → 403，且 gateway_read_log 恰好多 1 行（granted_level='denied'）
M-6 审计失败不阻塞：monkeypatch 让 _log_read 抛错 → 端点仍正常返回

脚手架（防假绿）：临时库走 db.init_db() 建完整 schema（先 monkeypatch
CONFIG.DB_PATH 再 init_db），gateway_read_log 真实存在；直调 handler 协程
（Depends 直传参），不起 TestClient；memory 数据直接 INSERT 临时库。
hub 是模块级单例，但 hub._db()/db_facade 运行时读 CONFIG.DB_PATH，
monkeypatch 后即指向临时库。
"""
import asyncio
import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit.memory_audit as memory_audit  # noqa: E402
import models  # noqa: E402
import routes_memory  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表）+ 临时审计目录（防 memory_audit 污染仓库 audit/）。"""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()
    monkeypatch.setattr(memory_audit, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(memory_audit, "AUDIT_FILE",
                        str(tmp_path / "audit" / "memory_pool.jsonl"))
    monkeypatch.setattr(memory_audit, "_rolling_chain", None)
    return db_path


def _insert_memory(db_path, memory_id, owner, key, content, with_embedding=False):
    embedding = None
    if with_embedding:
        # embedding 检索路径只认 embedding IS NOT NULL 的行（384 维 float32，对齐 hasher）
        import numpy as np
        embedding = np.array([0.1] * 384, dtype=np.float32).tobytes()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content,"
        " summary, embedding, importance, tags, kind, confidence, source_type,"
        " disclosure_level, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, '', ?, 1.0, '[]', 'fact', 1.0, 'user', 'summary',"
        " datetime('now'), datetime('now'))",
        (memory_id, owner, key, content, embedding),
    )
    conn.commit()
    conn.close()


def _log_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT requester, kind, query, target, granted_level, item_count,"
        " stripped_chunks FROM gateway_read_log ORDER BY log_id")]
    conn.close()
    return rows


# ═══════════ M-1 GET /memory ═══════════

def test_m1_get_memories_logs_read(env):
    _insert_memory(env, "m1", "agent-a", "k1", "星枢测试内容一")
    _insert_memory(env, "m2", "agent-a", "k2", "星枢测试内容二")
    before = len(_log_rows(env))
    result = asyncio.run(routes_memory.api_get_memories(
        agent_id="agent-a", kind="", current_agent="agent-a", principal=None))
    rows = _log_rows(env)
    assert len(rows) == before + 1, f"应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-a"
    assert row["kind"] == "memory"
    assert row["query"] == ""
    assert row["target"] == "agent-a"
    assert row["granted_level"] == ""
    assert row["stripped_chunks"] == 0
    assert row["item_count"] == len(result["memories"]) == 2


# ═══════════ M-2 POST /memory/search ═══════════

def test_m2_memory_search_logs_read(env):
    _insert_memory(env, "m1", "agent-a", "k1", "星枢读审计关键词", with_embedding=True)
    before = len(_log_rows(env))
    req = routes_memory.MemorySearchRequest(query="星枢读审计", agent_id="agent-a")
    result = asyncio.run(routes_memory.api_memory_search(
        req=req, current_agent="agent-a", principal=None))
    rows = _log_rows(env)
    assert len(rows) == before + 1, f"应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-a"
    assert row["kind"] == "memory"
    assert row["query"] == "星枢读审计"
    assert row["target"] == "agent-a"
    assert row["granted_level"] == ""
    assert row["stripped_chunks"] == 0
    assert row["item_count"] == len(result["results"]) == 1


# ═══════════ M-3 GET /memory/{key}/versions ═══════════

def test_m3_memory_versions_logs_read(env):
    # CD-056（Hermes 验收裁决时同步的既有夹具，2026-09-19）：本用例的意图是「成功路径落读审计」，
    # 但旧夹具只插 memory_versions、不插 memory_pool 行 —— 那恰好就是 CD-056 定义的「孤儿版本」场景，
    # 修复后按 fail-closed 必然 403（见 tests/test_memory_versions_owner_scope.py::V-4）。
    # 故补一行归属 memory_pool 行，断言语义一字未改；孤儿版本不可达属已登记残余，不是回归。
    _insert_memory(env, "m1", "agent-a", "k1", "第2版内容")
    conn = sqlite3.connect(env)
    for v in (1, 2):
        conn.execute(
            "INSERT INTO memory_versions (memory_id, memory_key, version, content)"
            " VALUES ('m1', 'k1', ?, ?)", (v, f"第{v}版内容"))
    conn.commit()
    conn.close()
    before = len(_log_rows(env))
    result = asyncio.run(routes_memory.api_memory_versions(
        memory_key="k1", agent_id="agent-a", current_agent="agent-a", principal=None))
    rows = _log_rows(env)
    assert len(rows) == before + 1, f"应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-a"
    assert row["kind"] == "memory"
    assert row["query"] == ""
    assert row["target"] == "agent-a"
    assert row["item_count"] == len(result["versions"]) == 2


# ═══════════ M-4 POST /memory/semantic_search（假语义检索，不连 chroma） ═══════════

def test_m4_semantic_search_logs_read(env, monkeypatch):
    async def _fake_semantic(req, scope=None):
        return {"query": req.query, "total": 2,
                "memories": [{"memory_id": "m1"}, {"memory_id": "m2"}]}

    monkeypatch.setattr(routes_memory.hub, "semantic_search", _fake_semantic)
    before = len(_log_rows(env))
    req = models.SemanticSearchRequest(
        query="语义查询", requester_agent_id="agent-a", filter_owner="agent-a")
    result = asyncio.run(routes_memory.api_semantic_search(
        req=req, current_agent="agent-a", principal=None))
    assert len(result["memories"]) == 2
    rows = _log_rows(env)
    assert len(rows) == before + 1, f"应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-a"
    assert row["kind"] == "memory"
    assert row["query"] == "语义查询"
    assert row["target"] == "agent-a"
    assert row["item_count"] == 2


# ═══════════ M-5 403 路径落 denied 行（语义随 CD-059 变更：403 拒绝必须落 denied 行；2026-09-20 用户追认，不回滚） ═══════════

def test_m5_403_logs_denied(env, monkeypatch):
    monkeypatch.setattr(routes_memory, "NO_AUTH", False)
    before = len(_log_rows(env))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_memory.api_get_memories(
            agent_id="agent-a", kind="", current_agent="agent-b", principal=None))
    assert exc_info.value.status_code == 403
    rows = _log_rows(env)
    assert len(rows) == before + 1, \
        f"403 拒绝必须恰好多 1 行 denied，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-b"
    assert row["kind"] == "memory"
    assert row["target"] == "agent-a"
    assert row["granted_level"] == "denied"
    assert row["item_count"] == 0


# ═══════════ M-6 审计失败不阻塞读取 ═══════════

def test_m6_audit_failure_not_blocking(env, monkeypatch):
    _insert_memory(env, "m1", "agent-a", "k1", "星枢测试内容")

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟审计后端故障")

    monkeypatch.setattr(routes_memory, "_log_read", _boom)
    result = asyncio.run(routes_memory.api_get_memories(
        agent_id="agent-a", kind="", current_agent="agent-a", principal=None))
    assert result["total"] == 1, "审计抛错时端点仍须正常返回"
    assert len(_log_rows(env)) == 0
