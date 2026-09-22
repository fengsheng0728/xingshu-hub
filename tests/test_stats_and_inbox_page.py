# -*- coding: utf-8 -*-
"""CD-043：收件箱分页/清理 + /api/v1/stats 容量与延迟观测。

动机（实测）：
- `/api/v1/wiki/inbox` 原无 LIMIT，积压 41,503 条 pending 时单次响应 7.1MB
  （SQL 123ms + JSON 121ms）→ 控制台（dashboard/wiki.html、hub_ui stores）首屏卡死。
- 膨胀只能靠人工翻库才发现 → 加 /api/v1/stats 把行数/体积/检索延迟变成可看数字。
"""
import asyncio
import os
import sqlite3

import pytest

import routes_common
import routes_dashboard
import routes_wiki
import wiki_engine

_HUB = {"auth_mode": "hub_token", "subject_id": ""}   # 控制台 hub_token 视为特权


def _mk_wiki_db(path, n_pending=150, extra=()):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE wiki_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, page_path TEXT NOT NULL UNIQUE,
            title TEXT, status TEXT DEFAULT 'pending', source TEXT,
            created_at TEXT DEFAULT (datetime('now')), reviewed_at TEXT,
            reviewed_by TEXT, trust_level TEXT NOT NULL DEFAULT 'internal')"""
    )
    for i in range(n_pending):
        conn.execute(
            "INSERT INTO wiki_inbox (page_path, title, status, source, created_at)"
            " VALUES (?, ?, 'pending', 'auto-sync', datetime('now', ?))",
            ("concepts/p%d.md" % i, "t%d" % i, "-%d minutes" % i),
        )
    for i, (pp, st) in enumerate(extra):
        conn.execute(
            "INSERT OR REPLACE INTO wiki_inbox (page_path, title, status, source) VALUES (?, ?, ?, 'test')",
            (pp, "x%d" % i, st),
        )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    yield


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = _mk_wiki_db(tmp_path / "w.db")
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    import db as db_mod
    monkeypatch.setattr(db_mod.CONFIG, "DB_PATH", db)
    wiki_root = tmp_path / "wiki"
    for sub in ("entities", "concepts", "comparisons", "queries"):
        (wiki_root / sub).mkdir(parents=True, exist_ok=True)
    # 只有 p0/p1 两个页面真实存在 → 其余 148 条属陈旧
    (wiki_root / "concepts" / "p0.md").write_text("x", encoding="utf-8")
    (wiki_root / "concepts" / "p1.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(wiki_engine, "WIKI_ROOT", str(wiki_root))
    return {"db": db, "wiki_root": str(wiki_root)}


def test_inbox_default_page_limited(env):
    """默认只返回 100 条 + total/has_more（不再一次性吐全部）。"""
    out = asyncio.run(routes_wiki.api_wiki_inbox())
    assert out["total"] == 150
    assert out["limit"] == 100 and len(out["inbox"]) == 100
    assert out["has_more"] is True


def test_inbox_offset_and_clamp(env):
    """offset 生效；limit 上限压制到 500，下限压到 1。"""
    a = asyncio.run(routes_wiki.api_wiki_inbox(limit=10, offset=0))
    b = asyncio.run(routes_wiki.api_wiki_inbox(limit=10, offset=10))
    assert [r["id"] for r in a["inbox"]] != [r["id"] for r in b["inbox"]]
    assert len(b["inbox"]) == 10
    assert asyncio.run(routes_wiki.api_wiki_inbox(limit=9999))["limit"] == 500
    assert asyncio.run(routes_wiki.api_wiki_inbox(limit=0))["limit"] == 1


def test_inbox_last_page_has_more_false(env):
    out = asyncio.run(routes_wiki.api_wiki_inbox(limit=200, offset=0))
    assert len(out["inbox"]) == 150 and out["has_more"] is False


def test_cleanup_dry_run_then_delete(env):
    """清理规则：pending 且页文件已不存在 → 删；dry_run 只统计。"""
    dry = asyncio.run(routes_wiki.api_wiki_inbox_cleanup(dry_run=True, principal=_HUB))
    assert dry["scanned"] == 150 and dry["removed"] == 148, dry
    conn = sqlite3.connect(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM wiki_inbox").fetchone()[0] == 150, "dry_run 不得动库"
    conn.close()

    real = asyncio.run(routes_wiki.api_wiki_inbox_cleanup(dry_run=False, principal=_HUB))
    assert real["removed"] == 148
    conn = sqlite3.connect(env["db"])
    left = [r[0] for r in conn.execute("SELECT page_path FROM wiki_inbox ORDER BY id").fetchall()]
    conn.close()
    assert left == ["concepts/p0.md", "concepts/p1.md"], left


def test_cleanup_requires_privilege(env):
    """清理与审批同门：worker principal → 403；hub_token → 放行。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_wiki.api_wiki_inbox_cleanup(principal={"auth_mode": "api_key", "subject_id": "w1"}))
    assert exc.value.status_code == 403
    out = asyncio.run(routes_wiki.api_wiki_inbox_cleanup(
        principal={"auth_mode": "hub_token", "subject_id": ""}))
    assert out["ok"] is True


def test_cleanup_keeps_reviewed_rows(env):
    """已审（approved/rejected）的行不属清理范围。"""
    conn = sqlite3.connect(env["db"])
    conn.execute("INSERT INTO wiki_inbox (page_path, title, status, source) VALUES ('gone.md', 't', 'approved', 'manual')")
    conn.commit()
    conn.close()
    asyncio.run(routes_wiki.api_wiki_inbox_cleanup(dry_run=False, principal=_HUB))
    conn = sqlite3.connect(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM wiki_inbox WHERE status='approved'").fetchone()[0] == 1
    conn.close()


def test_stats_shape(env):
    """/api/v1/stats 返回 DB/行数/wiki/检索/同步 四组指标，且轻量可读。"""
    out = asyncio.run(routes_dashboard.api_stats())
    assert out["status"] == "ok"
    assert set(out["rows"]) >= {"knowledge_base", "wiki_inbox_pending", "events"}
    assert out["db"]["size_mb"] is not None
    assert out["wiki"]["pages"] == 2, out["wiki"]
    assert "p50_ms" in out["search"] and "degraded" in out["search"]
    assert "running" in out["sync"]


def test_stats_counts_wiki_pages_recursively(env, tmp_path):
    (tmp_path / "wiki" / "entities" / "e1.md").write_text("x", encoding="utf-8")
    out = asyncio.run(routes_dashboard.api_stats())
    assert out["wiki"]["pages"] == 3
