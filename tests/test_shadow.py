# -*- coding: utf-8 -*-
"""ShadowWriter 单测 — 阶段3-P1 影子双写"""
import json
import os
from types import SimpleNamespace

import pytest

from data_trunk import DataTrunk
from hub_mixins.shadow import ShadowWriter


def _dt(tmp_path, switches=None):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW=switches or {"memory": True, "knowledge": True,
                                       "wiki": True, "shared": True},
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    return dt


def _writer(tmp_path, switches=None):
    dt = _dt(tmp_path, switches)
    w = ShadowWriter(dt)
    w.start()
    return w


def test_disabled_noop(tmp_path):
    dt = _dt(tmp_path)
    dt.enabled = False
    w = ShadowWriter(dt)
    w.start()  # 不应启动线程
    w.submit("memory", {"memory_id": "m1", "content": "x"})
    assert w.stats["submitted"] == 0
    assert w.queue_depth() == 0


def test_kind_switch_off(tmp_path):
    w = _writer(tmp_path, {"memory": False, "knowledge": True,
                           "wiki": True, "shared": True})
    w.submit("memory", {"memory_id": "m1", "content": "x"})
    assert w.stats["submitted"] == 0
    w.submit("knowledge", {"entry_id": "k1", "content": "y"})
    assert w.stats["submitted"] == 1


def test_flush_writes_vault_and_index(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", {"memory_id": "m-abc", "owner": "agent-1",
                        "memory_key": "客户偏好", "content": "客户不吃辣",
                        "trust": "internal", "level": "summary",
                        "tags": ["偏好"], "date": "2026-08-31"})
    # 直接触发批处理（清空队列）
    w._drain_once()
    dt = w.dt
    # vault 打标文件
    md = dt.branch_repo().read_at("vault/memory/2026-08-31/m-abc.md")
    assert md is not None
    assert "客户不吃辣" in md
    assert "trust: internal" in md
    assert "level: summary" in md
    assert "tags: [偏好]" in md
    # 主干 index 元数据
    idx = dt.trunk.read_at("index/memory.jsonl")
    assert idx is not None
    rec = json.loads(idx.strip().splitlines()[0])
    assert rec["id"] == "m-abc"
    assert rec["owner"] == "agent-1"
    # 蓝图红线：index 不含 content 全文
    assert "客户不吃辣" not in idx


def test_index_dedup_same_id(tmp_path):
    w = _writer(tmp_path)
    for _ in range(3):
        w.submit("memory", {"memory_id": "dup-1", "owner": "a", "content": "x",
                            "trust": "internal", "level": "summary",
                            "tags": [], "date": "2026-08-31"})
    w._drain_once()
    idx = w.dt.trunk.read_at("index/memory.jsonl")
    n = len([ln for ln in idx.splitlines() if ln.strip()])
    assert n == 1, f"同 id 重复提交应去重,实际 {n} 行"


def test_four_kinds_payloads(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", {"memory_id": "m1", "owner": "a", "memory_key": "k",
                        "content": "c1", "trust": "internal", "level": "full",
                        "tags": [], "date": "2026-08-31"})
    w.submit("knowledge", {"entry_id": "k1", "title": "t", "content": "c2",
                           "created_by": "b", "tags": ["x"], "date": "2026-08-31"})
    w.submit("wiki", {"doc_id": "doc1", "piece_index": 0, "content": "c3",
                      "source_agent_id": "c", "trust": "external", "level": "none",
                      "date": "2026-08-31"})
    w.submit("shared", {"doc_id": "d1", "title": "s", "created_by": "d",
                        "trust": "internal", "level": "summary", "date": "2026-08-31"})
    w._drain_once()
    br = w.dt.branch_repo()
    assert br.read_at("vault/memory/2026-08-31/m1.md") is not None
    assert br.read_at("vault/knowledge/2026-08-31/k1.md") is not None
    assert br.read_at("vault/wiki/doc1/000.md") is not None
    assert br.read_at("vault/shared/2026-08-31/d1.md") is not None
    for kind in ("memory", "knowledge", "wiki", "shared"):
        assert w.dt.trunk.read_at(f"index/{kind}.jsonl") is not None
    # 两仓库都有 commit
    assert len(br.log()) >= 2
    assert len(w.dt.trunk.log()) >= 2


def test_batch_commit_message(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", {"memory_id": "m1", "owner": "a", "content": "x",
                        "trust": "internal", "level": "summary", "tags": [],
                        "date": "2026-08-31"})
    w._drain_once()
    log = w.dt.trunk.log()
    assert any("阶段3-P1" in c["subject"] for c in log)


def test_colon_in_entry_id_windows_safe(tmp_path):
    """entry_id 含冒号（如 'doc:p1-doc-0'）必须安全化——实测直接写抛 OSError。"""
    w = _writer(tmp_path)
    w.submit("knowledge", {"entry_id": "doc:p1-doc-0", "title": "t",
                           "content": "c", "created_by": "b", "tags": [],
                           "date": "2026-08-31"})
    w._drain_once()
    br = w.dt.branch_repo()
    # 冒号替换为下划线
    assert br.read_at("vault/knowledge/2026-08-31/doc_p1-doc-0.md") is not None
    assert w.stats["failures"] == 0
    # index 里的 id 保持原始值（可回溯）
    idx = w.dt.trunk.read_at("index/knowledge.jsonl")
    assert '"doc:p1-doc-0"' in idx


def test_stop_flushes_remaining(tmp_path):
    w = _writer(tmp_path)
    w.submit("memory", {"memory_id": "m-last", "owner": "a", "content": "x",
                        "trust": "internal", "level": "summary", "tags": [],
                        "date": "2026-08-31"})
    w.stop(flush=True)
    assert w.dt.branch_repo().read_at("vault/memory/2026-08-31/m-last.md") is not None
    assert w.stats["flushed"] >= 1
