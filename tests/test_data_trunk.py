# -*- coding: utf-8 -*-
"""data_trunk.py 单测 — 阶段3-P0 主干-分干数据底座"""
import json
import os
from types import SimpleNamespace

import pytest

from data_trunk import DataTrunk, key_fingerprint


def _cfg(root, enabled=True):
    return SimpleNamespace(
        DATA_TRUNK_ENABLED=enabled,
        DATA_TRUNK_ROOT=str(root),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": True, "wiki": True, "shared": True},
    )


def test_disabled_noop(tmp_path):
    dt = DataTrunk(_cfg(tmp_path, enabled=False))
    assert dt.ensure() is True
    # 不建任何目录
    assert not os.path.exists(os.path.join(str(tmp_path), "data-trunk"))
    assert dt.sync_identity([{"agent_id": "a", "api_key": "secret"}]) is True


def test_enabled_creates_structure(tmp_path):
    dt = DataTrunk(_cfg(tmp_path))
    assert dt.ensure()
    root = dt.root
    for d in ("identity", "customers", "index", "audit", "projects", "branches"):
        assert os.path.isdir(os.path.join(root, d)), d
    # 分干仓库
    br = dt.branch_root()
    for d in ("inbox", "vault", "_originals", "audit"):
        assert os.path.isdir(os.path.join(br, d)), d
    # 有 commit 历史
    assert len(dt.trunk.log()) >= 1
    assert len(dt.branch_repo().log()) >= 1


def test_boundary_contains_rule_anchors(tmp_path):
    dt = DataTrunk(_cfg(tmp_path))
    dt.ensure()
    bd = dt.trunk.read_at("BOUNDARY.md")
    assert bd is not None
    for anchor in ("r1_self", "r9_sensitivity_cap", "r10_default_none",
                   "分干之间", "内容不上行", "6 维", "合同价", "orchestrator"):
        assert anchor in bd, anchor


def test_identity_mirror_fingerprint(tmp_path):
    dt = DataTrunk(_cfg(tmp_path))
    dt.ensure()
    rows = [
        {"agent_id": "agent-x", "agent_name": "客服甲", "role": "worker",
         "department": "客服部", "api_key": "plain-secret-key-123",
         "registered_at": "2026-08-01T00:00:00+00:00"},
    ]
    assert dt.sync_identity(rows)
    agents = dt.trunk.read_at("identity/agents.jsonl")
    keys = dt.trunk.read_at("identity/keys.jsonl")
    assert agents is not None and keys is not None
    # 明文 key 绝不落仓库
    assert "plain-secret-key-123" not in agents
    assert "plain-secret-key-123" not in keys
    a = json.loads(agents.strip().splitlines()[0])
    assert a["key_fp"] == key_fingerprint("plain-secret-key-123")
    assert a["key_fp"] != "plain-secret-key-123"
    assert a["agent_id"] == "agent-x"
    assert a["role"] == "worker"


def test_identity_idempotent_no_new_commit(tmp_path):
    dt = DataTrunk(_cfg(tmp_path))
    dt.ensure()
    rows = [{"agent_id": "a1", "agent_name": "n", "role": "worker",
             "department": "", "api_key": "k1", "registered_at": "2026-08-01T00:00:00+00:00"}]
    assert dt.sync_identity(rows)
    n1 = len(dt.trunk.log())
    # 相同内容二次 sync → 无新 commit
    assert dt.sync_identity(rows)
    assert len(dt.trunk.log()) == n1
    # 内容变化 → 新 commit
    rows[0]["role"] = "manager"
    assert dt.sync_identity(rows)
    assert len(dt.trunk.log()) > n1


def test_branch_repo_structure(tmp_path):
    dt = DataTrunk(_cfg(tmp_path))
    dt.ensure()
    br = dt.branch_repo()
    assert br.exists()
    assert os.path.isdir(os.path.join(br.root, "vault"))
    # 分干指针登记
    branches = dt.trunk.read_at("projects/branches.jsonl")
    assert branches is not None
    rec = json.loads(branches.strip().splitlines()[0])
    assert rec["branch"] == "default"
    assert rec["status"] == "active"


def test_key_fingerprint_sha256():
    fp = key_fingerprint("abc")
    assert len(fp) == 16
    assert fp.isalnum()
    assert key_fingerprint("abc") == key_fingerprint("abc")
    assert key_fingerprint("abc") != key_fingerprint("abd")
    assert key_fingerprint("") == ""
    assert key_fingerprint(None) == ""
