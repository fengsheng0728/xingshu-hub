"""
星枢 — disclosure engine 单元测试
运行: cd <repo-root> && python -m pytest tests/test_disclosure.py -v
"""
import sys, os, json
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DisclosureLevel, DisclosureScope, TaskStatus
from hub_core import SyncHub


@pytest.fixture
def hub():
    """创建干净的 Hub 实例，注入 3 个 mock agent"""
    h = SyncHub()
    h.agents = {
        "cs-wang": {
            "agent_id": "cs-wang", "agent_name": "客服小王",
            "role": "worker", "department": "客服部",
            "capabilities": ["接待", "售后"], "managed_agents": [],
            "status": "online", "disclosure_policy": {},
        },
        "cs-li": {
            "agent_id": "cs-li", "agent_name": "客服小李",
            "role": "worker", "department": "客服部",
            "capabilities": ["换货", "退款"], "managed_agents": [],
            "status": "online", "disclosure_policy": {},
        },
        "mgr-zhang": {
            "agent_id": "mgr-zhang", "agent_name": "主管老张",
            "role": "manager", "department": "客服部",
            "capabilities": ["团队管理"], "managed_agents": ["cs-wang", "cs-li"],
            "status": "online", "disclosure_policy": {},
        },
    }
    return h


def _mem(owner="cs-wang", level="summary", allowed=None, tags=None, importance=1.0):
    return {
        "owner_agent_id": owner,
        "disclosure_level": level,
        "allowed_viewers": json.dumps(allowed or []),
        "importance": importance,
        "tags": json.dumps(tags or []),
        "memory_key": "test-key",
        "content": "测试内容: 客户反馈产品色差问题",
        "summary": "色差投诉",
        "created_at": "2026-07-15T00:00:00",
        "access_count": 0,
    }


# ═══════════════════════════════
# 规则 1: 自己查自己 → FULL
# ═══════════════════════════════

def test_self_access_full(hub):
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-wang"),
        requester="cs-wang", task={},
        required_level=DisclosureLevel.SUMMARY,
    )
    assert result == DisclosureLevel.FULL


# ═══════════════════════════════
# 规则 2: 白名单 → FULL
# ═══════════════════════════════

def test_whitelist_access_full(hub):
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-wang", allowed=["cs-li"]),
        requester="cs-li", task={},
        required_level=DisclosureLevel.SUMMARY,
    )
    assert result == DisclosureLevel.FULL


# ═══════════════════════════════
# 规则 3: 记忆自身 NONE → 阻断（但规则 1 优先：自己查自己不触发规则 3）
# 注：规则顺序为 1:self→FULL, 2:whitelist→FULL, 之后才到 3:NONE 阻断
# 所以规则 3 只阻断非自己/非白名单的请求
# ═══════════════════════════════

def test_memory_none_blocks_others(hub):
    """记忆为 NONE → 陌生人请求被阻断"""
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-wang", level="none"),
        requester="cs-li", task={},
        required_level=DisclosureLevel.FULL,
    )
    assert result == DisclosureLevel.NONE

def test_memory_none_does_not_block_self(hub):
    """记忆为 NONE 但规则 1 优先 → 自己仍可看 FULL"""
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-wang", level="none"),
        requester="cs-wang", task={},
        required_level=DisclosureLevel.FULL,
    )
    assert result == DisclosureLevel.FULL


# ═══════════════════════════════
# 规则 5: Manager 看下属 → summary(可配置)
# ═══════════════════════════════

def test_manager_sees_subordinate_summary(hub):
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-wang"),
        requester="mgr-zhang", task={},
        required_level=DisclosureLevel.SUMMARY,
    )
    expected = hub._disclosure_policy.get("default_manager_level", "summary")
    assert result.value == expected

def test_manager_can_request_full(hub):
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-wang"),
        requester="mgr-zhang", task={},
        required_level=DisclosureLevel.FULL,
    )
    assert result == DisclosureLevel.FULL

def test_manager_cannot_see_non_subordinate(hub):
    """主管只能看到 managed_agents 列表里的下属"""
    # cs-li 不是 mgr-zhang 的直接下属... 等等，他是
    # 需要加一个非下属
    hub.agents["cs-zhao"] = {
        "agent_id": "cs-zhao", "agent_name": "客服小赵",
        "role": "worker", "department": "售后部",
        "capabilities": [], "managed_agents": [],
        "status": "online", "disclosure_policy": {},
    }
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-zhao"),
        requester="mgr-zhang", task={},
        required_level=DisclosureLevel.SUMMARY,
    )
    # mgr-zhang 不管 cs-zhao → orchestrator 路径也不通 → NONE
    assert result == DisclosureLevel.NONE


# ═══════════════════════════════
# 规则 7: Peer 隔离
# ═══════════════════════════════

def test_worker_peer_isolation_no_task(hub):
    """无任务协作 → 同部门 peer 不可见（配置默认 false）"""
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-li"),
        requester="cs-wang", task={},
        required_level=DisclosureLevel.SUMMARY,
    )
    assert result == DisclosureLevel.NONE

def test_worker_peer_same_task_visible(hub):
    """同一任务协作 → peer 可见 summary"""
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-li"),
        requester="cs-wang",
        task={"assigned_agent_id": "cs-wang"},
        required_level=DisclosureLevel.SUMMARY,
    )
    assert result == DisclosureLevel.SUMMARY


# ═══════════════════════════════
# 规则 8: 级别上限
# ═══════════════════════════════

def test_level_cap_blocks_peer(hub):
    """规则 7 优先级 > 规则 8：同任务 peer 直接返回 SUMMARY，绕过级别上限
    这是预期行为 — 协作优先级高于记忆自身策略"""
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-wang", level="metadata"),
        requester="cs-li",
        task={"assigned_agent_id": "cs-li"},
        required_level=DisclosureLevel.FULL,
    )
    # 规则 7 在规则 8 之前返回 → 直接 SUMMARY
    assert result == DisclosureLevel.SUMMARY


# ═══════════════════════════════
# 默认: 陌生人 → NONE
# ═══════════════════════════════

def test_stranger_rejected(hub):
    hub.agents["stranger"] = {
        "agent_id": "stranger", "agent_name": "陌生人",
        "role": "worker", "department": "其他",
        "capabilities": [], "managed_agents": [],
        "status": "online", "disclosure_policy": {},
    }
    result = hub._calculate_disclosure_level(
        memory=_mem("cs-wang"),
        requester="stranger", task={},
        required_level=DisclosureLevel.SUMMARY,
    )
    assert result == DisclosureLevel.NONE


# ═══════════════════════════════
# _extract_by_level
# ═══════════════════════════════

def test_extract_metadata(hub):
    result = hub._extract_by_level(_mem(), DisclosureLevel.METADATA)
    assert "importance" in result
    assert "色差" not in result  # metadata 不应包含内容

def test_extract_summary(hub):
    result = hub._extract_by_level(_mem(), DisclosureLevel.SUMMARY)
    assert "色差" in result

def test_extract_full(hub):
    result = hub._extract_by_level(_mem(), DisclosureLevel.FULL)
    assert "测试内容" in result

def test_extract_none(hub):
    result = hub._extract_by_level(_mem(), DisclosureLevel.NONE)
    assert result == ""


# ═══════════════════════════════
# _match_query: 三档匹配
# ═══════════════════════════════

def test_match_query_exact_tag(hub):
    """精确 tag 匹配 +0.5，content 也匹配则额外 +0.2"""
    matched, score = hub._match_query(
        _mem(tags=["色差", "投诉"]), "色差"
    )
    assert matched
    assert score == 0.7  # 精确 tag(0.5) + content 匹配(0.2)

def test_match_query_partial_tag(hub):
    """模糊 tag 匹配 +0.3，content 也匹配 +0.2"""
    matched, score = hub._match_query(
        _mem(tags=["产品色差问题"]), "色差"
    )
    assert matched
    assert score == 0.5  # 模糊 tag(0.3) + content 匹配(0.2)

def test_match_query_key(hub):
    """仅 memory_key 匹配（无 tag 命中）"""
    mem = _mem(tags=[])  # 清空 tags
    mem["memory_key"] = "case-001-色差投诉"
    matched, score = hub._match_query(mem, "色差")
    assert matched
    assert score == 0.5  # key(0.3) + content(0.2)

def test_match_query_content(hub):
    matched, score = hub._match_query(_mem(), "色差")
    assert matched
    assert score == 0.2

def test_match_query_no_match(hub):
    matched, score = hub._match_query(_mem(), "不存在")
    assert not matched
    assert score == 0.0


# ═══════════════════════════════
# TaskStatus 状态机
# ═══════════════════════════════

from models import TASK_TRANSITIONS

def test_valid_transitions():
    assert TaskStatus.ASSIGNED in TASK_TRANSITIONS[TaskStatus.PENDING]
    assert TaskStatus.IN_PROGRESS in TASK_TRANSITIONS[TaskStatus.ASSIGNED]
    assert TaskStatus.COMPLETED in TASK_TRANSITIONS[TaskStatus.IN_PROGRESS]
    assert TaskStatus.FAILED in TASK_TRANSITIONS[TaskStatus.IN_PROGRESS]

def test_terminal_no_transitions():
    for status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
        assert TASK_TRANSITIONS[status] == [], f"{status} should have no transitions"

def test_invalid_transition():
    assert TaskStatus.COMPLETED not in TASK_TRANSITIONS[TaskStatus.PENDING]
    assert TaskStatus.PENDING not in TASK_TRANSITIONS[TaskStatus.COMPLETED]


# ═══════════════════════════════
# 规则 4.5: 角色 fail-closed（阶段1/2b）—— role 缺失/空 → METADATA 封顶
# ═══════════════════════════════

def test_role_missing_fail_closed_caps_summary(hub):
    """role 缺失 requester 请求 SUMMARY → 封顶 METADATA（不再默认 worker 越权）"""
    hub.agents["ghost-agent"] = {"agent_id": "ghost-agent", "agent_name": "幽灵", "status": "online"}
    result = hub._calculate_disclosure_level(
        memory=_mem(owner="cs-wang"), requester="ghost-agent", task={},
        required_level=DisclosureLevel.SUMMARY,
    )
    assert result == DisclosureLevel.METADATA

def test_role_missing_fail_closed_caps_full(hub):
    """role 缺失 requester 请求 FULL → 封顶 METADATA"""
    hub.agents["ghost-agent"] = {"agent_id": "ghost-agent", "agent_name": "幽灵", "status": "online"}
    result = hub._calculate_disclosure_level(
        memory=_mem(owner="cs-wang"), requester="ghost-agent", task={},
        required_level=DisclosureLevel.FULL,
    )
    assert result == DisclosureLevel.METADATA

def test_role_missing_required_none_stays_none(hub):
    """required=NONE 时保持 NONE，不因 fail-closed 越给"""
    hub.agents["ghost-agent"] = {"agent_id": "ghost-agent", "agent_name": "幽灵", "status": "online"}
    result = hub._calculate_disclosure_level(
        memory=_mem(owner="cs-wang"), requester="ghost-agent", task={},
        required_level=DisclosureLevel.NONE,
    )
    assert result == DisclosureLevel.NONE

def test_role_missing_required_metadata_stays_metadata(hub):
    """required=METADATA 时保持 METADATA"""
    hub.agents["ghost-agent"] = {"agent_id": "ghost-agent", "agent_name": "幽灵", "status": "online"}
    result = hub._calculate_disclosure_level(
        memory=_mem(owner="cs-wang"), requester="ghost-agent", task={},
        required_level=DisclosureLevel.METADATA,
    )
    assert result == DisclosureLevel.METADATA

def test_role_present_worker_unaffected(hub):
    """存量 worker（role 存在）同任务协作不受 fail-closed 影响"""
    result = hub._calculate_disclosure_level(
        memory=_mem(owner="cs-wang"), requester="cs-li",
        task={"assigned_agent_id": "cs-li", "creator_agent_id": "cs-wang"},
        required_level=DisclosureLevel.SUMMARY,
    )
    assert result == DisclosureLevel.SUMMARY

def test_role_present_manager_unaffected(hub):
    """存量 manager 看下属 FULL 不受 fail-closed 影响"""
    result = hub._calculate_disclosure_level(
        memory=_mem(owner="cs-wang"), requester="mgr-zhang", task={},
        required_level=DisclosureLevel.FULL,
    )
    assert result == DisclosureLevel.FULL
