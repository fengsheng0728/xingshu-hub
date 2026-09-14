# -*- coding: utf-8 -*-
"""scope ↔ 分干映射 — 阶段3-P2 交付1

key_scopes 三层 scope（endpoints/data_domain/level_cap）决定可写分干：
- 默认全走 default（D1：映射表预留，不提前抽象）
- config.data_trunk.branches: {agent_id: branch} 显式映射优先
- scope.data_domain 命中已登记分干名 → 该分干
"""
import json
import os
from types import SimpleNamespace

import pytest

from data_trunk import DataTrunk
from hub_mixins.shadow import ShadowWriter


def _cfg(root, branches=None):
    return SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(root),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": True,
                           "wiki": True, "shared": True},
        DATA_TRUNK_BRANCHES=branches,
    )


# ═══════════ 1. 映射解析优先级 ═══════════

def test_default_branch_without_mapping(tmp_path):
    """无映射表 / 无 scope → 一律默认分干（D1）"""
    dt = DataTrunk(_cfg(tmp_path / "dt"))
    dt.ensure()
    assert dt.branch_for_agent("ag-x") == "default"
    assert dt.branch_for_agent("") == "default"
    # 三层 scope 不含有效分干信息 → default
    scope = {"endpoints": ["/api/v1/memory/store"], "data_domain": [],
             "level_cap": "summary"}
    assert dt.branch_for_agent("ag-x", scope) == "default"
    # data_domain 指向未登记的分干名 → 不生效，仍 default
    assert dt.branch_for_agent("ag-x", {"data_domain": ["ghost"]}) == "default"


def test_explicit_mapping_wins(tmp_path):
    """config.data_trunk.branches {agent_id: branch} 显式映射优先"""
    dt = DataTrunk(_cfg(tmp_path / "dt", branches={"ag-a": "proj-x"}))
    dt.ensure()
    assert dt.branch_for_agent("ag-a") == "proj-x"
    # 显式映射优先于 scope.data_domain
    assert dt.branch_for_agent("ag-a", {"data_domain": ["default"]}) == "proj-x"
    # 未映射 agent 仍走 default
    assert dt.branch_for_agent("ag-b") == "default"


def test_scope_data_domain_maps_to_registered_branch(tmp_path):
    """scope.data_domain 命中已登记分干名 → 写到该分干"""
    dt = DataTrunk(_cfg(tmp_path / "dt"))
    dt.ensure()
    dt.ensure_branch("proj-y", agent_id="ag-c")
    scope = {"endpoints": [], "data_domain": ["proj-y"], "level_cap": ""}
    assert dt.branch_for_agent("ag-c", scope) == "proj-y"


# ═══════════ 2. ensure_branch 分干登记 ═══════════

def test_ensure_branch_registers_pointer(tmp_path):
    dt = DataTrunk(_cfg(tmp_path / "dt"))
    dt.ensure()
    assert dt.ensure_branch("proj-x", agent_id="ag-a")
    # 分干仓库结构
    br = dt.branch_repo("proj-x")
    assert br.exists()
    assert os.path.isdir(os.path.join(br.root, "vault"))
    assert len(br.log()) >= 1
    # 主干 projects/branches.jsonl 登记 + mapped_agents
    text = dt.trunk.read_at("projects/branches.jsonl")
    recs = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    rec = next(r for r in recs if r["branch"] == "proj-x")
    assert rec["status"] == "active"
    assert "ag-a" in rec["mapped_agents"]
    # 幂等：重复登记不新增行
    assert dt.ensure_branch("proj-x", agent_id="ag-a")
    text2 = dt.trunk.read_at("projects/branches.jsonl")
    n = len([ln for ln in text2.splitlines()
             if ln.strip() and json.loads(ln)["branch"] == "proj-x"])
    assert n == 1
    # 追加映射第二个 agent
    assert dt.ensure_branch("proj-x", agent_id="ag-b")
    text3 = dt.trunk.read_at("projects/branches.jsonl")
    rec3 = next(json.loads(ln) for ln in text3.splitlines()
                if ln.strip() and json.loads(ln)["branch"] == "proj-x")
    assert set(rec3["mapped_agents"]) == {"ag-a", "ag-b"}


def test_ensure_branch_disabled_noop(tmp_path):
    cfg = _cfg(tmp_path / "dt")
    cfg.DATA_TRUNK_ENABLED = False
    dt = DataTrunk(cfg)
    assert dt.ensure_branch("proj-x") is True  # 静默成功，不建目录
    assert not os.path.exists(os.path.join(str(tmp_path), "dt", "branches", "proj-x"))


# ═══════════ 3. 影子写入走映射分干 ═══════════

def test_shadow_writes_to_mapped_branch(tmp_path):
    """mapped agent 的影子镜像落自己的分干，default 分干零污染"""
    dt = DataTrunk(_cfg(tmp_path / "dt", branches={"ag-a": "proj-x"}))
    dt.ensure()
    w = ShadowWriter(dt)
    w.submit("memory", {"memory_id": "m-mapped", "owner": "ag-a",
                        "memory_key": "k", "content": "机密内容",
                        "trust": "internal", "level": "summary",
                        "tags": [], "date": "2026-09-01"})
    w.submit("memory", {"memory_id": "m-plain", "owner": "ag-b",
                        "memory_key": "k2", "content": "普通内容",
                        "trust": "internal", "level": "summary",
                        "tags": [], "date": "2026-09-01"})
    w._drain_once()
    # ag-a → proj-x 分干
    br_x = dt.branch_repo("proj-x")
    md = br_x.read_at("vault/memory/2026-09-01/m-mapped.md")
    assert md is not None and "机密内容" in md
    # ag-b 未映射 → default 分干
    br_d = dt.branch_repo("default")
    assert br_d.read_at("vault/memory/2026-09-01/m-plain.md") is not None
    # 隔离：proj-x 不含 ag-b 的，default 不含 ag-a 的
    assert br_x.read_at("vault/memory/2026-09-01/m-plain.md") is None
    assert br_d.read_at("vault/memory/2026-09-01/m-mapped.md") is None
    # 主干 index 元数据记录各自分干
    idx = dt.trunk.read_at("index/memory.jsonl")
    recs = {json.loads(ln)["id"]: json.loads(ln)
            for ln in idx.splitlines() if ln.strip()}
    assert recs["m-mapped"]["branch"] == "proj-x"
    assert recs["m-plain"]["branch"] == "default"
    # 内容不上行红线不变
    assert "机密内容" not in idx and "普通内容" not in idx
    # 新分干自动登记 + mapped_agents
    text = dt.trunk.read_at("projects/branches.jsonl")
    rec = next(json.loads(ln) for ln in text.splitlines()
               if ln.strip() and json.loads(ln)["branch"] == "proj-x")
    assert "ag-a" in rec["mapped_agents"]


def test_shadow_default_branch_unchanged(tmp_path):
    """无映射时影子行为与 P1 完全一致（回归保护）"""
    dt = DataTrunk(_cfg(tmp_path / "dt"))
    dt.ensure()
    w = ShadowWriter(dt)
    w.submit("memory", {"memory_id": "m1", "owner": "ag-a", "content": "x",
                        "trust": "internal", "level": "summary", "tags": [],
                        "date": "2026-09-01"})
    w._drain_once()
    assert dt.branch_repo("default").read_at(
        "vault/memory/2026-09-01/m1.md") is not None
    idx = dt.trunk.read_at("index/memory.jsonl")
    assert json.loads(idx.strip().splitlines()[0])["branch"] == "default"


# ═══════════ 4. 配置层：data_trunk.branches 映射段 ═══════════

def test_yaml_branches_override(tmp_path, monkeypatch):
    """config.yaml data_trunk.branches 段 → CONFIG.DATA_TRUNK_BRANCHES"""
    import models
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "data_trunk:\n  enabled: true\n  branches:\n    ag-a: proj-x\n",
        encoding="utf-8")
    monkeypatch.setenv("SYNC_HUB_CONFIG_DIR", str(cfg_dir))
    overrides = models._load_config_from_yaml()
    assert overrides.get("DATA_TRUNK_BRANCHES") == {"ag-a": "proj-x"}


def test_config_branches_default_none():
    """Config 类默认 DATA_TRUNK_BRANCHES=None（映射表预留，默认全走 default）"""
    from models import Config
    assert Config.DATA_TRUNK_BRANCHES is None
