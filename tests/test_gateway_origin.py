# -*- coding: utf-8 -*-
"""网关读取真相源定位（origin）— 阶段3-P2 交付2

网关读取端点（/api/v1/gateway/read）响应元数据附「真相源定位」：
index 条目 → git 路径 + commit hash。读取仍走 SQLite（影子期），
origin 为可选新字段：data-trunk 未启用 / 无定位数据 → 不附加，存量请求兼容。
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes_gateway
from data_trunk import DataTrunk
from hub_mixins.shadow import ShadowWriter, collect_origins


# ═══════════ 测试夹具：真实 DataTrunk + ShadowWriter（tmp 隔离）═══════════

def _make_dt(tmp_path, branches=None):
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


def _submit_memory(w, mid="m-org", owner="ag-a", content="客户偏好记录"):
    w.submit("memory", {"memory_id": mid, "owner": owner, "memory_key": "偏好",
                        "content": content, "trust": "internal",
                        "level": "summary", "tags": [], "date": "2026-09-01"})
    w._drain_once()


class FakePrincipal:
    def __init__(self, scope=None, auth_mode="api_key"):
        self.scope = scope
        self.auth_mode = auth_mode


class FakeDisc:
    """规则 1 自查 FULL（与 disclosure.py 实际行为一致的最小 stub）"""
    def disclose_for_principal(self, memory, requester, task, required_level, scope=None):
        from models import DisclosureLevel
        if memory.get("owner_agent_id") == requester:
            return DisclosureLevel.FULL
        return DisclosureLevel.SUMMARY


class FakeHub:
    """带真实 data-trunk 底座的 hub stub（网关读取链路用）"""
    def __init__(self, dt=None, shadow=None, mem_id="m-org", owner="ag-a"):
        self.data_trunk = dt
        self._shadow = shadow
        self.disclosure = FakeDisc()
        self._mem_id = mem_id
        self._owner = owner

    async def memory_search(self, req):
        return {"results": [
            {"memory_id": self._mem_id, "memory_key": "偏好", "content": "客户偏好记录",
             "disclosure_level": "summary", "owner_agent_id": self._owner},
        ], "total": 1}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立测试实例：临时 db（gateway_read_log）+ 临时 data-trunk（不碰生产）"""
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE gateway_read_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT, requester TEXT NOT NULL,
        auth_mode TEXT DEFAULT '', scope_json TEXT DEFAULT '', kind TEXT NOT NULL,
        query TEXT DEFAULT '', target TEXT DEFAULT '', granted_level TEXT DEFAULT '',
        item_count INTEGER DEFAULT 0, stripped_chunks INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    conn.close()
    from models import CONFIG
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    dt = _make_dt(tmp_path)
    w = ShadowWriter(dt)
    _submit_memory(w)
    monkeypatch.setattr(routes_gateway, "hub", FakeHub(dt, w))
    return {"db": db, "dt": dt, "writer": w}


# ═══════════ 1. 影子批 commit 登记定位锚点 ═══════════

def test_shadow_records_origin(tmp_path):
    """批写入后：内存映射 id → git 路径 + commit hash；.commits.jsonl 持久化"""
    dt = _make_dt(tmp_path)
    w = ShadowWriter(dt)
    _submit_memory(w)
    o = w._origins.get("m-org")
    assert o is not None
    assert o["branch"] == "default"
    assert o["path"] == "vault/memory/2026-09-01/m-org.md"
    # commit hash 真实存在于 git 历史
    trunk_hashes = [c["hash"] for c in dt.trunk.log(50)]
    branch_hashes = [c["hash"] for c in dt.branch_repo().log(50)]
    assert o["trunk_commit"] in trunk_hashes
    assert o["branch_commit"] in branch_hashes
    assert o["ts"]
    # index/.commits.jsonl 已随锚点登记 commit 进 git（重启可重建）
    text = dt.trunk.read_at("index/.commits.jsonl")
    assert text is not None
    rec = json.loads(text.strip().splitlines()[-1])
    assert "m-org" in rec["ids"]
    assert rec["commit"] == o["trunk_commit"]
    assert rec["branches"]["default"] == o["branch_commit"]


def test_commits_log_multi_batch_accumulates(tmp_path):
    """多批追加：.commits.jsonl 逐批累积，后批不覆盖前批"""
    dt = _make_dt(tmp_path)
    w = ShadowWriter(dt)
    _submit_memory(w, mid="m-b1")
    _submit_memory(w, mid="m-b2")
    text = dt.trunk.read_at("index/.commits.jsonl")
    lines = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert lines[0]["ids"] == ["m-b1"]
    assert lines[1]["ids"] == ["m-b2"]


# ═══════════ 2. 网关读取响应附 origin ═══════════

def test_gateway_memory_read_attaches_origin(env):
    """kind=memory 响应条目带 origin（git 路径 + commit hash）"""
    req = routes_gateway.GatewayReadRequest(kind="memory", query="偏好")
    resp = asyncio.run(routes_gateway.api_gateway_read(
        req, current_agent="ag-a", principal=FakePrincipal()))
    assert resp["status"] == "ok"
    mem = resp["memories"][0]
    assert "origin" in mem
    o = mem["origin"]
    assert o["branch"] == "default"
    assert o["path"].endswith("m-org.md")
    dt = env["dt"]
    assert o["trunk_commit"] in [c["hash"] for c in dt.trunk.log(50)]
    assert o["branch_commit"] == dt.branch_repo().head_hash() or \
        o["branch_commit"] in [c["hash"] for c in dt.branch_repo().log(50)]


def test_gateway_read_disabled_no_origin(tmp_path, monkeypatch):
    """data-trunk 未启用 → 响应无 origin 字段（存量请求兼容，零影响）"""
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE gateway_read_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT, requester TEXT NOT NULL,
        auth_mode TEXT DEFAULT '', scope_json TEXT DEFAULT '', kind TEXT NOT NULL,
        query TEXT DEFAULT '', target TEXT DEFAULT '', granted_level TEXT DEFAULT '',
        item_count INTEGER DEFAULT 0, stripped_chunks INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    conn.close()
    from models import CONFIG
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    monkeypatch.setattr(routes_gateway, "hub", FakeHub(dt=None, shadow=None))
    req = routes_gateway.GatewayReadRequest(kind="memory", query="偏好")
    resp = asyncio.run(routes_gateway.api_gateway_read(
        req, current_agent="ag-a", principal=FakePrincipal()))
    assert resp["status"] == "ok"
    assert "origin" not in resp["memories"][0]


def test_gateway_read_unknown_id_no_origin(env):
    """影子未镜像的条目（如历史存量数据）→ 无 origin，不报错"""
    routes_gateway.hub._mem_id = "m-legacy"  # 不在 .commits.jsonl
    req = routes_gateway.GatewayReadRequest(kind="memory", query="偏好")
    resp = asyncio.run(routes_gateway.api_gateway_read(
        req, current_agent="ag-a", principal=FakePrincipal()))
    assert resp["status"] == "ok"
    assert "origin" not in resp["memories"][0]


# ═══════════ 3. 重启重建：纯文件回源（无内存映射）═══════════

def test_collect_origins_rebuild_from_files(tmp_path):
    """进程重启后内存映射丢失 → 从 index/.commits.jsonl + index/<kind>.jsonl 重建"""
    dt = _make_dt(tmp_path)
    w = ShadowWriter(dt)
    _submit_memory(w, mid="m-restart")
    mem_ver = dict(w._origins["m-restart"])
    # 模拟重启：不传 shadow_writer，纯文件回源
    rebuilt = collect_origins(dt, None, ["m-restart"])
    o = rebuilt["m-restart"]
    assert o["trunk_commit"] == mem_ver["trunk_commit"]
    assert o["branch_commit"] == mem_ver["branch_commit"]
    assert o["path"] == mem_ver["path"]
    assert o["branch"] == "default"
    # 未命中的 id 不出现在结果里
    assert "ghost" not in collect_origins(dt, None, ["ghost"])


# ═══════════ 4. doc/wiki chunk 定位 ═══════════

def test_attach_origin_doc_chunk(tmp_path, monkeypatch):
    """doc 读取的 chunk（chunk_id=f"{doc_id}-c{idx}"）也能定位到 wiki 分干文件"""
    dt = _make_dt(tmp_path)
    w = ShadowWriter(dt)
    w.submit("wiki", {"doc_id": "doc9", "piece_index": 0, "content": "段落零",
                      "source_agent_id": "ag-a", "trust": "internal",
                      "level": "summary", "date": "2026-09-01"})
    w._drain_once()
    monkeypatch.setattr(routes_gateway, "hub", FakeHub(dt, w))
    chunks = [{"chunk_id": "doc9-c0", "piece_index": 0,
               "content": "段落零", "disclosure_level": "summary"}]
    routes_gateway._attach_origin(chunks)
    o = chunks[0].get("origin")
    assert o is not None
    assert o["kind"] == "wiki"
    assert o["path"] == "vault/wiki/doc9/000.md"
    assert o["branch_commit"] in [c["hash"] for c in dt.branch_repo().log(50)]
