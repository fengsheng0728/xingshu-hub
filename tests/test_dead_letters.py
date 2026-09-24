# -*- coding: utf-8 -*-
"""CD-084（2026-09-23）：死信表 + 死信运维端点测试。

口径：空库走 db.init_db()（CONFIG.DB_PATH monkeypatch 到 tmp_path，同
test_schema_hard_equality 配方）；端点函数直调（不起 TestClient、不绑端口、
不 spawn Hub，同 test_departments 配方）；NO_AUTH=1 由 conftest 保证，
403 用例 monkeypatch routes_common.NO_AUTH=False 才测得到。
"""
import asyncio
import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as db_mod  # noqa: E402
import routes_common  # noqa: E402
import routes_maintenance  # noqa: E402
from models import CONFIG  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """空库：init_db 建全 schema（含 dead_letters），CONFIG.DB_PATH 指向 tmp。"""
    db_path = str(tmp_path / "dl.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


def _fetch(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class _FakeReq:
    """最小 Request 替身：require_ops_privilege 只读 .scope['principal']。"""

    def __init__(self, principal=None):
        self.scope = {"principal": principal}


# ── 落库 helper ──

def test_record_dead_letter_basic(fresh_db):
    rid = db_mod.record_dead_letter(
        "automation_job:7", "automation_alert", {"job_id": 7}, "RuntimeError: boom")
    assert isinstance(rid, int) and rid >= 1
    rows = _fetch(fresh_db, "SELECT * FROM dead_letters WHERE id=?", (rid,))
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["source"] == "automation_job:7"
    assert r["kind"] == "automation_alert"
    assert '"job_id": 7' in r["payload_json"]
    assert r["error"] == "RuntimeError: boom"
    assert r["retried"] == 0 and r["retried_at"] == ""
    assert r["failed_at"], "failed_at 应由 DB 默认 datetime('now') 落值"


def test_record_dead_letter_truncates_and_never_raises(fresh_db, tmp_path):
    # 超长字段截断（error 500 上限）
    rid = db_mod.record_dead_letter("s", "k", "p", "x" * 600)
    n = _fetch(fresh_db, "SELECT length(error) FROM dead_letters WHERE id=?", (rid,))[0][0]
    assert n == 500
    # 落库目标不可写 → 返回 None 且不抛（不阻塞主流程是硬语义）
    bad = str(tmp_path / "no-such-dir" / "x.db")
    assert db_mod.record_dead_letter("s", db_path=bad) is None


# ── 列表端点 ──

def _seed(fresh_db):
    db_mod.record_dead_letter("automation_job:1", "automation_alert", {}, "e1")
    db_mod.record_dead_letter("notify_send", "notification", {}, "e2")
    db_mod.record_dead_letter("automation_job:2", "automation_alert", {}, "e3")


def test_dead_letters_list_pagination_and_filter(fresh_db):
    _seed(fresh_db)
    lst = asyncio.run(routes_maintenance.api_dead_letters())
    assert lst["total"] == 3 and lst["pending"] == 3 and len(lst["items"]) == 3
    # id 倒序
    ids = [i["id"] for i in lst["items"]]
    assert ids == sorted(ids, reverse=True)
    # 分页
    p1 = asyncio.run(routes_maintenance.api_dead_letters(limit=2, offset=0))
    p2 = asyncio.run(routes_maintenance.api_dead_letters(limit=2, offset=2))
    assert len(p1["items"]) == 2 and len(p2["items"]) == 1
    assert p1["items"][0]["id"] != p2["items"][0]["id"]
    # source 过滤 + limit 上限收紧
    flt = asyncio.run(routes_maintenance.api_dead_letters(source="notify_send"))
    assert flt["total"] == 1 and flt["items"][0]["source"] == "notify_send"
    capped = asyncio.run(routes_maintenance.api_dead_letters(limit=99999))
    assert capped["limit"] == 500


# ── 重试端点 ──

def test_retry_marks_retried_for_plain_source(fresh_db):
    rid = db_mod.record_dead_letter("notify_send", "notification", {"to": "a"}, "SMTPError")
    res = asyncio.run(routes_maintenance.api_dead_letter_retry(
        rid, _FakeReq({"auth_mode": "hub_token"}), current_agent="mgr"))
    assert res["ok"] and res["action"] == "marked" and res["source"] == "notify_send"
    r = dict(_fetch(fresh_db, "SELECT * FROM dead_letters WHERE id=?", (rid,))[0])
    assert r["retried"] == 1 and r["retried_at"]
    # 重试后不再计入 pending
    lst = asyncio.run(routes_maintenance.api_dead_letters())
    assert lst["pending"] == 0


def test_retry_automation_job_reenables(fresh_db):
    """source=automation_job:<id> → 重置连失败计数并重新启用（对齐 toggle _reset_fail_count）。"""
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO automation_jobs (id, name, trigger_type, trigger_spec,"
        " instruction, owner_agent_id, enabled, consecutive_failures)"
        " VALUES (42, 'j', 'cron', '* * * * *', 'p', 'agent-a', 0, 5)")
    conn.commit()
    conn.close()
    rid = db_mod.record_dead_letter("automation_job:42", "automation_alert",
                                    {"job_id": 42}, "连续 5 次失败已停用")
    res = asyncio.run(routes_maintenance.api_dead_letter_retry(
        rid, _FakeReq({"auth_mode": "hub_token"}), current_agent="mgr"))
    assert res["action"] == "automation_job_reenabled"
    enabled, fails = _fetch(
        fresh_db,
        "SELECT enabled, consecutive_failures FROM automation_jobs WHERE id=42")[0]
    assert enabled == 1 and fails == 0


def test_retry_404_on_missing_id(fresh_db):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_maintenance.api_dead_letter_retry(
            9999, _FakeReq({"auth_mode": "hub_token"}), current_agent="mgr"))
    assert ei.value.status_code == 404


def test_retry_requires_ops_privilege(fresh_db, monkeypatch):
    """CD-061 重运维门：NO_AUTH 关闭时 worker 级 principal 一律 403。"""
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    rid = db_mod.record_dead_letter("notify_send", "notification", {}, "e")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_maintenance.api_dead_letter_retry(
            rid, _FakeReq({"auth_mode": "api_key"}), current_agent="worker-1"))
    assert ei.value.status_code == 403
    # 被拒后死信保持未处理
    r = dict(_fetch(fresh_db, "SELECT retried FROM dead_letters WHERE id=?", (rid,))[0])
    assert r["retried"] == 0


# ── 维护路径失败落死信（本文件名下的两处埋点） ──

def test_force_cleanup_failure_lands_dead_letter(fresh_db, monkeypatch):
    from hub_core import SyncHub  # noqa: F401

    # 测试隔离修复（2026-09-24，opencode 首轮 Hermes 收口期实测发现）：
    # 原写法 `monkeypatch.setattr(hub, "force_cleanup", _boom)` 是**实例级**——
    # pytest 记录 oldval=getattr(hub, "force_cleanup")（沿类查到的是**绑定方法**），
    # teardown 时 setattr 回实例 → 给实例**永久留下**一个实例属性，遮蔽此后所有
    # 对类属性的 monkeypatch → 后续 test_ops_gate_matrix 打桩 SyncHub.force_cleanup
    # 失效、走真实实现（计数 after=20 而非 18），**单跑不红、全量才红**。
    # 改类级 + 带 self 签名：monkeypatch 保存/恢复的都是类属性，零残留。
    async def _boom(self):
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(SyncHub, "force_cleanup", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(routes_maintenance.api_force_cleanup(
            _FakeReq({"auth_mode": "hub_token"}), current_agent="mgr"))
    rows = _fetch(fresh_db,
                  "SELECT source, kind, error FROM dead_letters"
                  " WHERE source='maintenance_cleanup'")
    assert len(rows) == 1
    assert rows[0]["kind"] == "cleanup"
    assert "cleanup exploded" in rows[0]["error"]
