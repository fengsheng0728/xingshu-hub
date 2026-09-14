# -*- coding: utf-8 -*-
"""S3a Hub 侧 taint 落库验收测试（2026-08-05）

覆盖：
1. 信任降级矩阵（_merge_trust / _trust_from_source）
2. memory_pool 落库带 trust_level/source_agent_id/tainted_at
3. shared_docs / wiki_inbox 表结构含 trust 列
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# D-11: 迁移后 _insert_new_memory 委托 _insert_new_memory_sync，MiniHub 需继承 mixin
from hub_mixins.memory import MemoryMixin  # noqa: E402


def test_trust_merge_matrix():
    from hub_core import SyncHub
    assert SyncHub._merge_trust("internal", "external") == "external"
    assert SyncHub._merge_trust("external", "internal") == "external"
    assert SyncHub._merge_trust("internal", "system") == "internal"
    assert SyncHub._merge_trust("federated", "system") == "federated"
    assert SyncHub._merge_trust("system", "external") == "external"
    assert SyncHub._trust_from_source("tool") == "external"
    assert SyncHub._trust_from_source("user") == "internal"


def test_insert_new_memory_writes_trust():
    """_insert_new_memory 落库 trust_level/source_agent_id/tainted_at。"""
    import asyncio
    from hub_core import SyncHub
    from models import MemoryEntry

    tmp = tempfile.mkdtemp(prefix="s3a-")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE memory_pool (
            memory_id TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL,
            memory_key TEXT, content TEXT, summary TEXT, embedding BLOB,
            importance REAL, tags TEXT, disclosure_level TEXT,
            disclosure_scope TEXT, allowed_viewers TEXT, created_at TEXT,
            access_count INTEGER DEFAULT 0, last_accessed TEXT,
            kind TEXT DEFAULT 'fact', source_session_id TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0, source_type TEXT DEFAULT 'user',
            updated_at TEXT DEFAULT NULL,
            trust_level TEXT NOT NULL DEFAULT 'internal',
            source_agent_id TEXT DEFAULT '', tainted_at TEXT DEFAULT '')"""
    )
    conn.commit()

    class MiniHub(MemoryMixin):
        def __init__(self, db_path):
            self._db_path = db_path

        def _db(self):
            c = sqlite3.connect(self._db_path)
            c.row_factory = sqlite3.Row
            return c

    mh = MiniHub(db)
    entry = MemoryEntry(memory_key="k1", content="内部记忆", trust_level="internal")
    c = mh._db().cursor()
    asyncio.run(SyncHub._insert_new_memory(
        mh, c, "ag1", entry, "fact", "user", "s1", 1.0, None, None,
        "2026-08-05T00:00:00", "internal"))
    c.connection.commit()
    row = c.execute(
        "SELECT trust_level, source_agent_id, tainted_at FROM memory_pool WHERE memory_key='k1'"
    ).fetchone()
    c.connection.close()
    assert row[0] == "internal"
    assert row[1] == "ag1"
    assert row[2]  # tainted_at 非空
    # conn 变量已由 c.connection 关闭
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_insert_external_trust():
    """显式 external 落库。"""
    import asyncio
    from hub_core import SyncHub
    from models import MemoryEntry

    tmp = tempfile.mkdtemp(prefix="s3a-")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE memory_pool (
            memory_id TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL,
            memory_key TEXT, content TEXT, summary TEXT, embedding BLOB,
            importance REAL, tags TEXT, disclosure_level TEXT,
            disclosure_scope TEXT, allowed_viewers TEXT, created_at TEXT,
            access_count INTEGER DEFAULT 0, last_accessed TEXT,
            kind TEXT DEFAULT 'fact', source_session_id TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0, source_type TEXT DEFAULT 'user',
            updated_at TEXT DEFAULT NULL,
            trust_level TEXT NOT NULL DEFAULT 'internal',
            source_agent_id TEXT DEFAULT '', tainted_at TEXT DEFAULT '')"""
    )
    conn.commit()

    class MiniHub(MemoryMixin):
        def __init__(self, db_path):
            self._db_path = db_path

        def _db(self):
            c = sqlite3.connect(self._db_path)
            c.row_factory = sqlite3.Row
            return c

    mh = MiniHub(db)
    entry = MemoryEntry(memory_key="k2", content="外部内容", trust_level="external")
    c = mh._db().cursor()
    asyncio.run(SyncHub._insert_new_memory(
        mh, c, "ag1", entry, "fact", "tool", "s1", 0.3, None, None,
        "2026-08-05T00:00:00", "external"))
    c.connection.commit()
    row = c.execute("SELECT trust_level FROM memory_pool WHERE memory_key='k2'").fetchone()
    assert row[0] == "external"
    c.connection.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_tables_have_trust_columns():
    """生产库（sync_hub.db）迁移后含 trust 列。"""
    # 该测试针对真实库验证迁移——若库不存在则跳过
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync_hub.db")
    if not os.path.exists(db_path):
        pytest.skip("生产库不存在")
    conn = sqlite3.connect(db_path)
    for t in ("memory_pool", "shared_docs"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        assert "trust_level" in cols, f"{t} 缺 trust_level"
        assert "source_agent_id" in cols, f"{t} 缺 source_agent_id"
        assert "tainted_at" in cols, f"{t} 缺 tainted_at"
    wcols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_inbox)")}
    assert "trust_level" in wcols, "wiki_inbox 缺 trust_level"
    conn.close()
