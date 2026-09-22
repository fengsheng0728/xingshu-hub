# -*- coding: utf-8 -*-
"""T16 · CD-057：memory 关键词检索降级链断裂修复 验收测试（2026-09-19）

背景：hub_mixins/memory.py 的 memory_search 关键词降级链实际断着——
FTS5 分支在 LEFT JOIN 语境对非主表用 MATCH 恒抛 OperationalError 被静默吞；
模型可用但行无 embedding 时不走 LIKE 兜底 → 无向量记忆关键词检索恒空。

K-1 强制 chroma/模型故障 → 关键词路径真实返回行（核心，修复前必红）
K-2 模型可用但行无 embedding → 同样命中（回归锚点：旧行为恒空）
K-3 FTS5 抛 OperationalError → 落 LIKE 兜底仍返回行 + logger.error 被调用
K-4 FTS 索引恒空（诊断实测）→ 落 LIKE 且返回行；FTS 主语形态在索引有行时真命中
K-5 不静默：memory_search 内无 except-pass 形态；降级路径有日志 + 返回体降级标记

脚手架（对齐 tests/test_memory_read_audit.py）：临时库走 db.init_db() 建全表
（含 memory_pool_fts），monkeypatch CONFIG.DB_PATH；直调 memory_search 协程；
hub 为模块级单例，db_facade 运行时读 CONFIG.DB_PATH。
"""
import ast
import asyncio
import logging
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit.memory_audit as memory_audit  # noqa: E402
import db_facade  # noqa: E402
import models  # noqa: E402
import routes_memory  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONTENT = "浴室柜颜色差异处理流程：先拍照留证再报主管"


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


@pytest.fixture()
def no_model(monkeypatch):
    """强制 chroma/模型故障：collection 置 None + _ensure_embedding_model 返回 None。"""
    hub = routes_memory.hub

    async def _no_model():
        return None

    monkeypatch.setattr(hub, "_chroma_collection", None)
    monkeypatch.setattr(hub, "_ensure_embedding_model", _no_model)
    return hub


class _FakeModel:
    """假 embedding 模型（384 维，对齐 hasher）——模型可用但库里行无 embedding 用。"""

    def encode(self, text):
        import numpy as np
        return np.full(384, 0.1, dtype=np.float32)


def _insert_memory(db_path, memory_id, owner, key, content, with_embedding=False):
    embedding = None
    if with_embedding:
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


def _search(hub, query="浴室柜", agent_id="agent-a"):
    req = routes_memory.MemorySearchRequest(query=query, agent_id=agent_id)
    return asyncio.run(hub.memory_search(req))


def _fts_docsize(db_path):
    """memory_pool_fts 已索引文档数（docsize 影子表；COUNT(*) 主表对外部内容表会
    读内容表而非索引，不能用来度量索引行数）。"""
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM memory_pool_fts_docsize").fetchone()[0]
    conn.close()
    return n


# ═══════════ K-1 强制 chroma/模型故障 → 关键词路径真实返回行（核心） ═══════════

def test_k1_forced_chroma_failure_keyword_path_returns_rows(env, no_model):
    _insert_memory(env, "m1", "agent-a", "bath-cabinet", CONTENT)  # 无 embedding
    result = _search(no_model, query="浴室柜")
    ids = [r["memory_id"] for r in result["results"]]
    assert result["total"] >= 1, f"关键词降级链必须真实返回行，实际 results={result['results']}"
    assert "m1" in ids, f"必须命中无 embedding 的目标记忆，实际 ids={ids}"
    assert result.get("degraded") is True, "降级路径必须在返回体体现降级标记"


# ═══════════ K-2 模型可用但行无 embedding → 同样命中（回归锚点） ═══════════

def test_k2_model_ok_but_row_without_embedding(env, monkeypatch):
    _insert_memory(env, "m1", "agent-a", "bath-cabinet", CONTENT)  # embedding=NULL
    hub = routes_memory.hub

    async def _model():
        return _FakeModel()

    monkeypatch.setattr(hub, "_ensure_embedding_model", _model)
    result = _search(hub, query="浴室柜")
    ids = [r["memory_id"] for r in result["results"]]
    assert "m1" in ids, f"模型可用但行无 embedding 时旧行为恒空，实际 ids={ids}"


# ═══════════ K-3 FTS5 抛 OperationalError → 落 LIKE 兜底 + logger.error ═══════════

def test_k3_fts_error_falls_back_to_like(env, no_model, monkeypatch, caplog):
    _insert_memory(env, "m1", "agent-a", "bath-cabinet", CONTENT)
    real_query = db_facade.query

    async def _raising_query(sql, params=(), *, db_path=None):
        if "MATCH" in str(sql):
            raise sqlite3.OperationalError(
                "unable to use function MATCH in the requested context")
        return await real_query(sql, params, db_path=db_path)

    monkeypatch.setattr(db_facade, "query", _raising_query)
    with caplog.at_level(logging.WARNING):
        result = _search(no_model, query="浴室柜")
    ids = [r["memory_id"] for r in result["results"]]
    assert "m1" in ids, f"FTS 抛错必须落 LIKE 兜底返回行，实际 ids={ids}"
    assert result.get("degraded") is True
    assert result.get("degraded_reason") == "fts_error"
    assert any(
        r.levelno >= logging.ERROR and "FTS5" in r.getMessage()
        for r in caplog.records
    ), "FTS 抛错必须有 logger.error（含异常类型），不许静默吞"


# ═══════════ K-4 FTS 路径 ═══════════

def test_k4a_fts_index_empty_falls_back_to_like(env, no_model):
    """诊断结论：memory_pool_fts 索引恒空（写侧只发 delete 命令且抛错被吞、
    无同步触发器，实测 docsize=0），故不构造『写路径自动灌索引后 FTS 命中』用例；
    本用例断言索引空时落 LIKE 且返回行。"""
    _insert_memory(env, "m1", "agent-a", "bath-cabinet", CONTENT)
    assert _fts_docsize(env) == 0, "前置：FTS 索引应为空（诊断实测结论）"
    result = _search(no_model, query="浴室柜")
    ids = [r["memory_id"] for r in result["results"]]
    assert "m1" in ids, f"FTS 索引空必须落 LIKE 返回行，实际 ids={ids}"
    assert result.get("keyword_path") == "like"


def test_k4b_fts_subject_form_actually_hits(env, no_model):
    """正向证明修复后的 FTS 形态（fts 表做主语）本身可用：手工灌索引 +
    整段中文 run 作查询（默认 unicode61 分词把连续中文当单 token）→ FTS 命中。"""
    _insert_memory(env, "m1", "agent-a", "bath-cabinet", CONTENT)
    conn = sqlite3.connect(env)
    rid = conn.execute(
        "SELECT rowid FROM memory_pool WHERE memory_id='m1'").fetchone()[0]
    conn.execute(
        "INSERT INTO memory_pool_fts(rowid, content, summary, tags) VALUES(?,?,?,?)",
        (rid, CONTENT, "", "[]"))
    conn.commit()
    conn.close()
    assert _fts_docsize(env) == 1
    result = _search(no_model, query="浴室柜颜色差异处理流程")
    ids = [r["memory_id"] for r in result["results"]]
    assert "m1" in ids, f"索引有行且 token 精确时 FTS 主语形态必须命中，实际 ids={ids}"
    assert result.get("keyword_path") == "fts"


# ═══════════ K-5 不静默：无 except-pass 形态 + 降级有日志与标记 ═══════════

def test_k5_no_silent_swallow(env, no_model, caplog):
    # (a) 源码断言：memory_search 内不得存在 except-pass / except-print 形态
    src = open(os.path.join(REPO_ROOT, "hub_mixins", "memory.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "memory_search"
    )
    silent = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = node.body
        is_pass = all(isinstance(s, ast.Pass) for s in body)
        is_print = all(
            isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
            and getattr(s.value.func, "id", "") == "print"
            for s in body
        )
        if is_pass or is_print:
            silent.append(node.lineno)
    assert not silent, f"memory_search 存在静默吞异常（except-pass/print）行: {silent}"

    # (b) 降级路径：caplog 有 WARNING+ 日志（含 agent/query），返回体有降级标记
    _insert_memory(env, "m1", "agent-a", "bath-cabinet", CONTENT)
    with caplog.at_level(logging.WARNING):
        result = _search(no_model, query="浴室柜")
    assert result.get("degraded") is True, "返回体必须有 degraded 降级标记"
    assert "m1" in [r["memory_id"] for r in result["results"]]
    assert any(
        r.levelno >= logging.WARNING and "agent-a" in r.getMessage()
        for r in caplog.records
    ), "降级路径必须有含 agent_id 的 WARNING+ 日志"
