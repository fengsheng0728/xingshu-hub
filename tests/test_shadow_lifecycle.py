# -*- coding: utf-8 -*-
"""T31 · B 组：影子档案生命周期（删除/覆盖/归档 + 版本清理）验收测试

先红后绿：R-1~R-3 贴改动前失败原文；R-4 验幂等。
"""
import asyncio
import inspect
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import models  # noqa: E402
import db as dbmod  # noqa: E402
from data_trunk import DataTrunk  # noqa: E402
from hub_core import SyncHub  # noqa: E402
from hub_mixins.memory import MemoryMixin  # noqa: E402
from hub_mixins.outbox import OutboxConsumer  # noqa: E402
from models import MemoryEntry  # noqa: E402


class _MiniHub(MemoryMixin):
    """最小 MemoryMixin 宿主：绑定真实 DataTrunk + ShadowWriter（事件消费用）。"""

    _merge_trust = staticmethod(SyncHub._merge_trust)
    _trust_from_source = staticmethod(SyncHub._trust_from_source)

    def _shadow_mirror_sync(self, payload):
        return SyncHub._shadow_mirror_sync(self, payload)

    def _reindex_vector_sync(self, payload):
        return SyncHub._reindex_vector_sync(self, payload)

    def _shadow_delete_sync(self, payload):
        if self.data_trunk is None:
            return
        from hub_mixins.shadow.reconcile import archive_by_memory_id
        archive_by_memory_id(self.data_trunk, payload.get("memory_id"),
                             db_path=models.CONFIG.DB_PATH)

    def _shadow_archive_sync(self, payload):
        if self.data_trunk is None:
            return
        from hub_mixins.shadow.reconcile import archive_by_path
        archive_by_path(self.data_trunk, payload.get("old_path"),
                        payload.get("memory_id"), db_path=models.CONFIG.DB_PATH)

    def __init__(self, dt):
        self.agents = {}
        self.data_trunk = dt
        self._shadow = None
        if dt and getattr(dt, "enabled", False):
            from hub_mixins.shadow import ShadowWriter
            self._shadow = ShadowWriter(dt, pending_db_path="")
            self._shadow.start()
        self._chroma_collection = None
        self._memory_lock = asyncio.Lock()

    async def _ensure_embedding_model(self):
        return None

    async def _log_event(self, *args, **kwargs):
        return None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    dbmod.init_db()
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": False,
                           "wiki": False, "shared": False},
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    return {"db_path": db_path, "dt": dt, "tmp_path": tmp_path}


def _outbox_rows(db_path, event_type=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM event_outbox"
    params = ()
    if event_type:
        sql += " WHERE event_type=?"
        params = (event_type,)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]
    conn.close()
    return rows


def _consumer(db_path, hub):
    kw = {"vector_fn": hub._reindex_vector_sync}
    sig = inspect.signature(OutboxConsumer.__init__).parameters
    if "shadow_fn" in sig:
        kw["shadow_fn"] = hub._shadow_mirror_sync
    if "shadow_delete_fn" in sig:
        kw["shadow_delete_fn"] = hub._shadow_delete_sync
    if "shadow_archive_fn" in sig:
        kw["shadow_archive_fn"] = hub._shadow_archive_sync
    return OutboxConsumer(db_path, **kw)


def _drain(db_path, hub):
    _consumer(db_path, hub)._drain_once()
    if hub._shadow is not None:
        hub._shadow._drain_once()


def _store(hub, key, content, **kw):
    return asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key=key, content=content, **kw)))


# ═══════════ R-1：删记忆后其档案应移入 _trash ═══════════

def test_r1_delete_memory_moves_shadow_to_trash(env):
    hub = _MiniHub(env["dt"])
    r = _store(hub, "r1-key", "r1-content")
    mid = r["memory_id"]

    # 让 shadow_mirror 事件消费，档案落地
    _drain(env["db_path"], hub)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    branch_root = env["dt"].branch_root()
    vault_path = os.path.join(branch_root, "vault", "memory", today, f"{mid}.md")
    assert os.path.exists(vault_path), "档案应先写入 vault"

    # 删除记忆
    res = asyncio.run(hub.delete_memory("r1-key", "agent-1"))
    assert res["status"] == "deleted"

    # 消费 delete 事件
    _drain(env["db_path"], hub)

    trash_path = os.path.join(branch_root, "vault", "_trash", today,
                              "vault", "memory", today, f"{mid}.md")
    assert not os.path.exists(vault_path), \
        "删记忆后原 vault 档案应移入 _trash（改动前：档案仍在 vault/memory/... 原位）"
    assert os.path.exists(trash_path), "档案应出现在 _trash"


# ═══════════ R-2：同 id 多日期旧档归档 ═══════════

def test_r2_duplicate_date_archives_old(env):
    hub = _MiniHub(env["dt"])
    r = _store(hub, "r2-key", "r2-content")
    mid = r["memory_id"]
    _drain(env["db_path"], hub)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    branch_root = env["dt"].branch_root()

    # 模拟跨天遗留：直接在工作区旧日期路径写入文件（= CD-053③ 实测场景）
    old_vault = os.path.join(branch_root, "vault", "memory", yesterday, f"{mid}.md")
    os.makedirs(os.path.dirname(old_vault), exist_ok=True)
    with open(old_vault, "w", encoding="utf-8") as f:
        f.write("old cross-day content")

    new_vault = os.path.join(branch_root, "vault", "memory", today, f"{mid}.md")
    assert os.path.exists(old_vault), "旧日期档案应存在"
    assert os.path.exists(new_vault), "新日期档案应存在"

    # 对账（注意签名：reconcile_shadow_archives(data_trunk, db_path=None)）
    from hub_mixins.shadow import reconcile_shadow_archives
    stats = reconcile_shadow_archives(env["dt"], env["db_path"])

    trash_old = os.path.join(branch_root, "vault", "_trash", today,
                             "vault", "memory", yesterday, f"{mid}.md")

    assert not os.path.exists(old_vault), \
        "对账后旧日期档案应移入 _trash（改动前：两份都在）"
    assert os.path.exists(new_vault), "新日期档案应保留"
    assert os.path.exists(trash_old), "旧档应出现在 _trash"
    assert stats["duplicate_archived"] >= 1


# ═══════════ R-3：delete 后 memory_versions 无残留 ═══════════

def test_r3_delete_clears_memory_versions(env):
    hub = _MiniHub(env["dt"])
    r = _store(hub, "r3-key", "r3-content")
    mid = r["memory_id"]

    # 制造一个版本记录（通过覆盖）
    r2 = _store(hub, "r3-key", "r3-content-v2")
    assert r2["action"] == "conflict_overwrite"

    conn = sqlite3.connect(env["db_path"])
    rows = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id=?", (mid,)).fetchall()
    conn.close()
    assert len(rows) >= 1, "应有版本记录"

    asyncio.run(hub.delete_memory("r3-key", "agent-1"))

    conn = sqlite3.connect(env["db_path"])
    rows = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id=?", (mid,)).fetchall()
    conn.close()
    assert len(rows) == 0, \
        "delete_memory 后 memory_versions 应无该 memory_id 残留（改动前：残留）"


# ═══════════ R-4：幂等（连跑两次对账，第二次归档数为 0）═══════════

def test_r4_reconcile_idempotent(env):
    hub = _MiniHub(env["dt"])
    r = _store(hub, "r4-key", "r4-content")
    mid = r["memory_id"]
    _drain(env["db_path"], hub)

    # 删除记忆制造孤儿
    conn = sqlite3.connect(env["db_path"])
    conn.execute("DELETE FROM memory_pool WHERE memory_id=?", (mid,))
    conn.commit()
    conn.close()

    from hub_mixins.shadow import reconcile_shadow_archives
    stats1 = reconcile_shadow_archives(env["dt"], env["db_path"])
    assert stats1["orphan_archived"] == 1, "第一次应归档 1 个孤儿"

    stats2 = reconcile_shadow_archives(env["dt"], env["db_path"])
    assert stats2["orphan_archived"] == 0, \
        "幂等：第二次孤儿归档数应为 0"
    assert stats2["duplicate_archived"] == 0


# ═══════════ 补充：事件消费路径 + 归档不物理删除/目标存在跳过 ═══════════

def test_archive_not_delete_and_skip_existing(env):
    hub = _MiniHub(env["dt"])
    r = _store(hub, "r5-key", "r5-content")
    mid = r["memory_id"]
    _drain(env["db_path"], hub)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    branch_root = env["dt"].branch_root()
    vault_rel = f"vault/memory/{today}/{mid}.md"
    vault_path = os.path.join(branch_root, "vault", "memory", today, f"{mid}.md")
    assert os.path.exists(vault_path)

    # 手动调用 archive_by_path（验证归档而非物理删除）
    from hub_mixins.shadow.reconcile import archive_by_path
    res1 = archive_by_path(env["dt"], vault_rel, mid, env["db_path"])
    assert res1["archived"] == 1
    assert not os.path.exists(vault_path), "原文件应被移走（归档）"

    trash_path = os.path.join(branch_root, "vault", "_trash", today,
                              "vault", "memory", today, f"{mid}.md")
    assert os.path.exists(trash_path), "文件应出现在 _trash"

    # 再次归档应跳过（目标已存在）
    res2 = archive_by_path(env["dt"], vault_rel, mid, env["db_path"])
    assert res2["skipped"] == 1
    assert res2["archived"] == 0


def test_delete_event_consumption_path(env):
    """delete 事件被 outbox 消费，触发归档。"""
    hub = _MiniHub(env["dt"])
    r = _store(hub, "r6-key", "r6-content")
    mid = r["memory_id"]
    _drain(env["db_path"], hub)

    # 确认 outbox 中 shadow_mirror 事件已 done
    rows_before = _outbox_rows(env["db_path"], "shadow_mirror")
    assert all(r["status"] == "done" for r in rows_before)

    res = asyncio.run(hub.delete_memory("r6-key", "agent-1"))
    assert res["status"] == "deleted"

    # 基线使用独立事件类型 shadow_delete（非 shadow_mirror op=delete）
    rows_after = _outbox_rows(env["db_path"], "shadow_delete")
    assert len(rows_after) == 1, "delete 后应有一条 shadow_delete 事件"
    payload = json.loads(rows_after[0]["payload"])
    assert payload.get("memory_id") == mid

    # 消费
    _drain(env["db_path"], hub)
    rows_done = _outbox_rows(env["db_path"], "shadow_delete")
    assert all(r["status"] == "done" for r in rows_done)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    branch_root = env["dt"].branch_root()
    vault_path = os.path.join(branch_root, "vault", "memory", today, f"{mid}.md")
    assert not os.path.exists(vault_path), "事件消费后档案应被归档"
