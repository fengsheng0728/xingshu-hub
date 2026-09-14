# -*- coding: utf-8 -*-
"""O3 升级回滚 + 版本协商验收测试（2026-08-05）

覆盖：
1. _version_ge 语义版本比较
2. hello 握手带 agent_version + 低于最低版本 → 426 拒连
3. 迁移前自动备份（_pre_migrate_backup VACUUM INTO 快照）
4. user_version 记录 schema 版本
5. Agent hello payload 含 agent_version
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend"))


# ============ 1. 版本比较 ============

def test_version_ge():
    from routes import _version_ge
    assert _version_ge("1.0.0", "1.0.0") is True
    assert _version_ge("1.0.1", "1.0.0") is True
    assert _version_ge("1.1.0", "1.0.9") is True
    assert _version_ge("2.0.0", "1.9.9") is True
    assert _version_ge("0.9.0", "1.0.0") is False
    assert _version_ge("1.0.0-alpha", "1.0.0") is True  # 非数字段忽略
    assert _version_ge("", "1.0.0") is False  # 空版本 → 视为不满足


# ============ 2. Agent hello 带版本 ============

def test_hello_payload_has_agent_version():
    # 直接读 Agent 源文件验证（避免 Hub 目录同名 envelope 干扰 import）
    import importlib.util
    # tests/ 在 Hub 根下,dirname×2 = Hub 根;Agent 是平行目录 E:\sync-hub-agent
    hub_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    agent_env_path = os.path.join(
        os.path.dirname(hub_root), "sync-hub-agent", "backend", "envelope.py")
    spec = importlib.util.spec_from_file_location("agent_envelope", agent_env_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env = mod.envelope_hello("ag1")
    payload = env.get("payload", {}) or mod.extract_payload(env)
    assert payload.get("agent_version") == "1.0.0", f"hello 应带版本: {payload}"


# ============ 3. 迁移前备份 ============

def test_pre_migrate_backup_creates_snapshot():
    from db import _pre_migrate_backup
    tmp = tempfile.mkdtemp(prefix="o3-")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE x (id INTEGER)")
    conn.execute("INSERT INTO x VALUES (1)")
    conn.execute("INSERT INTO x VALUES (2)")
    conn.commit()
    conn.close()

    bak = _pre_migrate_backup(db, os.path.join(tmp, "backups"))
    assert bak and os.path.exists(bak), "备份应生成"
    conn2 = sqlite3.connect(bak)
    assert conn2.execute("SELECT count(*) FROM x").fetchone()[0] == 2
    conn2.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_pre_migrate_backup_missing_db_returns_empty():
    from db import _pre_migrate_backup
    tmp = tempfile.mkdtemp(prefix="o3-")
    r = _pre_migrate_backup(os.path.join(tmp, "nonexist.db"), os.path.join(tmp, "bk"))
    assert r == ""
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ============ 4. user_version 记录 ============

def test_user_version_recorded():
    """init_db 后 user_version 应 == SCHEMA_VERSION（幂等，可重复跑）。"""
    from db import SCHEMA_VERSION
    import models
    tmp = tempfile.mkdtemp(prefix="o3-")
    db = os.path.join(tmp, "t.db")
    old = models.CONFIG.DB_PATH
    models.CONFIG.DB_PATH = db
    try:
        import db as dbmod
        dbmod.CONFIG.DB_PATH = db
        dbmod.init_db()
        conn = sqlite3.connect(db)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert ver >= SCHEMA_VERSION, f"user_version {ver} 应 >= {SCHEMA_VERSION}"
        # 幂等：再跑一次不崩
        dbmod.init_db()
    finally:
        models.CONFIG.DB_PATH = old
        dbmod.CONFIG.DB_PATH = old
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ============ 5. 426 拒连逻辑（单元级：模拟 hello 版本检查） ============

def test_version_reject_condition():
    """低版本 Agent → 判定应拒连（426 分支条件成立）。"""
    from routes import _version_ge
    from models import CONFIG
    low_ver = "0.5.0"
    assert not _version_ge(low_ver, CONFIG.AGENT_MIN_VERSION), "低版本应不满足"
    # 兼容版本应满足
    assert _version_ge("1.0.0", CONFIG.AGENT_MIN_VERSION)
