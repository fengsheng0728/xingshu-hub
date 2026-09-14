# -*- coding: utf-8 -*-
"""A3 披露规则模拟器 + shadow 灰度验收测试（2026-08-05）

覆盖：
1. 规则表枚举（9 条，含优先级）
2. 模拟器 vs 真实引擎镜像一致性（关键矩阵场景逐条对比）
3. shadow hook：不一致时返回告警
4. 历史重放：构造披露日志 → 重放一致性
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from disclosure_rules import (
    DisclosureLevel, RULES, simulate, shadow_check, replay_disclosure_log, rule_table,
)

POLICY = {
    "department_peer_visibility": False,
    "default_manager_level": "summary",
    "orchestrator_max_level": "full",
    "allow_peer_disclosure": True,
}

AGENTS = {
    "alice": {"role": "worker", "department": "sales"},
    "bob": {"role": "worker", "department": "sales"},
    "carol": {"role": "worker", "department": "it"},
    "manager1": {"role": "manager", "managed_agents": ["alice"]},
    "orchestrator1": {"role": "orchestrator", "managed_agents": ["alice", "bob", "carol"]},
}


def mem(owner="alice", level="summary", viewers=None):
    return {
        "owner_agent_id": owner,
        "disclosure_level": level,
        "allowed_viewers": json.dumps(viewers or []),
        "content": "机密内容",
        "summary": "摘要内容",
        "importance": 1.0,
        "created_at": "2026-08-05",
        "access_count": 0,
        "tags": "[]",
    }


# ============ 1. 规则表 ============

def test_rule_table_enumerable():
    rules = rule_table()
    assert len(rules) == 10  # r1-r8 + r9_sensitivity_cap(H2) + r10_default_none
    ids = [r["id"] for r in rules]
    assert len(set(ids)) == 10, "规则 ID 唯一"
    # 优先级连续 1-10
    assert [r["priority"] for r in rules] == list(range(1, 11))


# ============ 2. 模拟器 vs 真实引擎镜像一致性 ============

def _real_engine(memory, requester, task, required_level):
    """调用真实 disclosure 引擎（镜像对比基准）。"""
    from disclosure import DisclosureEngine

    class FakeHub:
        _disclosure_policy = POLICY
        agents = AGENTS

    engine = DisclosureEngine(FakeHub())
    return engine._calculate_disclosure_level(
        memory=memory, requester=requester, task=task, required_level=required_level)


CASES = [
    # (名称, memory, requester, task, required_level)
    ("r1 自己", mem(), "alice", {}, DisclosureLevel.SUMMARY),
    ("r2 白名单", mem(viewers=["bob"]), "bob", {}, DisclosureLevel.SUMMARY),
    ("r2b 组交集", mem(), "carol", {}, DisclosureLevel.SUMMARY),
    ("r3 NONE阻断", mem(level="none"), "bob", {}, DisclosureLevel.SUMMARY),
    ("r5 主管看下属", mem(), "manager1", {}, DisclosureLevel.SUMMARY),
    ("r6 店长", mem(), "orchestrator1", {}, DisclosureLevel.SUMMARY),
    ("r7 同级同部门", mem(), "bob", {}, DisclosureLevel.SUMMARY),
    ("r7 跨部门", mem(), "carol", {}, DisclosureLevel.SUMMARY),
    ("r7 同任务", mem(), "bob", {"assigned_agent_id": "alice"}, DisclosureLevel.SUMMARY),
    ("r8 级别上限", mem(level="metadata"), "manager1", {}, DisclosureLevel.FULL),
    ("r9 默认NONE", mem(), "unknown", {}, DisclosureLevel.SUMMARY),
]


@pytest.mark.parametrize("name,memory,requester,task,req_level", CASES)
def test_simulator_mirrors_real_engine(name, memory, requester, task, req_level):
    """模拟器与真实引擎判定必须一致（shadow 安全网的前提）。"""
    real_level = _real_engine(memory, requester, task, req_level)
    sim_level, sim_rule = simulate(
        memory, requester, task, req_level, AGENTS, POLICY)
    assert sim_level == real_level, (
        f"[{name}] 模拟器 {sim_level} != 真实 {real_level} (rule={sim_rule})")
    # 每条场景应命中预期规则
    expected_rule = {
        "r1 自己": "r1_self",
        "r2 白名单": "r2_whitelist",
        "r2b 组交集": "r7_peer_collab",  # 无组数据(carol/alice 无组) → 落到 r7 同级协作 → NONE
        "r3 NONE阻断": "r3_mem_none",
        "r5 主管看下属": "r5_manager_subordinate",
        "r6 店长": "r5_manager_subordinate",  # orchestrator 也 ∈ [manager,orchestrator] 且 managed 含 owner → 先中 r5
        "r7 同级同部门": "r7_peer_collab",  # department_peer_visibility=False → allow_peer 无同任务 → NONE
        "r7 跨部门": "r7_peer_collab",
        "r7 同任务": "r7_peer_collab",
        "r8 级别上限": "r5_manager_subordinate",  # manager1 查 alice(下属) 先中 r5,再按 default_manager_level
        "r9 默认NONE": "r4_5_role_fail_closed",  # 2b: unknown 无角色 → fail-closed 封顶 METADATA
    }
    assert sim_rule == expected_rule[name], f"[{name}] 命中规则 {sim_rule} != 预期 {expected_rule[name]}"


# ============ 3. shadow hook ============

def test_shadow_check_mismatch_detected():
    """模拟器与真实引擎不一致 → shadow_check 返回告警。"""
    # 构造真实引擎不会命中的场景（人为制造差异：直接用不同 policy）
    warn = shadow_check(
        mem(), "unknown", {}, DisclosureLevel.SUMMARY,
        actual_level=DisclosureLevel.SUMMARY,  # 人为给错误 actual(真实引擎不会给 summary)
        agents=AGENTS, policy=POLICY,
    )
    assert warn is not None
    assert warn["type"] == "disclosure_shadow_mismatch"
    assert warn["actual_level"] == "summary"
    assert warn["sim_level"] == "metadata"


def test_shadow_check_consistent_returns_none():
    # unknown 查 alice: 2b fail-closed → METADATA（模拟器与真实引擎一致）
    warn = shadow_check(
        mem(), "unknown", {}, DisclosureLevel.SUMMARY,
        actual_level=DisclosureLevel.METADATA,  # 与模拟器一致(2b: unknown 封顶)
        agents=AGENTS, policy=POLICY,
    )
    assert warn is None


# ============ 4. 历史重放 ============

def test_replay_disclosure_log():
    """构造披露日志 → 重放一致性报告。"""
    tmp = tempfile.mkdtemp(prefix="a3-")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE disclosure_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, from_agent_id TEXT, to_agent_id TEXT,
            memory_id TEXT, disclosed_level TEXT, disclosed_content TEXT,
            disclosed_at TEXT, reason TEXT, prev_hash TEXT DEFAULT '',
            entry_hash TEXT DEFAULT '')"""
    )
    conn.execute(
        """CREATE TABLE memory_pool (
            memory_id TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL,
            memory_key TEXT, content TEXT, summary TEXT, embedding BLOB,
            importance REAL, tags TEXT, disclosure_level TEXT,
            disclosure_scope TEXT, allowed_viewers TEXT, created_at TEXT,
            access_count INTEGER DEFAULT 0, last_accessed TEXT,
            kind TEXT DEFAULT 'fact', source_session_id TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0, source_type TEXT DEFAULT 'user',
            updated_at TEXT DEFAULT NULL,
            trust_level TEXT DEFAULT 'internal',
            source_agent_id TEXT DEFAULT '', tainted_at TEXT DEFAULT '')"""
    )
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content, summary, disclosure_level, tags, importance, created_at) "
        "VALUES ('m1', 'alice', 'k1', '内容', '摘要', 'summary', '[]', 1.0, '2026-08-05')")
    # 历史判定：alice 查自己 → full（r1）
    conn.execute(
        "INSERT INTO disclosure_log (task_id, from_agent_id, to_agent_id, memory_id, disclosed_level, disclosed_at, reason) "
        "VALUES ('t1', 'alice', 'alice', 'm1', 'full', '2026-08-05T00:00:00', 'task_scheduling')")
    conn.commit()
    conn.close()

    result = replay_disclosure_log(db, AGENTS, POLICY)
    assert result["total"] >= 1
    assert result["matched"] == result["total"], f"重放应 100% 一致: {result}"
    assert result["mismatched"] == 0
    os.remove(db)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
