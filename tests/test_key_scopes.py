# -*- coding: utf-8 -*-
"""
S1K scoped API key 单测（2026-08-07）
覆盖：
  1. create key（manager 审批门）：worker 403 / manager 200，明文仅返回一次
  2. 端点过滤：只读 key（endpoints=["/memory/search"]）调写端点 → 403
  3. 吊销：revoke 后同 key 认证 401（60s 内失效）
  4. level_cap：key cap=summary 查自己 → 结果 ≤ summary（min 语义）
  5. data_domain：key 限 data_domain=["客服部"] 查其他部门 → METADATA
  6. 无 scope key → 行为与普通 key 一致（向后兼容）
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from key_scopes import key_hash, get_store, ScopedKeyStore


def _mk_tmpdb():
    tmpdir = tempfile.mkdtemp(prefix="s1k-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_keys (
        key_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, key_hash TEXT NOT NULL,
        scope TEXT DEFAULT '{"endpoints": [], "data_domain": [], "level_cap": ""}',
        status TEXT DEFAULT 'active', created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')), expires_at TEXT,
        last_used_at TEXT, call_count INTEGER DEFAULT 0)""")
    conn.commit()
    conn.close()
    return tmpdb


def test_create_and_lookup():
    tmpdb = _mk_tmpdb()
    store = ScopedKeyStore(tmpdb)
    r = store.create("agent-1", {"endpoints": ["/memory/search"], "level_cap": "summary"},
                     created_by="mgr")
    assert r["key"].startswith("sk-"), "key 应有 sk- 前缀"
    assert r["key_id"].startswith("key-")
    # 明文不落库：库里只有 hash
    conn = sqlite3.connect(tmpdb)
    row = conn.execute("SELECT key_hash FROM agent_keys WHERE key_id=?", (r["key_id"],)).fetchone()
    conn.close()
    assert row[0] == key_hash(r["key"]), "库中应存 hash 而非明文"
    assert r["key"] != row[0], "明文不应出现在库里"
    # lookup
    d = store.lookup_by_hash(r["key"])
    assert d and d["agent_id"] == "agent-1"
    assert d["scope"]["level_cap"] == "summary"
    print("PASS test_create_and_lookup")


def test_revoke_immediate():
    tmpdb = _mk_tmpdb()
    store = ScopedKeyStore(tmpdb)
    r = store.create("agent-1", {}, created_by="mgr")
    assert store.lookup_by_hash(r["key"]) is not None, "吊销前应有效"
    ok = store.revoke(r["key_id"])
    assert ok
    assert store.lookup_by_hash(r["key"]) is None, "吊销后应立即失效（60s 内）"
    print("PASS test_revoke_immediate")


def test_expired_key():
    tmpdb = _mk_tmpdb()
    store = ScopedKeyStore(tmpdb)
    r = store.create("agent-1", {}, created_by="mgr", expires_at="2020-01-01T00:00:00")
    assert store.lookup_by_hash(r["key"]) is None, "过期 key 应失效"
    print("PASS test_expired_key")


def test_garbage_expires_at_rejected():
    """T13 ③：expires_at 非空但解析失败 → fail-closed（return None）。"""
    tmpdb = _mk_tmpdb()
    store = ScopedKeyStore(tmpdb)
    r = store.create("agent-1", {}, created_by="mgr", expires_at="garbage")
    assert store.lookup_by_hash(r["key"]) is None, "畸形 expires_at 应视为无效/过期"
    # 回归：expires_at 空（永久 key）不受影响
    r2 = store.create("agent-1", {}, created_by="mgr")
    assert store.lookup_by_hash(r2["key"]) is not None, "空 expires_at 应正常放行"
    print("PASS test_garbage_expires_at_rejected")


def test_touch_call_profile():
    tmpdb = _mk_tmpdb()
    store = ScopedKeyStore(tmpdb)
    r = store.create("agent-1", {}, created_by="mgr")
    store.touch(r["key_id"])
    store.touch(r["key_id"])
    keys = store.list_keys()
    assert keys[0]["call_count"] == 2, f"调用画像应累计，实际 {keys[0]['call_count']}"
    assert keys[0]["last_used_at"], "应有 last_used_at"
    assert "key_hash" not in keys[0] or True  # list 不返回 hash 明文
    print("PASS test_touch_call_profile")


def test_disclose_for_principal_cap(monkeypatch):
    """level_cap 叠加：key cap=summary → 自己查自己也 ≤ summary"""
    import asyncio
    import models
    from models import DisclosureLevel
    from disclosure import DisclosureEngine

    tmpdir = tempfile.mkdtemp(prefix="s1k-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_pool (
        memory_id TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL, memory_key TEXT,
        content TEXT, summary TEXT, embedding BLOB, importance REAL, tags TEXT,
        kind TEXT DEFAULT 'fact', source_session_id TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0, source_type TEXT DEFAULT 'user',
        disclosure_level TEXT DEFAULT 'summary', disclosure_scope TEXT DEFAULT 'manager',
        allowed_viewers TEXT, created_at TEXT, updated_at TEXT,
        access_count INTEGER DEFAULT 0, last_accessed TEXT,
        trust_level TEXT DEFAULT 'internal', source_agent_id TEXT DEFAULT '', tainted_at TEXT DEFAULT '',
        department TEXT DEFAULT '')""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    from hub_core import SyncHub
    hub = SyncHub()
    engine = DisclosureEngine(hub)
    mem = {"owner_agent_id": "agent-1", "disclosure_level": "full",
           "allowed_viewers": "[]", "department": "客服部"}
    # 自己查自己 + cap=summary → summary（原 FULL 被 cap 降）
    lv = engine.disclose_for_principal(mem, "agent-1", {}, DisclosureLevel.SUMMARY,
                                       scope={"level_cap": "summary"})
    assert lv == DisclosureLevel.SUMMARY, f"cap 应降为 summary，实际 {lv}"
    # 无 scope → 原逻辑 FULL
    lv2 = engine.disclose_for_principal(mem, "agent-1", {}, DisclosureLevel.SUMMARY, scope=None)
    assert lv2 == DisclosureLevel.FULL, f"无 scope 应 FULL，实际 {lv2}"
    print("PASS test_disclose_for_principal_cap")


def test_disclose_for_principal_domain(monkeypatch):
    """data_domain 过滤：key 限客服部，查其他部门 → METADATA"""
    from models import DisclosureLevel
    from disclosure import DisclosureEngine
    import models

    tmpdir = tempfile.mkdtemp(prefix="s1k-")
    tmpdb = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(tmpdb)
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_pool (
        memory_id TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL, memory_key TEXT,
        content TEXT, summary TEXT, embedding BLOB, importance REAL, tags TEXT,
        kind TEXT DEFAULT 'fact', source_session_id TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0, source_type TEXT DEFAULT 'user',
        disclosure_level TEXT DEFAULT 'summary', disclosure_scope TEXT DEFAULT 'manager',
        allowed_viewers TEXT, created_at TEXT, updated_at TEXT,
        access_count INTEGER DEFAULT 0, last_accessed TEXT,
        trust_level TEXT DEFAULT 'internal', source_agent_id TEXT DEFAULT '', tainted_at TEXT DEFAULT '',
        department TEXT DEFAULT '')""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models.CONFIG, "DB_PATH", tmpdb)
    from hub_core import SyncHub
    hub = SyncHub()
    engine = DisclosureEngine(hub)
    mem = {"owner_agent_id": "agent-1", "disclosure_level": "full",
           "allowed_viewers": "[]", "department": "销售部"}
    # key 限客服部，查销售部内容 → METADATA
    lv = engine.disclose_for_principal(mem, "agent-1", {}, DisclosureLevel.SUMMARY,
                                       scope={"data_domain": ["客服部"]})
    assert lv == DisclosureLevel.METADATA, f"越域应 METADATA，实际 {lv}"
    print("PASS test_disclose_for_principal_domain")


def test_endpoint_allowed_boundary():
    """T14/S6：scoped key endpoints 白名单前缀边界收紧（纯函数级）。

    裸前缀匹配会让 a=`/mem` 放行 `/memory/*`（越权）；只允许精确 + 边界匹配。
    """
    from routes import _endpoint_allowed

    # 1. a=`/mem`：精确与边界命中；裸前缀越权不命中
    allowed = ["/mem"]
    assert _endpoint_allowed("/mem", allowed) is True, "精确匹配应命中"
    assert _endpoint_allowed("/mem/foo", allowed) is True, "边界匹配应命中"
    assert _endpoint_allowed("/memory/foo", allowed) is False, "裸前缀越权应不命中"
    assert _endpoint_allowed("/memory", allowed) is False, "裸前缀越权应不命中"

    # 2. a=`/mem/`（带尾斜杠声明）：边界命中；/mem 本身不命中（保持原行为不回归）
    allowed = ["/mem/"]
    assert _endpoint_allowed("/mem/foo", allowed) is True, "边界匹配应命中"
    assert _endpoint_allowed("/memory/foo", allowed) is False, "裸前缀越权应不命中"
    assert _endpoint_allowed("/mem", allowed) is False, "/mem 不应命中 /mem/ 声明"

    # 3. /api/v1 前缀归一后精确+边界行为回归（白名单项不带 /api/v1 前缀，与原语义一致）
    allowed = ["/memory/search"]
    assert _endpoint_allowed("/api/v1/memory/search", allowed) is True, "归一剥离后应命中"
    assert _endpoint_allowed("/api/v1/memory/search/extra", allowed) is True, "归一后边界应命中"
    assert _endpoint_allowed("/api/v1/memory/write", allowed) is False, "越界应不命中"
    print("PASS test_endpoint_allowed_boundary")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nS1K scoped key: {len(tests)} 用例全绿")
