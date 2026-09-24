"""CD-082 启动迁移检查 · 验收用例（运维轮 2026-09-23）

执行方 = Hermes 自实现（kimi-code 5h 配额 403 中断，无外部执行方）。

覆盖任务书 §3 T3-2 的四条判据：
  1. 库已是最新            → 不做任何动作
  2. 库落后 + 开关开（默认）→ 执行迁移且版本推进到 head
  3. 库落后 + 开关关        → 告警、不执行、不退出
  4. 迁移执行失败           → fail-closed 抛错（不许 fail-open 继续启动）
外加：
  5. 库无 alembic_version 表（全新库 / 内联 DDL 建的库）→ 不猜、不迁移
  6. 真子进程调用的命令形状（argv / cwd / SYNC_HUB_DB 注入）
  7. 生产库副本的真实读取路径（只读复制，不碰生产库本体）

先红形态说明（诚实登记）：本条属「**新增能力**」，基线 `main.py` 里不存在
`run_startup_migration` / `_alembic_head` 等符号，故先红天然是 **AttributeError 退化红**，
而不是 AssertionError。替代证据 = 基线能力缺失的事实断言：
  `grep -c "alembic" main.py` → 0；`grep -c "migrations" main.py` → 0；
  `config.example.yaml` 无 `migrations:` 段（见验收记录 §先红）。
"""
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as main_mod  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mk_db(path, version=None, with_version_table=True):
    conn = sqlite3.connect(str(path))
    try:
        if with_version_table:
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            if version is not None:
                conn.execute("INSERT INTO alembic_version VALUES (?)", (version,))
        else:
            conn.execute("CREATE TABLE probe (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return str(path)


@pytest.fixture()
def head():
    h = main_mod._alembic_head(REPO)
    assert isinstance(h, str) and h, "head revision 必须是非空字符串"
    return h


def _find_production_db():
    """生产库位于「主工作区」仓库根；本用例可能跑在 git worktree 里（如 E:/xingshu-wt-*）。

    先看本仓根，再用 `git rev-parse --git-common-dir` 反查主工作区根 —— 只读探测，不写。
    """
    candidates = [os.path.join(REPO, "sync_hub.db")]
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=REPO, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if common:
            candidates.append(os.path.join(os.path.dirname(common), "sync_hub.db"))
    except Exception:
        pass
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

# ── 1. 已是最新 → 无动作 ────────────────────────────────────────────────

def test_up_to_date_does_nothing(tmp_path, monkeypatch, head):
    db = _mk_db(tmp_path / "t.db", version=head)
    called = []
    monkeypatch.setattr(main_mod, "_run_alembic_upgrade",
                        lambda *a, **k: called.append(a) or 0)

    res = main_mod.run_startup_migration(db, repo_root=REPO)

    assert res["action"] == "up_to_date", res
    assert res["from"] == head and res["to"] == head
    assert not called, "已是最新时不应执行任何迁移"


# ── 2. 落后 + 开关开 → 执行并推进到 head ────────────────────────────────

def test_behind_and_enabled_runs_and_advances(tmp_path, monkeypatch, head):
    db = _mk_db(tmp_path / "t.db", version="0001_baseline")
    calls = []

    def _fake_upgrade(repo_root, db_path, config_dir=""):
        calls.append((repo_root, db_path, config_dir))
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE alembic_version SET version_num = ?", (head,))
            conn.commit()
        finally:
            conn.close()
        return 0

    monkeypatch.setattr(main_mod, "_run_alembic_upgrade", _fake_upgrade)

    res = main_mod.run_startup_migration(db, repo_root=REPO, config_dir="<cfg>")

    assert res["action"] == "upgraded", res
    assert res["from"] == "0001_baseline" and res["to"] == head
    assert len(calls) == 1 and calls[0][2] == "<cfg>"
    assert main_mod._db_alembic_version(db) == head, "迁移后库内版本必须推进到 head"


# ── 3. 落后 + 开关关 → 告警、不执行、不退出 ─────────────────────────────

def test_behind_and_disabled_warns_without_running(tmp_path, monkeypatch, capsys):
    db = _mk_db(tmp_path / "t.db", version="0001_baseline")
    called = []
    monkeypatch.setattr(main_mod, "_run_alembic_upgrade",
                        lambda *a, **k: called.append(a) or 0)

    res = main_mod.run_startup_migration(db, repo_root=REPO, auto_upgrade=False)

    out = capsys.readouterr().out
    assert res["action"] == "skipped_disabled", res
    assert not called, "开关关闭时不许执行迁移"
    assert "[WARN]" in out and "alembic upgrade head" in out, f"应给运维可操作告警: {out!r}"
    assert main_mod._db_alembic_version(db) == "0001_baseline", "库不应被改动"


# ── 4. 迁移失败 → fail-closed ──────────────────────────────────────────

def test_upgrade_failure_is_fail_closed(tmp_path, monkeypatch):
    db = _mk_db(tmp_path / "t.db", version="0001_baseline")
    monkeypatch.setattr(main_mod, "_run_alembic_upgrade", lambda *a, **k: 1)

    with pytest.raises(RuntimeError) as ei:
        main_mod.run_startup_migration(db, repo_root=REPO)

    assert "拒绝带着未完成的迁移启动" in str(ei.value), str(ei.value)
    assert main_mod._db_alembic_version(db) == "0001_baseline"


def test_upgrade_silent_noop_is_also_fail_closed(tmp_path, monkeypatch, head):
    """rc=0 但版本没推进（例如 alembic 被别的东西挡掉）→ 同样 fail-closed。"""
    db = _mk_db(tmp_path / "t.db", version="0001_baseline")
    monkeypatch.setattr(main_mod, "_run_alembic_upgrade", lambda *a, **k: 0)

    with pytest.raises(RuntimeError) as ei:
        main_mod.run_startup_migration(db, repo_root=REPO)

    assert "版本仍不符" in str(ei.value), str(ei.value)


# ── 5. 无 alembic_version 表 → 不猜不迁移 ──────────────────────────────

def test_no_version_table_skips(tmp_path, monkeypatch):
    db = _mk_db(tmp_path / "t.db", with_version_table=False)
    called = []
    monkeypatch.setattr(main_mod, "_run_alembic_upgrade",
                        lambda *a, **k: called.append(a) or 0)

    res = main_mod.run_startup_migration(db, repo_root=REPO)

    assert res["action"] == "skipped_no_version_table", res
    assert not called, "没有迁移登记的库不许被自动迁移（否则会撞 0001 重复建表）"


# ── 6. 真子进程调用的命令形状 ──────────────────────────────────────────

def test_real_upgrade_command_shape(tmp_path, monkeypatch):
    db = _mk_db(tmp_path / "t.db", version="0001_baseline")
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs

        class _P:
            returncode = 0
            stdout = "INFO  [alembic.runtime.migration] Running upgrade"
            stderr = ""
        return _P()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    rc = main_mod._run_alembic_upgrade(REPO, db, "<cfg>")

    assert rc == 0
    assert captured["argv"][1:] == ["-m", "alembic", "upgrade", "head"], captured["argv"]
    assert captured["kwargs"]["cwd"] == REPO
    assert captured["kwargs"]["env"]["SYNC_HUB_DB"] == os.path.abspath(db), \
        "env.py 靠 SYNC_HUB_DB 定位目标库 —— 必须注入成功"
    assert captured["kwargs"]["env"]["SYNC_HUB_CONFIG_DIR"] == "<cfg>"


# ── 7. head 是真实存在的 revision；生产库副本走真实读路径 ────────────────

def test_head_revision_exists_in_versions_dir(head):
    vdir = os.path.join(REPO, "migrations", "alembic", "versions")
    revs = [f[:-3] for f in os.listdir(vdir) if f.endswith(".py")]
    assert head in revs, f"head {head!r} 不在 revisions 里: {sorted(revs)}"


def test_real_read_path_on_production_copy(tmp_path):
    """只读复制生产库 → 真实读路径跑到 head。不碰生产库本体（CD-070 纪律）。

    终审断点 3（2026-09-23）后 head=0013：生产库停在 0012 → 首次检查判
    upgraded（0013 对已哈希库为 no-op，只推进版本指针）；二次检查应判 up_to_date
    且不再改写库文件（「判定为最新时零改写」这条不变）。
    """
    prod = _find_production_db()
    if not prod:
        pytest.skip("找不到生产库 ./sync_hub.db（本机/CI 无该文件），跳过真实读路径用例")
    dst = str(tmp_path / "prod-copy.db")
    shutil.copy2(prod, dst)

    head = main_mod._alembic_head(REPO)
    res = main_mod.run_startup_migration(dst, repo_root=REPO)
    assert res["action"] in ("upgraded", "up_to_date"), f"生产库副本迁移异常: {res}"
    assert res["to"] == head, f"副本应到 head（{head}）: {res}"
    assert main_mod._db_alembic_version(dst) == head
    size_after = os.path.getsize(dst)

    res2 = main_mod.run_startup_migration(dst, repo_root=REPO)
    assert res2["action"] == "up_to_date", f"二次检查应判最新: {res2}"
    assert os.path.getsize(dst) == size_after, "判定为最新时不应改写库文件"


def _agents_cols(db_path):
    """读 agents 表列名集合（判 0013 兜底补列是否生效）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    finally:
        conn.close()


# ── 8. 全新库 init_db 自动登记 head（数据层修复轮）──────────────────────────
# 先红背景：init_db 建的全新库没有 alembic_version 表 → 本检查把它们归入
# skipped_no_version_table 永久跳过，此后新增 revision 永远不会作用到这些库。
# 修复：init_db 对全新库登记 alembic 版本，使「全新库」与「已迁移库」不可区分；
# 老库（已有表但无登记）不猜不动。
# 终审断点 3 修正（2026-09-23）：内联 agents DDL **有意不含** api_key_hash /
# api_key_prev_hash（CD-060 登记差异），若直接登记 head，兜底迁移 0013 永不执行
# → 新部署永久明文模式且启动迁移报 up_to_date 误导。故缺列库只登记到 head 前一版，
# 0013 由启动迁移 / `alembic upgrade head` 执行补列。
# CD-111 连带回归修复（2026-09-24，Hermes 验收期实测）：0014 落地后 head=0014，
# 「head 前一版」漂到 0013，0013 兜底补列被跳过 → agents 永久缺 hash 两列（本用例抓出）。
# 登记目标改为**最早一条内联 DDL 缺口迁移的前一版**（db._INLINE_DDL_GAP_REVISIONS），
# head 再前进也不漂。本断言由验收方按新契约同步（非执行方扩面，未放宽任何判据）。


def test_fresh_init_db_stamps_head(tmp_path, monkeypatch, head):
    import models
    db_path = str(tmp_path / "fresh.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()

    # 缺 hash 列（CD-060 内联 DDL 口径）→ 只登记到「最早缺口迁移」的前一版，
    # 留 0013 兜底补列（CD-111 连带回归修复后为固定锚，不随 head 漂移）
    prev = dbmod._alembic_down_revision(dbmod._INLINE_DDL_GAP_REVISIONS[0])
    assert prev, "最早缺口迁移必须有前一版 revision（0013 兜底迁移的前置）"
    expect = head if "api_key_hash" in _agents_cols(db_path) else prev
    assert main_mod._db_alembic_version(db_path) == expect, \
        f"全新库应登记到 {expect}（缺 hash 列时停在最早缺口迁移的前一版）"
    res = main_mod.run_startup_migration(db_path, repo_root=REPO)
    assert res["action"] == "upgraded" and res["to"] == head, \
        f"全新库应执行兜底迁移到 head（不再 skipped_no_version_table），实际: {res}"
    assert main_mod._db_alembic_version(db_path) == head
    cols = _agents_cols(db_path)
    assert {"api_key_hash", "api_key_prev_hash"} <= cols, \
        f"0013 兜底补列后 agents 应含 hash 两列，实测: {sorted(cols)}"


def test_legacy_db_without_version_table_not_stamped(tmp_path, monkeypatch):
    """老库（建库前已有业务表、无 alembic_version）不猜不登记——
    main.py 的 skipped_no_version_table 告警口径对它保持不变。"""
    import models
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()

    assert main_mod._db_alembic_version(db_path) is None, \
        "已有库不得被自动登记（否则落后老库被误判为最新，升级路径被绕过）"
    res = main_mod.run_startup_migration(db_path, repo_root=REPO)
    assert res["action"] == "skipped_no_version_table", res
