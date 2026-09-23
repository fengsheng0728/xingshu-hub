"""CD-077 自动备份改造验收用例（T1-0 先红 → T1-1/1-2/1-3 转绿）

配方（确定性优先，遵循 conftest 约定）：
  - 不起真实 Hub、不连 3060、不碰生产 sync_hub.db / chroma_db；
  - CONFIG.DB_PATH / CONFIG.CHROMA_PATH 用 monkeypatch 指到 tmp（deps.CONFIG 与
    models.CONFIG 同一对象，实测 a is b）；
  - SYNC_HUB_CONFIG_DIR 指到 tmp 配置目录（里面只有 database 段）；
  - WAL 一致性用真 sqlite WAL 语义：journal_mode=wal + wal_autocheckpoint=0，
    连接保持打开（连接关闭会触发 checkpoint 并把 -wal 合并进主库，先红就造不出来）。

先红预期（基线 6e81fa9）：
  - test_wal_committed_data_visible_in_backup   → AssertionError（copy2 只拿主库文件）
  - test_backup_includes_chroma_and_manifest    → AssertionError（基线无 chroma/manifest）
  - test_failure_leaves_machine_readable_trace  → AssertionError（基线只有 warning）
  - test_retention_new_artifact_matrix          → 退化红（_cleanup_old_backups 为新符号），
                                                    文件内附不依赖新符号的行为探针段
  - test_keep_days_precedence                   → AssertionError（基线只读 backup_interval_days）
其余用例（backup_enabled / 冷却保留语义）在基线上即绿，是防回归护栏。
"""
import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from hub_mixins.maintenance import MaintenanceMixin

_EVENTS_DDL = """CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    agent_id TEXT,
    payload TEXT,
    timestamp TEXT
)"""


class _MiniHub(MaintenanceMixin):
    """最小 MaintenanceMixin 宿主：只提供 _run_backup 依赖的 _db / _log_event。

    _log_event 的写库语义与 hub_core._log_event 主链一致（INSERT INTO events）。
    """

    def __init__(self, db_path: str):
        self._mini_db_path = db_path

    def _db(self):
        conn = sqlite3.connect(self._mini_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    async def _log_event(self, event_type: str, agent_id: str, payload: dict):
        conn = sqlite3.connect(self._mini_db_path)
        conn.execute(
            "INSERT INTO events (event_type, agent_id, payload, timestamp)"
            " VALUES (?, ?, ?, ?)",
            (event_type, agent_id,
             json.dumps(payload, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """tmp 配置目录 + WAL 源库（连接保持打开）+ chroma 目录 + CONFIG 指向。"""
    # 1) 配置目录：只有 database 段（保持「不依赖本机 config.yaml」的测试约定）
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "database:\n"
        "  backup_enabled: true\n"
        "  backup_keep_days: 7\n"
        "  backup_interval_days: 1\n",  # 故意与 keep_days 不一致，用于优先级断言
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNC_HUB_CONFIG_DIR", str(cfg_dir))

    # 2) WAL 源库：autocheckpoint 关、连接不关（关了会触发 checkpoint，先红造不出）
    db_path = str(tmp_path / "src.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("PRAGMA busy_timeout = 8000")
    conn.execute(_EVENTS_DDL)
    conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT)")
    conn.commit()

    # 3) chroma 目录（含一个占位索引文件）
    chroma_dir = tmp_path / "chroma_db"
    chroma_dir.mkdir()
    (chroma_dir / "index.bin").write_bytes(b"\x00" * 16)

    # 4) CONFIG 指向 tmp（deps.CONFIG is models.CONFIG，patch 对象属性两边同时生效）
    import models
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    monkeypatch.setattr(models.CONFIG, "CHROMA_PATH", str(chroma_dir))

    return {
        "cfg_dir": str(cfg_dir),
        "db_path": db_path,
        "conn": conn,
        "chroma_dir": str(chroma_dir),
        "backup_dir": str(tmp_path / "config" / "backups"),
    }


def _newest_backup_db(backup_dir: str) -> str:
    files = [f for f in os.listdir(backup_dir)
             if f.startswith("sync_hub.") and f.endswith(".db")]
    assert files, f"备份目录里没有 sync_hub.*.db 备份文件: {backup_dir}"
    files.sort(key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)))
    return os.path.join(backup_dir, files[-1])


def _count_probe_rows(db_file: str) -> int:
    try:
        bconn = sqlite3.connect(db_file)
        try:
            return bconn.execute(
                "SELECT COUNT(*) FROM probe WHERE note LIKE 'cd077-probe-%'"
            ).fetchone()[0]
        finally:
            bconn.close()
    except sqlite3.OperationalError:
        return 0  # 连 probe 表都没有（WAL 未合并进主库文件）


# ── 先红断言 1：WAL 一致性 ──────────────────────────────────────────────

def test_wal_committed_data_visible_in_backup(env):
    """WAL 模式下已 commit 但未 checkpoint 的数据，必须出现在自动备份副本里。"""
    conn = env["conn"]
    conn.executemany(
        "INSERT INTO probe (id, note) VALUES (?, ?)",
        [(i, f"cd077-probe-{i}") for i in range(500)],
    )
    conn.commit()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 前置条件：数据还在 -wal 里（没 checkpoint）。若此后置条件不成立，
    # 说明用例环境被破坏（连接被关/触发过 checkpoint），不是被测行为问题。
    wal_file = env["db_path"] + "-wal"
    assert os.path.exists(wal_file) and os.path.getsize(wal_file) > 0, \
        "前置条件失败：-wal 不存在或为空，数据可能已被 checkpoint 进主库"

    hub = _MiniHub(env["db_path"])
    asyncio.run(hub._run_backup())

    n = _count_probe_rows(_newest_backup_db(env["backup_dir"]))
    assert n == 500, \
        f"备份副本里读不到已 commit 的数据（WAL 一致性破坏）：读到 {n} 行，预期 500 行"


# ── 先红断言 2：产物完整性 ─────────────────────────────────────────────

def test_backup_includes_chroma_and_manifest(env):
    """自动备份产物应包含 chroma_db/ 目录与 manifest.json，且 marker 对齐。"""
    hub = _MiniHub(env["db_path"])
    asyncio.run(hub._run_backup())

    chroma_backup = os.path.join(env["backup_dir"], "chroma_db")
    assert os.path.isdir(chroma_backup), \
        f"备份目录缺少 chroma_db/ 目录: {env['backup_dir']}"
    assert os.path.exists(os.path.join(chroma_backup, ".backup_marker")), \
        "chroma 备份目录缺少 .backup_marker"

    manifest_path = os.path.join(env["backup_dir"], "manifest.json")
    assert os.path.exists(manifest_path), \
        f"备份目录缺少 manifest.json: {env['backup_dir']}"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest.get("sqlite", "").startswith("sync_hub."), \
        f"manifest.sqlite 未指向本次 sqlite 备份: {manifest!r}"


# ── 先红断言 3：失败可观测 ─────────────────────────────────────────────

def test_failure_leaves_machine_readable_trace(env, monkeypatch, caplog):
    """备份失败必须在 events 表留可机器判读的痕迹（不止一条 warning 日志）。"""
    import hub_cli

    def _boom(*args, **kwargs):
        raise RuntimeError("cd077 injected failure")

    monkeypatch.setattr(hub_cli, "cmd_backup", _boom)

    import logging
    with caplog.at_level(logging.ERROR, logger="xingshu.maintenance"):
        hub = _MiniHub(env["db_path"])
        asyncio.run(hub._run_backup())

    conn = sqlite3.connect(env["db_path"])
    try:
        rows = conn.execute(
            "SELECT event_type, agent_id, payload FROM events"
            " WHERE event_type = 'backup_failed'"
        ).fetchall()
    finally:
        conn.close()

    assert rows, "备份失败未在 events 表留下 backup_failed 痕迹（仍为静默失败）"
    _etype, agent_id, payload_raw = rows[-1]
    payload = json.loads(payload_raw)
    assert "cd077 injected failure" in payload.get("error", ""), \
        f"backup_failed 事件 payload 缺少错误摘要: {payload!r}"
    assert agent_id == "__system__"


# ── T1-3：保留策略对齐新产物 ───────────────────────────────────────────

def test_retention_new_artifact_matrix(env):
    """过期清理 / 未过期保留 / 非本实现文件不被删（覆盖 chroma_db/ 与 manifest）。"""
    backup_dir = env["backup_dir"]
    os.makedirs(backup_dir, exist_ok=True)
    now = time.time()
    old_ts = now - 10 * 86400   # 10 天前 > keep_days(7)
    fresh_ts = now - 1 * 86400  # 1 天前 < keep_days(7)

    def _touch(path, ts):
        os.utime(path, (ts, ts))

    # 行为探针（不依赖新符号，基线即可跑）：_run_backup 的清理阶段
    # 基线语义 = 只清 sync_hub.*.db；本探针段在基线/改后都应绿。
    old_db_probe = os.path.join(backup_dir, "sync_hub.2020-01-01-000000.db")
    with open(old_db_probe, "wb") as f:
        f.write(b"probe")
    foreign_db = os.path.join(backup_dir, "manual_important.db")
    with open(foreign_db, "wb") as f:
        f.write(b"keep me")
    _touch(old_db_probe, old_ts)
    _touch(foreign_db, old_ts)
    hub = _MiniHub(env["db_path"])
    asyncio.run(hub._run_backup())
    assert not os.path.exists(old_db_probe), "过期的 sync_hub.*.db 备份未被清理"
    assert os.path.exists(foreign_db), "非本实现文件 manual_important.db 不应被删"

    # 新产物矩阵：直调清理助手（T1-3 新增符号；基线上此处为退化红 AttributeError，
    # 故上面保留了一段不依赖新符号的行为探针）
    old_db = os.path.join(backup_dir, "sync_hub.2000-01-01-000000.db")
    with open(old_db, "wb") as f:
        f.write(b"old")
    _touch(old_db, old_ts)  # 补（验收方 2026-09-23）：判据是 mtime，原用例漏了这一步
    fresh_db = _newest_backup_db(backup_dir)
    _touch(fresh_db, fresh_ts)  # 本次产物若 keep_days=7 则未过期

    old_manifest = os.path.join(backup_dir, "manifest.json")
    old_chroma = os.path.join(backup_dir, "chroma_db")
    # 把 manifest/chroma 改老（模拟「很久没备份成功」场景）。
    # 基线上 cmd_backup 不存在、产物也不会生成，这里自建旧文件（清理判据只认
    # mtime 与本实现命名，不自建就无法覆盖基线缺失产物的分支）。
    if not os.path.exists(old_manifest):
        with open(old_manifest, "w", encoding="utf-8") as f:
            json.dump({"marker_id": "old"}, f)
    if not os.path.isdir(old_chroma):
        os.makedirs(old_chroma, exist_ok=True)
    _touch(old_manifest, old_ts)
    _touch(old_chroma, old_ts)

    foreign_dir = os.path.join(backup_dir, "user_notes")
    os.makedirs(foreign_dir, exist_ok=True)
    _touch(foreign_dir, old_ts)
    foreign_marker = os.path.join(backup_dir, "keep.me")
    with open(foreign_marker, "w", encoding="utf-8") as f:
        f.write("x")
    _touch(foreign_marker, old_ts)

    hub._cleanup_old_backups(backup_dir, keep_days=7)

    assert not os.path.exists(old_db), "过期 sync_hub.*.db 未被清理"
    assert not os.path.exists(old_manifest), "过期 manifest.json 未被清理"
    assert not os.path.exists(old_chroma), "过期 chroma_db/ 未被清理"
    assert os.path.exists(fresh_db), "未过期（1 天）的备份被误删"
    assert os.path.exists(foreign_db), "外来文件 manual_important.db 被误删"
    assert os.path.isdir(foreign_dir), "外来目录 user_notes/ 被误删"
    assert os.path.exists(foreign_marker), "外来文件 keep.me 被误删"


def test_keep_days_precedence(env):
    """keep_days 读取顺序：database.backup_keep_days > backup_interval_days > 默认 7。"""
    backup_dir = env["backup_dir"]
    os.makedirs(backup_dir, exist_ok=True)
    # mtime 3 天前：backup_interval_days=1（旧字段）会删，backup_keep_days=7 应保留
    mid_db = os.path.join(backup_dir, "sync_hub.2026-09-20-000000.db")
    with open(mid_db, "wb") as f:
        f.write(b"x")
    os.utime(mid_db, (time.time() - 3 * 86400,) * 2)

    hub = _MiniHub(env["db_path"])
    asyncio.run(hub._run_backup())

    assert os.path.exists(mid_db), \
        "backup_keep_days=7 应优先于 backup_interval_days=1，3 天前的备份不应被删"


# ── 既有语义护栏（基线上即绿，防回归） ─────────────────────────────────

def test_backup_disabled_skips(env):
    """backup_enabled=False 时跳过备份（既有语义保留）。"""
    with open(os.path.join(env["cfg_dir"], "config.yaml"), "w", encoding="utf-8") as f:
        f.write("database:\n  backup_enabled: false\n")
    hub = _MiniHub(env["db_path"])
    asyncio.run(hub._run_backup())
    assert not os.path.exists(env["backup_dir"]) or \
        not [x for x in os.listdir(env["backup_dir"])
             if x.startswith("sync_hub.") and x.endswith(".db")], \
        "backup_enabled=False 时不应产生备份文件"


def test_cooldown_skips_second_run(env):
    """1 小时冷却：紧接的第二次触发应跳过（既有语义保留）。"""
    hub = _MiniHub(env["db_path"])
    asyncio.run(hub._run_backup())
    count_after_first = len(
        [x for x in os.listdir(env["backup_dir"])
         if x.startswith("sync_hub.") and x.endswith(".db")])
    asyncio.run(hub._run_backup())  # 冷却期内
    count_after_second = len(
        [x for x in os.listdir(env["backup_dir"])
         if x.startswith("sync_hub.") and x.endswith(".db")])
    assert count_after_first == 1, f"首次备份应恰好 1 份，实际 {count_after_first}"
    assert count_after_second == count_after_first, \
        "1 小时冷却内第二次触发不应产生新备份"
