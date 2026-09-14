# -*- coding: utf-8 -*-
"""A3 披露规则模拟器 + shadow 灰度（2026-08-05）

披露引擎（disclosure.py _calculate_disclosure_level）的 8 条优先级规则链
数据表化 + 可重放模拟器，用于：

1. **shadow 灰度**：真实引擎照旧判定，模拟器并行跑一遍——不一致时 audit 告警，
   规则重构/新增有安全网（改了规则不会悄悄改变线上行为）
2. **历史重放**：读 disclosure_log 审计 → 模拟器重放 → 对比历史判定 100% 一致
   （规则重构后跑一遍证明行为等价）

设计：simulate() 完全镜像真实引擎的分支逻辑，但每一步返回 (level, rule_id)，
使「命中了哪条规则」可枚举、可测试、可审计。
"""
import json
import sqlite3
import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("xingshu.disclosure_rules")


def _connect(db_path: str):
    """T2-2: 统一连接入口(busy_timeout 5000, row_factory Row)——防锁竞争与列名访问。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class DisclosureLevel(str, Enum):
    NONE = "none"
    METADATA = "metadata"
    SUMMARY = "summary"
    FULL = "full"


# ── 规则表（可枚举，供展示/文档/测试）──

RULES: List[dict] = [
    {"id": "r1_self", "priority": 1, "name": "自己查自己", "desc": "requester == owner → FULL"},
    {"id": "r2_whitelist", "priority": 2, "name": "白名单", "desc": "requester ∈ allowed_viewers → FULL"},
    {"id": "r2b_group_intersect", "priority": 3, "name": "组交集", "desc": "requester 组 ∩ owner 组非空 → SUMMARY（S1 身份接入）"},
    {"id": "r3_mem_none", "priority": 4, "name": "记忆 NONE 阻断", "desc": "记忆自身 disclosure_level == NONE → NONE"},
    {"id": "r5_manager_subordinate", "priority": 5, "name": "主管看下属", "desc": "manager/orchestrator 且 owner ∈ managed_agents → 按 default_manager_level"},
    {"id": "r6_orchestrator_global", "priority": 6, "name": "店长全局", "desc": "orchestrator → orchestrator_max_level"},
    {"id": "r7_peer_collab", "priority": 7, "name": "同级协作", "desc": "worker×worker 同部门/同任务 → SUMMARY，否则 NONE"},
    {"id": "r8_mem_level_cap", "priority": 8, "name": "记忆级别上限", "desc": "required > mem_level → 截断到 mem_level"},
    {"id": "r9_sensitivity_cap", "priority": 9, "name": "敏感度写入打标", "desc": "写入时敏感度链(fail-closed: trust/PII/机密词/角色/父级/类型)定存储级别，读取时由 r8 消费 → min(请求方判定, 存储级别)（H2，附录 E v1.4）"},
    {"id": "r10_default_none", "priority": 10, "name": "默认不披露", "desc": "兜底 → NONE"},
]


def rule_table() -> List[dict]:
    """规则表（复制，防外部修改）。"""
    return [dict(r) for r in RULES]


# ── 模拟器（镜像真实引擎，逐规则返回命中 ID）──

def _principal_groups(agent_id: str, db_path: str) -> set:
    try:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute("SELECT group_dn FROM principal_groups WHERE principal_id = ?", (agent_id,))
        groups = {r[0] for r in c.fetchall()}
        conn.close()
        return groups
    except Exception as _e:
        if "no such table" in str(_e):
            return set()  # 表未迁移: 零影响(镜像真实引擎语义)
        logger.warning("disclosure_rules principal_groups failed (fail-closed empty): %s", _e)
        return set()


def _is_known_employee(requester: str, db_path: str) -> bool:
    """1e：requester 是否已登记员工(active)。无表/异常 → False(零影响)。"""
    try:
        conn = _connect(db_path)
        try:
            c = conn.cursor()
            c.execute("PRAGMA table_info(employee_accounts)")
            cols = {r[1] for r in c.fetchall()}
            if "status" not in cols:
                return False
            c.execute(
                "SELECT 1 FROM employee_accounts WHERE employee_id = ? AND status = 'active'",
                (requester,),
            )
            return c.fetchone() is not None
        finally:
            conn.close()
    except Exception as _e:
        if "no such table" in str(_e):
            return False  # 表未迁移: 零影响
        logger.warning("disclosure_rules employee lookup failed (fail-closed False): %s", _e)
        return False


def simulate(
    memory: dict,
    requester: str,
    task: dict,
    required_level: DisclosureLevel,
    agents: Dict[str, dict],
    policy: dict,
    db_path: str = "",
) -> Tuple[DisclosureLevel, str]:
    """模拟披露判定，返回 (level, 命中的规则 ID)。

    分支结构必须与 disclosure._calculate_disclosure_level 保持镜像；
    一旦不一致 → shadow 对比会告警（这就是安全网）。
    """
    owner = memory["owner_agent_id"]

    # r1 自己查自己
    if requester == owner:
        return DisclosureLevel.FULL, "r1_self"

    # r2 白名单
    allowed = json.loads(memory.get("allowed_viewers") or "[]")
    if requester in allowed:
        return DisclosureLevel.FULL, "r2_whitelist"

    # r2b 组交集
    if db_path:
        _rg = _principal_groups(requester, db_path)
        _og = _principal_groups(owner, db_path)
        if _rg and _og and (_rg & _og):
            mem_level_2b = DisclosureLevel(memory.get("disclosure_level", "summary"))
            if mem_level_2b != DisclosureLevel.NONE:
                return DisclosureLevel.SUMMARY, "r2b_group_intersect"

    # r3 记忆 NONE 阻断
    mem_level = DisclosureLevel(memory.get("disclosure_level", "summary"))
    if mem_level == DisclosureLevel.NONE:
        return DisclosureLevel.NONE, "r3_mem_none"

    # r4 角色获取（真实引擎在 r4 位置获取角色，此处保持一致）
    requester_info = agents.get(requester, {})
    owner_info = agents.get(owner, {})
    requester_role = (requester_info.get("role") or "").strip()
    owner_role = (owner_info.get("role") or "").strip()

    # r4.5 主体 fail-closed（2b/1e，镜像真实引擎规则 4.5）
    # 已登记员工(active) → SUMMARY 基础；完全未知 → METADATA 封顶
    if not requester_role:
        if db_path and _is_known_employee(requester, db_path):
            if required_level in (DisclosureLevel.SUMMARY, DisclosureLevel.FULL):
                return DisclosureLevel.SUMMARY, "r4_5_role_fail_closed"
            return required_level, "r4_5_role_fail_closed"
        if required_level in (DisclosureLevel.SUMMARY, DisclosureLevel.FULL):
            return DisclosureLevel.METADATA, "r4_5_role_fail_closed"
        return required_level, "r4_5_role_fail_closed"

    # r5 主管看下属
    if requester_role in ["manager", "orchestrator"]:
        managed = requester_info.get("managed_agents", [])
        if owner in managed:
            default_level = policy.get("default_manager_level", "summary")
            if required_level == DisclosureLevel.FULL:
                return DisclosureLevel.FULL, "r5_manager_subordinate"
            return DisclosureLevel(default_level), "r5_manager_subordinate"

    # r6 店长全局
    if requester_role == "orchestrator":
        agent_policy = requester_info.get("disclosure_policy", {})
        max_level_str = agent_policy.get("max_disclosure") or policy.get("orchestrator_max_level", "full")
        max_level = DisclosureLevel(max_level_str)
        if required_level.value in ("full",):
            return (max_level if max_level.value == "full" else DisclosureLevel.SUMMARY), "r6_orchestrator_global"
        return required_level, "r6_orchestrator_global"

    # r7 同级协作
    if requester_role == "worker" and owner_role == "worker":
        if policy.get("department_peer_visibility", False):
            req_dept = requester_info.get("department", "")
            own_dept = owner_info.get("department", "")
            if req_dept and own_dept and req_dept == own_dept:
                return DisclosureLevel.SUMMARY, "r7_peer_collab"
        if policy.get("allow_peer_disclosure", True):
            assigned = task.get("assigned_agent_id", "")
            creator = task.get("creator_agent_id", "")
            if requester in (assigned, creator) or owner in (assigned, creator):
                return DisclosureLevel.SUMMARY, "r7_peer_collab"
        return DisclosureLevel.NONE, "r7_peer_collab"

    # r8 记忆级别上限
    level_map = {
        DisclosureLevel.METADATA: 1,
        DisclosureLevel.SUMMARY: 2,
        DisclosureLevel.FULL: 3,
    }
    if level_map.get(required_level, 0) > level_map.get(mem_level, 0):
        return mem_level, "r8_mem_level_cap"

    # r9 敏感度打标（写入时已定存储级别，此处由 r8 消费；说明性存在）
    # r10 默认不披露
    return DisclosureLevel.NONE, "r10_default_none"


# ── 历史重放对比 ──

def replay_disclosure_log(db_path: str, agents: Dict[str, dict], policy: dict) -> dict:
    """读 disclosure_log 审计，逐条重放模拟器，与历史 disclosed_level 对比。

    返回：
    {
        "total": N, "matched": N, "mismatched": N,
        "mismatches": [{log_id, memory_id, from_agent, to_agent,
                        history_level, sim_level, history_reason, sim_rule}]
    }

    100% 一致 = 规则表化未改变线上行为（shadow 安全网验证）。
    """
    try:
        conn = _connect(db_path)
        c = conn.cursor()
        c.execute(
            "SELECT log_id, task_id, from_agent_id, to_agent_id, memory_id, "
            "disclosed_level, reason FROM disclosure_log ORDER BY log_id"
        )
        rows = c.fetchall()
        conn.close()
    except Exception:
        return {"total": 0, "matched": 0, "mismatched": 0, "mismatches": []}

    total = 0
    matched = 0
    mismatches = []

    for row in rows:
        log_id, task_id, from_agent, to_agent, memory_id, hist_level, reason = row
        total += 1
        # 取该记忆当前快照（历史判定时点的记忆内容不可考，用当前值近似）
        try:
            conn = _connect(db_path)
            c = conn.cursor()
            c.execute("SELECT * FROM memory_pool WHERE memory_id = ?", (memory_id,))
            mrow = c.fetchone()
            conn.close()
            if mrow is None:
                continue
            cols = [d[0] for d in c.description] if c.description else []
            # 重新拿列名
            conn = _connect(db_path)
            c2 = conn.cursor()
            c2.execute("SELECT * FROM memory_pool WHERE memory_id = ?", (memory_id,))
            mrow2 = c2.fetchone()
            colnames = [d[0] for d in c2.description]
            memory = dict(zip(colnames, mrow2))
            conn.close()
        except Exception:
            continue

        try:
            hist_level_enum = DisclosureLevel(hist_level)
        except ValueError:
            continue

        sim_level, sim_rule = simulate(
            memory=memory, requester=to_agent, task={},
            required_level=hist_level_enum,
            agents=agents, policy=policy, db_path=db_path,
        )
        if sim_level.value == hist_level:
            matched += 1
        else:
            mismatches.append({
                "log_id": log_id, "memory_id": memory_id,
                "from_agent": from_agent, "to_agent": to_agent,
                "history_level": hist_level, "sim_level": sim_level.value,
                "history_reason": reason, "sim_rule": sim_rule,
            })

    return {
        "total": total, "matched": matched,
        "mismatched": len(mismatches), "mismatches": mismatches[:20],
    }


# ── shadow 判定（供 request_disclosure 挂 hook）──

def shadow_check(
    memory: dict, requester: str, task: dict, required_level: DisclosureLevel,
    actual_level: DisclosureLevel, agents: Dict[str, dict], policy: dict,
    db_path: str = "",
) -> Optional[dict]:
    """shadow 并行判定：模拟器 vs 真实引擎。不一致返回告警信息，一致返回 None。"""
    try:
        sim_level, sim_rule = simulate(
            memory, requester, task, required_level, agents, policy, db_path)
        if sim_level.value != actual_level.value:
            return {
                "type": "disclosure_shadow_mismatch",
                "requester": requester, "owner": memory.get("owner_agent_id"),
                "actual_level": actual_level.value, "sim_level": sim_level.value,
                "sim_rule": sim_rule,
            }
    except Exception as _e:
        # T2-2: shadow 安全网故障可观测(静默=规则漂移无告警)
        logger.warning("disclosure_rules shadow_check failed: %s", _e)
        return None
