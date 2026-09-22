# -*- coding: utf-8 -*-
"""CD-055（2026-09-19）：knowledge_base.embedding 缺列收口 + wiki_sync 向量写失败不再静默。

E-1 全新库开箱有列：只走 db.init_db()（不跑 alembic），knowledge_base 必含 embedding。
E-2 alembic 0005 幂等双向：已含列的库 upgrade 不抛错且列集合不变；缺列老库 upgrade 补列。
E-3 静默失败消除：向量写回失败 → ERROR 级结构化日志 + 降级标记置位（不再只 print）。
E-4 硬等式（knowledge_base 范围）：内联 DDL 建库 与 空库 alembic upgrade head 列序逐字一致。

不起真实 Hub、不绑端口；alembic 走子进程（沿用 test_alembic_0002_schema 的配方）。
"""
import importlib.util
import logging
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod
import wiki_sync
from models import CONFIG

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REV_PATH = os.path.join(
    ROOT, "migrations", "alembic", "versions", "0005_kb_embedding_column.py")

# db.py 改动前的旧内联 DDL（10 列，缺 embedding）——用于构造「缺列老库」
OLD_KB_DDL = """CREATE TABLE knowledge_base (
            entry_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT,
            tags TEXT,
            links TEXT,
            category TEXT DEFAULT 'general',
            importance REAL DEFAULT 1.0,
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT
        )"""

EXPECTED_KB_COLS = ["entry_id", "title", "content", "tags", "links", "category",
                    "importance", "created_by", "created_at", "updated_at", "embedding"]


def _cols(db_path, table="knowledge_base"):
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _alembic(db_path, *args):
    env = dict(os.environ, SYNC_HUB_DB=str(db_path))
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"alembic {' '.join(args)} 失败:\n{r.stdout}\n{r.stderr}"
    return r


def _version(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()


def _load_revision():
    """真实加载 0005 revision 模块（不只断言文件存在）。"""
    spec = importlib.util.spec_from_file_location("rev0005", REV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════ E-1 全新库开箱有列（先红） ═══════════

def test_e1_fresh_db_init_has_embedding(tmp_path, monkeypatch):
    """只走 db.init_db()（monkeypatch CONFIG.DB_PATH 到 tmp，不跑 alembic）→ 必含 embedding。"""
    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(CONFIG, "DB_PATH", str(db_path))
    db_mod.init_db()
    cols = _cols(db_path)
    assert "embedding" in cols, f"全新库 knowledge_base 缺 embedding 列，实测列: {cols}"
    assert cols[-3:] == ["created_at", "updated_at", "embedding"], \
        f"embedding 必须位于 updated_at 之后（列序对齐 0001 基线），实测: {cols}"


# ═══════════ E-2 alembic 0005 幂等双向 ═══════════

def test_e2a_upgrade_idempotent_on_db_with_column(tmp_path):
    """(a) 已含 embedding 的库：stamp 0004 后 upgrade head → 不抛错、列集合不变。"""
    db = tmp_path / "has_col.db"
    conn = sqlite3.connect(str(db))
    conn.execute(OLD_KB_DDL)
    conn.execute("ALTER TABLE knowledge_base ADD COLUMN embedding BLOB")  # 已有该列的现库
    conn.commit()
    conn.close()
    _alembic(db, "stamp", "0004_event_outbox")
    before = _cols(db)
    assert "embedding" in before
    _alembic(db, "upgrade", "head")
    assert _cols(db) == before, "已有列的库 upgrade 后列集合不得变化"
    # 版本随 CD-060 变更：本断言在 "upgrade head" 之后，属版本指针
    assert _version(db) == "0010_employee_keys"


def test_e2b_upgrade_adds_column_on_old_db(tmp_path):
    """(b) 按旧内联 DDL 建的缺列老库：stamp 0004 后 upgrade head → 补上 embedding。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(OLD_KB_DDL)
    conn.commit()
    conn.close()
    assert "embedding" not in _cols(db)
    _alembic(db, "stamp", "0004_event_outbox")
    _alembic(db, "upgrade", "head")
    cols = _cols(db)
    assert "embedding" in cols, f"0005 未补 embedding 列，实测: {cols}"
    assert cols[-3:] == ["created_at", "updated_at", "embedding"]  # ALTER 追加在末尾
    # 版本随 CD-060 变更：本断言在 "upgrade head" 之后，属版本指针
    assert _version(db) == "0010_employee_keys"


def test_e2_revision_chain():
    """0005 revision 元数据：id 与 down_revision 实测（接在 0004_event_outbox 之后）。"""
    mod = _load_revision()
    assert mod.revision == "0005_kb_embedding_column"
    assert mod.down_revision == "0004_event_outbox"


# ═══════════ E-3 静默失败消除（先红） ═══════════

def test_e3_embedding_write_failure_logged_and_flagged(tmp_path, monkeypatch, caplog):
    """向量写回失败 → (a) ERROR 级结构化日志（含 entry_id/异常类型）(b) 降级标记置位。"""
    monkeypatch.setattr(
        wiki_sync, "EMBEDDING_SYNC_STATE",
        {"degraded": False, "failures": 0, "last_error": None}, raising=False)
    # 指向一个 knowledge_base 缺 embedding 列的库 → UPDATE 必然 OperationalError
    bad_db = tmp_path / "no_embedding.db"
    conn = sqlite3.connect(str(bad_db))
    conn.execute(OLD_KB_DDL)
    conn.execute("INSERT INTO knowledge_base (entry_id, title) VALUES ('kb-x', 't')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(wiki_sync, "DB_PATH", str(bad_db))

    with caplog.at_level(logging.ERROR, logger="xingshu.wiki_sync"):
        wiki_sync._generate_embeddings(
            [{"entry_id": "kb-x", "title": "t", "content": "c"}])

    errors = [r for r in caplog.records
              if r.levelno >= logging.ERROR and r.name == "xingshu.wiki_sync"]
    assert errors, "向量写失败必须产生 ERROR 级日志（改动前只有 print）"
    msg = errors[0].getMessage()
    assert "kb-x" in msg or "entry" in msg.lower(), f"日志须含 entry_id 线索，实测: {msg}"
    state = wiki_sync.EMBEDDING_SYNC_STATE
    assert state["degraded"] is True, "降级标记必须置位"
    assert state["failures"] >= 1 and state["last_error"]


def test_e3b_sync_result_carries_degraded_flag(tmp_path, monkeypatch):
    """降级标记经 sync() 返回 stats 透出（routes_wiki last_result → /sync/status）。"""
    monkeypatch.setattr(
        wiki_sync, "EMBEDDING_SYNC_STATE",
        {"degraded": True, "failures": 1, "last_error": "x"}, raising=False)
    monkeypatch.setattr(wiki_sync, "_generate_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(wiki_sync, "_clean_orphans", lambda *a, **k: None)
    monkeypatch.setattr(wiki_sync, "_update_index", lambda *a, **k: None)
    monkeypatch.setattr(wiki_sync, "_update_log", lambda *a, **k: None)
    monkeypatch.setattr(wiki_sync, "ensure_wiki", lambda: None)
    # 页面写入重定向到 tmp，防污染仓库 wiki/ 目录（validate_path 读 wiki_engine.WIKI_ROOT）
    import wiki_engine
    fake_root = str(tmp_path / "wiki")
    monkeypatch.setattr(wiki_engine, "WIKI_ROOT", fake_root)
    monkeypatch.setattr(wiki_sync, "WIKI_ROOT", fake_root)
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db))
    conn.execute(OLD_KB_DDL)
    conn.execute("INSERT INTO knowledge_base (entry_id, title) VALUES ('kb-1', 't')")
    conn.execute("CREATE TABLE memory_pool (memory_id TEXT PRIMARY KEY, importance REAL)")
    conn.execute("CREATE TABLE wiki_inbox (id INTEGER PRIMARY KEY, page_path TEXT UNIQUE, title TEXT, status TEXT, source TEXT, trust_level TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(wiki_sync, "DB_PATH", str(db))
    stats = wiki_sync.sync()
    assert stats["embedding_degraded"] is True
    # 既有键契约不变
    for k in ("created", "updated", "skipped", "errors"):
        assert k in stats


# ═══════════ E-4 硬等式（knowledge_base 范围，可选加分） ═══════════

def test_e4_hard_equality_kb_columns(tmp_path, monkeypatch):
    """「只走内联 DDL 建库」与「空库 alembic upgrade head」的 knowledge_base 列序逐字一致。"""
    db_inline = tmp_path / "inline.db"
    monkeypatch.setattr(CONFIG, "DB_PATH", str(db_inline))
    db_mod.init_db()
    db_alembic = tmp_path / "alembic.db"
    _alembic(db_alembic, "upgrade", "head")
    assert _cols(db_inline) == _cols(db_alembic) == EXPECTED_KB_COLS
