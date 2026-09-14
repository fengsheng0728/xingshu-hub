# -*- coding: utf-8 -*-
"""审批权限矩阵测试（D-2 / 3-8，2026-09-10）

⚠️ 本用例固化的是 **2026-09-10 的现状**，不是应然设计：

1. n1_delete 通道：POST /api/v1/n1/reviews/{review_id}（routes_n1.py:166-196）
   有角色门（:172-173 判 role not in ("manager","orchestrator") → 403）——
   worker → 403；manager/orchestrator → 非 403。
2. wiki 通道：POST /api/v1/wiki/inbox/{inbox_id}/approve｜/reject（routes_wiki.py）
   **CD-031 已收紧（2026-09-14）**：补审批人角色门，与 n1 通道一致——
   worker → 403；manager/orchestrator（或 hub_token 控制台 principal）→ 放行。
   历史：原实现只有 get_current_agent 认证、零角色门，任意 worker 可把内容
   trust_level 由 external 升 internal（S3）；本文件曾以
   test_wiki_approve_worker_currently_allowed 固化该现状，收紧后同步改为
   worker 403 / manager 放行 / hub_token 放行 三条 + 跨通道一致性断言。

实现方式：任务书三.5 方案 (a)——直接调 handler 协程（参考 tests/test_n1_gate.py
的 _no_auth_off + FakeHub 注入模式），tmp_path 独立 sqlite 库，不起真实端口。
选 (a) 的理由：两条通道的审批人判定都发生在 handler 函数体内（n1 的角色门在
routes_n1 函数内；wiki 端点压根没有门、差异点正是"函数体内无 role 判断"），
直调协程能精确打到判定行；TestClient 走全 app 会拉起 TokenAuthMiddleware /
静态挂载等无关面，且 conftest 的 SYNC_HUB_NO_AUTH=1 会把 Depends 鉴权整体绕过，
反而测不到"角色门"这一层。
"""
import asyncio
import json
import os
import sqlite3
import sys
import types

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes_common
import routes_n1
import routes_wiki
import wiki_sync
from models import CONFIG


# ═══════════ 配方 ═══════════

class FakeHub:
    """routes_n1 的 hub 是模块级单例（真实 SyncHub）——测试注入 FakeHub。"""

    def __init__(self, db_path):
        self._db_path = db_path
        self.agents = {}
        self.events = []

    def _db(self):
        return sqlite3.connect(self._db_path)

    async def _log_event(self, *a, **k):
        self.events.append((a, k))

    async def knowledge_delete(self, entry_id):
        self.deleted_knowledge = entry_id


class _FakeRequest:
    """最小 Request stub：审批 handler 只读 request.json()"""

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _no_auth_off(monkeypatch):
    """conftest 强制 SYNC_HUB_NO_AUTH=1 → routes_n1.NO_AUTH=True 全放行；
    审批矩阵测试必须在鉴权语义下跑（NO_AUTH 会绕过角色门）。"""
    monkeypatch.setattr(routes_n1, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)   # CD-031：wiki 门运行期读它


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    """tmp_path 独立库：review_queue 插一行 pending n1_delete。"""
    db = tmp_path / "n1.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE review_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL DEFAULT 'entity',
            doc_id TEXT NOT NULL, name TEXT NOT NULL, detail TEXT DEFAULT '',
            level TEXT DEFAULT 'summary', status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'llm', created_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT, reviewed_by TEXT)"""
    )
    conn.execute(
        "INSERT INTO review_queue (item_type, doc_id, name, detail, status)"
        " VALUES ('n1_delete', 'knowledge', 'e-1', ?, 'pending')",
        (json.dumps({"endpoint": "knowledge",
                     "params": {"entry_id": "e-1"},
                     "requester": "ag-a"}, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()
    h = FakeHub(str(db))
    monkeypatch.setattr(routes_n1, "hub", h)
    return h


@pytest.fixture()
def wiki_db(tmp_path, monkeypatch):
    """tmp_path 独立库：wiki_inbox 插两行 pending、trust_level=external；
    CONFIG.DB_PATH 指向该库；wiki_sync.sync 打桩防触碰真实 wiki/ 目录。"""
    db = tmp_path / "wiki.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE wiki_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_path TEXT NOT NULL UNIQUE,
            title TEXT,
            status TEXT DEFAULT 'pending',
            source TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT,
            reviewed_by TEXT,
            trust_level TEXT NOT NULL DEFAULT 'internal')"""
    )
    conn.execute(
        "INSERT INTO wiki_inbox (id, page_path, title, status, source, trust_level)"
        " VALUES (1, 'raw/w1.md', 't1', 'pending', 'ext', 'external')"
    )
    conn.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY, role TEXT)")
    conn.execute("INSERT INTO agents (agent_id, role) VALUES ('w1', 'worker')")
    conn.execute("INSERT INTO agents (agent_id, role) VALUES ('m1', 'manager')")
    conn.execute(
        "INSERT INTO wiki_inbox (id, page_path, title, status, source, trust_level)"
        " VALUES (2, 'raw/w2.md', 't2', 'pending', 'ext', 'external')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(CONFIG, "DB_PATH", str(db))
    monkeypatch.setattr(wiki_sync, "sync", lambda *a, **k: {"stubbed": True})
    return str(db)


def _wiki_row(db_path, inbox_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT status, reviewed_by, trust_level FROM wiki_inbox WHERE id = ?",
        (inbox_id,),
    ).fetchone()
    conn.close()
    return row


# ═══════════ ① n1_delete 通道：角色门真实生效 ═══════════

def test_n1_review_worker_403(hub):
    """worker 角色 principal 调 POST /api/v1/n1/reviews/{id} → 403"""
    hub.agents["w1"] = {"agent_id": "w1", "role": "worker"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_n1.api_n1_review_decision(
            1, _FakeRequest({"decision": "approved"}), current_agent="w1"))
    assert exc.value.status_code == 403
    # 403 前置：审批项未被处理
    conn = hub._db()
    status = conn.execute(
        "SELECT status FROM review_queue WHERE id = 1").fetchone()[0]
    conn.close()
    assert status == "pending"


def test_n1_review_manager_not_403(hub):
    """manager 角色 principal 调同端点 → 非 403（approved 真执行删除）"""
    hub.agents["m1"] = {"agent_id": "m1", "role": "manager"}
    result = asyncio.run(routes_n1.api_n1_review_decision(
        1, _FakeRequest({"decision": "approved"}), current_agent="m1"))
    assert result["status"] == "approved"
    assert result["executed"]["status"] == "executed"
    assert hub.deleted_knowledge == "e-1"  # 真过了审批执行器
    conn = hub._db()
    row = conn.execute(
        "SELECT status, reviewed_by FROM review_queue WHERE id = 1").fetchone()
    conn.close()
    assert row == ("approved", "m1")


def test_n1_review_orchestrator_not_403(hub):
    """orchestrator 角色同样在角色门白名单内 → 非 403"""
    hub.agents["o1"] = {"agent_id": "o1", "role": "orchestrator"}
    result = asyncio.run(routes_n1.api_n1_review_decision(
        1, _FakeRequest({"decision": "rejected"}), current_agent="o1"))
    assert result["status"] == "rejected"


# ═══════════ ② wiki 通道：CD-031 已收紧（worker 403 / manager·hub_token 放行） ═══════════

def _P(agent_id, mode="api_key"):
    """构造 principal（等价 auth_provider.Principal.to_dict()，够 principal_is_privileged 判定）"""
    return {"auth_mode": mode, "subject_id": agent_id}


def test_wiki_approve_worker_403(wiki_db):
    """CD-031 收紧后：worker 批准 → 403，且不产生信任提升（反假绿：行仍 pending/external）"""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_wiki.api_wiki_approve(1, current_agent="w1", principal=_P("w1")))
    assert exc.value.status_code == 403
    assert _wiki_row(wiki_db, 1) == ("pending", None, "external")


def test_wiki_approve_manager_ok(wiki_db):
    """manager 角色（agents 表 role=manager）→ 放行并升 internal"""
    result = asyncio.run(routes_wiki.api_wiki_approve(2, current_agent="m1", principal=_P("m1")))
    assert result == {"ok": True, "approved": True}
    assert _wiki_row(wiki_db, 2) == ("approved", "m1", "internal")


def test_wiki_approve_hub_token_console_ok(wiki_db):
    """hub_token（控制台 wiki.html / hub_ui）视为特权 → 控制台审批流程不受收门影响"""
    result = asyncio.run(routes_wiki.api_wiki_approve(1, current_agent="", principal=_P("", mode="hub_token")))
    assert result == {"ok": True, "approved": True}


def test_wiki_reject_worker_403(wiki_db):
    """reject 与 approve 同一审批人要求（CD-031 一致性）"""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_wiki.api_wiki_reject(1, current_agent="w1", principal=_P("w1")))
    assert exc.value.status_code == 403
    assert _wiki_row(wiki_db, 1) == ("pending", None, "external")


def test_wiki_approve_still_requires_authentication(monkeypatch):
    """认证门与角色门是两层：未认证仍在 get_current_agent 401（角色门不替代认证）"""
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    monkeypatch.setattr(
        routes_common, "_auth_provider",
        lambda: types.SimpleNamespace(authenticate=lambda token, ip="": None))
    req = types.SimpleNamespace(
        headers={}, scope={"client": None}, query_params={}, path_params={})
    with pytest.raises(HTTPException) as exc:
        routes_common.get_current_agent(req)
    assert exc.value.status_code == 401


# ═══════════ ③ 跨通道一致性：同一 worker，两侧都被挡（CD-031 关闭证据） ═══════════

def test_cross_channel_worker_both_403(hub, wiki_db):
    """CD-031 关闭断言：同一 worker 在 n1 与 wiki 通道都被 403（原为『两种命运』的不一致）。
    两侧都为 403 才说明两通道审批人要求已对齐；wiki 行必须未被改动（反假绿）。"""
    hub.agents["w1"] = {"agent_id": "w1", "role": "worker"}

    n1_result = None
    try:
        asyncio.run(routes_n1.api_n1_review_decision(
            1, _FakeRequest({"decision": "approved"}), current_agent="w1"))
    except HTTPException as e:
        n1_result = e.status_code

    wiki_result = None
    try:
        asyncio.run(routes_wiki.api_wiki_approve(1, current_agent="w1", principal=_P("w1")))
    except HTTPException as e:
        wiki_result = e.status_code

    assert n1_result == 403 and wiki_result == 403, (n1_result, wiki_result)
    assert _wiki_row(wiki_db, 1) == ("pending", None, "external")

# ═══════════ ③ 跨通道差异固化：同一 worker，两种命运 ═══════════
