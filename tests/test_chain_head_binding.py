# -*- coding: utf-8 -*-
"""审计绑定：git 链头 ↔ 哈希链互证 — 阶段3-P2 交付4

每次影子批 commit 后，把当前 audit_log 哈希链链头追加到主干
audit/chain-head.jsonl 并随锚点登记 commit 落 git：
- git 历史证明「某时刻链头是 X」（仓库历史不可篡改）
- 哈希链证明「审计记录未被改写」（链式依赖）
两者互证；与 export_anchor 外部介质锚定并存不冲突。
"""
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from audit_chain import GENESIS, AuditChain, current_chain_head
from data_trunk import DataTrunk
from hub_mixins.shadow import ShadowWriter


def _mk_db(tmp_path) -> str:
    """独立测试 db（audit_log 主链表），绝不碰生产 sync_hub.db"""
    db = str(tmp_path / "audit.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE audit_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_type TEXT, ref_table TEXT, ref_id TEXT, payload TEXT,
        prev_hash TEXT, entry_hash TEXT, created_at TEXT)""")
    conn.commit()
    conn.close()
    return db


def _mk_dt(tmp_path):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": True,
                           "wiki": True, "shared": True},
        DATA_TRUNK_BRANCHES=None,
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    return dt


def _flush_one(w, mid="m-ch"):
    w.submit("memory", {"memory_id": mid, "owner": "ag-a", "memory_key": "k",
                        "content": "x", "trust": "internal", "level": "summary",
                        "tags": [], "date": "2026-09-01"})
    w._drain_once()


def _chain_head_lines(dt):
    text = dt.trunk.read_at("audit/chain-head.jsonl")
    if text is None:
        return []
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# ═══════════ 1. current_chain_head 语义 ═══════════

def test_chain_head_genesis_fallbacks(tmp_path):
    """空链 / 无表 → GENESIS 兜底；库文件不可连 → 空串（静默降级）"""
    db = _mk_db(tmp_path)
    assert current_chain_head(db) == GENESIS  # 空表
    bare = str(tmp_path / "bare.db")
    sqlite3.connect(bare).close()  # 无 audit_log 表
    assert current_chain_head(bare) == GENESIS
    assert current_chain_head(str(tmp_path / "no" / "such.db")) == ""


def test_chain_head_matches_audit_tail(tmp_path):
    db = _mk_db(tmp_path)
    ac = AuditChain(db)
    r = ac.append("event", "t", "1", {"a": 1})
    assert current_chain_head(db) == r["entry_hash"]
    r2 = ac.append("event", "t", "2", {"a": 2})
    assert current_chain_head(db) == r2["entry_hash"] != r["entry_hash"]


# ═══════════ 2. 每 git commit 追加链头 ═══════════

def test_chain_head_appended_after_batch_commit(tmp_path):
    """影子批 commit → chain-head.jsonl 追加 {git_commit, chain_head} 且已入 git"""
    db = _mk_db(tmp_path)
    ac = AuditChain(db)
    head = ac.append("event", "t", "1", {"a": 1})["entry_hash"]
    dt = _mk_dt(tmp_path)
    w = ShadowWriter(dt, audit_db_path=db)
    _flush_one(w)
    lines = _chain_head_lines(dt)
    assert len(lines) == 1
    rec = lines[0]
    # 链头与 audit_chain 当前链头一致
    assert rec["chain_head"] == head == current_chain_head(db)
    # git_commit 指向真实存在的数据 commit（互证：git 历史可回溯）
    hashes = [c["hash"] for c in dt.trunk.log(50)]
    assert rec["git_commit"] in hashes
    assert rec["ts"]
    # 文件本身已 commit（read_at 读的是 HEAD）
    assert dt.trunk.read_at("audit/chain-head.jsonl") is not None


def test_chain_head_tracks_chain_growth(tmp_path):
    """审计链增长 → 下一批记录的链头随之更新（逐批快照互证）"""
    db = _mk_db(tmp_path)
    ac = AuditChain(db)
    h1 = ac.append("event", "t", "1", {"a": 1})["entry_hash"]
    dt = _mk_dt(tmp_path)
    w = ShadowWriter(dt, audit_db_path=db)
    _flush_one(w, mid="m-1")
    h2 = ac.append("event", "t", "2", {"a": 2})["entry_hash"]
    _flush_one(w, mid="m-2")
    lines = _chain_head_lines(dt)
    assert [ln["chain_head"] for ln in lines] == [h1, h2]
    # 两条记录的 git_commit 不同（各自绑定自己的批 commit）
    assert lines[0]["git_commit"] != lines[1]["git_commit"]
    # 最终一致：文件尾行链头 == 当前链头
    assert lines[-1]["chain_head"] == current_chain_head(db)


def test_chain_head_genesis_when_audit_empty(tmp_path):
    """审计链为空时记录 GENESIS（语义与 AuditChain._tail_hash 一致）"""
    db = _mk_db(tmp_path)
    dt = _mk_dt(tmp_path)
    w = ShadowWriter(dt, audit_db_path=db)
    _flush_one(w)
    lines = _chain_head_lines(dt)
    assert lines[0]["chain_head"] == GENESIS


# ═══════════ 3. 降级与隔离 ═══════════

def test_no_db_path_skips_chain_head(tmp_path):
    """未配置 audit_db_path → 不写 chain-head.jsonl（交付1/2 既有行为零影响）"""
    dt = _mk_dt(tmp_path)
    w = ShadowWriter(dt)
    _flush_one(w)
    assert dt.trunk.read_at("audit/chain-head.jsonl") is None
    # commits 锚点仍正常
    assert dt.trunk.read_at("index/.commits.jsonl") is not None


def test_db_failure_silent(tmp_path):
    """审计库不可连 → 影子批写入照常成功，链头记空串（D4 不阻塞主链路）"""
    dt = _mk_dt(tmp_path)
    w = ShadowWriter(dt, audit_db_path=str(tmp_path / "no" / "x.db"))
    _flush_one(w)
    assert w.stats["failures"] == 0
    lines = _chain_head_lines(dt)
    assert lines and lines[0]["chain_head"] == ""
    # 数据本体仍落分干
    assert dt.branch_repo().read_at("vault/memory/2026-09-01/m-ch.md") is not None


def test_disabled_noop(tmp_path):
    """enabled=false → 完全不写（影子铁律）"""
    db = _mk_db(tmp_path)
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=False, DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default", DATA_TRUNK_SHADOW={}, 
        DATA_TRUNK_BRANCHES=None)
    dt = DataTrunk(cfg)
    w = ShadowWriter(dt, audit_db_path=db)
    w.submit("memory", {"memory_id": "m-x", "owner": "a", "content": "x"})
    w._drain_once()
    assert not os.path.exists(os.path.join(str(tmp_path), "dt"))
