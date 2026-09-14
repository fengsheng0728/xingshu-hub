"""T2-3：alembic 0002 schema 收口回归。

空库 upgrade head 后断言 0002 增量产物（代表列/代表表）存在；
二次 upgrade 验证幂等（schema 不变）。只跑迁移，不连真实 Hub。
"""
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _upgrade_head(db_path):
    env = dict(os.environ, SYNC_HUB_DB=str(db_path))
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"alembic upgrade head 失败:\n{r.stdout}\n{r.stderr}"


def _cols(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _snapshot(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
        "ORDER BY type, name").fetchall()
    conn.close()
    return rows


def test_0002_upgrade_head_empty_db(tmp_path):
    db = tmp_path / "empty.db"
    _upgrade_head(db)
    conn = sqlite3.connect(db)
    try:
        version = conn.execute(
            "SELECT version_num FROM alembic_version").fetchone()[0]
        # head 已前进到 0003（T1-2 agents.api_key 哈希化）
        assert version == "0003_hash_agents_api_key"
        # 代表列：0002 增量 ALTER 产物
        assert "api_key_prev" in _cols(conn, "agents")
        assert "full_access" in _cols(conn, "agents")
        # 0003 增量：api_key 哈希列
        assert "api_key_hash" in _cols(conn, "agents")
        assert "api_key_prev_hash" in _cols(conn, "agents")
        assert "shared_secret" in _cols(conn, "team_members")
        assert "visibility" in _cols(conn, "shared_docs")
        assert "allowed_agents" in _cols(conn, "shared_docs")
        assert "parent_task_id" in _cols(conn, "tasks")
        assert "trust_level" in _cols(conn, "wiki_inbox")
        # 代表新表：0002 补建（0001 漏收/db.py 裸建表段收口）
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("messages", "review_queue", "shadow_pending",
                  "agent_keys", "integrations_state", "audit_log"):
            assert t in tables
    finally:
        conn.close()


def test_0002_upgrade_idempotent_on_existing_db(tmp_path):
    """已有全部列的库再跑 upgrade head：0002 全部幂等跳过，schema 不变。"""
    db = tmp_path / "existing.db"
    _upgrade_head(db)
    before = _snapshot(db)
    _upgrade_head(db)
    assert _snapshot(db) == before
