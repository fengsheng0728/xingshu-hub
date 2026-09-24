# -*- coding: utf-8 -*-
"""CD-085(a)：GET /api/v1/activity 活动流统一读面（只读）。

配方（同 test_cd106_anchor_verify_endpoint.py：不起真实 Hub、不绑端口）：
  - tmp 库（monkeypatch CONFIG.DB_PATH + db.init_db()，含 events/gateway_read_log/
    disclosure_log/wiki_inbox 四源表）+ 手工插入各来源样本行
  - 直调 handler 协程 + 显式传 current_agent；principal 走 _FakeRequest.scope
  - 权限三态用例 monkeypatch routes_common.NO_AUTH=False（打中 403 门，
    否则 conftest 的 SYNC_HUB_NO_AUTH=1 会让门恒放行）
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as db_mod  # noqa: E402
import routes_common  # noqa: E402
import routes_activity  # noqa: E402
from models import CONFIG  # noqa: E402

W1 = {"auth_mode": "api_key", "subject_id": "w1"}
W2 = {"auth_mode": "api_key", "subject_id": "w2"}
HUB_TOKEN = {"auth_mode": "hub_token", "subject_id": "__hub__"}


class _FakeRequest:
    """最小 Request 替身：权限判定只读 .scope['principal']。"""

    def __init__(self, principal=None):
        self.scope = {}
        if principal is not None:
            self.scope["principal"] = principal
        self.query_params = {}
        self.path_params = {}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立库 + 四源表齐全 + NO_AUTH 关闭（打中权限门）。"""
    db_path = str(tmp_path / "cd085.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    conn = sqlite3.connect(db_path)
    for aid, role in (("w1", "worker"), ("w2", "worker"), ("ops-bot", "worker")):
        conn.execute(
            "INSERT INTO agents (agent_id, agent_name, role, status)"
            " VALUES (?, ?, ?, 'offline')", (aid, aid, role))
    conn.commit()
    conn.close()
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    return db_path


def _insert_event(db_path, event_type="memory_store", agent_id="w1", payload=None,
                  ts="2026-09-24T10:00:00+00:00"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_type, agent_id, payload, timestamp)"
        " VALUES (?, ?, ?, ?)",
        (event_type, agent_id, json.dumps(payload or {}, ensure_ascii=False), ts))
    conn.commit()
    conn.close()


def _insert_read(db_path, requester="w1", kind="memory", query="q", target="",
                 granted_level="summary", item_count=1, stripped=0,
                 created_at="2026-09-24 10:00:01"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO gateway_read_log (requester, auth_mode, scope_json, kind, query,"
        " target, granted_level, item_count, stripped_chunks, created_at)"
        " VALUES (?, 'api_key', '{}', ?, ?, ?, ?, ?, ?, ?)",
        (requester, kind, query, target, granted_level, item_count, stripped, created_at))
    conn.commit()
    conn.close()


def _insert_disclosure(db_path, from_agent="w1", to_agent="w2", level="summary",
                       disclosed_at="2026-09-24T10:00:02+00:00"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO disclosure_log (task_id, from_agent_id, to_agent_id, memory_id,"
        " disclosed_level, disclosed_content, disclosed_at, reason, trace_id)"
        " VALUES ('t1', ?, ?, 'm1', ?, 'c', ?, 'r1', '')",
        (from_agent, to_agent, level, disclosed_at))
    conn.commit()
    conn.close()


def _insert_wiki(db_path, page_path="concepts/a.md", status="pending",
                 created_at="2026-09-24 10:00:03", reviewed_by=""):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO wiki_inbox (page_path, title, status, source, created_at,"
        " reviewed_at, reviewed_by) VALUES (?, 'T', ?, 'auto', ?, '', ?)",
        (page_path, status, created_at, reviewed_by))
    conn.commit()
    conn.close()


def _call(principal=W1, current_agent="w1", **kw):
    return asyncio.run(routes_activity.api_activity_feed(
        _FakeRequest(principal), current_agent=current_agent, **kw))


# ═══════════ 1. 默认只查 events ═══════════

def test_default_source_events_only(env):
    """不带 source 时只回 events 行（read 2 行不混入），sources == ["events"]。"""
    _insert_event(env, ts="2026-09-24T10:00:00+00:00")
    _insert_event(env, ts="2026-09-24T10:00:01+00:00")
    _insert_event(env, ts="2026-09-24T10:00:02+00:00")
    _insert_read(env, query="q1", created_at="2026-09-24 10:00:03")
    _insert_read(env, query="q2", created_at="2026-09-24 10:00:04")
    resp = _call()
    assert resp["status"] == "ok"
    assert resp["sources"] == ["events"], resp["sources"]
    assert len(resp["items"]) == 3, f"默认应只回 3 行 events，实际 {len(resp['items'])}"
    assert all(it["source"] == "events" for it in resp["items"]), resp["items"]
    assert resp["total"] == 3
    assert resp["returned"] == 3
    assert resp["has_more"] is False


# ═══════════ 2. 多来源归并 + 时间倒序 ═══════════

def test_multi_source_merge_sorted_desc(env):
    """source=events,read → 两源按 at 倒序混排（断言完整顺序，不只是条数）。"""
    _insert_event(env, ts="2026-09-24T10:00:00+00:00")          # e1
    _insert_read(env, query="r1", created_at="2026-09-24 10:00:01")  # r1
    _insert_event(env, ts="2026-09-24T10:00:02+00:00")          # e2
    _insert_read(env, query="r2", created_at="2026-09-24 10:00:03")  # r2
    resp = _call(source="events,read")
    got = [(it["source"], it["at"], it.get("event_type")) for it in resp["items"]]
    expect_order = [
        ("read", "2026-09-24T10:00:03+00:00", "read.memory"),
        ("events", "2026-09-24T10:00:02+00:00", "memory_store"),
        ("read", "2026-09-24T10:00:01+00:00", "read.memory"),
        ("events", "2026-09-24T10:00:00+00:00", "memory_store"),
    ]
    assert got == expect_order, f"归并顺序不符：{got}"
    assert resp["sources"] == ["events", "read"]
    assert resp["total"] == 4


# ═══════════ 3. 分页 ═══════════

def test_pagination_limit_offset(env):
    """limit=2&offset=2 → returned==2、total 仍为全量、has_more 正确。"""
    for i in range(5):
        _insert_event(env, ts=f"2026-09-24T10:00:0{i}+00:00")
    resp = _call(limit=2, offset=2)
    assert resp["returned"] == 2, resp
    assert resp["total"] == 5, f"total 应为过滤后全量 5，实际 {resp['total']}"
    assert len(resp["items"]) == 2
    assert resp["has_more"] is True, "offset=2 limit=2 total=5 → 还有第 5 行，has_more 应为 True"
    assert resp["items"][0]["at"] == "2026-09-24T10:00:02+00:00"
    assert resp["items"][1]["at"] == "2026-09-24T10:00:01+00:00"
    resp2 = _call(limit=2, offset=4)
    assert resp2["returned"] == 1
    assert resp2["has_more"] is False
    limit_clamped = _call(limit=999)
    assert limit_clamped["filters"]["limit"] == 200, "超限应钳到 200 不报错"


# ═══════════ 4. event_type 前缀过滤 ═══════════

def test_event_type_prefix_filter(env):
    """event_type=ops. 只回 ops.trigger 行（ops_trigger 归一后前缀命中）。"""
    _insert_event(env, event_type="ops_trigger", agent_id="ops-bot",
                  payload={"endpoint": "/api/v1/maintenance/cleanup", "requester": "ops-bot"},
                  ts="2026-09-24T10:00:00+00:00")
    _insert_event(env, event_type="memory_store", agent_id="ops-bot",
                  ts="2026-09-24T10:00:01+00:00")
    _insert_event(env, event_type="ops_trigger", agent_id="ops-bot",
                  payload={"endpoint": "/api/v1/embeddings/rebuild", "requester": "ops-bot"},
                  ts="2026-09-24T10:00:02+00:00")
    resp = _call(current_agent="ops-bot", principal=HUB_TOKEN, event_type="ops.")
    assert resp["total"] == 2, resp
    assert all(it["event_type"] == "ops.trigger" for it in resp["items"]), resp["items"]
    assert {it["summary"] for it in resp["items"]} == {
        "/api/v1/maintenance/cleanup by ops-bot",
        "/api/v1/embeddings/rebuild by ops-bot",
    }
    resp_other = _call(current_agent="ops-bot", principal=HUB_TOKEN, event_type="memory.")
    assert resp_other["total"] == 0, resp_other


# ═══════════ 5. payload 打码 ═══════════

def test_payload_masked(env):
    """payload 含 api_key/nested.token → 返回体两处均 ***，明文不进整个响应 JSON。"""
    secret_payload = {"api_key": "sk-xxx", "nested": {"token": "t"},
                      "note": "hello", "token_extra_secret": "tok-orig-987"}
    _insert_event(env, payload=secret_payload, ts="2026-09-24T10:00:00+00:00")
    resp = _call()
    assert resp["returned"] == 1, resp
    payload = resp["items"][0]["payload"]
    assert payload["api_key"] == "***", payload
    assert payload["nested"]["token"] == "***", payload
    assert payload["note"] == "hello"
    assert payload["token_extra_secret"] == "***", payload
    dumped = json.dumps(resp, ensure_ascii=False)
    assert "sk-xxx" not in dumped, "api_key 明文泄漏进响应 JSON"
    assert "tok-orig-987" not in dumped, "token 明文泄漏进响应 JSON"
    # spec 里的短值 "t" 无法在整段 JSON 里做全串断言（单字符必然出现），
    # 按值级断言：整个响应里不得再有任何位置保留该明文值
    def _walk_leaks(node):
        if isinstance(node, dict):
            return any(_walk_leaks(v) for v in node.values())
        if isinstance(node, list):
            return any(_walk_leaks(v) for v in node)
        return node == "t" or node == "sk-xxx"
    assert not _walk_leaks(resp), f"响应中仍存在明文敏感值: {payload}"


# ═══════════ 6. 权限三态 ═══════════

def test_permission_three_states(env):
    """本人可见；他人 agent_id + 非 hub_token → 403；他人 + hub_token → 可见。"""
    _insert_event(env, agent_id="w1", ts="2026-09-24T10:00:00+00:00",
                  payload={"who": "w1"})
    _insert_event(env, agent_id="w2", ts="2026-09-24T10:00:01+00:00",
                  payload={"who": "w2"})
    # ① 本人：不带 agent_id → 只回本人
    resp = _call(principal=W1, current_agent="w1")
    assert resp["total"] == 1, resp
    assert resp["items"][0]["agent_id"] == "w1"
    # ①b 本人：带自己的 agent_id → 放行
    resp_self = _call(principal=W1, current_agent="w1", agent_id="w1")
    assert resp_self["total"] == 1, resp_self
    # ② 他人 + 非 hub_token → 403
    with pytest.raises(HTTPException) as ei:
        _call(principal=W1, current_agent="w1", agent_id="w2")
    assert ei.value.status_code == 403, f"跨主体非 hub_token 应 403，实际 {ei.value.status_code}"
    # ③ 他人 + hub_token → 放行且只见对方
    resp_hub = _call(principal=HUB_TOKEN, current_agent="w1", agent_id="w2")
    assert resp_hub["total"] == 1, resp_hub
    assert resp_hub["items"][0]["agent_id"] == "w2"


# ═══════════ 7. 来源不可用不 500 ═══════════

def test_unavailable_source_not_500(env):
    """drop gateway_read_log 后 source=events,read 仍 200：read 空 + filters 标注不可用。"""
    _insert_event(env, ts="2026-09-24T10:00:00+00:00")
    _insert_read(env, query="q1", created_at="2026-09-24 10:00:01")
    conn = sqlite3.connect(env)
    conn.execute("DROP TABLE gateway_read_log")
    conn.commit()
    conn.close()
    resp = _call(source="events,read")
    assert resp["status"] == "ok", resp
    assert all(it["source"] == "events" for it in resp["items"]), resp["items"]
    assert resp["total"] == 1, resp
    assert "read" in resp["filters"].get("unavailable_sources", []), \
        f"filters 应标注 read 不可用：{resp['filters']}"


# ═══════════ 8. 越权面：summary 不得整段带出超限内容 ═══════════

def test_summary_truncates_long_query(env):
    """read 行 query 超长 → summary 只截断（≤200 字），超限明文不整段进响应。"""
    long_query = "SENSITIVE-LONG-" + ("x" * 500)
    assert len(long_query) > 200
    _insert_read(env, query=long_query, created_at="2026-09-24 10:00:00")
    resp = _call(source="read")
    assert resp["returned"] == 1, resp
    summary = resp["items"][0]["summary"]
    assert len(summary) <= 200, f"summary 超 200 字：len={len(summary)}"
    assert long_query not in summary, "summary 整段带出超限 query"
    dumped = json.dumps(resp, ensure_ascii=False)
    assert long_query not in dumped, "超限 query 整段出现在响应 JSON"
    assert summary.startswith("memory SENSITIVE-LONG-"), summary[:40]


# ═══════════ 9. 补充：disclosure / wiki 归一规则 ═══════════

def test_disclosure_and_wiki_normalization(env):
    """disclosure.<disclosed_level> / wiki.<status> 归一 + 主体收口（from_agent_id / reviewed_by）。"""
    _insert_disclosure(env, from_agent="w1", to_agent="w2", level="summary",
                       disclosed_at="2026-09-24T10:00:02+00:00")
    _insert_wiki(env, page_path="concepts/a.md", status="approved",
                 created_at="2026-09-24 10:00:03", reviewed_by="w1")
    resp = _call(source="disclosure,wiki")
    assert resp["total"] == 2, resp
    wiki_it, disc_it = resp["items"][0], resp["items"][1]
    assert disc_it["event_type"] == "disclosure.summary", disc_it
    assert disc_it["agent_id"] == "w1"
    assert disc_it["summary"] == "w1 → w2 m1（summary）", disc_it["summary"]
    assert disc_it["at"] == "2026-09-24T10:00:02+00:00"
    assert wiki_it["event_type"] == "wiki.approved", wiki_it
    assert wiki_it["agent_id"] == "w1"
    assert wiki_it["summary"] == "concepts/a.md", wiki_it["summary"]
    assert wiki_it["at"] == "2026-09-24T10:00:03+00:00"
    # 主体收口：wiki 行 reviewed_by=w2 时不进 w1 流
    _insert_wiki(env, page_path="concepts/b.md", status="pending",
                 created_at="2026-09-24 10:00:04", reviewed_by="w2")
    resp2 = _call(source="disclosure,wiki")
    assert resp2["total"] == 2, resp2
