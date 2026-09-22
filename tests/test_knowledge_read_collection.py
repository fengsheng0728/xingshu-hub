# -*- coding: utf-8 -*-
"""T19 · CD-054 knowledge 组收编 验收测试（2026-09-20）

覆盖（编号对齐任务书 T4）：
R-1 from-memories 非特权 worker → 只统计自己 owner 的记忆（先红核心）
R-2 from-memories 特权主体 → 全员统计、level=full、读审计落行
R-3 from-memories 无主体（requester 空）→ fail-closed 空 suggestions + 告警
R-4 knowledge_graph 被隐藏节点被 links 引用 → 不以 title=id 回填、edges 不带它（先红）
R-5 knowledge_graph 过滤判定抛异常 → fail-closed 隐藏节点 + logger.warning（先红）
R-6 auto-complete worker → 403 + denied 读审计行（对齐 T18 口径）
R-7 knowledge_graph 无登记属主条目（企业已发布公共区）→ 复用 r4_published_public 可见

配方（对齐 test_403_policy_matrix / test_n4_knowledge_graph 既有惯例）：
  - tmp_path 独立 sqlite 库：monkeypatch CONFIG.DB_PATH + db.init_db() 全 schema
  - hub_agent 指临时库：monkeypatch routes_knowledge.hub_agent = HubAgent(tmp)
  - 直调 handler 协程、显式传 current_agent / principal（不起真实 Hub、不绑端口）
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod  # noqa: E402
from models import CONFIG  # noqa: E402
from hub_agent import HubAgent  # noqa: E402
import routes_knowledge  # noqa: E402

# hub_token 主体：principal_is_privileged 短路 True（不触库）；
# api_key 主体：按 agents 表 role 判定（alice=worker → 非特权）
PRIV_PRINCIPAL = SimpleNamespace(auth_mode="hub_token", subject_id="op-admin",
                                 scope=None)
WORKER_PRINCIPAL = SimpleNamespace(auth_mode="api_key", subject_id="alice",
                                   scope=None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（全 schema）+ 指向临时库的 HubAgent 替换 routes_knowledge 单例"""
    monkeypatch.delenv("SYNC_HUB_LLM_API_KEY", raising=False)
    db_path = str(tmp_path / "kb_read.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    agent = HubAgent(db_path)
    monkeypatch.setattr(routes_knowledge, "hub_agent", agent)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO agents (agent_id, role) VALUES ('alice', 'worker')")
    conn.execute("INSERT INTO agents (agent_id, role) VALUES ('bob', 'worker')")
    conn.commit()
    conn.close()
    return SimpleNamespace(db_path=db_path, agent=agent)


def _insert_memory(db_path, memory_id, owner, key, content, tags):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content,"
        " summary, embedding, importance, tags, kind, confidence, source_type,"
        " disclosure_level, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, '', NULL, 1.0, ?, 'fact', 1.0, 'user', 'summary',"
        " datetime('now'), datetime('now'))",
        (memory_id, owner, key, content, json.dumps(tags, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def _insert_kb(db_path, entry_id, title, created_by, links=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO knowledge_base (entry_id, title, content, tags, links,"
        " category, importance, created_by, created_at, updated_at)"
        " VALUES (?, ?, '', '[]', ?, 'general', 0.8, ?,"
        " datetime('now'), datetime('now'))",
        (entry_id, title, json.dumps(links or []), created_by),
    )
    conn.commit()
    conn.close()


def _readlog_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT requester, kind, query, target, granted_level, item_count,"
        " stripped_chunks FROM gateway_read_log"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_graph_hub(db_path):
    """最小 hub：knowledge_graph 披露判定所需的面（对齐 test_n4 惯例）。"""
    from hub_core import SyncHub

    hub = SyncHub.__new__(SyncHub)
    hub._disclosure_policy = {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
    }
    hub.agents = {
        "alice": {"role": "worker", "department": "sales", "managed_agents": []},
        "bob": {"role": "worker", "department": "it", "managed_agents": []},
    }

    def _db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    hub._db = _db
    return hub


# ═══ R-1 from-memories 非特权 worker → 只见自己 owner（先红核心） ═══

def test_r1_from_memories_nonpriv_only_own(env):
    _insert_memory(env.db_path, "m-a1", "alice", "k1",
                   "alice 自己的客诉处理心得", tags=["客诉处理"])
    _insert_memory(env.db_path, "m-b1", "bob", "k2",
                   "bob私密话术-他人绝不可见-CD054", tags=["bob私密标签"])
    res = asyncio.run(routes_knowledge.api_knowledge_from_memories(
        limit=10, current_agent="alice", principal=WORKER_PRINCIPAL))
    assert res["status"] == "ok"
    assert res.get("level") == "summary", \
        f"非特权应标 summary 级: {res.get('level')}"
    suggestions = res.get("suggestions") or []
    srcs = {s.get("source_agent") for s in suggestions}
    assert "bob" not in srcs, f"非特权不得出现他人 owner id: {srcs}"
    blob = json.dumps(suggestions, ensure_ascii=False)
    assert "bob私密话术" not in blob and "bob私密标签" not in blob, \
        f"非特权不得看到他人记忆内容/标签: {blob[:200]}"
    own_tags = {t for s in suggestions for t in s.get("suggested_tags", [])}
    assert "客诉处理" in own_tags, f"自己的记忆应参与统计: {suggestions}"
    rows = _readlog_rows(env.db_path)
    assert any(r["target"] == "from-memories" and r["kind"] == "knowledge"
               and r["granted_level"] == "summary" and r["requester"] == "alice"
               for r in rows), f"缺 from-memories summary 读审计行: {rows}"


# ═══ R-2 from-memories 特权主体 → 全员统计 ═══

def test_r2_from_memories_privileged_all_owners(env):
    _insert_memory(env.db_path, "m-a1", "alice", "k1",
                   "alice 自己的客诉处理心得", tags=["客诉处理"])
    _insert_memory(env.db_path, "m-b1", "bob", "k2",
                   "bob私密话术-他人绝不可见-CD054", tags=["bob私密标签"])
    res = asyncio.run(routes_knowledge.api_knowledge_from_memories(
        limit=10, current_agent="op-admin", principal=PRIV_PRINCIPAL))
    assert res["status"] == "ok"
    assert res.get("level") == "full", f"特权应标 full 级: {res.get('level')}"
    tags = {t for s in res["suggestions"] for t in s.get("suggested_tags", [])}
    assert "客诉处理" in tags and "bob私密标签" in tags, \
        f"特权应见全员统计: {tags}"
    rows = _readlog_rows(env.db_path)
    assert any(r["target"] == "from-memories" and r["granted_level"] == "full"
               and r["stripped_chunks"] == 0
               for r in rows), f"缺 from-memories full 读审计行: {rows}"


# ═══ R-3 from-memories 无主体 → fail-closed 空 ═══

def test_r3_from_memories_no_subject_fail_closed(env, caplog):
    _insert_memory(env.db_path, "m-b1", "bob", "k2",
                   "bob私密话术-他人绝不可见-CD054", tags=["bob私密标签"])
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(routes_knowledge.api_knowledge_from_memories(
            limit=10, current_agent="", principal=None))
    assert res.get("suggestions") == [], \
        f"无主体必须 fail-closed 返回空 suggestions: {res}"
    assert any(r.levelno >= logging.WARNING for r in caplog.records), \
        "无主体 fail-closed 降级必须 logger.warning（不许静默）"


# ═══ R-4 knowledge_graph 回填节点不泄露（先红） ═══

def test_r4_graph_backfill_hidden_node_not_leaked(env, monkeypatch):
    """CD-065（2026-09-20 用户拍板）后已发布条目默认全员可见，改用
    published_public=False 关掉 r4 链尾提升来构造「被隐藏节点」，
    回填过滤回归断言本身逐字保留并加严（隐藏节点连 id 都不得出现在边端点）。"""
    _insert_kb(env.db_path, "e-vis", "Alice 可见条目", "alice", links=["e-hid"])
    _insert_kb(env.db_path, "e-hid", "Bob 保密条目", "bob")
    hub = _make_graph_hub(env.db_path)
    hub._disclosure_policy["published_public"] = False  # CD-065: 构造隐藏节点
    monkeypatch.setattr(routes_knowledge, "hub", hub)
    res = asyncio.run(routes_knowledge.api_knowledge_graph(
        current_agent="alice", principal=WORKER_PRINCIPAL))
    ids = {n["id"] for n in res["nodes"]}
    assert "e-vis" in ids, f"自己的节点应可见: {ids}"
    assert "e-hid" not in ids, f"被隐藏节点不得以 title=id 回填: {ids}"
    for e in res["edges"]:
        assert e["source"] in ids and e["target"] in ids, \
            f"edges 两端必须均过可见性判定: {e}"
    assert res["hidden"] >= 1, f"加严：被隐藏节点必须计入 hidden: {res['hidden']}"
    rows = _readlog_rows(env.db_path)
    assert any(r["target"] == "graph" and r["kind"] == "knowledge"
               and r["granted_level"] == "summary" and r["requester"] == "alice"
               and r["stripped_chunks"] >= 1
               for r in rows), f"缺 graph 读审计行（含隐藏计数）: {rows}"


# ═══ R-5 knowledge_graph 过滤异常 → 节点隐藏（先红） ═══

def test_r5_graph_filter_exception_fail_closed(env, monkeypatch, caplog):
    _insert_kb(env.db_path, "e-a", "Alice 条目", "alice")
    _insert_kb(env.db_path, "e-b", "Bob 条目", "bob")
    hub = _make_graph_hub(env.db_path)
    monkeypatch.setattr(routes_knowledge, "hub", hub)
    import disclosure

    def _boom(self, *args, **kwargs):
        raise RuntimeError("injected disclosure failure")

    monkeypatch.setattr(disclosure.DisclosureEngine,
                        "_calculate_disclosure_level", _boom)
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(routes_knowledge.api_knowledge_graph(
            current_agent="alice", principal=WORKER_PRINCIPAL))
    ids = {n["id"] for n in res["nodes"]}
    assert "e-b" not in ids, f"过滤异常必须 fail-closed 隐藏: {ids}"
    assert "e-a" not in ids, f"fail-closed 下自己节点同样隐藏: {ids}"
    assert any("e-b" in r.getMessage() and r.levelno >= logging.WARNING
               for r in caplog.records), \
        "过滤异常必须 logger.warning 且含 entry_id（不许静默）"


# ═══ R-6 auto-complete worker → 403 + denied 读审计行 ═══

def test_r6_auto_complete_worker_403_denied_log(env, monkeypatch):
    monkeypatch.setattr(routes_knowledge, "NO_AUTH", False)
    monkeypatch.setattr(routes_knowledge, "hub",
                        SimpleNamespace(agents={"alice": {"role": "worker"}}))

    async def main():
        with pytest.raises(HTTPException) as exc_info:
            await routes_knowledge.api_knowledge_auto_complete(
                {"title": "任意标题"}, current_agent="alice",
                principal=WORKER_PRINCIPAL)
        assert exc_info.value.status_code == 403, \
            f"worker 必须 403: {exc_info.value.status_code}"

    asyncio.run(main())
    rows = _readlog_rows(env.db_path)
    assert any(r["granted_level"] == "denied" and r["kind"] == "knowledge"
               and r["target"] == "auto-complete" and r["requester"] == "alice"
               and r["item_count"] == 0
               for r in rows), f"缺 auto-complete denied 读审计行: {rows}"


# ═══ R-7 knowledge_graph 已发布公共条目可见（r4 复用） ═══

def test_r7_graph_published_public_entry_visible(env, monkeypatch):
    """无登记属主（created_by 空 = 企业已发布公共内容）→ 复用 r4_published_public
    链尾提升对全员可见。语义随 CD-065 变更（2026-09-20 用户拍板）：有登记属主的
    条目同样按已发布对齐可见（原断言「登记属主条目保持 N4 语义隐藏」翻转并加严）。"""
    _insert_kb(env.db_path, "e-pub", "企业公共知识", "")
    _insert_kb(env.db_path, "e-hid", "Bob 保密条目", "bob")
    hub = _make_graph_hub(env.db_path)
    monkeypatch.setattr(routes_knowledge, "hub", hub)
    res = asyncio.run(routes_knowledge.api_knowledge_graph(
        current_agent="alice", principal=WORKER_PRINCIPAL))
    ids = {n["id"] for n in res["nodes"]}
    assert "e-pub" in ids, f"已发布公共条目应对全员可见（r4 提升）: {ids}"
    assert "e-hid" in ids, f"登记属主条目同为已发布内容，按 CD-065 对齐可见: {ids}"
    assert ids == {"e-pub", "e-hid"}, f"加严：节点全集应为两条已发布条目: {ids}"
    assert res["hidden"] == 0, f"加严：已发布条目不应有隐藏计数: {res['hidden']}"
    for n in res["nodes"]:
        assert set(n) == {"id", "title", "category", "importance", "tags"}, \
            f"加严：图谱节点只许 metadata 键（正文永不进图谱）: {sorted(n)}"
