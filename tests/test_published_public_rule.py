# -*- coding: utf-8 -*-
"""CD-033 阶段 A（2026-09-17）：「企业已发布 → 全员摘要级」披露规则验收测试

语义（用户 2026-09-17 拍板）：
- 已发布内容（知识层命中伪 dict，带 "published": True 标记）对全体已注册 Agent
  可见到 SUMMARY 级，规则 id = r4_published_public；
- 仅从 NONE 提升，绝不降级任何现有判定（r1/r2/r2b/r5/r6 等早退结果原样返回）；
- 记忆行永远不带 published 标记 → r3_mem_none 阻断语义天然不受影响；
- 开关 disclosure.published_public 默认 True；关闭后行为与改动前完全一致；
- 主体 fail-closed 不变：未知主体（无 role）仍封顶 METADATA。

覆盖 T6-1 ~ T6-6（每条断言命中规则 id）+ 规则表登记。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from disclosure_rules import DisclosureLevel, simulate, rule_table


def _policy(published_public=True):
    return {
        "department_peer_visibility": False,
        "default_manager_level": "summary",
        "orchestrator_max_level": "full",
        "allow_peer_disclosure": True,
        "published_public": published_public,
    }


AGENTS = {
    "w1": {"role": "worker", "department": "sales"},
    "w2": {"role": "worker", "department": "it"},
    "manager1": {"role": "manager", "managed_agents": ["w1"]},
}


def pub_mem(owner="w2", published=True, level="summary"):
    """知识层伪 memory dict（对齐 disclosure._knowledge_hit 构造形态）。"""
    m = {
        "owner_agent_id": owner,
        "disclosure_level": level,
        "allowed_viewers": "[]",
        "content": "企业已发布知识正文",
        "summary": "企业已发布知识摘要",
        "importance": 0.8,
        "created_at": "2026-09-17",
        "access_count": 0,
        "tags": "[]",
    }
    if published:
        m["published"] = True
    return m


def _real_level(memory, requester, task, required_level, policy):
    """真实引擎判定（镜像对比基准）。"""
    from disclosure import DisclosureEngine

    class FakeHub:
        agents = AGENTS
        _disclosure_policy = policy

    engine = DisclosureEngine(FakeHub())
    return engine._calculate_disclosure_level(
        memory=memory, requester=requester, task=task, required_level=required_level)


def _both(memory, requester, task, required_level, policy):
    """同一输入跑真实引擎 + 模拟器，返回 ((real_level), (sim_level, sim_rule))。"""
    real = _real_level(memory, requester, task, required_level, policy)
    sim_level, sim_rule = simulate(
        memory, requester, task, required_level, AGENTS, policy)
    return real, (sim_level, sim_rule)


# ============ T6-1：worker 查另一 worker 的已发布知识 → SUMMARY/r4_published_public ============

def test_t6_1_published_public_promote_to_summary():
    real, (sim_level, sim_rule) = _both(
        pub_mem(owner="w2", published=True), "w1", {},
        DisclosureLevel.SUMMARY, _policy(True))
    assert real == DisclosureLevel.SUMMARY, f"真实引擎应为 SUMMARY，实际 {real}"
    assert sim_level == DisclosureLevel.SUMMARY
    assert sim_rule == "r4_published_public", f"命中规则应为 r4_published_public，实际 {sim_rule}"


# ============ T6-2：开关关闭 → 回到 NONE/r7_peer_collab（与今天行为一致）============

def test_t6_2_switch_off_restores_none():
    real, (sim_level, sim_rule) = _both(
        pub_mem(owner="w2", published=True), "w1", {},
        DisclosureLevel.SUMMARY, _policy(False))
    assert real == DisclosureLevel.NONE, f"开关关闭应回到 NONE，实际 {real}"
    assert sim_level == DisclosureLevel.NONE
    assert sim_rule == "r7_peer_collab", f"命中规则应为 r7_peer_collab，实际 {sim_rule}"


# ============ T6-3：记忆行不带标记 → 不触发提升；记忆 NONE 阻断不受影响 ============

def test_t6_3_memory_row_without_marker_stays_none():
    # 同场景但伪 dict 无 published 键（= 记忆行形态）→ 仍 NONE
    real, (sim_level, sim_rule) = _both(
        pub_mem(owner="w2", published=False), "w1", {},
        DisclosureLevel.SUMMARY, _policy(True))
    assert real == DisclosureLevel.NONE, f"无 published 标记应保持 NONE，实际 {real}"
    assert sim_level == DisclosureLevel.NONE
    assert sim_rule == "r7_peer_collab", f"命中规则应为 r7_peer_collab，实际 {sim_rule}"


def test_t6_3_mem_none_block_unaffected():
    # disclosure_level="none" 的记忆行（无标记）→ 仍 NONE/r3_mem_none
    real, (sim_level, sim_rule) = _both(
        pub_mem(owner="w2", published=False, level="none"), "w1", {},
        DisclosureLevel.SUMMARY, _policy(True))
    assert real == DisclosureLevel.NONE
    assert sim_level == DisclosureLevel.NONE
    assert sim_rule == "r3_mem_none", f"命中规则应为 r3_mem_none，实际 {sim_rule}"


# ============ T6-4：不降级——已有判定原样返回 ============

def test_t6_4_owner_self_stays_full():
    real, (sim_level, sim_rule) = _both(
        pub_mem(owner="w2", published=True), "w2", {},
        DisclosureLevel.FULL, _policy(True))
    assert real == DisclosureLevel.FULL, f"owner 查自己应仍为 FULL，实际 {real}"
    assert sim_level == DisclosureLevel.FULL
    assert sim_rule == "r1_self", f"命中规则应为 r1_self，实际 {sim_rule}"


def test_t6_4_manager_subordinate_stays_full():
    real, (sim_level, sim_rule) = _both(
        pub_mem(owner="w1", published=True), "manager1", {},
        DisclosureLevel.FULL, _policy(True))
    assert real == DisclosureLevel.FULL, f"manager 查下属应仍为 FULL，实际 {real}"
    assert sim_level == DisclosureLevel.FULL
    assert sim_rule == "r5_manager_subordinate", f"命中规则应为 r5_manager_subordinate，实际 {sim_rule}"


# ============ T6-5：fail-closed 不变——未知主体仍封顶 METADATA ============

def test_t6_5_unknown_principal_stays_metadata():
    real, (sim_level, sim_rule) = _both(
        pub_mem(owner="w2", published=True), "ghost_no_role", {},
        DisclosureLevel.SUMMARY, _policy(True))
    assert real == DisclosureLevel.METADATA, f"未知主体应仍封顶 METADATA，实际 {real}"
    assert sim_level == DisclosureLevel.METADATA
    assert sim_rule == "r4_5_role_fail_closed", f"命中规则应为 r4_5_role_fail_closed，实际 {sim_rule}"


# ============ T6-6：镜像一致——同组输入在两侧返回完全相同的 (level, rule_id) ============

def test_t6_6_mirror_consistency_all_cases():
    cases = [
        # T6-1
        (pub_mem(owner="w2", published=True), "w1", {}, DisclosureLevel.SUMMARY, _policy(True)),
        # T6-2
        (pub_mem(owner="w2", published=True), "w1", {}, DisclosureLevel.SUMMARY, _policy(False)),
        # T6-3a
        (pub_mem(owner="w2", published=False), "w1", {}, DisclosureLevel.SUMMARY, _policy(True)),
        # T6-3b
        (pub_mem(owner="w2", published=False, level="none"), "w1", {}, DisclosureLevel.SUMMARY, _policy(True)),
        # T6-4a
        (pub_mem(owner="w2", published=True), "w2", {}, DisclosureLevel.FULL, _policy(True)),
        # T6-4b
        (pub_mem(owner="w1", published=True), "manager1", {}, DisclosureLevel.FULL, _policy(True)),
        # T6-5
        (pub_mem(owner="w2", published=True), "ghost_no_role", {}, DisclosureLevel.SUMMARY, _policy(True)),
    ]
    for i, (memory, requester, task, req_level, policy) in enumerate(cases):
        real, (sim_level, sim_rule) = _both(memory, requester, task, req_level, policy)
        assert sim_level == real, (
            f"case#{i}: 模拟器 {sim_level}({sim_rule}) != 真实引擎 {real}")


# ============ 规则表登记（T4）============

def test_rule_table_has_published_public():
    rules = rule_table()
    entry = next((r for r in rules if r["id"] == "r4_published_public"), None)
    assert entry is not None, "RULES 表缺少 r4_published_public"
    assert entry["priority"] == 4  # 2026-09-17 验收：优先级唯一化重排（原 3 与 r2b 撞号）
    assert entry["name"] == "企业已发布公共区"
