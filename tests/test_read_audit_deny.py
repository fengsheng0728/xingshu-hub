# -*- coding: utf-8 -*-
"""T18 · CD-059：读端点 403/404 拒绝留痕（gateway_read_log 落 denied 行）验收测试（2026-09-20）

口径（任务书冻结）：拒绝落行 granted_level="denied"、item_count=0、stripped_chunks=0；
kind 沿用端点既有值；auth_mode 只记类别（hub_token/api_key/anonymous-tool/空串），
严禁凭据明文；400/401/503 不落盘；成功路径既有 _log_read 口径逐字不变。

- D-1 先红核心：非特权主体读他人记忆（api_get_memories 身份门 403）→
  gateway_read_log 恰好多 1 行 denied（改动前 0 行，本用例改动前必失败）
- D-2 不存在资源 404（gateway kind=doc 无分块）→ 同样落 1 行 denied
- D-3 成功路径不回归：有权读取仍落 1 行，granted_level 与改动前一致（对齐
  tests/test_memory_read_audit.py M-1 断言写法）
- D-4 凭据不明文：哨兵值挂在 principal 凭据形态属性上，落行全列扫描，
  哨兵串不得出现在任何列；auth_mode 列只落类别 "api_key"
- D-5 400 不落盘：kind 非法 / query 空 两种参数错误后行数不变
- D-6 多端点覆盖：routes_shared（api_shared_get 私有无权 403）与
  routes_knowledge（api_knowledge_get 不存在 404）各落 1 行 denied

脚手架（防假绿，对齐 test_memory_read_audit.py / test_shared_read_audit.py /
test_403_policy_matrix.py）：临时库走 db.init_db() 建完整 schema（先 monkeypatch
CONFIG.DB_PATH 再 init_db），gateway_read_log 真实存在；memory 审计目录重定向到
tmp（防污染仓库 audit/）；shared workspace 用 SharedWorkspace(db_path, store_dir=...)
临时实例并 monkeypatch routes_shared._sw.workspace；直调 handler 协程（显式传
current_agent/principal；SYNC_HUB_NO_AUTH=1 绕 Depends，身份门用 monkeypatch
routes_memory.NO_AUTH=False 才测得到），不起 TestClient、不绑端口、不 spawn Hub。
"""
import asyncio
import os
import sqlite3
import sys
from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit.memory_audit as memory_audit  # noqa: E402
import db  # noqa: E402
import models  # noqa: E402
import routes_gateway  # noqa: E402
import routes_knowledge  # noqa: E402
import routes_memory  # noqa: E402
import routes_shared  # noqa: E402
from shared_workspace import SharedWorkspace  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表）+ 临时审计目录 + 临时 workspace 单例"""
    db_path = str(tmp_path / "deny.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    db.init_db()
    monkeypatch.setattr(memory_audit, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(memory_audit, "AUDIT_FILE",
                        str(tmp_path / "audit" / "memory_pool.jsonl"))
    monkeypatch.setattr(memory_audit, "_rolling_chain", None)
    ws = SharedWorkspace(db_path, store_dir=str(tmp_path / "store"))
    monkeypatch.setattr(routes_shared._sw, "workspace", ws)
    return ws, db_path


def _log_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT requester, auth_mode, scope_json, kind, query, target,"
        " granted_level, item_count, stripped_chunks FROM gateway_read_log"
        " ORDER BY log_id")]
    conn.close()
    return rows


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


# ═══════════ D-1 先红核心：memory 403 → 落 1 行 denied ═══════════

def test_d1_memory_403_logs_denied(env, monkeypatch):
    _ws, db_path = env
    monkeypatch.setattr(routes_memory, "NO_AUTH", False)
    before = len(_log_rows(db_path))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_memory.api_get_memories(
            agent_id="agent-a", kind="", current_agent="agent-b", principal=None))
    assert exc_info.value.status_code == 403
    rows = _log_rows(db_path)
    assert len(rows) == before + 1, f"403 拒绝应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-b"
    assert row["kind"] == "memory"
    assert row["target"] == "agent-a"
    assert row["granted_level"] == "denied"
    assert row["item_count"] == 0
    assert row["stripped_chunks"] == 0


# ═══════════ D-2 不存在资源 404 → 落 1 行 denied ═══════════

def test_d2_gateway_doc_404_logs_denied(env):
    _ws, db_path = env
    req = routes_gateway.GatewayReadRequest(kind="doc", doc_id="doc-不存在00000")
    before = len(_log_rows(db_path))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_gateway.api_gateway_read(
            req=req, current_agent="agent-a", principal=None))
    assert exc_info.value.status_code == 404
    rows = _log_rows(db_path)
    assert len(rows) == before + 1, f"404 拒绝应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-a"
    assert row["kind"] == "doc"
    assert row["target"] == "doc-不存在00000"
    assert row["granted_level"] == "denied"
    assert row["item_count"] == 0
    assert row["stripped_chunks"] == 0


# ═══════════ D-3 成功路径不回归：有权读取仍落 1 行、口径与改动前一致 ═══════════

def test_d3_success_path_unchanged(env):
    _ws, db_path = env
    _insert_memory(db_path, "m1", "agent-a", "k1", "星枢测试内容一")
    _insert_memory(db_path, "m2", "agent-a", "k2", "星枢测试内容二")
    before = len(_log_rows(db_path))
    result = asyncio.run(routes_memory.api_get_memories(
        agent_id="agent-a", kind="", current_agent="agent-a", principal=None))
    rows = _log_rows(db_path)
    assert len(rows) == before + 1, f"应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "agent-a"
    assert row["kind"] == "memory"
    assert row["query"] == ""
    assert row["target"] == "agent-a"
    assert row["granted_level"] == ""  # 与改动前逐字一致（M-1 同口径）
    assert row["stripped_chunks"] == 0
    assert row["item_count"] == len(result["memories"]) == 2


# ═══════════ D-4 凭据不明文：哨兵串不得出现在落行任何列 ═══════════

def test_d4_no_credential_plaintext(env, monkeypatch):
    _ws, db_path = env
    monkeypatch.setattr(routes_memory, "NO_AUTH", False)
    sentinel = "sk-CD059-哨兵凭据-7f3a9c-绝不可落盘"
    principal = SimpleNamespace(auth_mode="api_key", subject_id="agent-b",
                                scope={"dept": "ops"})
    principal.token = sentinel    # 凭据形态属性：_log_deny 不得读取落盘
    principal.api_key = sentinel
    before = len(_log_rows(db_path))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_memory.api_get_memories(
            agent_id="agent-a", kind="", current_agent="agent-b",
            principal=principal))
    assert exc_info.value.status_code == 403
    rows = _log_rows(db_path)
    assert len(rows) == before + 1, "403 拒绝应恰好多 1 行"
    row = rows[-1]
    assert row["auth_mode"] == "api_key", \
        f"auth_mode 只记类别，实际 {row['auth_mode']!r}"
    # 全列扫描（含未显式断言的列）：哨兵凭据串不得出现
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    full = [dict(r) for r in conn.execute("SELECT * FROM gateway_read_log")]
    conn.close()
    for r in full:
        for col, val in r.items():
            assert sentinel not in str(val), \
                f"凭据哨兵串落入列 {col}: {val!r}"


# ═══════════ D-5 400 不落盘：参数错误后行数不变 ═══════════

def test_d5_400_not_logged(env):
    _ws, db_path = env
    before = len(_log_rows(db_path))
    # kind 非法 → 400
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_gateway.api_gateway_read(
            req=routes_gateway.GatewayReadRequest(kind="bogus"),
            current_agent="agent-a", principal=None))
    assert exc_info.value.status_code == 400
    # kind=memory 但 query 空 → 400
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_gateway.api_gateway_read(
            req=routes_gateway.GatewayReadRequest(kind="memory", query="  "),
            current_agent="agent-a", principal=None))
    assert exc_info.value.status_code == 400
    assert len(_log_rows(db_path)) == before, "400 参数错误不应落读审计行"


# ═══════════ D-6 多端点覆盖：shared 403 与 knowledge 404 各落 1 行 denied ═══════════

def test_d6_shared_403_logs_denied(env):
    ws, db_path = env

    async def main():
        await ws.start()
        try:
            doc = await ws.create_doc("私密", "ag-a", visibility="private",
                                      allowed_agents=["ag-c"])
            before = len(_log_rows(db_path))
            with pytest.raises(HTTPException) as exc_info:
                await routes_shared.api_shared_get(
                    doc["doc_id"], current_agent="ag-b", principal=None)
            assert exc_info.value.status_code == 403
            rows = _log_rows(db_path)
            assert len(rows) == before + 1, \
                f"shared 403 应恰好多 1 行，实际 {len(rows) - before}"
            row = rows[-1]
            assert row["requester"] == "ag-b"
            assert row["kind"] == "shared"
            assert row["target"] == doc["doc_id"]
            assert row["granted_level"] == "denied"
            assert row["item_count"] == 0
            assert row["stripped_chunks"] == 0
        finally:
            await ws.stop()

    anyio.run(main)


def test_d6_knowledge_404_logs_denied(env):
    _ws, db_path = env
    before = len(_log_rows(db_path))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_knowledge.api_knowledge_get(
            "entry-不存在00000", current_agent="wkr-1", principal=None))
    assert exc_info.value.status_code == 404
    rows = _log_rows(db_path)
    assert len(rows) == before + 1, \
        f"knowledge 404 应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "wkr-1"
    assert row["kind"] == "knowledge"
    assert row["target"] == "entry-不存在00000"
    assert row["granted_level"] == "denied"
    assert row["item_count"] == 0
    assert row["stripped_chunks"] == 0
