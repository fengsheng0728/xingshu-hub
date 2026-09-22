# -*- coding: utf-8 -*-
"""T17 · CD-056 配套：「存在性不泄露」403 策略冻结矩阵断言（2026-09-19）

冻结策略（逐字落档于 docs/api-error-policy.md）：
资源存在但无权（403）必须与资源不存在同响应——对非特权主体，「不存在」与
「无权」返回同一状态码 + 同一 detail 文本，不得由响应差异反推资源是否存在
（防 id/key 枚举预言机）；对特权主体或资源 owner，保持「存在但无权 = 403 /
不存在 = 404」的可区分语义（控制台与 owner 自查流程不受影响）。

矩阵：
- P-1 /shared/docs/{doc_id} 非特权主体：不存在的 doc_id → 403；私有无权 → 403；
  两者 status_code 与 detail 完全相同（先红：改动前不存在 → 404，本用例必失败）
- P-2 /shared/docs/{doc_id} 特权主体（hub_token principal）：不存在 → 404、
  私有无权 → 403、有权 → 200——两侧结果必须不同（同 403/403 不算打中判定，
  可区分语义同时证明 P-1 的 403/403 不是「恒 403」的假打中）
- P-3 owner 读自己的 private 文档 → 200（回归）
- P-4 /memory/{key}/versions 对照组（CD-056 已对齐）：非特权主体的
  「不存在 key」与「存在但非本人 key」→ 同一 403 + 同 detail
- P-5 读审计口径：成功路径落 1 行、403 路径落 1 行 denied（语义随 CD-059 变更：2026-09-20 用户追认，不回滚）
- P-6 未对齐清单：GET /knowledge/{entry_id} 现状以 xfail(run=False) 登记
  （只登记不修，归 knowledge 组收编轮；不许静默通过，也不放宽断言换假绿）

脚手架同 tests/test_shared_read_audit.py / tests/test_memory_versions_owner_scope.py：
临时库走 db.init_db() 建全 schema（先 monkeypatch CONFIG.DB_PATH）；shared
workspace 用 SharedWorkspace(db_path, store_dir=...) 临时实例并 monkeypatch
routes_shared._sw.workspace；memory 审计目录重定向到 tmp（防污染仓库 audit/）；
直调 handler 协程（本仓 SYNC_HUB_NO_AUTH=1 绕过 Depends，身份门须显式传
current_agent / principal 才测得到），不起 TestClient、不绑端口、不 spawn Hub。
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException

import audit.memory_audit as memory_audit
import db
import models
import routes_memory
import routes_shared
from models import CONFIG
from shared_workspace import SharedWorkspace

# 特权主体：auth_mode == "hub_token" 时 principal_is_privileged 短路为 True，
# 不触库；_log_read 只需 .scope / .auth_mode 两个属性
PRIV_PRINCIPAL = SimpleNamespace(auth_mode="hub_token", subject_id="op-admin",
                                 scope=None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（完整 schema）+ 临时 workspace 单例 + 临时 memory 审计目录"""
    db_path = str(tmp_path / "policy_matrix.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db.init_db()  # 完整 schema，含 shared_docs / memory_pool / gateway_read_log
    ws = SharedWorkspace(db_path, store_dir=str(tmp_path / "store"))
    monkeypatch.setattr(routes_shared._sw, "workspace", ws)
    monkeypatch.setattr(memory_audit, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(memory_audit, "AUDIT_FILE",
                        str(tmp_path / "audit" / "memory_pool.jsonl"))
    monkeypatch.setattr(memory_audit, "_rolling_chain", None)
    return ws, db_path


async def _capture(coro):
    """捕获 handler 协程结果：HTTPException 原样返回，否则返回正常结果"""
    try:
        return await coro
    except HTTPException as e:
        return e


def _readlog_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT requester, kind, query, target, granted_level, item_count,"
        " stripped_chunks FROM gateway_read_log"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    conn = sqlite3.connect(db_path)
    for v, content in versions:
        conn.execute(
            "INSERT INTO memory_versions (memory_id, memory_key, version, content,"
            " summary, confidence, archived_by)"
            " VALUES (?, ?, ?, ?, ?, 0.9, 'test')",
            (memory_id, key, v, content, f"摘要v{v}"))
    conn.commit()
    conn.close()


# ═══ P-1 /shared/docs/{doc_id} 非特权主体：不存在 与 存在但无权 → 同一 403 + 同 detail ═══

def test_p1_nonpriv_nonexistent_and_forbidden_same_403(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc = await ws.create_doc("机密文档", "ag-owner", visibility="private")
            missing = await _capture(routes_shared.api_shared_get(
                "doc-不存在00000", current_agent="ag-outsider", principal=None))
            forbidden = await _capture(routes_shared.api_shared_get(
                doc["doc_id"], current_agent="ag-outsider", principal=None))
            assert isinstance(missing, HTTPException), \
                f"不存在的 doc_id 应拒绝，实际正常返回: {missing}"
            assert isinstance(forbidden, HTTPException), \
                f"私有无权应拒绝，实际正常返回: {forbidden}"
            assert missing.status_code == 403, \
                f"策略：非特权主体对不存在的 doc_id 必须 403（与无权同响应），" \
                f"实际 {missing.status_code} detail={missing.detail!r}"
            assert forbidden.status_code == 403
            assert missing.status_code == forbidden.status_code
            assert missing.detail == forbidden.detail, \
                f"detail 必须逐字相同，实际 {missing.detail!r} vs {forbidden.detail!r}"
        finally:
            await ws.stop()

    anyio.run(main)


# ═══ P-2 特权主体：不存在 → 404 / 私有无权 → 403 / 有权 → 200（两侧必须可区分） ═══

def test_p2_privileged_distinguishable(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            priv_doc = await ws.create_doc("机密文档", "ag-owner",
                                           visibility="private")
            team_doc = await ws.create_doc("团队文档", "ag-owner")
            missing = await _capture(routes_shared.api_shared_get(
                "doc-不存在00000", current_agent="ag-outsider",
                principal=PRIV_PRINCIPAL))
            forbidden = await _capture(routes_shared.api_shared_get(
                priv_doc["doc_id"], current_agent="ag-outsider",
                principal=PRIV_PRINCIPAL))
            assert isinstance(missing, HTTPException)
            assert missing.status_code == 404, \
                f"特权主体对不存在的 doc_id 应保持 404，实际 {missing.status_code}"
            assert isinstance(forbidden, HTTPException)
            assert forbidden.status_code == 403
            assert forbidden.detail == "无权访问该文档"
            # 两侧结果必须不同——同 404/404 或同 403/403 不算打中判定
            assert missing.status_code != forbidden.status_code
            # 有权 → 200（team 文档全员可访问）
            ok = await routes_shared.api_shared_get(
                team_doc["doc_id"], current_agent="ag-outsider",
                principal=PRIV_PRINCIPAL)
            assert ok["doc_id"] == team_doc["doc_id"]
            assert ok["content"] == await ws.get_doc_content(team_doc["doc_id"])
        finally:
            await ws.stop()

    anyio.run(main)


# ═══ P-3 owner 读自己的 private 文档 → 200（回归） ═══

def test_p3_owner_reads_own_private_doc(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc = await ws.create_doc("owner 私有", "ag-owner",
                                      visibility="private")
            result = await routes_shared.api_shared_get(
                doc["doc_id"], current_agent="ag-owner", principal=None)
            assert result == {"doc_id": doc["doc_id"],
                              "content": await ws.get_doc_content(doc["doc_id"])}
        finally:
            await ws.stop()

    anyio.run(main)


# ═══ P-4 /memory/{key}/versions 对照组（CD-056 已对齐）═══

def test_p4_memory_versions_control_group(env):
    _ws, db_path = env
    _insert_memory(db_path, "m-b1", "agent-b", "b-秘密key", "B 的现行正文")
    _insert_versions(db_path, "m-b1", "b-秘密key", [(1, "B-旧版正文-甲")])
    cross = asyncio.run(_capture(routes_memory.api_memory_versions(
        memory_key="b-秘密key", agent_id="agent-a",
        current_agent="agent-a", principal=None)))
    missing = asyncio.run(_capture(routes_memory.api_memory_versions(
        memory_key="根本不存在的key", agent_id="agent-a",
        current_agent="agent-a", principal=None)))
    assert isinstance(cross, HTTPException) and cross.status_code == 403
    assert isinstance(missing, HTTPException) and missing.status_code == 403
    assert cross.detail == missing.detail, \
        "对照组：归属不成立 与 key 不存在 必须同一 403 响应（CD-056 已对齐）"


# ═══ P-5 读审计口径：成功落 1 行；403（含「不存在→403」新路径）落 1 行 denied（语义随 CD-059 变更；2026-09-20 用户追认） ═══

def test_p5_read_audit_success_and_denied(env):
    ws, db_path = env

    async def main():
        await ws.start()
        try:
            doc = await ws.create_doc("私密", "ag-owner", visibility="private",
                                      allowed_agents=["ag-friend"])
            # 成功路径：恰好多 1 行，字段核对（对齐 T9 S-2）
            before = len(_readlog_rows(db_path))
            result = await routes_shared.api_shared_get(
                doc["doc_id"], current_agent="ag-owner", principal=None)
            rows = _readlog_rows(db_path)
            assert len(rows) - before == 1, \
                f"成功路径应恰好多 1 行，实际 {len(rows) - before}"
            row = rows[-1]
            assert row["requester"] == "ag-owner"
            assert row["kind"] == "shared"
            assert row["target"] == doc["doc_id"]
            assert row["granted_level"] == "full"
            assert row["item_count"] == 1
            assert result["doc_id"] == doc["doc_id"]
            # 403 路径（私有无权）落 1 行 denied（CD-059 翻转 T9 S-4 旧断言）
            before = len(_readlog_rows(db_path))
            r = await _capture(routes_shared.api_shared_get(
                doc["doc_id"], current_agent="ag-outsider", principal=None))
            assert isinstance(r, HTTPException) and r.status_code == 403
            rows = _readlog_rows(db_path)
            assert len(rows) == before + 1, \
                f"403 拒绝必须恰好多 1 行 denied，实际 {len(rows) - before}"
            assert rows[-1]["granted_level"] == "denied"
            # 403 路径（不存在的 doc_id，策略合并后的新 403）同样落 1 行 denied
            before = len(_readlog_rows(db_path))
            r = await _capture(routes_shared.api_shared_get(
                "doc-不存在00000", current_agent="ag-outsider", principal=None))
            assert isinstance(r, HTTPException) and r.status_code == 403
            rows = _readlog_rows(db_path)
            assert len(rows) == before + 1, \
                f"403 拒绝必须恰好多 1 行 denied，实际 {len(rows) - before}"
            assert rows[-1]["granted_level"] == "denied"
        finally:
            await ws.stop()

    anyio.run(main)


# ═══ P-6 未对齐清单：GET /knowledge/{entry_id} 现状登记（只登记不修） ═══

@pytest.mark.xfail(run=False, reason=(
    "未对齐登记（T17 只登记不修，归 knowledge 组收编轮）："
    "GET /api/v1/knowledge/{entry_id}（routes_knowledge.py:100-119）只有认证门、"
    "无权限门——任何认证主体对存在条目均可取（CD-052 仅做内容降级剥离，不构成"
    "「无权」侧）；不存在 → 404「知识条目不存在」→ 存在性可枚举。"
    "待对齐词条详见 docs/api-error-policy.md「未对齐清单」。"))
def test_p6_knowledge_entry_existence_oracle_registered():
    """待对齐词条（对齐验收时取消 xfail 并按下述草案实跑，不得放宽断言换绿）：

    - GET /api/v1/knowledge/{entry_id}：当前无「无权」侧、缺失即 404，
      存在性可枚举。策略适用性待 knowledge 组拍板（条目是否引入 owner /
      可见性概念；若引入，则按本策略要求：非特权主体 × (不存在 / 无权)
      → 同一 status_code + 同一 detail；特权主体 / owner 保持可区分）。
    """
    raise AssertionError("登记用例：对齐前不应通过")
