# -*- coding: utf-8 -*-
"""T13 · CD-052 方案A：知识读出口剥离 + 读审计 负向探针（2026-09-19）

出口侧（直调 handler 协程，不起 TestClient；临时库走 db.init_db() 建全
schema 防假绿，同 CD-054 shared/memory 组测试的做法）：

E-1 GET /knowledge/{id}：低权限 worker 请求 → 只拿到前 200 字 +
    level="summary"，且返回正文不包含全文特有的尾部哨兵串
E-2 GET /knowledge（list）：worker → 每条都是摘要级；created_by == requester
    的条目 → full；hub_token / manager（principal_is_privileged 为真）→ full；
    doc: 前缀条目命中摘要级时原样返回（KB 里本就是 ≤300 字摘要）
E-3 读审计：成功路径在 gateway_read_log 各落 1 行，granted_level / stripped
    与实际一致
E-4 fail-closed 而非 fail-open：无法判定主体（principal 为 None 且无当前
    agent 角色）→ 一律摘要级，绝不给全文（含 created_by 为空的条目，
    空串 == 空串不得被误判为「自己」）
"""
import asyncio
import os
import sqlite3
import sys

from types import SimpleNamespace

import pytest  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models  # noqa: E402
import db as db_mod  # noqa: E402
import routes_common  # noqa: E402
import routes_knowledge  # noqa: E402

# 全文特有的尾部哨兵：放在 200 字之后，摘要级绝不应包含
TAIL_SENTINEL = "尾部哨兵CD052-TAIL-9d4f2b"


def _principal(auth_mode, subject_id):
    """与 auth_provider.Principal 同形状（_log_read 读 .scope/.auth_mode，
    principal_is_privileged 读 .auth_mode/.subject_id）"""
    return SimpleNamespace(auth_mode=auth_mode, subject_id=subject_id, scope=None)


def _long_content():
    return ("星枢知识库正文段落，用于验证出口剥离语义。" * 20) + TAIL_SENTINEL


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表）+ 可控角色查询"""
    db_path = str(tmp_path / "cd052_exit.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    roles = {"wkr-1": "worker", "mgr-1": "manager", "orc-1": "orchestrator"}
    monkeypatch.setattr(routes_common, "_agent_role",
                        lambda agent_id: roles.get(agent_id, ""))
    return db_path


def _insert_kb(db_path, entry_id, created_by, content, title="测试条目"):
    now = "2026-09-19T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO knowledge_base
           (entry_id, title, content, tags, links, category, importance,
            created_by, created_at, updated_at)
           VALUES (?, ?, ?, '[]', '[]', 'process', 0.8, ?, ?, ?)""",
        (entry_id, title, content, created_by, now, now))
    conn.commit()
    conn.close()


def _log_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT requester, kind, query, target, granted_level, item_count,"
        " stripped_chunks FROM gateway_read_log ORDER BY log_id")]
    conn.close()
    return rows


_WORKER = _principal("api_key", "wkr-1")
_MANAGER = _principal("api_key", "mgr-1")
_HUB_TOKEN = _principal("hub_token", "__hub__")


# ═══════════ E-1 single：worker → 摘要级 + 无尾部哨兵 ═══════════

def test_e1_single_worker_gets_summary_only(env):
    _insert_kb(env, "e-full", "orc-1", _long_content())
    result = asyncio.run(routes_knowledge.api_knowledge_get(
        "e-full", current_agent="wkr-1", principal=_WORKER))
    entry = result["entry"]
    assert entry["level"] == "summary", f"worker 应只拿到摘要级: {entry.get('level')}"
    assert len(entry["content"]) <= 203, \
        f"摘要级正文不得超 200 字(+省略号)，实际 {len(entry['content'])}"
    assert TAIL_SENTINEL not in entry["content"], \
        "摘要级正文不得包含全文尾部哨兵串"
    # 既有键不丢（不改动任何既有键）
    assert entry["entry_id"] == "e-full" and entry["title"] == "测试条目"


def test_e1_single_owner_and_privileged_get_full(env):
    _insert_kb(env, "e-own", "wkr-1", _long_content())
    # 本人（created_by == requester，r1 自己）→ full
    r1 = asyncio.run(routes_knowledge.api_knowledge_get(
        "e-own", current_agent="wkr-1", principal=_WORKER))
    assert r1["entry"]["level"] == "full"
    assert TAIL_SENTINEL in r1["entry"]["content"]
    # hub_token → full
    r2 = asyncio.run(routes_knowledge.api_knowledge_get(
        "e-own", current_agent="wkr-1", principal=_HUB_TOKEN))
    assert r2["entry"]["level"] == "full"
    assert TAIL_SENTINEL in r2["entry"]["content"]
    # manager（principal_is_privileged 为真）→ full
    r3 = asyncio.run(routes_knowledge.api_knowledge_get(
        "e-own", current_agent="mgr-1", principal=_MANAGER))
    assert r3["entry"]["level"] == "full"
    assert TAIL_SENTINEL in r3["entry"]["content"]


# ═══════════ E-2 list：按主体逐条定级 ═══════════

def test_e2_list_worker_summary_except_own(env):
    _insert_kb(env, "e-own", "wkr-1", _long_content())
    _insert_kb(env, "e-other", "orc-1", _long_content())
    doc_summary = "文档聚合摘要段。" * 30  # ~210 字，doc: 条目 KB 里本就是摘要
    _insert_kb(env, "doc:demo", "orc-1", doc_summary, title="文档聚合条目")
    result = asyncio.run(routes_knowledge.api_knowledge_list(
        current_agent="wkr-1", principal=_WORKER))
    by_id = {e["entry_id"]: e for e in result["entries"]}
    assert by_id["e-own"]["level"] == "full", "本人条目应 full"
    assert TAIL_SENTINEL in by_id["e-own"]["content"]
    assert by_id["e-other"]["level"] == "summary", "他人条目 worker 只能拿摘要级"
    assert TAIL_SENTINEL not in by_id["e-other"]["content"]
    assert by_id["doc:demo"]["level"] == "summary"
    assert by_id["doc:demo"]["content"] == doc_summary, \
        "doc: 条目命中摘要级时原样返回（本就是摘要）"


def test_e2_list_hub_token_and_manager_full(env):
    _insert_kb(env, "e-other", "orc-1", _long_content())
    r_hub = asyncio.run(routes_knowledge.api_knowledge_list(
        current_agent="wkr-1", principal=_HUB_TOKEN))
    assert all(e["level"] == "full" for e in r_hub["entries"]), \
        "hub_token 应全部 full"
    assert TAIL_SENTINEL in r_hub["entries"][0]["content"]
    r_mgr = asyncio.run(routes_knowledge.api_knowledge_list(
        current_agent="mgr-1", principal=_MANAGER))
    assert all(e["level"] == "full" for e in r_mgr["entries"]), \
        "manager 应全部 full"
    assert TAIL_SENTINEL in r_mgr["entries"][0]["content"]


# ═══════════ E-3 读审计落行 ═══════════

def test_e3_read_audit_rows(env):
    _insert_kb(env, "e-own", "wkr-1", _long_content())
    _insert_kb(env, "e-other", "orc-1", _long_content())

    before = len(_log_rows(env))
    # single 摘要级 → 1 行（granted_level=summary, stripped=1）
    asyncio.run(routes_knowledge.api_knowledge_get(
        "e-other", current_agent="wkr-1", principal=_WORKER))
    rows = _log_rows(env)
    assert len(rows) == before + 1, f"single 成功路径应恰好多 1 行: {rows}"
    r = rows[-1]
    assert r["requester"] == "wkr-1" and r["kind"] == "knowledge"
    assert r["target"] == "e-other"
    assert r["granted_level"] == "summary"
    assert r["item_count"] == 1 and r["stripped_chunks"] == 1

    # list worker → 1 行（2 条中 1 条被降级）
    asyncio.run(routes_knowledge.api_knowledge_list(
        current_agent="wkr-1", principal=_WORKER))
    r = _log_rows(env)[-1]
    assert r["kind"] == "knowledge" and r["granted_level"] == "summary"
    assert r["item_count"] == 2 and r["stripped_chunks"] == 1

    # list hub_token → 1 行（全 full，无降级）
    asyncio.run(routes_knowledge.api_knowledge_list(
        current_agent="wkr-1", principal=_HUB_TOKEN))
    r = _log_rows(env)[-1]
    assert r["granted_level"] == "full"
    assert r["item_count"] == 2 and r["stripped_chunks"] == 0


# ═══════════ E-4 主体不可判定 → fail-closed 摘要级 ═══════════

def test_e4_unknown_principal_fail_closed(env):
    _insert_kb(env, "e-x", "orc-1", _long_content())
    _insert_kb(env, "e-anon", "", _long_content(), title="无主条目")
    # principal 为 None 且无当前 agent 身份 → 一律摘要级
    r1 = asyncio.run(routes_knowledge.api_knowledge_get(
        "e-x", current_agent="", principal=None))
    assert r1["entry"]["level"] == "summary"
    assert TAIL_SENTINEL not in r1["entry"]["content"]
    # created_by 为空的条目：空串 == 空串不得被误判为「自己」给全文
    r2 = asyncio.run(routes_knowledge.api_knowledge_get(
        "e-anon", current_agent="", principal=None))
    assert r2["entry"]["level"] == "summary", \
        "created_by 与 requester 皆空不得判为本人（fail-closed）"
    assert TAIL_SENTINEL not in r2["entry"]["content"]
    r3 = asyncio.run(routes_knowledge.api_knowledge_list(
        current_agent="", principal=None))
    assert all(e["level"] == "summary" for e in r3["entries"])
    assert all(TAIL_SENTINEL not in e["content"] for e in r3["entries"])
