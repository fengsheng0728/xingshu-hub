# -*- coding: utf-8 -*-
"""tests/test_backfeed_b1.py — 阶段4-B1：GitRepo remove/move + review_queue backfeed 消费器骨架

对齐 docs/phase4-backfeed-design.md §6-B1 验收：
① move_file 后 read_at(HEAD~1) 旧路径可读   ② remove_file 后 read_at 历史可读
③ 失败返回 False（monkeypatch git 命令失败）  ④ queue_backfeed_merge 入队成功
⑤ approve 幂等（二次审批返回同结果 409 语义） ⑥ role 门（非 manager 拒绝）
⑦ 审批落审计链
fail-closed 红线：以上方法不被现有流程调用，仅本测试直连。
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gitrepo import GitRepo
from hub_core import hub
from models import CONFIG


# ═══════════ GitRepo move_file / remove_file ═══════════

@pytest.fixture()
def repo(tmp_path):
    r = GitRepo(str(tmp_path / "r"))
    assert r.ensure()
    return r


def test_move_file_history_readable(repo):
    """① 移动后旧路径在 HEAD 消失，但 read_at(HEAD~1) 历史可读（归档而非删除）。"""
    assert repo.write_file("vault/memory/2026-09-01/mem-001.md", "旧内容")
    assert repo.commit("add mem-001")
    assert repo.move_file("vault/memory/2026-09-01/mem-001.md",
                          "vault/_merged/2026-09-02/mem-001.md")
    # 新路径当前可读
    assert repo.read_at("vault/_merged/2026-09-02/mem-001.md") == "旧内容"
    # 旧路径 HEAD 已不存在，HEAD~1 历史可读
    assert repo.read_at("vault/memory/2026-09-01/mem-001.md") is None
    assert repo.read_at("vault/memory/2026-09-01/mem-001.md", "HEAD~1") == "旧内容"
    # 工作区干净（移动已提交）
    assert repo.status() == ""


def test_move_file_creates_dst_parent_dirs(repo):
    assert repo.write_file("a.md", "x")
    assert repo.commit("c")
    assert repo.move_file("a.md", "deep/nested/dir/b.md")
    assert os.path.isfile(os.path.join(repo.root, "deep", "nested", "dir", "b.md"))
    assert not os.path.exists(os.path.join(repo.root, "a.md"))


def test_remove_file_history_readable(repo):
    """② 删除后 HEAD 无此文件，read_at 历史 ref 仍可读（假删除语义）。"""
    assert repo.write_file("vault/wiki/page.md", "页面内容")
    assert repo.commit("add page")
    assert repo.remove_file("vault/wiki/page.md")
    assert repo.read_at("vault/wiki/page.md") is None
    assert repo.read_at("vault/wiki/page.md", "HEAD~1") == "页面内容"
    assert repo.status() == ""


def test_move_remove_failure_returns_false(repo, monkeypatch):
    """③ 失败静默 False 不抛：源不存在 / 路径逃逸 / monkeypatch git 命令失败。"""
    assert repo.write_file("a.md", "x")
    assert repo.commit("c")
    # 源文件不存在
    assert repo.move_file("nope.md", "b.md") is False
    assert repo.remove_file("nope.md") is False
    # 路径逃逸（.. 越出 root）
    assert repo.move_file("../escape.md", "b.md") is False
    assert repo.remove_file("../escape.md") is False
    # 目标已存在不覆盖
    assert repo.write_file("exists.md", "y")
    assert repo.commit("c2")
    assert repo.move_file("a.md", "exists.md") is False
    # git 命令失败 → commit 失败 → False（文件系统动作已发生，仅验证返回值语义）
    monkeypatch.setattr(GitRepo, "_git",
                        lambda self, args, check=True: (-1, "simulated git failure"))
    assert repo.move_file("a.md", "moved.md") is False
    assert repo.remove_file("exists.md") is False


# ═══════════ review_queue backfeed_merge 消费器骨架 ═══════════

def _make_db(db_path: str):
    """最小表集合：review_queue / events（对齐 test_guard_liveness 风格）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_type TEXT NOT NULL DEFAULT 'entity',
        doc_id TEXT NOT NULL, name TEXT NOT NULL, detail TEXT DEFAULT '',
        level TEXT DEFAULT 'summary', status TEXT DEFAULT 'pending',
        source TEXT DEFAULT 'llm', created_at TEXT DEFAULT (datetime('now')),
        reviewed_at TEXT, reviewed_by TEXT)""")
    conn.execute("""CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, agent_id TEXT, payload TEXT, timestamp TEXT)""")
    conn.commit()
    conn.close()


@pytest.fixture()
def bf(tmp_path, monkeypatch):
    """临时库 + hub.agents 注入（role 门读内存态），结束清理。"""
    db_path = str(tmp_path / "bf.db")
    _make_db(db_path)
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    injected = []

    class Env:
        db = db_path

        @staticmethod
        def agent(agent_id, role):
            hub.agents[agent_id] = {"agent_id": agent_id, "role": role}
            injected.append(agent_id)

    yield Env
    for aid in injected:
        hub.agents.pop(aid, None)


_DETAIL = {
    "pair": [{"branch": "default", "id": "mem-001",
              "path": "vault/memory/2026-09-01/mem-001.md", "excerpt": "客户X偏好"},
             {"branch": "proj-alpha", "id": "mem-117",
              "path": "vault/memory/2026-09-01/mem-117.md", "excerpt": "客户X偏好"}],
    "cos": 0.78, "suggested": "merge", "reasons": ["cos 落入人工区间"],
}


def test_queue_backfeed_merge(bf):
    """④ 入队成功：item_type='backfeed_merge' / source='backfeed' / status='pending'。"""
    r = asyncio.run(hub.queue_backfeed_merge(
        "pair:mem-001:mem-117", "[cos=0.78] 客户X交付偏好 × 2 来源", _DETAIL))
    assert r["status"] == "queued" and r["queue_id"]
    conn = sqlite3.connect(bf.db)
    row = conn.execute(
        "SELECT item_type, doc_id, status, source, level, detail FROM review_queue WHERE id=?",
        (r["queue_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row[:5] == ("backfeed_merge", "pair:mem-001:mem-117", "pending", "backfeed", "summary")
    assert json.loads(row[5])["cos"] == 0.78


def test_approve_backfeed_merge_idempotent_409(bf):
    """⑤ approve 幂等：二次审批返回 409 语义 + 已有结果，状态/审批人不被翻转。"""
    bf.agent("bf-mgr", "manager")
    qid = asyncio.run(hub.queue_backfeed_merge(
        "pair:mem-001:mem-117", "t", _DETAIL))["queue_id"]
    r1 = asyncio.run(hub.approve_backfeed_merge(qid, reviewer="bf-mgr"))
    assert r1 == {"status": "approved", "queue_id": qid}
    # 二次审批（含换 decision 的 reject）→ 409 already_processed，已有结果原样返回
    r2 = asyncio.run(hub.approve_backfeed_merge(qid, reviewer="bf-mgr"))
    assert r2["code"] == 409 and r2["status"] == "already_processed"
    assert r2["existing_status"] == "approved" and r2["reviewed_by"] == "bf-mgr"
    r3 = asyncio.run(hub.reject_backfeed_merge(qid, reviewer="bf-mgr"))
    assert r3["code"] == 409 and r3["existing_status"] == "approved"
    conn = sqlite3.connect(bf.db)
    row = conn.execute(
        "SELECT status, reviewed_by FROM review_queue WHERE id=?", (qid,)).fetchone()
    conn.close()
    assert row == ("approved", "bf-mgr"), "重复审批不得翻转状态/审批人"


def test_reject_backfeed_merge(bf):
    bf.agent("bf-orch", "orchestrator")
    qid = asyncio.run(hub.queue_backfeed_merge(
        "pair:a:b", "t", _DETAIL))["queue_id"]
    r = asyncio.run(hub.reject_backfeed_merge(qid, reviewer="bf-orch"))
    assert r == {"status": "rejected", "queue_id": qid}
    conn = sqlite3.connect(bf.db)
    row = conn.execute("SELECT status, reviewed_by FROM review_queue WHERE id=?",
                       (qid,)).fetchone()
    conn.close()
    assert row == ("rejected", "bf-orch")


def test_backfeed_role_gate(bf):
    """⑥ role 门：worker / 未知 reviewer 拒绝（403 语义），队列保持 pending。"""
    bf.agent("bf-worker", "worker")
    bf.agent("bf-mgr", "manager")
    qid = asyncio.run(hub.queue_backfeed_merge(
        "pair:a:b", "t", _DETAIL))["queue_id"]
    r = asyncio.run(hub.approve_backfeed_merge(qid, reviewer="bf-worker"))
    assert r["status"] == "error" and r["code"] == 403
    # 未知 reviewer（不在 hub.agents）fail-closed 拒绝
    r = asyncio.run(hub.approve_backfeed_merge(qid, reviewer="ghost"))
    assert r["status"] == "error" and r["code"] == 403
    conn = sqlite3.connect(bf.db)
    row = conn.execute("SELECT status FROM review_queue WHERE id=?", (qid,)).fetchone()
    conn.close()
    assert row == ("pending",), "被拒的审批不得改变队列状态"
    # manager 正常通过（对照）
    r = asyncio.run(hub.approve_backfeed_merge(qid, reviewer="bf-mgr"))
    assert r["status"] == "approved"


def test_backfeed_approve_audit_logged(bf):
    """⑦ 审批动作落审计链：events 表有 backfeed_merge_reviewed（含 queue_id/decision/canonical_id/cos）。"""
    bf.agent("bf-mgr", "manager")
    qid = asyncio.run(hub.queue_backfeed_merge(
        "pair:mem-001:mem-117", "t", _DETAIL))["queue_id"]
    asyncio.run(hub.approve_backfeed_merge(qid, reviewer="bf-mgr"))
    conn = sqlite3.connect(bf.db)
    row = conn.execute(
        "SELECT agent_id, payload FROM events"
        " WHERE event_type='backfeed_merge_reviewed'").fetchone()
    queued = conn.execute(
        "SELECT payload FROM events WHERE event_type='backfeed_merge_queued'").fetchone()
    conn.close()
    assert row, "审批未落审计链（events 缺 backfeed_merge_reviewed）"
    assert row[0] == "bf-mgr"
    payload = json.loads(row[1])
    assert payload["queue_id"] == qid and payload["decision"] == "approved"
    assert payload["canonical_id"] == "pair:mem-001:mem-117"
    assert payload["cos"] == 0.78
    assert queued, "入队未落审计（events 缺 backfeed_merge_queued）"


def test_backfeed_decide_not_found(bf):
    """边界：queue_id 不存在 → 404 语义；entity 类型的同 id 不可被 backfeed 审批串扰。"""
    bf.agent("bf-mgr", "manager")
    r = asyncio.run(hub.approve_backfeed_merge(99999, reviewer="bf-mgr"))
    assert r["status"] == "error" and r["code"] == 404
    # 与 entity 审查解耦：entity 行同 id 不被 backfeed 审批命中
    conn = sqlite3.connect(bf.db)
    conn.execute(
        "INSERT INTO review_queue (item_type, doc_id, name, status, source)"
        " VALUES ('entity', 'doc-x', 'ent', 'pending', 'llm')")
    ent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    r = asyncio.run(hub.approve_backfeed_merge(ent_id, reviewer="bf-mgr"))
    assert r["status"] == "error" and r["code"] == 404
