# -*- coding: utf-8 -*-
"""CD-040（2026-09-14）：认证凭据查询缓存 —— 列集缓存（默认开）+ 行缓存（默认关，可选开）。

背景：鉴权中间件对每个已认证请求执行 `PRAGMA table_info(agents)` + SELECT（同步、事件循环内），
200 并发下与写缓冲 BEGIN IMMEDIATE 争锁。实测（同 DB 快照/同 harness/worktree）：
现状峰 4.62s·吞吐 1841 → 配额快照 + 行缓存后 3.15s·3339。

设计取舍（与既有语义的冲突已在测试里锁死）：
- 列集缓存默认开：DDL 运行期不变，零语义影响；
- token→row 缓存默认**关**（SYNC_HUB_AUTH_ROW_CACHE=1 显式开）：它会把「凭据变更立即生效」
  放宽为「≤TTL 生效」，与 test_s1_auth_provider 的轮换/过期语义冲突 → 严格语义为默认。
"""
import os
import sqlite3
from types import SimpleNamespace

import pytest

from auth_provider import LocalProvider


def _mk_db(tmp_path, rows):
    p = os.path.join(str(tmp_path), "agents.db")
    c = sqlite3.connect(p)
    c.execute(
        """CREATE TABLE agents (
               agent_id TEXT PRIMARY KEY, api_key TEXT, api_key_prev TEXT,
               api_key_created_at TEXT, api_key_expires_at TEXT,
               api_key_prev_expires_at TEXT, api_key_ip_whitelist TEXT,
               last_used_at TEXT)"""
    )
    for r in rows:
        c.execute("INSERT INTO agents (agent_id, api_key, api_key_expires_at) VALUES (?, ?, ?)", r)
    c.commit()
    c.close()
    return p


@pytest.fixture(autouse=True)
def _clear_caches():
    LocalProvider._lookup_cache.clear()
    LocalProvider._schema_cols_cache.clear()
    yield
    LocalProvider._lookup_cache.clear()
    LocalProvider._schema_cols_cache.clear()


def _provider(db_path):
    return LocalProvider(SimpleNamespace(DB_PATH=db_path))


def _count_connects(provider, monkeypatch):
    calls = {"n": 0}
    orig = provider._connect

    def wrapped():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(provider, "_connect", wrapped)
    return calls


def test_default_strict_no_row_cache(tmp_path, monkeypatch):
    """默认（未开开关）：不得启用行缓存 —— 每次查询都读库，凭据变更立即生效。"""
    monkeypatch.delenv("SYNC_HUB_AUTH_ROW_CACHE", raising=False)
    db = _mk_db(tmp_path, [("a1", "key-abc", None)])
    p = _provider(db)
    calls = _count_connects(p, monkeypatch)

    assert p._lookup_agent("key-abc")["agent_id"] == "a1"
    n = calls["n"]
    assert p._lookup_agent("key-abc")["agent_id"] == "a1"
    assert calls["n"] > n, "默认必须每次读库（行缓存关闭）"
    assert not LocalProvider._lookup_cache, "默认不得写入行缓存"


def test_optin_row_cache_single_db_hit(tmp_path, monkeypatch):
    """显式开开关：正命中缓存，连续查询只访问 DB 一次。"""
    monkeypatch.setenv("SYNC_HUB_AUTH_ROW_CACHE", "1")
    db = _mk_db(tmp_path, [("a1", "key-abc", None)])
    p = _provider(db)
    calls = _count_connects(p, monkeypatch)

    r1 = p._lookup_agent("key-abc")
    first = calls["n"]
    r2 = p._lookup_agent("key-abc")

    assert r1 and r1["agent_id"] == "a1"
    assert r2 and r2["agent_id"] == "a1"
    assert first == 1, "首次应恰好 1 次连接（列集与查询同连接），实测 %d" % first
    assert calls["n"] == first, "第二次必须命中缓存、零 DB 访问，实测 %d" % calls["n"]


def test_columns_cache_shared_across_tokens(tmp_path, monkeypatch):
    """列集缓存默认生效：不同 token 各 1 次查询连接，列集不额外连库。"""
    monkeypatch.delenv("SYNC_HUB_AUTH_ROW_CACHE", raising=False)
    db = _mk_db(tmp_path, [("a1", "k1", None), ("a2", "k2", None)])
    p = _provider(db)
    calls = _count_connects(p, monkeypatch)
    p._lookup_agent("k1")
    p._lookup_agent("k2")
    assert calls["n"] == 2, "两个 token 各 1 次连接（无额外 PRAGMA 连接），实测 %d" % calls["n"]


def test_invalidate_forces_reload(tmp_path, monkeypatch):
    """失效钩子：invalidate_auth_cache() 后强制重读。"""
    monkeypatch.setenv("SYNC_HUB_AUTH_ROW_CACHE", "1")
    db = _mk_db(tmp_path, [("a1", "key-abc", None)])
    p = _provider(db)
    calls = _count_connects(p, monkeypatch)

    p._lookup_agent("key-abc")
    n = calls["n"]
    p.invalidate_auth_cache()
    p._lookup_agent("key-abc")
    assert calls["n"] > n, "清缓存后必须重新访问 DB"


def test_negative_result_not_cached(tmp_path, monkeypatch):
    """负结果不缓存：新签发的 key 立即可用，不受 TTL 拖累。"""
    monkeypatch.setenv("SYNC_HUB_AUTH_ROW_CACHE", "1")
    db = _mk_db(tmp_path, [])
    p = _provider(db)
    assert p._lookup_agent("late-key") is None
    c = sqlite3.connect(db)
    c.execute("INSERT INTO agents (agent_id, api_key) VALUES ('a9', 'late-key')")
    c.commit()
    c.close()
    r = p._lookup_agent("late-key")
    assert r and r["agent_id"] == "a9", "负结果被缓存 → 新 key 要等 TTL 才生效（回归）"


def test_ttl_expiry_reloads(tmp_path, monkeypatch):
    """TTL 过期后重读。"""
    monkeypatch.setenv("SYNC_HUB_AUTH_ROW_CACHE", "1")
    db = _mk_db(tmp_path, [("a1", "key-abc", None)])
    p = _provider(db)
    calls = _count_connects(p, monkeypatch)
    monkeypatch.setattr(LocalProvider, "AUTH_CACHE_TTL_S", 0.0)

    p._lookup_agent("key-abc")
    n = calls["n"]
    p._lookup_agent("key-abc")
    assert calls["n"] > n, "TTL=0 时必须每次重读"


def test_rotate_keys_clears_cache(tmp_path, monkeypatch):
    """rotate_keys() 挂失效钩子：轮换后行缓存立即清空。"""
    monkeypatch.setenv("SYNC_HUB_AUTH_ROW_CACHE", "1")
    db = _mk_db(tmp_path, [("a1", "old-key", "2020-01-01T00:00:00+00:00")])
    p = _provider(db)
    assert p._lookup_agent("old-key") is not None
    assert LocalProvider._lookup_cache, "缓存应已填充"
    rotated = p.rotate_keys()
    assert rotated == 1, "过期主 key 应轮换 1 条，实测 %s" % rotated
    assert not LocalProvider._lookup_cache, "rotate_keys 必须清空 token→row 缓存"
