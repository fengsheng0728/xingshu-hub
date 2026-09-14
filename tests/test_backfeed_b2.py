# -*- coding: utf-8 -*-
"""tests/test_backfeed_b2.py — 阶段4-B2：精确去重合并执行器（chunk_hash 档）

对齐 docs/phase4-backfeed-design.md §2.3/§3/§6-B2 与任务书 batch-b2 验收：
T1 ① canonical_id 格式确定性  ② canonical 写→读往返字段一致 + 幂等不写盘
   ③ tags_snapshot 取最严（level min / trust min / taint 最早 / locked 传染 / 只降不升）
T2 ④ 双分干同内容 → execute_merge：主干 1 档案、被吸收分干归档 vault/_merged/、
      HEAD~1 历史可读、index merged_into 修正行  ⑤ 内存 _origins 重指向 canonical
   ⑥ undo：文件回原路径 + unmerged 修正行 + git 历史留痕  ⑦ 审计落链（events 含 canonical_id）
   ⑧ update：第三来源并入 → merge_history 增长 + updated_at 刷新
T3 ⑨ scan 自动合并 + 二次扫描幂等零动作  ⑩ 不同 hash 不动  ⑪ dry_run 零写
   ⑫ fail-closed（backfeed 缺省 / data_trunk 关 / hub 未接线）  ⑬ 60s 不可合并窗口
T4 ⑭ BOUNDARY 生成源含 canonical 声明  ⑮ 读取端 merged 条目重指向 + merged:true
fail-closed 红线：tmp_path 双仓库构造，不碰生产 config.yaml / sync_hub.db / data-trunk。
"""
import asyncio
import datetime
import json
import os
import re
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_trunk
from data_trunk import DataTrunk, canonical_id_for, synthesize_tags, _boundary_md
from chunker import chunk_hash
from hub_mixins.shadow import (
    ShadowWriter, collect_origins, _index_rows, _today, _MERGE_MIN_AGE_SEC,
)

CONTENT = "客户X偏好交付周期两周，优先周五前交付"


# ═══════════ 夹具：tmp_path 双分干 DataTrunk + ShadowWriter ═══════════

def _make_dt(tmp_path, branches=None, backfeed=True, enabled=True, name="dt"):
    cfg = SimpleNamespace(
        DATA_TRUNK_ENABLED=enabled,
        DATA_TRUNK_ROOT=str(tmp_path / name),
        DATA_TRUNK_BRANCH_DEFAULT="default",
        DATA_TRUNK_SHADOW={"memory": True, "knowledge": True,
                           "wiki": True, "shared": True},
        DATA_TRUNK_BRANCHES=branches or {},
        DATA_TRUNK_BACKFEED=({"enabled": True} if backfeed else None),
    )
    dt = DataTrunk(cfg)
    dt.ensure()
    for b in set((branches or {}).values()):
        dt.ensure_branch(b)
    return dt


def _submit(w, mid, owner, content, trust="internal", level="summary",
            date="2026-09-01"):
    w.submit("memory", {"memory_id": mid, "owner": owner, "memory_key": mid,
                        "content": content, "trust": trust, "level": level,
                        "tags": [], "date": date})
    w._drain_once()


@pytest.fixture()
def env2(tmp_path):
    """双分干环境：ag-a → default，ag-b → proj-alpha。"""
    dt = _make_dt(tmp_path, branches={"ag-b": "proj-alpha"})
    w = ShadowWriter(dt)
    return dt, w


def _two_branch_dup(env2, content=CONTENT, t1=("internal", "full"),
                    t2=("federated", "summary")):
    """双分干各写一条同内容 memory。返回 (dt, w)。"""
    dt, w = env2
    _submit(w, "mem-001", "ag-a", content, trust=t1[0], level=t1[1])
    _submit(w, "mem-117", "ag-b", content, trust=t2[0], level=t2[1])
    return dt, w


def _sources(w, ids, kind="memory"):
    """组装 execute_merge 的 sources 入参：branch/path 取 index 原始行（修正行不改
    历史行，合并后 _origins 已重指向 canonical，不能作为来源路径）；commit 锚点
    从内存 _origins 补充（可选元数据）。"""
    rows = {r["id"]: r for r in _index_rows(w.dt, kind) if r.get("path")}
    out = []
    for rid in ids:
        row = rows[rid]
        o = w._origins.get(rid, {})
        out.append({"branch": row["branch"], "id": rid, "path": row["path"],
                    "trust": row.get("trust", ""), "level": row.get("level", ""),
                    "owner": row.get("owner", ""), "title": row.get("title", ""),
                    "trunk_commit": o.get("trunk_commit", ""),
                    "branch_commit": o.get("branch_commit", "")})
    return out


def _hub_env(tmp_path, monkeypatch):
    """hub 接线环境：临时 events 库 + 双分干 dt + ShadowWriter 挂到 hub 单例。"""
    db = str(tmp_path / "b2.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, agent_id TEXT, payload TEXT, timestamp TEXT)""")
    conn.commit()
    conn.close()
    from models import CONFIG
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    dt = _make_dt(tmp_path, branches={"ag-b": "proj-alpha"})
    w = ShadowWriter(dt, audit_db_path=db)
    from hub_core import hub
    monkeypatch.setattr(hub, "data_trunk", dt)
    monkeypatch.setattr(hub, "_shadow", w)
    return {"db": db, "dt": dt, "w": w, "hub": hub}


# ═══════════ T1：customers/canonical 档案读写 + tags 合成（§3/§2.2）═══════════

def test_canonical_id_format_deterministic():
    """① canonical_id = cust:c-<8 hex>，同内容确定性相等。"""
    c1 = canonical_id_for(CONTENT)
    c2 = canonical_id_for(CONTENT)
    assert c1 == c2
    assert re.fullmatch(r"cust:c-[0-9a-f]{8}", c1), c1
    assert canonical_id_for("别的内容") != c1


def test_canonical_write_read_roundtrip(tmp_path):
    """② 写→读往返字段一致（§3 逐字段）；幂等：内容不变不写盘。"""
    dt = _make_dt(tmp_path)
    cid = canonical_id_for(CONTENT)
    rec = {
        "canonical_id": cid, "kind": "memory", "title": "客户X的交付周期偏好",
        "content_digest": "sha256:" + chunk_hash(CONTENT),
        "identity": {"owner_key_fp": "a1b2c3d4e5f60708",
                     "owner_agent_ids": ["sales-bot-1", "cs-bot-2"]},
        "sources": [{"branch": "default", "kind": "memory", "id": "mem-001",
                     "path": "vault/memory/2026-09-01/mem-001.md",
                     "trunk_commit": "a" * 40, "branch_commit": "b" * 40}],
        "tags_snapshot": {"trust": "federated", "level": "summary",
                          "tainted_at": "", "locked": False},
        "merge_history": [],
        "created_at": "2026-09-02T03:00:00+08:00",
        "updated_at": "2026-09-02T03:00:00+08:00",
    }
    assert dt.write_canonical(rec)
    back = dt.read_canonical(cid)
    assert back == rec, "写→读往返字段不一致"
    assert back["content_digest"] == "sha256:" + chunk_hash(CONTENT)
    # 幂等：内容不变不写盘（spy 观测 write_file 零调用）
    calls = []
    orig = dt.trunk.write_file
    dt.trunk.write_file = lambda rel, text: (calls.append(rel), orig(rel, text))[1]
    assert dt.write_canonical(rec)
    assert calls == [], "内容不变仍写盘，违反幂等语义"


def test_synthesize_tags_min_only_down():
    """③ level min by _level_rank；trust TRUST_ORDER 小者胜；taint 最早；locked 传染；只降不升。"""
    snap = synthesize_tags([
        {"trust": "internal", "level": "full"},
        {"trust": "federated", "level": "summary"},
    ])
    assert snap["level"] == "summary", "full+summary 应合成 summary"
    assert snap["trust"] == "federated", "internal+federated 应合成 federated"
    assert snap["tainted_at"] == "" and snap["locked"] is False
    snap2 = synthesize_tags([
        {"trust": "internal", "level": "full",
         "tainted_at": "2026-09-02T08:00:00+08:00"},
        {"trust": "system", "level": "summary", "locked": True,
         "tainted_at": "2026-09-01T08:00:00+08:00"},
    ])
    assert snap2["trust"] == "internal"  # min(internal=3, system=4)
    assert snap2["tainted_at"] == "2026-09-01T08:00:00+08:00", "taint 记最早"
    assert snap2["locked"] is True, "任一硬锁 → locked"
    # 只降不升：base(summary/federated) + 新来源(full/system) → 仍 summary/federated
    snap3 = synthesize_tags([{"trust": "system", "level": "full"}], base=snap)
    assert snap3["level"] == "summary" and snap3["trust"] == "federated"


# ═══════════ T2：execute_merge 动作序列 + undo（§2.3）═══════════

def test_execute_merge_two_branches(env2):
    """④ 双分干同内容 → 主干 1 档案、被吸收分干归档、HEAD~1 可读、index 修正行。"""
    dt, w = _two_branch_dup(env2)
    cid = canonical_id_for(CONTENT)
    r = w.execute_merge("memory", _sources(w, ["mem-001", "mem-117"]), CONTENT)
    assert r["status"] == "merged" and r["canonical_id"] == cid
    # 主干 customers/ 恰好 1 个档案，§3 字段齐
    cpath = dt.canonical_path(cid)  # 文件名层冒号→下划线（NTFS ADS 规避）
    cust = os.listdir(os.path.join(dt.root, "customers"))
    assert cust == [os.path.basename(cpath)]
    arch = dt.read_canonical(cid)
    assert arch["kind"] == "memory"
    assert arch["content_digest"] == "sha256:" + chunk_hash(CONTENT)
    assert arch["identity"]["owner_agent_ids"] == ["ag-a", "ag-b"]
    assert arch["tags_snapshot"]["level"] == "summary"    # min(full, summary)
    assert arch["tags_snapshot"]["trust"] == "federated"  # min(internal, federated)
    assert len(arch["sources"]) == 2
    mh = arch["merge_history"][-1]
    assert mh["action"] == "auto_merge" and mh["cos"] == 1.0
    assert mh["absorbed"] == ["proj-alpha:mem-117"]
    assert mh["before"] == {"trust": "internal", "level": "full"}
    assert mh["after"] == {"trust": "federated", "level": "summary"}
    # 被吸收分干文件在 vault/_merged/<date>/，front-matter 含 merged_into
    br = dt.branch_repo("proj-alpha")
    merged_rel = f"vault/_merged/{_today()}/mem-117.md"
    full = os.path.join(br.root, merged_rel)
    assert os.path.isfile(full), "被吸收分干文件未归档到 vault/_merged/"
    with open(full, encoding="utf-8") as f:
        assert f"merged_into: {cid}" in f.read()
    # 旧路径 HEAD 消失，HEAD~1 历史可读（归档而非删除）
    old_rel = "vault/memory/2026-09-01/mem-117.md"
    assert br.read_at(old_rel) is None
    hist = br.read_at(old_rel, "HEAD~1")
    assert hist is not None and CONTENT in hist
    # 主来源保留原路径（§3 示例 sources[0]）
    assert os.path.isfile(os.path.join(dt.branch_root("default"),
                                       "vault/memory/2026-09-01/mem-001.md"))
    assert arch["sources"][0]["path"] == "vault/memory/2026-09-01/mem-001.md"
    assert arch["sources"][1]["path"] == merged_rel
    # index：历史行未改 + merged_into 修正行追加
    lines = _index_rows(dt, "memory")
    orig = [l for l in lines if l.get("path")]
    assert len(orig) == 2, "历史行被改写"
    corr = [l for l in lines if l.get("merged_into") == cid]
    assert {l["id"] for l in corr} == {"mem-001", "mem-117"}
    # 批 commit message 含 canonical_id
    assert any(cid in c["subject"] for c in dt.trunk.log(10))


def test_origins_repointed_to_canonical(env2):
    """⑤ 内存 _origins + 纯文件回源（模拟重启）均重指向 canonical + merged:true。"""
    dt, w = _two_branch_dup(env2)
    cid = canonical_id_for(CONTENT)
    w.execute_merge("memory", _sources(w, ["mem-001", "mem-117"]), CONTENT)
    cpath = dt.canonical_path(cid)
    for rid in ("mem-001", "mem-117"):
        o = w._origins[rid]
        assert o["path"] == cpath and o["merged"] is True
    rebuilt = collect_origins(dt, None, ["mem-001", "mem-117"])  # 不传 writer = 纯文件回源
    for rid in ("mem-001", "mem-117"):
        assert rebuilt[rid]["path"] == cpath
        assert rebuilt[rid]["merged"] is True
        assert rebuilt[rid]["canonical_id"] == cid


def test_undo_merge_restores(env2):
    """⑥ undo：文件回原路径、index unmerged 修正行、git 历史留痕、读取端取消重指向。"""
    dt, w = _two_branch_dup(env2)
    cid = canonical_id_for(CONTENT)
    w.execute_merge("memory", _sources(w, ["mem-001", "mem-117"]), CONTENT)
    merged_rel = f"vault/_merged/{_today()}/mem-117.md"
    r = w.undo_merge(cid)
    assert r["status"] == "unmerged"
    assert r["restored"] == [{"branch": "proj-alpha", "id": "mem-117",
                              "path": "vault/memory/2026-09-01/mem-117.md"}]
    br = dt.branch_repo("proj-alpha")
    orig_rel = "vault/memory/2026-09-01/mem-117.md"
    # 文件回原路径，front-matter 去掉 merged_into
    full = os.path.join(br.root, orig_rel)
    assert os.path.isfile(full)
    with open(full, encoding="utf-8") as f:
        assert "merged_into" not in f.read()
    # index 有 unmerged 修正行
    lines = _index_rows(dt, "memory")
    assert any(l.get("unmerged") and l.get("id") == "mem-117" for l in lines)
    # git 历史全程留痕：归档版本在 HEAD~1 可读
    hist = br.read_at(merged_rel, "HEAD~1")
    assert hist is not None and cid in hist
    # 读取端不再重指向（回源原始路径，无 merged 标记）
    rebuilt = collect_origins(dt, None, ["mem-117"])
    assert rebuilt["mem-117"]["path"] == orig_rel
    assert "merged" not in rebuilt["mem-117"]
    # 内存 _origins 同样回指
    assert w._origins["mem-117"]["path"] == orig_rel
    assert "merged" not in w._origins["mem-117"]


def test_merge_update_history_grows(env2, monkeypatch):
    """⑧ 第三来源并入既有 canonical：merge_history 增长、updated_at 刷新、tags 只降不升。"""
    dt, w = _two_branch_dup(env2)
    cid = canonical_id_for(CONTENT)
    w.execute_merge("memory", _sources(w, ["mem-001", "mem-117"]), CONTENT)
    arch1 = dt.read_canonical(cid)
    _submit(w, "mem-233", "ag-a", CONTENT, trust="system", level="full")
    monkeypatch.setattr(data_trunk, "_now_iso",
                        lambda: "2026-09-03T12:00:00+08:00")
    r = w.execute_merge("memory", _sources(w, ["mem-001", "mem-233"]), CONTENT)
    assert r["status"] == "merged"
    arch2 = dt.read_canonical(cid)
    assert len(arch2["merge_history"]) == len(arch1["merge_history"]) + 1
    assert arch2["updated_at"] == "2026-09-03T12:00:00+08:00", "updated_at 未刷新"
    assert arch2["created_at"] == arch1["created_at"]
    assert {s["id"] for s in arch2["sources"]} == {"mem-001", "mem-117", "mem-233"}
    # 只降不升：新来源 system/full 并入后仍 federated/summary
    assert arch2["tags_snapshot"]["trust"] == "federated"
    assert arch2["tags_snapshot"]["level"] == "summary"


def test_execute_merge_idempotent(env2):
    """幂等：同批次重复 execute_merge → already_merged，零新增修正行。"""
    dt, w = _two_branch_dup(env2)
    cid = canonical_id_for(CONTENT)
    src = _sources(w, ["mem-001", "mem-117"])
    w.execute_merge("memory", src, CONTENT)
    n_lines = len(_index_rows(dt, "memory"))
    r = w.execute_merge("memory", _sources(w, ["mem-001", "mem-117"]), CONTENT)
    assert r["status"] == "already_merged"
    assert len(_index_rows(dt, "memory")) == n_lines


def test_execute_merge_audit_logged(tmp_path, monkeypatch):
    """⑦ 审计落链：hub.backfeed_execute_merge → events 表 backfeed_merge 含 canonical_id。"""
    env = _hub_env(tmp_path, monkeypatch)
    hub, w = env["hub"], env["w"]
    _submit(w, "mem-001", "ag-a", CONTENT)
    _submit(w, "mem-117", "ag-b", CONTENT)
    r = asyncio.run(hub.backfeed_execute_merge(
        "memory", _sources(w, ["mem-001", "mem-117"]), CONTENT,
        action="manual_merge", actor="mgr-1", queue_id=7))
    assert r["status"] == "merged"
    conn = sqlite3.connect(env["db"])
    row = conn.execute(
        "SELECT agent_id, payload FROM events WHERE event_type='backfeed_merge'"
    ).fetchone()
    conn.close()
    assert row, "events 表缺 backfeed_merge 审计行"
    assert row[0] == "mgr-1"
    payload = json.loads(row[1])
    assert payload["canonical_id"] == r["canonical_id"]
    assert payload["queue_id"] == 7 and payload["action"] == "manual_merge"


# ═══════════ T3：自动合并扫描入口（§6-B2 判据）═══════════

def test_scan_auto_merge_and_idempotent(tmp_path, monkeypatch):
    """⑨ 双条目同 hash → scan 自动合并；二次 scan 零动作（幂等）。"""
    env = _hub_env(tmp_path, monkeypatch)
    hub, w, dt = env["hub"], env["w"], env["dt"]
    _submit(w, "mem-001", "ag-a", CONTENT)
    _submit(w, "mem-117", "ag-b", CONTENT)
    r1 = asyncio.run(hub.backfeed_scan_and_merge(min_age_sec=0))
    assert r1["enabled"] is True and r1["merged"] == 1
    assert r1["merges"][0]["status"] == "merged"
    cid = canonical_id_for(CONTENT)
    assert os.path.isfile(os.path.join(dt.root, dt.canonical_path(cid)))
    # 二次扫描幂等零动作
    r2 = asyncio.run(hub.backfeed_scan_and_merge(min_age_sec=0))
    assert r2["merged"] == 0 and r2["merges"] == []
    assert len(os.listdir(os.path.join(dt.root, "customers"))) == 1
    # scan 路径审计同样落链
    conn = sqlite3.connect(env["db"])
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='backfeed_merge'").fetchone()[0]
    conn.close()
    assert n == 1


def test_scan_different_hash_noop(tmp_path, monkeypatch):
    """⑩ 不同 hash → 不动。"""
    env = _hub_env(tmp_path, monkeypatch)
    hub, w, dt = env["hub"], env["w"], env["dt"]
    _submit(w, "mem-001", "ag-a", "内容甲完全不同")
    _submit(w, "mem-117", "ag-b", "内容乙完全不同")
    r = asyncio.run(hub.backfeed_scan_and_merge(min_age_sec=0))
    assert r["merged"] == 0 and r["groups"] == []
    assert os.listdir(os.path.join(dt.root, "customers")) == []
    assert not os.path.isdir(os.path.join(dt.branch_root("proj-alpha"),
                                          "vault", "_merged"))


def test_scan_dry_run_no_writes(tmp_path, monkeypatch):
    """⑪ dry_run 只报告：customers 空、无归档、index/commit 不变。"""
    env = _hub_env(tmp_path, monkeypatch)
    hub, w, dt = env["hub"], env["w"], env["dt"]
    _submit(w, "mem-001", "ag-a", CONTENT)
    _submit(w, "mem-117", "ag-b", CONTENT)
    head_t = dt.trunk.head_hash()
    head_b = dt.branch_repo("proj-alpha").head_hash()
    n_index = len(_index_rows(dt, "memory"))
    r = asyncio.run(hub.backfeed_scan_and_merge(min_age_sec=0, dry_run=True))
    assert r["dry_run"] is True and r["merged"] == 0
    assert len(r["groups"]) == 1
    assert r["groups"][0]["ids"] == ["mem-001", "mem-117"]
    assert r["groups"][0]["branches"] == ["default", "proj-alpha"]
    assert os.listdir(os.path.join(dt.root, "customers")) == []
    assert len(_index_rows(dt, "memory")) == n_index
    assert dt.trunk.head_hash() == head_t
    assert dt.branch_repo("proj-alpha").head_hash() == head_b


def test_scan_fail_closed(tmp_path, monkeypatch):
    """⑫ fail-closed：backfeed 缺省 / data_trunk 关 / hub 未接线 → {"enabled": False}。"""
    # backfeed.enabled 缺省（DATA_TRUNK_BACKFEED=None）
    dt = _make_dt(tmp_path, backfeed=False, name="dt1")
    assert ShadowWriter(dt).scan_and_merge() == {"enabled": False}
    # data_trunk.enabled=false
    dt2 = _make_dt(tmp_path, enabled=False, name="dt2")
    assert ShadowWriter(dt2).scan_and_merge() == {"enabled": False}
    # hub 未接线（data_trunk=None）
    from hub_core import hub
    monkeypatch.setattr(hub, "data_trunk", None)
    monkeypatch.setattr(hub, "_shadow", None)
    assert asyncio.run(hub.backfeed_scan_and_merge()) == {"enabled": False}
    assert asyncio.run(hub.backfeed_execute_merge("memory", [], "x")) == {"enabled": False}
    assert asyncio.run(hub.backfeed_undo_merge("cust:c-00000000")) == {"enabled": False}


def test_scan_min_age_window(env2):
    """⑬ 60s 不可合并窗口：刚写入 <60s 的条目不参与；min_age_sec=0 放行。"""
    dt, w = env2
    _submit(w, "mem-001", "ag-a", CONTENT)
    _submit(w, "mem-117", "ag-b", CONTENT)
    assert _MERGE_MIN_AGE_SEC == 60.0
    r = w.scan_and_merge()  # 默认 60s 窗口
    assert r["merged"] == 0 and r["skipped_young"] == 2
    assert os.listdir(os.path.join(dt.root, "customers")) == []
    r2 = w.scan_and_merge(min_age_sec=0)
    assert r2["merged"] == 1


# ═══════════ T4：BOUNDARY 声明 + 读取端重指向 ═══════════

def test_boundary_md_canonical_declaration():
    """⑭ _boundary_md() 生成源第 5 节含 canonical 声明（改生成源，不碰 BOUNDARY.md）。"""
    text = _boundary_md(SimpleNamespace())
    sec5 = text.split("## 5. 分干隔离声明")[1]
    assert "customers/" in sec5 and "canonical" in sec5
    assert "唯一受控例外" in sec5


def test_read_side_redirect_merged_flag(tmp_path):
    """⑮ 读取端：merged 条目返回 canonical 路径 + merged:true（含 wiki kind 路径）。"""
    dt = _make_dt(tmp_path, branches={"ag-b": "proj-alpha"})
    w = ShadowWriter(dt)
    w.submit("wiki", {"doc_id": "doc1", "piece_index": 0, "content": CONTENT,
                      "source_agent_id": "ag-a", "trust": "internal",
                      "level": "summary", "date": "2026-09-01"})
    w.submit("wiki", {"doc_id": "doc2", "piece_index": 0, "content": CONTENT,
                      "source_agent_id": "ag-b", "trust": "internal",
                      "level": "summary", "date": "2026-09-01"})
    w._drain_once()
    r = w.scan_and_merge(kinds=["wiki"], min_age_sec=0)
    assert r["merged"] == 1
    cid = r["merges"][0]["canonical_id"]
    rebuilt = collect_origins(dt, None, ["doc1-c0", "doc2-c0"])
    assert rebuilt["doc1-c0"]["path"] == dt.canonical_path(cid)
    assert rebuilt["doc1-c0"]["merged"] is True
    assert rebuilt["doc2-c0"]["merged"] is True
