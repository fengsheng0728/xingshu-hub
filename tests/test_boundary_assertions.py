# -*- coding: utf-8 -*-
"""BOUNDARY.md 生效断言 — 阶段3-P2 交付3

双向锚点（声明 ↔ 实际行为，任一侧漂移即红）：

A. 生成方向：BOUNDARY.md 必须完整渲染代码源
   - 第 1 节披露规则表 ↔ disclosure_rules.rule_table()
   - 第 3 节机密词库 ↔ sensitivity.DEFAULT_SECRET_KEYWORDS
B. 行为方向：BOUNDARY 声明的每条链在实际代码行为上成立
   - 披露规则链 §1/§4 ↔ disclosure.py DisclosureEngine 真实判定
   - 敏感度 6 维链 §2 ↔ sensitivity.classify 真实打标
   - 分干隔离声明 §5 ↔ data_trunk / shadow 实际隔离行为
"""
import json
import os
from types import SimpleNamespace

import pytest

import data_trunk as dt_mod
from data_trunk import DataTrunk, _boundary_md
from disclosure import DisclosureEngine
from disclosure_rules import rule_table
from hub_mixins.shadow import ShadowWriter
from models import DisclosureLevel
from sensitivity import DEFAULT_SECRET_KEYWORDS, classify


@pytest.fixture()
def boundary():
    return _boundary_md(SimpleNamespace())


# ═══════════ A. 生成方向：代码源 → BOUNDARY 完整渲染 ═══════════

def test_all_disclosure_rules_rendered(boundary):
    """rule_table() 每条规则的 id/名称/描述必须出现在第 1 节（缺一条即红）"""
    rules = rule_table()
    assert "## 1. 披露规则链（%d 条" % len(rules) in boundary
    for r in rules:
        assert f"`{r['id']}`" in boundary, f"规则 {r['id']} 未渲染进 BOUNDARY"
        assert r["name"] in boundary, f"规则 {r['id']} 名称缺失"
        assert (r["desc"] or "").replace("|", "\\|") in boundary


def test_all_secret_keywords_rendered(boundary):
    """DEFAULT_SECRET_KEYWORDS 全词渲染进第 3 节（词库改了 BOUNDARY 必须跟上）"""
    words = DEFAULT_SECRET_KEYWORDS
    assert "## 3. 机密词库（内置默认 %d 词" % len(words) in boundary
    for w in words:
        assert w in boundary, f"机密词 {w} 未渲染进 BOUNDARY"


def test_static_sections_present(boundary):
    """第 2/4/5/6 节静态声明锚点（六维链/角色分级/分干隔离/审计要求）"""
    for anchor in ("## 2. 敏感度 6 维判定链", "## 4. 角色分级",
                   "## 5. 分干隔离声明", "## 6. 审计要求",
                   "分干之间", "永不互读", "内容不上行", "独立 git 仓库",
                   "chain-head.jsonl", "worker", "manager", "orchestrator"):
        assert anchor in boundary, anchor


def test_boundary_generation_idempotent():
    """生成幂等：内容只依赖代码源，两次生成完全一致（不含时间戳）"""
    assert _boundary_md(SimpleNamespace()) == _boundary_md(SimpleNamespace())


# ═══════════ B1. 披露规则链 §1/§4 ↔ disclosure.py 实际行为 ═══════════

def _engine(tmp_path, monkeypatch, agents, policy=None):
    """真实 DisclosureEngine + 最小 hub stub；DB 指向不存在路径（组/员工查询静默降级）"""
    from models import CONFIG
    monkeypatch.setattr(CONFIG, "DB_PATH", str(tmp_path / "none.db"))
    hub = SimpleNamespace(
        agents=agents,
        _disclosure_policy=policy or {
            "default_manager_level": "summary",
            "orchestrator_max_level": "full",
            "allow_peer_disclosure": True,
            "department_peer_visibility": False,
        })
    return DisclosureEngine(hub)


def _mem(owner, level="summary", allowed=None):
    return {"owner_agent_id": owner, "disclosure_level": level,
            "allowed_viewers": json.dumps(allowed or [])}


def test_r1_self_full(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch, {})
    assert eng._calculate_disclosure_level(
        _mem("ag-a"), "ag-a", {}, DisclosureLevel.SUMMARY) == DisclosureLevel.FULL


def test_r2_whitelist_full(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch, {})
    assert eng._calculate_disclosure_level(
        _mem("ag-a", allowed=["ag-b"]), "ag-b", {},
        DisclosureLevel.SUMMARY) == DisclosureLevel.FULL


def test_r3_memory_none_blocks(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch, {})
    assert eng._calculate_disclosure_level(
        _mem("ag-a", level="none"), "ag-b", {},
        DisclosureLevel.FULL) == DisclosureLevel.NONE


def test_r5_manager_subordinate(tmp_path, monkeypatch):
    agents = {"mgr": {"role": "manager", "managed_agents": ["ag-a"]},
              "ag-a": {"role": "worker"}}
    eng = _engine(tmp_path, monkeypatch, agents)
    assert eng._calculate_disclosure_level(
        _mem("ag-a"), "mgr", {}, DisclosureLevel.SUMMARY) == DisclosureLevel.SUMMARY


def test_r6_orchestrator_global(tmp_path, monkeypatch):
    agents = {"boss": {"role": "orchestrator"}, "ag-a": {"role": "worker"}}
    eng = _engine(tmp_path, monkeypatch, agents)
    assert eng._calculate_disclosure_level(
        _mem("ag-a"), "boss", {}, DisclosureLevel.FULL) == DisclosureLevel.FULL


def test_r7_peer_unrelated_none(tmp_path, monkeypatch):
    agents = {"ag-a": {"role": "worker"}, "ag-b": {"role": "worker"}}
    eng = _engine(tmp_path, monkeypatch, agents)
    # 不同部门、无任务关系 → NONE
    assert eng._calculate_disclosure_level(
        _mem("ag-a"), "ag-b", {}, DisclosureLevel.SUMMARY) == DisclosureLevel.NONE


def test_r8_mem_level_cap(tmp_path, monkeypatch):
    agents = {"ag-b": {"role": "worker"}, "ag-a": {"role": "manager"}}
    eng = _engine(tmp_path, monkeypatch, agents)
    # 请求 FULL 超过记忆存储级 summary → 截断到 summary
    assert eng._calculate_disclosure_level(
        _mem("ag-a", level="summary"), "ag-b", {},
        DisclosureLevel.FULL) == DisclosureLevel.SUMMARY


def test_r10_default_none(tmp_path, monkeypatch):
    agents = {"ag-b": {"role": "worker"}, "ag-a": {"role": "manager"}}
    eng = _engine(tmp_path, monkeypatch, agents)
    # 兜底：请求不超存储级、无其他规则命中 → NONE
    assert eng._calculate_disclosure_level(
        _mem("ag-a", level="summary"), "ag-b", {},
        DisclosureLevel.SUMMARY) == DisclosureLevel.NONE


def test_role_fail_closed_unknown_requester(tmp_path, monkeypatch):
    """§4 角色分级 fail-closed：完全未知主体 → METADATA 封顶（不默认 worker 越权）"""
    eng = _engine(tmp_path, monkeypatch, {"ag-a": {"role": "worker"}})
    assert eng._calculate_disclosure_level(
        _mem("ag-a"), "ghost", {}, DisclosureLevel.FULL) == DisclosureLevel.METADATA


# ═══════════ B2. 敏感度 6 维链 §2 ↔ sensitivity.classify 实际行为 ═══════════

def test_dim1_untrusted_locked_none():
    r = classify("普通业务内容", trust_level="untrusted")
    assert r["level"] == "none" and r["locked"] and r["rule"] == "r1_trust"


def test_dim1_fail_closed_order_first():
    """UNTRUSTED + PII 同时存在 → r1 先命中（声明的顺序即停）"""
    r = classify("电话 13812345678", trust_level="untrusted")
    assert r["rule"] == "r1_trust"


def test_dim2_pii_locked_none_masked():
    r = classify("客户联系电话 13812345678 非诚勿扰")
    assert r["level"] == "none" and r["locked"] and r["rule"] == "r2_pii"
    assert r["pii_hits"], "PII 命中应有掩码样本"
    # E.1 红线：原始串不落结果（只留掩码）
    assert "13812345678" not in json.dumps(r, ensure_ascii=False)


def test_dim3_every_secret_keyword_caps_summary():
    """词库每个词实际生效：命中 → 不超 SUMMARY（词库与行为双向锚点）"""
    for w in DEFAULT_SECRET_KEYWORDS:
        r = classify(f"这份{w}请查收", owner_role="manager")
        assert r["level"] in ("none", "summary"), \
            f"机密词 {w} 未封顶: {r['level']}"
        assert r["sensitivity_score"] >= 0.7, f"机密词 {w} 未加分"


def test_dim4_worker_role_caps_summary():
    content = ("今天门店接待三组来访客人，分别看了浴室柜、花洒和智能马桶，"
               "意向中等偏上，已留下联系方式，约下周二回访跟进设计方案与送货周期。")
    assert len(content) > 50
    r = classify(content, owner_role="worker")
    assert r["level"] == "summary"
    assert any("r4_role" in x for x in r["reasons"])


def test_dim5_parent_none_inherits_none():
    r = classify("任意内容", parent_level="none")
    assert r["level"] == "none"


def test_dim6_kind_ordering():
    """类型映射：secret 最严 > fact > todo（声明的 secret 最高 / todo 最低）"""
    s_secret = classify("同内容", kind="secret")["sensitivity_score"]
    s_fact = classify("同内容", kind="fact")["sensitivity_score"]
    s_todo = classify("同内容", kind="todo")["sensitivity_score"]
    assert s_secret > s_fact > s_todo


# ═══════════ B3. 分干隔离声明 §5 ↔ data_trunk/shadow 实际行为 ═══════════

def _dt(tmp_path, branches=None):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": True,
                           "wiki": True, "shared": True},
        DATA_TRUNK_BRANCHES=branches,
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    return dt


def test_branch_isolation_independent_repos(tmp_path):
    """「每个分干 = 独立 git 仓库」：独立 root + 独立 .git + 互不包含对方提交"""
    dt = _dt(tmp_path, branches={"ag-a": "proj-x"})
    dt.ensure_branch("proj-x", agent_id="ag-a")
    br_d, br_x = dt.branch_repo("default"), dt.branch_repo("proj-x")
    assert br_d.root != br_x.root
    assert os.path.isdir(os.path.join(br_d.root, ".git"))
    assert os.path.isdir(os.path.join(br_x.root, ".git"))
    # 「分干之间永不互读」行为锚点：写入 proj-x 的内容在 default 不可见
    w = ShadowWriter(dt)
    w.submit("memory", {"memory_id": "m-iso", "owner": "ag-a",
                        "memory_key": "k", "content": "分干隔离验证内容",
                        "trust": "internal", "level": "summary", "tags": [],
                        "date": "2026-09-01"})
    w._drain_once()
    assert br_x.read_at("vault/memory/2026-09-01/m-iso.md") is not None
    assert br_d.read_at("vault/memory/2026-09-01/m-iso.md") is None


def test_index_never_carries_content(tmp_path):
    """「index/ 仅元数据级，内容不上行」：index 条目无 content 字段、无正文文本"""
    dt = _dt(tmp_path)
    w = ShadowWriter(dt)
    secret_body = "正文绝不进索引-9f3e2a"
    w.submit("memory", {"memory_id": "m-noleak", "owner": "ag-a",
                        "memory_key": "k", "content": secret_body,
                        "trust": "internal", "level": "summary", "tags": [],
                        "date": "2026-09-01"})
    w._drain_once()
    idx = dt.trunk.read_at("index/memory.jsonl")
    assert secret_body not in idx
    rec = json.loads(idx.strip().splitlines()[0])
    assert "content" not in rec
