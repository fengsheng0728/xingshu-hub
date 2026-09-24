# -*- coding: utf-8 -*-
"""T24 · CD-064：memory_pool_fts 写侧同步结构性断裂修复 验收测试（2026-09-20）

背景（Hermes 实测，2026-09-20）：memory_pool_fts 是 FTS5 外部内容表
（db.py 两处 DDL 均 content='memory_pool'），但全库零触发器、写侧只发
'delete' 命令（对空索引实测抛 DatabaseError: database disk image is malformed）
且被 except-pass 吞掉 → 索引从不被维护，memory_search 的 FTS 路径恒不命中。

修复选型（乙）：写侧改正确命令（insert / delete旧值+insert新值 / delete旧值，
提交后副作用、失败告警不阻塞）+ alembic 0006 对存量库一次 'rebuild'。
不选触发器（甲）的原因：tests/test_memory_keyword_fallback.py 的 K-4a 锚点
用例用裸 SQL 直插 memory_pool 并断言索引为空时落 LIKE——触发器会让该用例
失真，而 K-4a 属本任务禁改文件；写侧方案同时保留 D4 可用性优先的失败隔离。

F-1 写入后 FTS MATCH 命中（先红核心——直接测 FTS 表，不走 memory_search）
F-2 更新后旧内容不命中、新内容命中
F-3 删除后不再命中
F-4 存量库 rebuild：stamp 0005 → upgrade head（只跑 0006）→ 行数一致且可命中
F-5 FTS 同步抛错不阻塞业务写入 + 有 WARNING 告警（不静默）
F-6 T16 降级链不回归：FTS 可命中走 fts、不可命中落 like
F-7 CD-064 残余①：维护清理直删 memory_pool 后 FTS 不留孤儿词元（本轮补）

脚手架对齐 tests/test_memory_keyword_fallback.py：临时库走 db.init_db()
（它建 memory_pool_fts），monkeypatch CONFIG.DB_PATH；直调模块级 hub 单例。
"""
import asyncio
import logging
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit.memory_audit as memory_audit  # noqa: E402
import models  # noqa: E402
import routes_memory  # noqa: E402
from deps import MemoryEntry  # noqa: E402
from hub_core import SyncHub  # noqa: E402
from hub_mixins.memory import MemoryMixin  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# unicode61 把连续中文 run 当单 token——查询词取整段 run 才能 MATCH（同 K-4b 结论）
CONTENT_A = "浴室柜颜色差异处理流程"
CONTENT_B = "马桶漏水更换步骤"


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
def hub(monkeypatch):
    """模块级 hub 单例：强制无向量栈/无 embedding 模型（纯写路径，去掉重干扰）。"""
    h = routes_memory.hub

    async def _no_model(self):
        return None

    monkeypatch.setattr(h, "_chroma_collection", None)
    # CD-114：类级打桩（实例级会给单例留下永久实例属性，遮蔽后续类级 monkeypatch）
    monkeypatch.setattr(SyncHub, "_ensure_embedding_model", _no_model)
    monkeypatch.setattr(h, "_shadow", None)
    return h


def _match(db_path, token):
    """直查 FTS 表（先红口径：不许经 memory_search——它有 LIKE 兜底会掩盖）。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT rowid FROM memory_pool_fts WHERE memory_pool_fts MATCH ?",
        (token,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _docsize(db_path):
    """FTS 已索引文档数（COUNT(*) 主表对外部内容表读的是内容表，不能度量索引）。"""
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM memory_pool_fts_docsize").fetchone()[0]
    conn.close()
    return n


def _store(hub, key, content):
    return asyncio.run(hub.store_memory(
        "agent-a", MemoryEntry(memory_key=key, content=content)))


def _insert_raw(db_path, memory_id, content):
    """模拟存量库：裸 SQL 直插（不经过写侧同步），索引保持为空。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content,"
        " summary, tags, kind, confidence, source_type, disclosure_level,"
        " created_at, updated_at)"
        " VALUES (?, 'agent-a', ?, ?, '', '[]', 'fact', 1.0, 'user', 'summary',"
        " datetime('now'), datetime('now'))",
        (memory_id, memory_id, content),
    )
    conn.commit()
    conn.close()


# ═══════════ F-1 写入后 FTS 命中（先红核心） ═══════════

def test_f1_insert_then_fts_match(env, hub):
    res = _store(hub, "k1", CONTENT_A)
    assert res["status"] == "stored"
    hits = _match(env, CONTENT_A)
    assert len(hits) == 1, f"写入后 FTS 必须命中该条 rowid，实际 hits={hits}"


# ═══════════ F-2 更新后：旧内容不命中、新内容命中 ═══════════

def test_f2_update_old_miss_new_hit(env, hub):
    _store(hub, "k1", CONTENT_A)
    res = _store(hub, "k1", CONTENT_B)  # 同 key → conflict_overwrite
    assert res["action"] == "conflict_overwrite"
    assert _match(env, CONTENT_A) == [], "更新后旧内容不得再命中"
    hits = _match(env, CONTENT_B)
    assert len(hits) == 1, f"更新后新内容必须命中，实际 hits={hits}"


# ═══════════ F-3 删除后不再命中 ═══════════

def test_f3_delete_then_no_match(env, hub):
    _store(hub, "k1", CONTENT_A)
    assert len(_match(env, CONTENT_A)) == 1, "前置：写入必须已入索引（先红锚点）"
    res = asyncio.run(hub.delete_memory("k1", "agent-a"))
    assert res["status"] == "deleted"
    assert _match(env, CONTENT_A) == [], "删除后 FTS 不得再命中"


# ═══════════ F-4 存量库 rebuild（alembic 0006） ═══════════

def test_f4_stock_rebuild_via_alembic_0006(env):
    _insert_raw(env, "m1", CONTENT_A)
    _insert_raw(env, "m2", CONTENT_B)
    assert _docsize(env) == 0, "前置：存量库索引为空（诊断实测结论）"
    env_vars = dict(os.environ, SYNC_HUB_DB=env)
    # 存量库没有 alembic_version：stamp 到 0005 后 upgrade head = 只跑 0006
    r1 = subprocess.run(
        [sys.executable, "-m", "alembic", "stamp", "0005_kb_embedding_column"],
        cwd=ROOT, env=env_vars, capture_output=True, text=True, timeout=120)
    assert r1.returncode == 0, f"alembic stamp 失败:\n{r1.stdout}\n{r1.stderr}"
    for i in (1, 2):  # 跑两遍验证幂等
        r2 = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, env=env_vars, capture_output=True, text=True, timeout=120)
        assert r2.returncode == 0, \
            f"alembic upgrade head 第{i}次失败:\n{r2.stdout}\n{r2.stderr}"
    assert _docsize(env) == 2, \
        f"rebuild 后索引行数须与 memory_pool 一致，实际 docsize={_docsize(env)}"
    assert len(_match(env, CONTENT_A)) == 1, "rebuild 后 CONTENT_A 必须可命中"
    assert len(_match(env, CONTENT_B)) == 1, "rebuild 后 CONTENT_B 必须可命中"


# ═══════════ F-5 写失败不阻塞 + 告警（不静默） ═══════════

def test_f5_fts_failure_not_blocking_and_warns(env, hub, monkeypatch, caplog):
    async def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: memory_pool_fts")

    # CD-114：类级打桩（实例级会给单例留下永久实例属性，遮蔽后续类级 monkeypatch）
    monkeypatch.setattr(MemoryMixin, "_sync_memory_fts", _boom)
    with caplog.at_level(logging.WARNING):
        res = _store(hub, "k1", CONTENT_A)
    assert res["status"] == "stored", "FTS 同步失败不得阻塞业务写入（D4 可用性优先）"
    conn = sqlite3.connect(env)
    n = conn.execute(
        "SELECT COUNT(*) FROM memory_pool WHERE memory_key='k1'").fetchone()[0]
    conn.close()
    assert n == 1, "业务行必须真实落库"
    assert any(
        r.levelno >= logging.WARNING and "FTS" in r.getMessage()
        and "OperationalError" in r.getMessage()
        for r in caplog.records
    ), "FTS 同步失败必须 WARNING 告警（含异常类型），不许 except-pass 静默"


# ═══════════ F-6 T16 降级链不回归 ═══════════

def test_f6_t16_fallback_chain_intact(env, hub):
    """对照 tests/test_memory_keyword_fallback.py 语义（不重复实现）：
    FTS 有索引且 token 精确 → keyword_path='fts'；token 只是索引词前缀
    （unicode61 无前缀匹配）→ FTS 空命中 → 落 LIKE（keyword_path='like'）。"""
    _store(hub, "k1", CONTENT_A)

    req = routes_memory.MemorySearchRequest(query=CONTENT_A, agent_id="agent-a")
    res = asyncio.run(hub.memory_search(req))
    assert any(r["memory_key"] == "k1" for r in res["results"])
    assert res.get("keyword_path") == "fts", \
        f"FTS 可命中时必须走 FTS，实际 keyword_path={res.get('keyword_path')}"

    req2 = routes_memory.MemorySearchRequest(query="浴室柜", agent_id="agent-a")
    res2 = asyncio.run(hub.memory_search(req2))
    assert any(r["memory_key"] == "k1" for r in res2["results"])
    assert res2.get("keyword_path") == "like", \
        f"FTS 不可命中时必须落 LIKE，实际 keyword_path={res2.get('keyword_path')}"


def test_cd064_residual_cleanup_rebuilds_fts(env, hub):
    """CD-064 残余①（先红）：维护清理 `_run_cleanup` 用裸 SQL 直删 memory_pool，
    而 memory_pool_fts 是外部内容表且全库零触发器 —— 删主表不会摘索引词元，
    留下的孤儿词元会让 docsize 与主表行数长期背离（memory_search 回查主表
    过滤，语义不脏但索引持续漂移）。修复后清理必须同步收敛索引。"""
    _store(hub, "keep", CONTENT_A)
    _store(hub, "stale", CONTENT_B)
    assert _docsize(env) == 2

    # 造过期条件：created_at 拉老 + importance < 0.5（清理谓词的两项）
    conn = sqlite3.connect(env)
    conn.execute(
        "UPDATE memory_pool SET created_at=?, importance=0.1 WHERE memory_key=?",
        ("2020-01-01 00:00:00", "stale"))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM memory_pool").fetchone()[0] == 2
    conn.close()

    asyncio.run(hub._run_cleanup())

    conn = sqlite3.connect(env)
    left = conn.execute("SELECT COUNT(*) FROM memory_pool").fetchone()[0]
    conn.close()
    assert left == 1, "前置：清理应删掉 1 条过期记忆"
    assert _docsize(env) == 1, "清理后 FTS 索引残留孤儿词元（docsize 未随主表收敛）"
    assert _match(env, CONTENT_B) == [], "已删记忆仍能被 FTS 命中（孤儿词元）"
    assert _match(env, CONTENT_A) != [], "保留记忆的索引被误清"
