# -*- coding: utf-8 -*-
"""O2 备份一致性快照验收测试（2026-08-05）

覆盖：
1. backup：VACUUM INTO 在线热备 + marker 写入（SQLite 侧 + chroma 目录侧）
2. verify：全绿；删表/审计链篡改/chroma marker 篡改 → 检出
3. restore --verify：恢复内容正确 + 校验通过 + 恢复前安全备份
4. 老库（无 audit_log）verify 不误判损坏
"""
import json
import os
import sqlite3
import shutil
import tempfile

import pytest

from hub_cli import cmd_backup, cmd_restore, _verify_backup
from audit_chain import AuditChain


@pytest.fixture()
def src_env():
    tmp = tempfile.mkdtemp(prefix="o2-")
    db = os.path.join(tmp, "src.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY, api_key TEXT)")
    conn.execute("CREATE TABLE memory_pool (memory_id TEXT PRIMARY KEY, content TEXT)")
    conn.execute(
        """CREATE TABLE audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL DEFAULT '', ref_table TEXT DEFAULT '',
            ref_id TEXT DEFAULT '', payload TEXT DEFAULT '',
            prev_hash TEXT NOT NULL DEFAULT '', entry_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT)"""
    )
    conn.execute("INSERT INTO agents VALUES ('a1', 'k1')")
    conn.commit()
    conn.close()
    ac = AuditChain(db)
    ac.append("event", "events", "1", {"t": "x"})
    ac.append("event", "events", "2", {"t": "y"})
    chroma = os.path.join(tmp, "src-chroma")
    os.makedirs(os.path.join(chroma, "col1"))
    with open(os.path.join(chroma, "col1", "data.bin"), "w") as f:
        f.write("v1")
    yield tmp, db, chroma
    shutil.rmtree(tmp, ignore_errors=True)


def test_backup_creates_snapshot_and_markers(src_env):
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    r = cmd_backup(out, db, chroma)
    assert os.path.exists(r["sqlite"])
    assert r["sqlite_bytes"] > 0
    # SQLite 内 marker
    conn = sqlite3.connect(r["sqlite"])
    m = conn.execute("SELECT marker_id FROM backup_markers").fetchone()[0]
    conn.close()
    assert m == r["marker"]
    # chroma 目录 marker 文件
    mf = os.path.join(out, "chroma_db", ".backup_marker")
    assert os.path.exists(mf)
    with open(mf, "r", encoding="utf-8") as f:
        cm = json.load(f)
    assert cm["marker_id"] == r["marker"]
    # manifest
    assert os.path.exists(os.path.join(out, "manifest.json"))


def test_verify_clean_backup(src_env):
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    cmd_backup(out, db, chroma)
    v = _verify_backup(out)
    assert v["valid"], v["issues"]
    assert v["checks"]["tables"] >= 2
    assert v["checks"]["sqlite_marker"] == v["manifest"]["marker_id"]


def test_verify_detects_dropped_table(src_env):
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    r = cmd_backup(out, db, chroma)
    conn = sqlite3.connect(r["sqlite"])
    conn.execute("DROP TABLE memory_pool")
    conn.commit()
    conn.close()
    v = _verify_backup(out)
    assert not v["valid"]
    assert any("表计数" in i for i in v["issues"])


def test_verify_detects_audit_chain_tamper(src_env):
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    r = cmd_backup(out, db, chroma)
    conn = sqlite3.connect(r["sqlite"])
    conn.execute("UPDATE audit_log SET payload='{\"evil\":1}' WHERE log_id=1")
    conn.commit()
    conn.close()
    v = _verify_backup(out)
    assert not v["valid"]
    assert any("hash" in i for i in v["issues"])


def test_verify_detects_chroma_marker_tamper(src_env):
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    cmd_backup(out, db, chroma)
    with open(os.path.join(out, "chroma_db", ".backup_marker"), "w", encoding="utf-8") as f:
        f.write('{"marker_id": "evil", "ts": "x"}')
    v = _verify_backup(out)
    assert not v["valid"]
    assert any("marker" in i for i in v["issues"])


def test_restore_content_and_verify(src_env):
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    cmd_backup(out, db, chroma)
    dest_db = os.path.join(tmp, "restored.db")
    dest_chroma = os.path.join(tmp, "restored-chroma")
    rr = cmd_restore(out, dest_db, dest_chroma)
    assert rr["verify"]["valid"], rr["verify"]["issues"]
    # 内容正确
    conn = sqlite3.connect(dest_db)
    assert conn.execute("SELECT api_key FROM agents WHERE agent_id='a1'").fetchone()[0] == "k1"
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 2
    conn.close()
    assert os.path.exists(os.path.join(dest_chroma, "col1", "data.bin"))
    # 目标库原本不存在 → 无 safety 文件（正确行为，见 test_restore_backs_up）


def test_restore_backs_up_current_db_before_overwrite(src_env):
    """恢复前自动备份当前库（防误操作）：目标已有库时生成 safety + .pre-restore。"""
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    cmd_backup(out, db, chroma)
    dest_db = os.path.join(tmp, "existing.db")
    conn = sqlite3.connect(dest_db)
    conn.execute("CREATE TABLE keepme (x TEXT)")
    conn.commit()
    conn.close()
    cmd_restore(out, dest_db, os.path.join(tmp, "rc"))
    assert os.path.exists(dest_db + ".pre-restore")
    assert os.path.exists(os.path.join(out, "pre-restore-safety.db"))
    # safety 备份可打开且含原数据
    conn = sqlite3.connect(os.path.join(out, "pre-restore-safety.db"))
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='keepme'").fetchone()
    conn.close()


def test_verify_legacy_db_without_audit_tables():
    """老库（无 audit_log/disclosure_log）→ verify 不误判损坏。"""
    tmp = tempfile.mkdtemp(prefix="o2-")
    db = os.path.join(tmp, "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO agents VALUES ('old')")
    conn.commit()
    conn.close()
    out = os.path.join(tmp, "backup")
    cmd_backup(out, db, "")  # 无 chroma
    v = _verify_backup(out)
    assert v["valid"], v["issues"]
    shutil.rmtree(tmp, ignore_errors=True)
