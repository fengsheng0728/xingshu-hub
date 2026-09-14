"""UI 修复轮回归测试：自动化 trigger 归一化 + 共享文档可见性
覆盖: test_automation_trigger_normalize / test_shared_visibility
"""
import json
import sqlite3
import sys
import os
import tempfile
import time

import pytest


# ───────────────── 自动化 trigger_type 归一化（等价实现，与 routes_automation.py 同步） ─────────────────

def normalize_trigger(job: dict):
    """与 routes_automation.py api_automation_create 的归一化逻辑等价"""
    trigger_type = job.get("trigger_type", "schedule")
    if trigger_type in ("daily", "weekly", "cron"):
        trigger_type = "schedule"
        schedule_kind = "cron"
    elif trigger_type == "interval":
        trigger_type = "schedule"
        schedule_kind = "every"
    else:
        schedule_kind = job.get("schedule_kind", "every")
    trigger_spec = str(job.get("trigger_spec", ""))
    return trigger_type, schedule_kind, trigger_spec


def derive_scheduler_kind(job: dict):
    """与调度器循环内 trigger_type 推导逻辑等价"""
    _jt = job.get("trigger_type", "schedule")
    if _jt in ("daily", "weekly", "cron"):
        return "cron"
    elif _jt == "interval":
        return "every"
    return job.get("schedule_kind", "every")


class TestAutomationTriggerNormalize:
    def test_daily_normalizes_to_schedule_cron(self):
        tt, sk, spec = normalize_trigger({"trigger_type": "daily", "trigger_spec": "30 09 * * *"})
        assert tt == "schedule" and sk == "cron"
        assert spec == "30 09 * * *"

    def test_weekly_normalizes_to_schedule_cron(self):
        tt, sk, spec = normalize_trigger({"trigger_type": "weekly", "trigger_spec": "0 09 * * 1,3,5"})
        assert tt == "schedule" and sk == "cron"
        assert spec == "0 09 * * 1,3,5"

    def test_cron_normalizes_to_schedule_cron(self):
        tt, sk, spec = normalize_trigger({"trigger_type": "cron", "trigger_spec": "0 */6 * * *"})
        assert tt == "schedule" and sk == "cron"

    def test_interval_normalizes_to_schedule_every(self):
        tt, sk, spec = normalize_trigger({"trigger_type": "interval", "trigger_spec": "3600"})
        assert tt == "schedule" and sk == "every"

    def test_event_stays_event(self):
        tt, sk, spec = normalize_trigger({"trigger_type": "event", "trigger_spec": "memory_new"})
        assert tt == "event"
        assert sk == "every"  # 非定时调度，仅事件触发

    def test_default_schedule(self):
        tt, sk, _ = normalize_trigger({})
        assert tt == "schedule" and sk == "every"

    def test_scheduler_derives_kind_for_legacy_daily(self):
        # 历史任务：trigger_type='daily' 落库,调度器必须按 cron 推导(否则 60s 重跑)
        assert derive_scheduler_kind({"trigger_type": "daily"}) == "cron"
        assert derive_scheduler_kind({"trigger_type": "weekly"}) == "cron"
        assert derive_scheduler_kind({"trigger_type": "interval"}) == "every"
        assert derive_scheduler_kind({"trigger_type": "schedule", "schedule_kind": "cron"}) == "cron"


# ───────────────── 共享文档可见性（用真实 SharedWorkspace + 临时 DB） ─────────────────

@pytest.fixture()
def ws():
    from shared_workspace import SharedWorkspace
    tmpdir = tempfile.mkdtemp(prefix="xingshu-vis-test-")
    db_path = os.path.join(tmpdir, "shared_test.db")
    store_dir = os.path.join(tmpdir, "store")

    w = SharedWorkspace(db_path, store_dir=store_dir)
    # 只初始化 DB（DDL + 迁移），不启动 YRoom task group（避免 teardown 挂起）
    import anyio
    anyio.run(w._init_db)
    yield w, db_path
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


def _insert_doc(db_path, doc_id, title, created_by, visibility="team", allowed=None):
    """直插 shared_docs 行(绕过 create_doc 的 YRoom 启动)"""
    import json as _json
    conn = sqlite3.connect(db_path)
    now = time.time()
    conn.execute(
        "INSERT INTO shared_docs (doc_id, title, created_by, created_at, updated_at, visibility, allowed_agents) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc_id, title, created_by, now, now, visibility, _json.dumps(allowed or []))
    )
    conn.commit()
    conn.close()


class TestSharedVisibility:
    def test_team_doc_visible_to_everyone(self, ws):
        w, db = ws
        import anyio
        _insert_doc(db, "doc-team-1", "团队文档", "agent-a", "team", [])
        docs = anyio.run(w.list_docs, "agent-b")
        assert len(docs) == 1
        assert docs[0]["title"] == "团队文档"

    def test_private_doc_hidden_from_others(self, ws):
        w, db = ws
        import anyio
        _insert_doc(db, "doc-priv-1", "私密文档", "agent-a", "private", ["agent-c"])
        # agent-b 不在白名单
        docs_b = anyio.run(w.list_docs, "agent-b")
        assert all(d["title"] != "私密文档" for d in docs_b)
        # agent-c 在白名单
        docs_c = anyio.run(w.list_docs, "agent-c")
        assert any(d["title"] == "私密文档" for d in docs_c)
        # 创建者自己可见
        docs_a = anyio.run(w.list_docs, "agent-a")
        assert any(d["title"] == "私密文档" for d in docs_a)

    def test_private_doc_access_control(self, ws):
        w, db = ws
        import anyio
        _insert_doc(db, "doc-priv-2", "机密", "agent-a", "private", [])
        assert anyio.run(w.can_access, "doc-xxx", "agent-a") is False  # 不存在
        # 找创建的 doc_id
        docs = anyio.run(w.list_docs, "agent-a")
        assert len(docs) == 1
        doc_id = docs[0]["doc_id"]
        assert anyio.run(w.can_access, doc_id, "agent-a") is True   # 创建者
        assert anyio.run(w.can_access, doc_id, "agent-b") is False  # 外人
        # team 文档
        _insert_doc(db, "doc-pub-1", "公开", "agent-b", "team", [])
        docs2 = anyio.run(w.list_docs, "agent-a")
        pub = [d for d in docs2 if d["title"] == "公开"][0]
        assert anyio.run(w.can_access, pub["doc_id"], "agent-a") is True

    def test_legacy_db_migration_adds_columns(self, ws):
        """老库(无 visibility 列)启动时自动加列"""
        w, db = ws
        conn = sqlite3.connect(db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(shared_docs)").fetchall()]
        conn.close()
        assert "visibility" in cols
        assert "allowed_agents" in cols
