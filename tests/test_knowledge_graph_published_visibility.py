# -*- coding: utf-8 -*-
"""T21 · CD-065 知识图谱「已发布可见性」对齐 + 标签机密词守卫 验收测试（2026-09-20，用户拍板）

冻结口径：
1. 已发布条目（知识库条目 = 已发布内容，读出口 disclosure._knowledge_hit 无条件
   published=True）在图谱层全员可见 id/标题/标签（METADATA 级），正文永不进图谱；
2. 标签守卫：标签进图谱前逐个过 sensitivity._load_secret_keywords() 机密词库
   与 scan_pii()，命中即该节点对非特权主体 tags 置空（只留 id/标题）+
   logger.info（含 entry_id，不含标签内容）；特权主体（manager/orchestrator）不受限；
3. T19 三修的 fail-closed（过滤异常隐藏 + 告警）与回填/edges 过滤逐字保留。

覆盖（V-1/V-2 为先红用例）：
V-1 有登记属主（manager）的已发布条目 → 非特权 worker 请求图谱仍可见（改动前被 N4 隐藏）
V-2 标签命中机密词库真实词 → 非特权下节点 tags 置空、id/title 保留（改动前原样返回）
V-3 特权主体对照 → 命中机密词的标签原样可见
V-4 fail-closed 回归 → 披露判定异常注入 → 节点隐藏 + logger.warning
V-5 回填过滤回归 → 被隐藏节点不因 links 回填、edges 不带它
V-6 标签命中 PII（手机号形态）→ 非特权下 tags 置空
V-7 无 requester 内部调用 → 保持旧行为（全量 + 标签原样）
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 取 sensitivity 机密词库里的真实词作为命中样本（禁止自建词库）
from sensitivity import _load_secret_keywords  # noqa: E402

_SECRET_WORD = next(w for w in _load_secret_keywords() if w and w.isalpha())


def _make_hub(db_path, policy_overrides=None):
    """构造最小 hub（agents + knowledge_base + 披露引擎可用的 context，对齐 test_n4 惯例）。"""
    from hub_core import SyncHub

    hub = SyncHub.__new__(SyncHub)
    hub._disclosure_policy = {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
    }
    if policy_overrides:
        hub._disclosure_policy.update(policy_overrides)
    # agents: alice/bob(worker)，manager1(manager)
    hub.agents = {
        "alice": {"role": "worker", "department": "sales", "managed_agents": []},
        "bob": {"role": "worker", "department": "it", "managed_agents": []},
        "manager1": {"role": "manager", "managed_agents": ["alice"], "department": "sales"},
    }

    def _db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    hub._db = _db
    return hub


@pytest.fixture()
def graph_env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="cd065-")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE knowledge_base (
            entry_id TEXT PRIMARY KEY, title TEXT, content TEXT, tags TEXT,
            links TEXT, category TEXT, importance REAL, created_by TEXT,
            created_at TEXT, updated_at TEXT, embedding BLOB)"""
    )
    conn.commit()
    conn.close()
    # D-11: 迁移后读路径经 db_facade（运行时读 CONFIG.DB_PATH），把门面指向本测试临时库
    import models as _models
    monkeypatch.setattr(_models.CONFIG, "DB_PATH", db)
    yield db
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _insert_kb(db, entry_id, title, created_by, tags=None, links=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO knowledge_base (entry_id, title, content, tags, links,"
        " category, importance, created_by) VALUES (?, ?, '正文永不进图谱', ?, ?,"
        " 'general', 0.8, ?)",
        (entry_id, title, json.dumps(tags or [], ensure_ascii=False),
         json.dumps(links or [], ensure_ascii=False), created_by),
    )
    conn.commit()
    conn.close()


# ═══ V-1 有登记属主的已发布条目 → 非特权 worker 可见（先红） ═══

def test_v1_published_entry_with_registered_owner_visible(graph_env):
    """CD-065 口径 2a：知识库条目 = 已发布内容，图谱层 METADATA 级全员可见。
    e-mgr 属主 manager1 已登记；alice（跨部门 worker，与 manager1 无主管反向关系）
    改动前走 N4 判 NONE 被隐藏 → 本用例改动前必红。"""
    _insert_kb(graph_env, "e-mgr", "经理的已发布条目", "manager1", tags=["复盘"])
    _insert_kb(graph_env, "e-bob", "Bob 的已发布条目", "bob", tags=["运维"])
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="alice"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-mgr" in ids, f"有登记属主的已发布条目应对任何认证主体可见: {ids}"
    assert "e-bob" in ids, f"跨 agent 已发布条目同样可见: {ids}"
    assert r["hidden"] == 0, f"已发布条目不应有隐藏计数: {r['hidden']}"
    node = next(n for n in r["nodes"] if n["id"] == "e-mgr")
    assert node["title"] == "经理的已发布条目" and node["tags"] == ["复盘"], \
        f"可见节点应带 id/标题/标签: {node}"
    assert "content" not in node, "正文永不进图谱"


# ═══ V-2 标签命中机密词 → 非特权 tags 置空（先红） ═══

def test_v2_secret_word_tag_stripped_for_nonpriv(graph_env, caplog):
    """CD-065 口径 2c：标签过机密词扫描，命中即非特权下 tags 置空（只留 id/title），
    logger.info 含 entry_id 且不含标签内容。改动前 tags 原样返回 → 本用例必红。"""
    leaked_tag = f"客户A-{_SECRET_WORD}-续约谈判"
    _insert_kb(graph_env, "e-secret", "含机密标签条目", "alice",
               tags=[leaked_tag, "干净标签"])
    hub = _make_hub(graph_env)
    with caplog.at_level(logging.INFO):
        r = asyncio.run(hub.knowledge_graph(requester="bob"))
    node = next((n for n in r["nodes"] if n["id"] == "e-secret"), None)
    assert node is not None, "命中机密词只摘标签，不隐藏节点本身"
    assert node["tags"] == [], f"非特权下命中机密词的节点 tags 必须置空: {node['tags']}"
    assert node["id"] == "e-secret" and node["title"] == "含机密标签条目", \
        f"tags 置空后 id/标题必须保留: {node}"
    guard_logs = [rec for rec in caplog.records
                  if "e-secret" in rec.getMessage() and rec.levelno == logging.INFO]
    assert guard_logs, "标签守卫触发必须 logger.info（含 entry_id，不许静默）"
    for rec in guard_logs:
        assert _SECRET_WORD not in rec.getMessage() and "客户A" not in rec.getMessage(), \
            f"守卫日志不得含标签内容: {rec.getMessage()}"


# ═══ V-3 特权主体对照 → 原标签可见 ═══

def test_v3_privileged_sees_original_tags(graph_env):
    """特权主体（manager）不受标签守卫限制：命中机密词的标签原样返回。"""
    leaked_tag = f"客户A-{_SECRET_WORD}-续约谈判"
    _insert_kb(graph_env, "e-secret", "含机密标签条目", "alice", tags=[leaked_tag])
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="manager1"))
    node = next(n for n in r["nodes"] if n["id"] == "e-secret")
    assert node["tags"] == [leaked_tag], \
        f"特权主体应看到原标签（不受限）: {node['tags']}"


# ═══ V-4 fail-closed 回归：披露判定异常 → 节点隐藏 ═══

def test_v4_filter_exception_fail_closed(graph_env, monkeypatch, caplog):
    """T19 三修 a 逐字保留：过滤判定抛异常 → fail-closed 隐藏节点 + logger.warning。"""
    _insert_kb(graph_env, "e-a", "Alice 条目", "alice")
    _insert_kb(graph_env, "e-b", "Bob 条目", "bob")
    hub = _make_hub(graph_env)
    import disclosure

    def _boom(self, *args, **kwargs):
        raise RuntimeError("injected disclosure failure")

    monkeypatch.setattr(disclosure.DisclosureEngine,
                        "_calculate_disclosure_level", _boom)
    with caplog.at_level(logging.WARNING):
        r = asyncio.run(hub.knowledge_graph(requester="alice"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-b" not in ids, f"过滤异常必须 fail-closed 隐藏: {ids}"
    assert "e-a" not in ids, f"fail-closed 下自己节点同样隐藏: {ids}"
    assert any("e-b" in rec.getMessage() and rec.levelno >= logging.WARNING
               for rec in caplog.records), \
        "过滤异常必须 logger.warning 且含 entry_id（不许静默）"


# ═══ V-5 回填过滤回归：被隐藏节点不因 links 回填 ═══

def test_v5_hidden_node_not_backfilled_via_links(graph_env):
    """T19 三修 b 逐字保留：被隐藏节点不以 title=id 回填、edges 两端均须可见。
    新口径下已发布条目默认可见，用 published_public=False 关掉 r4 链尾提升
    构造出「被隐藏节点」来回归回填过滤本身。"""
    _insert_kb(graph_env, "e-vis", "Alice 可见条目", "alice", links=["e-hid"])
    _insert_kb(graph_env, "e-hid", "Bob 条目", "bob")
    hub = _make_hub(graph_env, policy_overrides={"published_public": False})
    r = asyncio.run(hub.knowledge_graph(requester="alice"))
    ids = {n["id"] for n in r["nodes"]}
    assert "e-vis" in ids, f"自己的节点应可见: {ids}"
    assert "e-hid" not in ids, f"被隐藏节点不得以 title=id 回填: {ids}"
    for e in r["edges"]:
        assert e["source"] in ids and e["target"] in ids, \
            f"edges 两端必须均过可见性判定: {e}"


# ═══ V-6 标签命中 PII（手机号）→ 非特权 tags 置空 ═══

def test_v6_pii_tag_stripped_for_nonpriv(graph_env):
    """口径 2c 的 scan_pii 分支：标签含手机号形态 → 非特权下 tags 置空。
    注：RE_PHONE 依赖 \b 词界，手机号与汉字直接粘连时 sensitivity 现实现不命中
    （sensitivity.py 不在本任务白名单，如实按现口径构造样本）。"""
    _insert_kb(graph_env, "e-pii", "含 PII 标签条目", "alice",
               tags=["续约联系人 13800138000", "干净标签"])
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph(requester="bob"))
    node = next(n for n in r["nodes"] if n["id"] == "e-pii")
    assert node["tags"] == [], f"命中 PII 的节点 tags 必须置空: {node['tags']}"
    assert node["title"] == "含 PII 标签条目", "id/标题保留"


# ═══ V-7 无 requester 内部调用 → 旧行为保持 ═══

def test_v7_no_requester_full_dump_unchanged(graph_env):
    """无 requester 的内部调用保持 T19 旧行为：全量节点 + 悬空回填 + 标签原样
    （标签守卫只约束对外的非特权主体路径）。"""
    _insert_kb(graph_env, "e-secret", "含机密标签条目", "alice",
               tags=[f"客户A-{_SECRET_WORD}"])
    _insert_kb(graph_env, "e-orphan-link", "引用悬空节点", "bob",
               links=["e-ghost"])
    hub = _make_hub(graph_env)
    r = asyncio.run(hub.knowledge_graph())
    ids = {n["id"] for n in r["nodes"]}
    assert {"e-secret", "e-orphan-link", "e-ghost"} <= ids, \
        f"无过滤应全量（含悬空回填）: {ids}"
    node = next(n for n in r["nodes"] if n["id"] == "e-secret")
    assert node["tags"] == [f"客户A-{_SECRET_WORD}"], \
        f"内部调用标签原样（守卫不拦无主体路径）: {node['tags']}"
