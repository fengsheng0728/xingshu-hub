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


# ═══ 数据层修复轮：备份残留清理 + restore 活库检测 ═══


def test_backup_vacuum_failure_leaves_no_partial_file(src_env, monkeypatch):
    """VACUUM INTO 失败时不得留下半截目标文件（先红：旧代码残留半成品，
    运维按目录清扫会把它当有效备份）。"""
    import hub_cli

    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    real_connect = hub_cli._connect

    class _FailVacuum:
        """包装连接：VACUUM INTO 时先写半截文件再抛错（模拟失败真实形态）。"""

        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if str(sql).startswith("VACUUM INTO"):
                target = str(sql).split("'", 2)[1]
                with open(target, "wb") as f:
                    f.write(b"partial-half-baked")
                raise sqlite3.OperationalError("disk I/O error")
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr(hub_cli, "_connect", lambda p: _FailVacuum(real_connect(p)))
    with pytest.raises(sqlite3.OperationalError):
        cmd_backup(out, db, chroma)
    leftovers = [f for f in os.listdir(out)
                 if f.startswith("sync_hub.") and f.endswith(".db")]
    assert leftovers == [], f"VACUUM 失败不得残留半截备份文件: {leftovers}"


def test_restore_refuses_when_target_wal_nonempty(src_env):
    """目标库 WAL 非空（Hub 在跑 / 崩溃残留）→ 拒绝恢复并提示先停 Hub。"""
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    cmd_backup(out, db, chroma)
    dest = os.path.join(tmp, "live.db")
    conn = sqlite3.connect(dest)
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()  # 连接不关、不 checkpoint → WAL 非空
    wal = dest + "-wal"
    assert os.path.exists(wal) and os.path.getsize(wal) > 0, "前置：WAL 必须非空"
    try:
        with pytest.raises(RuntimeError, match="先停止 Hub"):
            cmd_restore(out, dest, os.path.join(tmp, "rc"))
    finally:
        conn.close()


def test_restore_refuses_when_target_write_locked(src_env):
    """目标库被其它进程持有写锁（Hub 在跑的另一形态）→ 拒绝恢复。"""
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    cmd_backup(out, db, chroma)
    dest = os.path.join(tmp, "locked.db")
    conn = sqlite3.connect(dest)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    locker = sqlite3.connect(dest)
    locker.execute("BEGIN IMMEDIATE")  # 持有 RESERVED 写锁不放
    try:
        with pytest.raises(RuntimeError, match="先停止 Hub"):
            cmd_restore(out, dest, os.path.join(tmp, "rc"))
    finally:
        locker.rollback()
        locker.close()
        conn.close()


def test_restore_pre_restore_backup_is_consistent_snapshot(src_env):
    """.pre-restore 改走 VACUUM INTO：必须是可打开的有效库且含原数据
    （旧裸 copy2 在 WAL 模式下拿到的是旧快照）。"""
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    cmd_backup(out, db, chroma)
    dest_db = os.path.join(tmp, "existing.db")
    conn = sqlite3.connect(dest_db)
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("CREATE TABLE keepme (x TEXT)")
    conn.execute("INSERT INTO keepme VALUES ('v')")
    conn.commit()
    conn.close()  # 关连接触发 checkpoint，WAL 收编 → 活库检测放行
    cmd_restore(out, dest_db, os.path.join(tmp, "rc"))
    pre = dest_db + ".pre-restore"
    assert os.path.exists(pre), ".pre-restore 安全备份必须生成"
    conn = sqlite3.connect(pre)
    try:
        assert conn.execute("SELECT x FROM keepme").fetchone()[0] == "v", \
            ".pre-restore 必须是含原数据的一致性快照"
    finally:
        conn.close()


# ═══ CD-103：备份 manifest 覆盖式修复 —— 配套 manifest.<ts>.json ═══


def test_backup_writes_per_snapshot_manifest(src_env):
    """每次备份除覆盖式 manifest.json 外，落一份与快照同 ts 的配套 manifest。"""
    tmp, db, chroma = src_env
    out = os.path.join(tmp, "backup")
    r = cmd_backup(out, db, chroma)
    paired = os.path.join(out, f"manifest.{r['ts']}.json")
    assert os.path.exists(paired), "配套 manifest.<ts>.json 必须生成"
    with open(paired, "r", encoding="utf-8") as f:
        m = json.load(f)
    assert m["marker_id"] == r["marker"]
    assert m["sqlite"] == os.path.basename(r["sqlite"])
    # 返回体暴露配套 manifest 路径
    assert r["manifest_snapshot"] == paired


def test_old_snapshot_still_verifiable_after_second_backup(src_env):
    """连续两次备份后 manifest.json 已被第二次覆盖，旧快照仍可 verify
    （先红：旧代码 _verify_backup 无 snapshot 参数，旧快照必按新 manifest 误判）。
    注：chroma 备份目录是单副本覆盖式（本条目不动），用无 chroma 口径验证。"""
    import time as _t

    tmp, db, _chroma = src_env
    out = os.path.join(tmp, "backup")
    r1 = cmd_backup(out, db, "")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO agents VALUES ('a2', 'k2')")
    conn.commit()
    conn.close()
    _t.sleep(1.1)  # ts 秒级精度，防同秒同名
    r2 = cmd_backup(out, db, "")
    assert r1["ts"] != r2["ts"] and r1["marker"] != r2["marker"]

    # 旧快照：按配套 manifest 校验通过，且 manifest 确实是第一次备份的
    v1 = _verify_backup(out, snapshot=os.path.basename(r1["sqlite"]))
    assert v1["valid"], v1["issues"]
    assert v1["manifest"]["marker_id"] == r1["marker"]
    assert v1["checks"]["sqlite_marker"] == r1["marker"]
    # 新快照（默认 manifest.json）不受影响
    v2 = _verify_backup(out)
    assert v2["valid"], v2["issues"]
    assert v2["manifest"]["marker_id"] == r2["marker"]


def test_restore_specific_old_snapshot(src_env):
    """restore 指定旧快照：恢复到第一次备份的内容（不含第二次写入的 a2）。"""
    import time as _t

    tmp, db, _chroma = src_env
    out = os.path.join(tmp, "backup")
    r1 = cmd_backup(out, db, "")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO agents VALUES ('a2', 'k2')")
    conn.commit()
    conn.close()
    _t.sleep(1.1)
    cmd_backup(out, db, "")

    dest_db = os.path.join(tmp, "restored-old.db")
    rr = cmd_restore(out, dest_db, "", snapshot=os.path.basename(r1["sqlite"]))
    assert rr["verify"]["valid"], rr["verify"]["issues"]
    conn = sqlite3.connect(dest_db)
    try:
        ids = [r[0] for r in conn.execute("SELECT agent_id FROM agents")]
    finally:
        conn.close()
    assert ids == ["a1"], f"恢复的应是旧快照内容: {ids}"


def test_orphan_paired_manifest_collected_on_next_backup(src_env):
    """旧快照 db 被清理（_cleanup_old_backups 按 mtime 清 sync_hub.*.db）后，
    下一次备份连带清掉其孤儿配套 manifest.<ts>.json。"""
    import time as _t

    tmp, db, _chroma = src_env
    out = os.path.join(tmp, "backup")
    r1 = cmd_backup(out, db, "")
    paired1 = os.path.join(out, f"manifest.{r1['ts']}.json")
    assert os.path.exists(paired1)
    os.remove(r1["sqlite"])  # 模拟旧快照被清理
    _t.sleep(1.1)
    r2 = cmd_backup(out, db, "")
    assert os.path.exists(os.path.join(out, f"manifest.{r2['ts']}.json"))
    assert not os.path.exists(paired1), "孤儿配套 manifest 应随下一次备份清掉"
