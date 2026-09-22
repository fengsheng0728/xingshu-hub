# -*- coding: utf-8 -*-
"""T9 CD-054（shared 组）：共享文档读端点补读审计（2026-09-19）

GET /api/v1/shared/docs 与 GET /api/v1/shared/docs/{doc_id} 语义不变，
只在成功返回前把「谁读了哪份文档」落 gateway_read_log（复用 routes_gateway._log_read）。

覆盖：
- S-1 list 落审计：恰好多 1 行，字段核对（requester/kind/granted_level/item_count）
- S-2 get 落审计 + 响应不变：target=doc_id、granted_level='full'、item_count=1，
  返回 content 与 workspace 直取逐字一致
- S-3 审计写入失败不阻塞读取（D4）：端点仍 200 返回、不落行
- S-4 403 路径落 denied 行（语义随 CD-059 变更；private 文档 + 非白名单 agent）

脚手架：审计落真实 gateway_read_log 表——临时库走 db.init_db() 建完整 schema
（先 monkeypatch CONFIG.DB_PATH 再 init_db）；workspace 用
SharedWorkspace(db_path, store_dir=...) 临时实例并 monkeypatch
routes_shared._sw.workspace；直调 handler 协程（Depends 直调不生效，
agent id / principal 直接作参数传入），不起 TestClient、不绑端口。
"""
import anyio
import sqlite3

import pytest
from fastapi import HTTPException

import db
import routes_shared
from models import CONFIG
from shared_workspace import SharedWorkspace


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（完整 schema）+ 临时 workspace 单例"""
    db_path = str(tmp_path / "shared_audit.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db.init_db()  # 完整 schema，含 gateway_read_log（db.py:731）
    ws = SharedWorkspace(db_path, store_dir=str(tmp_path / "store"))
    monkeypatch.setattr(routes_shared._sw, "workspace", ws)
    return ws, db_path


def _readlog_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT requester, auth_mode, scope_json, kind, query, target,"
        " granted_level, item_count, stripped_chunks FROM gateway_read_log"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# S-1 list 落审计：成功返回 → gateway_read_log 恰好多 1 行，字段逐一核对
def test_s1_list_logs_read_audit(env):
    ws, db_path = env

    async def main():
        await ws.start()
        try:
            await ws.create_doc("文档A", "ag-a")
            await ws.create_doc("文档B", "ag-a")
            before = len(_readlog_rows(db_path))
            result = await routes_shared.api_shared_list(current_agent="ag-a", principal=None)
            after = _readlog_rows(db_path)
            assert len(after) - before == 1, f"审计行数应恰好多 1，实际 {len(after) - before}"
            row = after[-1]
            assert row["requester"] == "ag-a"
            assert row["kind"] == "shared"
            assert row["query"] == ""
            assert row["target"] == ""
            assert row["granted_level"] == "metadata"
            assert row["item_count"] == len(result["docs"]) == 2
            assert row["stripped_chunks"] == 0
        finally:
            await ws.stop()

    anyio.run(main)


# S-2 get 落审计 + 响应不变：owner 读自己的文档 → 恰好多 1 行，content 逐字一致
def test_s2_get_logs_read_audit_response_unchanged(env):
    ws, db_path = env

    async def main():
        await ws.start()
        try:
            doc = await ws.create_doc("私有文档", "ag-a", visibility="private")
            content_expected = await ws.get_doc_content(doc["doc_id"])
            before = len(_readlog_rows(db_path))
            result = await routes_shared.api_shared_get(
                doc["doc_id"], current_agent="ag-a", principal=None)
            after = _readlog_rows(db_path)
            assert result == {"doc_id": doc["doc_id"], "content": content_expected}
            assert len(after) - before == 1, f"审计行数应恰好多 1，实际 {len(after) - before}"
            row = after[-1]
            assert row["requester"] == "ag-a"
            assert row["kind"] == "shared"
            assert row["query"] == ""
            assert row["target"] == doc["doc_id"]
            assert row["granted_level"] == "full"
            assert row["item_count"] == 1
            assert row["stripped_chunks"] == 0
        finally:
            await ws.stop()

    anyio.run(main)


# S-3 审计失败不阻塞读取（D4 可用性优先）：审计写入抛错 → 端点仍正常返回
def test_s3_audit_failure_does_not_block_read(env, monkeypatch, tmp_path):
    ws, db_path = env

    async def main():
        await ws.start()
        try:
            doc = await ws.create_doc("容错文档", "ag-a")
            content_expected = await ws.get_doc_content(doc["doc_id"])
            # 让审计写入抛错：CONFIG.DB_PATH 指向不存在目录 → _log_read 内
            # sqlite3.connect 失败，被其内部 except 吞掉（D4 语义）
            monkeypatch.setattr(CONFIG, "DB_PATH",
                                str(tmp_path / "no_such_dir" / "x.db"))
            result = await routes_shared.api_shared_get(
                doc["doc_id"], current_agent="ag-a", principal=None)
            assert result == {"doc_id": doc["doc_id"], "content": content_expected}
        finally:
            await ws.stop()

    anyio.run(main)
    assert _readlog_rows(db_path) == []  # 审计未落行，但读取未被阻塞


# S-4 403 路径落 denied 行（语义随 CD-059 变更：403 拒绝必须落 denied 行；2026-09-20 用户追认，不回滚）：private 文档 + 非白名单 agent → 403 + 恰好多 1 行 denied
def test_s4_forbidden_read_logs_denied(env):
    ws, db_path = env

    async def main():
        await ws.start()
        try:
            doc = await ws.create_doc("私密", "ag-a", visibility="private",
                                      allowed_agents=["ag-c"])
            before = len(_readlog_rows(db_path))
            with pytest.raises(HTTPException) as exc_info:
                await routes_shared.api_shared_get(
                    doc["doc_id"], current_agent="ag-b", principal=None)
            assert exc_info.value.status_code == 403
            rows = _readlog_rows(db_path)
            assert len(rows) == before + 1, \
                f"403 拒绝必须恰好多 1 行 denied，实际 {len(rows) - before}"
            assert rows[-1]["granted_level"] == "denied"
            assert rows[-1]["kind"] == "shared"
            assert rows[-1]["target"] == doc["doc_id"]
            assert rows[-1]["item_count"] == 0
        finally:
            await ws.stop()

    anyio.run(main)
