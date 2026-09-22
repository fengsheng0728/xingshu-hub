# -*- coding: utf-8 -*-
"""T14 · CD-056：GET /api/v1/memory/{key}/versions owner 归属校验（越权读修复）验收测试（2026-09-19）

漏洞（HEAD c0f810d 实测）：hub_mixins/memory.py 的 get_memory_versions 只按
memory_key 过滤，agent_id 参数从未进 SQL；memory_key 跨 agent 不唯一 →
认证过的 agent A 用 B 的 memory_key 可拿到 B 的版本全文历史。
修复口径：owner-only，先按 (memory_key, owner_agent_id) 查 memory_pool 拿
memory_id（查不到 → forbidden，fail-closed），再按 memory_id 查版本。

V-1 跨 agent 被拒（先红）：B 的记忆+版本在库，A 以 A 身份请求 B 的 key → 403，
    且响应体不含 B 的版本正文（改动前必然失败：返回 B 的全文）
V-2 owner 自查正常（回归）：A 查自己的 key → 版本与库内逐条一致（字段、
    version DESC 顺序、LIMIT 20）
V-3 不存在的 key → 403（fail-closed，与 V-1 的 403 detail 完全一致，不可区分存在性）
V-4 孤儿版本（memory_pool 行已删、versions 残留）→ 403（fail-closed）
V-5 读审计：成功路径落 1 行 gateway_read_log（kind='memory'、target=agent_id、
    item_count == len(versions)）；403 路径落 1 行 denied（语义随 CD-059 变更）

脚手架同 tests/test_memory_read_audit.py：临时库走 db.init_db() 建全 schema
（先 monkeypatch CONFIG.DB_PATH），直调 handler 协程（Depends 直传参），
不起 TestClient；memory 数据直接 INSERT 临时库。
"""
import asyncio
import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit.memory_audit as memory_audit  # noqa: E402
import models  # noqa: E402
import routes_memory  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表）+ 临时审计目录（防 memory_audit 污染仓库 audit/）。"""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()
    monkeypatch.setattr(memory_audit, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(memory_audit, "AUDIT_FILE",
                        str(tmp_path / "audit" / "memory_pool.jsonl"))
    monkeypatch.setattr(memory_audit, "_rolling_chain", None)
    return db_path


def _insert_memory(db_path, memory_id, owner, key, content):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content,"
        " summary, embedding, importance, tags, kind, confidence, source_type,"
        " disclosure_level, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, '', NULL, 1.0, '[]', 'fact', 1.0, 'user', 'summary',"
        " datetime('now'), datetime('now'))",
        (memory_id, owner, key, content),
    )
    conn.commit()
    conn.close()


def _insert_versions(db_path, memory_id, key, versions):
    """versions: [(version, content), ...]"""
    conn = sqlite3.connect(db_path)
    for v, content in versions:
        conn.execute(
            "INSERT INTO memory_versions (memory_id, memory_key, version, content,"
            " summary, confidence, archived_by)"
            " VALUES (?, ?, ?, ?, ?, 0.9, 'test')",
            (memory_id, key, v, content, f"摘要v{v}"))
    conn.commit()
    conn.close()


def _log_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT requester, kind, query, target, granted_level, item_count,"
        " stripped_chunks FROM gateway_read_log ORDER BY log_id")]
    conn.close()
    return rows


# ═══════════ V-1 跨 agent 被拒（先红） ═══════════

def test_v1_cross_agent_forbidden(env):
    _insert_memory(env, "m-b1", "agent-b", "b-秘密key", "B 的现行正文")
    _insert_versions(env, "m-b1", "b-秘密key",
                     [(1, "B-旧版正文-甲"), (2, "B-旧版正文-乙")])
    try:
        result = asyncio.run(routes_memory.api_memory_versions(
            memory_key="b-秘密key", agent_id="agent-a",
            current_agent="agent-a", principal=None))
    except HTTPException as e:
        # 修复后期望：403，且 detail 不含 B 的任何正文
        assert e.status_code == 403
        detail = str(e.detail)
        assert "B-旧版正文" not in detail
        assert "B 的现行正文" not in detail
        return
    pytest.fail(f"越权读成功：agent-a 拿到了 agent-b 的版本全文历史: {result}")


# ═══════════ V-2 owner 自查正常（回归） ═══════════

def test_v2_owner_self_read(env):
    _insert_memory(env, "m-a1", "agent-a", "a-key", "A 的现行正文")
    _insert_versions(env, "m-a1", "a-key",
                     [(v, f"A-第{v}版正文") for v in range(1, 26)])  # 25 版 → 验证 LIMIT 20
    result = asyncio.run(routes_memory.api_memory_versions(
        memory_key="a-key", agent_id="agent-a",
        current_agent="agent-a", principal=None))
    assert result["memory_key"] == "a-key"
    versions = result["versions"]
    assert len(versions) == 20, f"LIMIT 20，实际 {len(versions)}"
    assert [x["version"] for x in versions] == list(range(25, 5, -1)), \
        "应按 version DESC 取最新 20 条"
    # 字段形状 + 与库内逐条一致
    conn = sqlite3.connect(env)
    conn.row_factory = sqlite3.Row
    db_rows = {r["version"]: dict(r) for r in conn.execute(
        "SELECT id, version, content, summary, confidence, archived_at, archived_by"
        " FROM memory_versions WHERE memory_key = 'a-key'")}
    conn.close()
    for x in versions:
        assert set(x.keys()) == {"id", "version", "content", "summary",
                                 "confidence", "archived_at", "archived_by"}, \
            f"返回形状变化: {sorted(x.keys())}"
        d = db_rows[x["version"]]
        for f in ("id", "version", "content", "summary", "confidence",
                  "archived_at", "archived_by"):
            assert x[f] == d[f], f"version={x['version']} 字段 {f} 与库内不一致"


# ═══════════ V-3 不存在的 key → 403（与 V-1 同一 403，不可区分存在性） ═══════════

def test_v3_nonexistent_key_403(env):
    _insert_memory(env, "m-b1", "agent-b", "b-秘密key", "B 的现行正文")
    _insert_versions(env, "m-b1", "b-秘密key", [(1, "B-旧版正文-甲")])
    with pytest.raises(HTTPException) as cross_exc:
        asyncio.run(routes_memory.api_memory_versions(
            memory_key="b-秘密key", agent_id="agent-a",
            current_agent="agent-a", principal=None))
    with pytest.raises(HTTPException) as missing_exc:
        asyncio.run(routes_memory.api_memory_versions(
            memory_key="根本不存在的key", agent_id="agent-a",
            current_agent="agent-a", principal=None))
    assert cross_exc.value.status_code == missing_exc.value.status_code == 403
    assert cross_exc.value.detail == missing_exc.value.detail, \
        "归属不成立 与 key 不存在 必须是同一个 403 响应，不可区分"


# ═══════════ V-4 孤儿版本（记忆行已删、versions 残留）→ 403 ═══════════

def test_v4_orphan_versions_403(env):
    # memory_pool 无对应行，memory_versions 残留 → fail-closed
    _insert_versions(env, "m-gone", "orphan-key", [(1, "孤儿残留正文")])
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_memory.api_memory_versions(
            memory_key="orphan-key", agent_id="agent-a",
            current_agent="agent-a", principal=None))
    assert exc_info.value.status_code == 403


# ═══════════ V-5 读审计：成功落 1 行；403 落 1 行 denied（语义随 CD-059 变更；2026-09-20 用户追认） ═══════════

def test_v5_read_audit(env):
    _insert_memory(env, "m-a1", "agent-a", "a-key", "A 的现行正文")
    _insert_versions(env, "m-a1", "a-key", [(1, "A-第1版正文"), (2, "A-第2版正文")])
    before = len(_log_rows(env))
    result = asyncio.run(routes_memory.api_memory_versions(
        memory_key="a-key", agent_id="agent-a",
        current_agent="agent-a", principal=None))
    rows = _log_rows(env)
    assert len(rows) == before + 1, f"成功路径应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-a"
    assert row["kind"] == "memory"
    assert row["target"] == "agent-a"
    assert row["item_count"] == len(result["versions"]) == 2
    # 403 路径落 1 行 denied（CD-059：拒绝留痕；只记拒绝事实，不记存在性差异）
    before = len(_log_rows(env))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_memory.api_memory_versions(
            memory_key="b-秘密key", agent_id="agent-a",
            current_agent="agent-a", principal=None))
    assert exc_info.value.status_code == 403
    rows = _log_rows(env)
    assert len(rows) == before + 1, \
        f"403 拒绝必须恰好多 1 行 denied，实际 {len(rows) - before}"
    assert rows[-1]["granted_level"] == "denied"
    assert rows[-1]["target"] == "agent-a"
