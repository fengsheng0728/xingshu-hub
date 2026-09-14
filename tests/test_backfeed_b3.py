# -*- coding: utf-8 -*-
"""tests/test_backfeed_b3.py — 阶段4-B3：cos 相似度三档分流

对齐 docs/phase4-backfeed-design.md §6-B3 验收：
① 注入假 embed_fn 的三档边界测试(0.59/0.6/0.9/0.91 语义 → 用 0.95/0.75/0.5
   + 精确边界 0.9/0.6：>=auto 自动合并、floor<=cos<auto 进 review_candidates、
   <floor 各自保留)
② 阈值随 provider 版本化失效测试(@hasher 键在 sentence 档不生效)

方法：2N 维正交子空间——每对独占两维，pair i 向量 = e[2i] 与 cosθ·e[2i]+sinθ·e[2i+1]，
pair 间 cos=0 互不干扰，pair 内 cos=cosθ 精确可控。
"""
import asyncio
import json
import math
import os
import sqlite3
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_trunk import DataTrunk
from hub_mixins.shadow import ShadowWriter, _index_rows, collect_origins

_CONTENT = "客户X的交付周期是三个工作日我们通常走加急通道"

# 内容生成:同长模板(长度剪枝 0.5-2.0 不干扰),按文本查向量表
def _mk_content(tag: str) -> str:
    return f"{_CONTENT} {tag} 补充说明文字用于对齐长度差异避免剪枝干扰判定"


def _angle_vec(theta: float, base: int) -> np.ndarray:
    """pair 在独占 2 维上的两个向量:e[2i] 与 cosθ·e[2i]+sinθ·e[2i+1]"""
    v1 = np.zeros(16, dtype=np.float32)
    v2 = np.zeros(16, dtype=np.float32)
    v1[base * 2] = 1.0
    v2[base * 2] = math.cos(theta)
    v2[base * 2 + 1] = math.sin(theta)
    return v1, v2


def _make_dt(tmp_path, backfeed_cfg=None, branches=None):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=True,
        DATA_TRUNK_ROOT=str(tmp_path / "dt"),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": True,
                           "wiki": True, "shared": True},
        DATA_TRUNK_BRANCHES=branches or {},
        DATA_TRUNK_BACKFEED=backfeed_cfg if backfeed_cfg is not None
        else {"enabled": True},
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    for b in set((branches or {}).values()):
        dt.ensure_branch(b)
    return dt


def _submit(w, mid, owner, content, branch_agent=None):
    w.submit("memory", {"memory_id": mid, "owner": owner, "memory_key": mid,
                        "content": content, "trust": "internal",
                        "level": "summary", "tags": [], "date": "2026-09-01"})
    w._drain_once()


@pytest.fixture()
def env(tmp_path):
    """双分干 + ShadowWriter(单分干 pair 亦可,双分干贴近生产归档语义)。"""
    dt = _make_dt(tmp_path, branches={"ag-b": "proj-alpha"})
    w = ShadowWriter(dt)
    return dt, w


def _pair_env(tmp_path, n_pairs, theta_list):
    """构造 n_pairs 对条目,每对 (default, proj-alpha) 各一。返回 (w, table, ids)。"""
    dt = _make_dt(tmp_path, branches={"ag-b": "proj-alpha"})
    w = ShadowWriter(dt)
    table = {}
    ids = []
    for i, theta in enumerate(theta_list):
        v1, v2 = _angle_vec(theta, i)
        c1 = _mk_content(f"pair{i}-a")
        c2 = _mk_content(f"pair{i}-b")
        table[c1] = v1
        table[c2] = v2
        m1, m2 = f"b3-{i}-a", f"b3-{i}-b"
        _submit(w, m1, "ag-a", c1)
        _submit(w, m2, "ag-b", c2)
        ids.append((m1, m2))
    return w, table, ids


def _fake_embed(table):
    def embed(texts):
        return np.array([table.get(t, np.zeros(16, dtype=np.float32))
                         for t in texts], dtype=np.float32)
    return embed


# ═══════════ ① 三档 + 边界 ═══════════

def test_three_bands_and_boundaries(tmp_path):
    """0.95/0.9(==auto)/0.75/0.6(==floor)/0.5 → auto×2, review×2, noop×1。"""
    w, table, ids = _pair_env(
        tmp_path, 5,
        [math.acos(0.95), math.acos(0.9), math.acos(0.75),
         math.acos(0.6), math.acos(0.5)])
    r = w.scan_cos_merge(kinds=["memory"], min_age_sec=0,
                         cos_auto_merge=0.9, cos_review_floor=0.6,
                         embed_fn=_fake_embed(table), provider_name="sentence")
    assert r["status"] == "ok", r
    # 0.95 与 0.9(==auto 边界, >=)都自动合并:被吸收侧(b)出现在 merge.audit.absorbed
    absorbed_ids = set()
    for m in r.get("merges", []):
        for a in (m.get("audit") or {}).get("absorbed") or []:
            absorbed_ids.add(a.split(":")[-1])
    for pair in (ids[0], ids[1]):
        assert pair[1] in absorbed_ids, \
            f"auto 对 {pair} 应合并(被吸收侧在 absorbed): {r}"
    assert r["auto_merged"] == 2, r
    # 0.75 与 0.6(==floor 边界, >=)进人工候选
    review_pairs = {(c["pair"][0]["id"], c["pair"][1]["id"])
                    for c in r.get("review_candidates", [])}
    for pair in (ids[2], ids[3]):
        assert pair in review_pairs, f"review 对 {pair} 应进候选: {r}"
    assert len(review_pairs) == 2, r
    # 0.5 < floor:无任何动作(b 侧既未被吸收也不进候选)
    assert ids[4][1] not in absorbed_ids and ids[4] not in review_pairs
    # 阈值绑定正确(显式传参)
    assert r["cos_auto_merge"] == 0.9 and r["cos_review_floor"] == 0.6


def test_below_floor_noop_and_0_59(tmp_path):
    """0.59(<floor)与 0.5 完全不动(边界下侧)。"""
    w, table, ids = _pair_env(tmp_path, 2, [math.acos(0.59), math.acos(0.5)])
    r = w.scan_cos_merge(kinds=["memory"], min_age_sec=0,
                         cos_auto_merge=0.9, cos_review_floor=0.6,
                         embed_fn=_fake_embed(table), provider_name="sentence")
    assert r["auto_merged"] == 0, r
    assert r["review_candidates"] == [], r


def test_hasher_default_auto_near_off(tmp_path):
    """hasher 档默认 auto=0.99(近关,词袋无语义区分度,设计 §1.6):0.95 对进 review。"""
    w, table, ids = _pair_env(tmp_path, 1, [math.acos(0.95)])
    r = w.scan_cos_merge(kinds=["memory"], min_age_sec=0,
                         embed_fn=_fake_embed(table), provider_name="hasher")
    assert r["cos_auto_merge"] == 0.99, r
    assert r["auto_merged"] == 0
    assert len(r["review_candidates"]) == 1


# ═══════════ ② 阈值 provider 版本化 ═══════════

def test_threshold_versioned_by_provider(tmp_path):
    """@hasher 键只在 hasher 档生效;sentence 档失效回落默认 0.9。
    0.75 对:hasher 档(0.7)→auto; sentence 档(0.9)→review。"""
    cfg = {"enabled": True, "cos_auto_merge@hasher": 0.7,
           "cos_review_floor": 0.6}
    dt = _make_dt(tmp_path, backfeed_cfg=cfg,
                  branches={"ag-b": "proj-alpha"})
    w = ShadowWriter(dt)
    theta = math.acos(0.75)
    v1, v2 = _angle_vec(theta, 0)
    c1, c2 = _mk_content("ver-a"), _mk_content("ver-b")
    table = {c1: v1, c2: v2}
    _submit(w, "ver-a", "ag-a", c1)
    _submit(w, "ver-b", "ag-b", c2)
    # sentence 档先跑: @hasher 失效 → 无 sentence 键无通用键 → 默认 0.9 → 0.75 review
    # (先跑 sentence 因为它不合并只出候选,数据保留给 hasher 档复扫)
    rs = w.scan_cos_merge(kinds=["memory"], min_age_sec=0,
                          embed_fn=_fake_embed(table), provider_name="sentence")
    assert rs["cos_auto_merge"] == 0.9, rs
    assert rs["auto_merged"] == 0, f"sentence 档不应误用 @hasher 键: {rs}"
    assert len(rs["review_candidates"]) == 1
    # hasher 档: @hasher=0.7 生效 → 0.75 auto
    rh = w.scan_cos_merge(kinds=["memory"], min_age_sec=0,
                          embed_fn=_fake_embed(table), provider_name="hasher")
    assert rh["cos_auto_merge"] == 0.7, rh
    assert rh["auto_merged"] == 1, f"hasher 档 0.75>=0.7 应 auto: {rh}"


# ═══════════ ③ 幂等/剪枝(fail-closed 语义由 B2 覆盖,补长度剪枝) ═══════════

def test_length_prune_skips_dissimilar_lengths(tmp_path):
    """长度比 >2 的对被剪枝(不算 cos、无动作),长文本不与短文本误合并。"""
    dt = _make_dt(tmp_path, branches={"ag-b": "proj-alpha"})
    w = ShadowWriter(dt)
    long_c = "很长" * 200  # 400 字
    short_c = "短文本"
    table = {long_c: np.array([1.0, 0.0], dtype=np.float32),
             short_c: np.array([0.999, 0.045], dtype=np.float32)}  # cos≈0.999
    _submit(w, "len-a", "ag-a", long_c)
    _submit(w, "len-b", "ag-b", short_c)
    r = w.scan_cos_merge(kinds=["memory"], min_age_sec=0,
                         cos_auto_merge=0.9, cos_review_floor=0.6,
                         embed_fn=_fake_embed(table), provider_name="sentence")
    assert r["auto_merged"] == 0, "长度比>2 应被剪枝"
    assert r["review_candidates"] == [], r


# ═══════════ ④ hub 接线:review 候选入队 → manager approve → 真执行 ═══════════

def _hub_env(tmp_path, monkeypatch, backfeed_cfg=None):
    """hub 接线环境:events+review_queue 表 + 双分干 dt + ShadowWriter 挂 hub 单例。"""
    db = str(tmp_path / "b3.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, agent_id TEXT, payload TEXT, timestamp TEXT)""")
    conn.execute("""CREATE TABLE review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_type TEXT NOT NULL DEFAULT 'entity',
        doc_id TEXT NOT NULL, name TEXT NOT NULL, detail TEXT DEFAULT '',
        level TEXT DEFAULT 'summary', status TEXT DEFAULT 'pending',
        source TEXT DEFAULT 'llm', created_at TEXT DEFAULT (datetime('now')),
        reviewed_at TEXT, reviewed_by TEXT)""")
    conn.execute("""CREATE TABLE audit_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT, entry_type TEXT,
        ref_table TEXT DEFAULT '', ref_id TEXT DEFAULT '', payload TEXT,
        prev_hash TEXT DEFAULT '', entry_hash TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    conn.close()
    from models import CONFIG
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    cfg = backfeed_cfg if backfeed_cfg is not None else {"enabled": True}
    dt = _make_dt(tmp_path, backfeed_cfg=cfg, branches={"ag-b": "proj-alpha"})
    w = ShadowWriter(dt, audit_db_path=db)
    from hub_core import hub
    monkeypatch.setattr(hub, "data_trunk", dt)
    monkeypatch.setattr(hub, "_shadow", w)
    hub.agents["b3-mgr"] = {"agent_id": "b3-mgr", "role": "manager"}
    yield db, dt, w, hub
    hub.agents.pop("b3-mgr", None)


@pytest.fixture()
def hub_env(tmp_path, monkeypatch):
    yield from _hub_env(tmp_path, monkeypatch)


def test_review_queued_and_approve_executes(hub_env):
    """0.75 对 → backfeed_scan_cos_and_merge 入 review_queue(pending) →
    manager approve → resolve_sources + execute_merge 真合并(canonical 档案 + 归档)。"""
    db, dt, w, hub = hub_env
    theta = math.acos(0.75)
    v1, v2 = _angle_vec(theta, 0)
    c1, c2 = _mk_content("hub-a"), _mk_content("hub-b")
    table = {c1: v1, c2: v2}
    _submit(w, "hub-a", "ag-a", c1)
    _submit(w, "hub-b", "ag-b", c2)
    r = asyncio.run(hub.backfeed_scan_cos_and_merge(
        kinds=["memory"], min_age_sec=0,
        embed_fn=_fake_embed(table), provider_name="sentence"))
    assert r["auto_merged"] == 0, r
    # 入队断言
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT id, status, detail FROM review_queue"
        " WHERE item_type='backfeed_merge' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row and row[1] == "pending", row
    qid = row[0]
    assert '"hub-a"' in row[2] and '"hub-b"' in row[2]
    # 重复 scan 不重复入队(幂等):候选仍被报告但已 pending 的不再 INSERT
    r2 = asyncio.run(hub.backfeed_scan_cos_and_merge(
        kinds=["memory"], min_age_sec=0,
        embed_fn=_fake_embed(table), provider_name="sentence"))
    assert r2["auto_merged"] == 0
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM review_queue"
                     " WHERE item_type='backfeed_merge'").fetchone()[0]
    conn.close()
    assert n == 1, f"重复 scan 不得重复入队: {n}"
    # manager approve → 真执行
    d = asyncio.run(hub.approve_backfeed_merge(qid, "b3-mgr"))
    assert d["status"] == "approved", d
    assert d.get("execute", {}).get("status") == "merged", d
    # canonical 档案 + 归档落盘(文件名 cust_c-*.json——Windows 路径禁 ':' 故换 '_')
    import glob as _glob
    cust = _glob.glob(os.path.join(dt.root, "customers", "cust_c-*.json"))
    assert len(cust) == 1, cust
    arch = _glob.glob(os.path.join(
        dt.branch_root("proj-alpha"), "vault", "_merged", "*", "hub-b.md"))
    assert len(arch) == 1, arch
    # 409 幂等:重复 approve 不翻转
    d2 = asyncio.run(hub.approve_backfeed_merge(qid, "b3-mgr"))
    assert d2.get("code") == 409, d2
