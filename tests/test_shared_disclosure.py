# -*- coding: utf-8 -*-
"""CD-094（2026-09-23，方案①已拍板）：共享文档纳入披露判定——读出口按主体级别剥离。

实测漏洞：scoped key `level_cap="metadata"` 只要端点白名单放行 /shared，
读 team 文档即得 200 + 全文（level_cap 形同虚设）。

级别映射口径（与 disclosure 既有机制一致）：
- metadata → 只返回元数据（doc_id/title/created_by/updated_at/block_count 等），无 content 键
- summary  → 正文前 200 字 + truncated 标记
- full     → 全文（响应形状与修复前逐字一致，S-2 审计断言不回归）
- none     → 403（与 T17「不存在/无权同 403」同 detail）

覆盖：
- D-1 scoped key level_cap=metadata（key 绑定创建者本人）→ 无正文，仅元数据
- D-2 scoped key level_cap=summary → 截断 + truncated
- D-3 scoped key level_cap=full → 全文（不受影响）
- D-4 普通 api_key 创建者（principal 无 scope）→ 全文（规则1，不回归）
- D-5 principal=None（NO_AUTH 开发态，无身份语义）→ 全文（不回归）
- D-6 文档密级：trust_level=external → 创建者也被封到 metadata（min(主体, 文档密级)）
- D-7 list：level_cap=none → 空列表；metadata → 列表可见（列表本就是元数据）

脚手架同 tests/test_shared_read_audit.py：临时库 + 临时 workspace 单例 +
直调 handler 协程（principal 直接作参数传入）。
"""
import anyio
import types

import pytest
from fastapi import HTTPException

import db
import routes_shared
from models import CONFIG
from shared_workspace import SharedWorkspace

SECRET = "营收密码-TopSecret-78245"  # 敏感标记：剥离后绝不允许出现在响应里


def _principal(scope):
    """构造 scoped key 主体（Principal 的最小替身：_log_read 只消费 .scope/.auth_mode）"""
    return types.SimpleNamespace(
        subject_id="ag-a", auth_mode="api_key", scope=scope, scoped_key_id="key-x")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "shared_disclosure.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db.init_db()
    ws = SharedWorkspace(db_path, store_dir=str(tmp_path / "store"))
    monkeypatch.setattr(routes_shared._sw, "workspace", ws)
    return ws, db_path


def _run(coro):
    return anyio.run(lambda: coro)


async def _mk_doc(ws, trust_level="internal"):
    doc = await ws.create_doc("营收机密文档", "ag-a", trust_level=trust_level)
    await ws.append_block(doc["doc_id"], SECRET + " 全文正文" * 30, "ag-a")
    return doc["doc_id"]


# D-1：scoped key level_cap=metadata → 响应无 content 键，只有元数据
def test_d1_scoped_key_metadata_cap_strips_body(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws)
            result = await routes_shared.api_shared_get(
                doc_id, current_agent="ag-a",
                principal=_principal({"level_cap": "metadata"}))
            assert "content" not in result, f"metadata 级不得返回正文，实际键: {sorted(result)}"
            assert result.get("disclosure_level") == "metadata"
            # 元数据字段齐全
            for k in ("doc_id", "title", "created_by", "updated_at", "block_count"):
                assert k in result, f"metadata 级缺元数据字段 {k}"
        finally:
            await ws.stop()

    anyio.run(main)


# D-2：scoped key level_cap=summary → 正文截断 + truncated 标记
def test_d2_scoped_key_summary_cap_truncates(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws)
            full = await ws.get_doc_content(doc_id)
            assert len(full) > 200, "前置：正文须超过 200 字才能验证截断"
            result = await routes_shared.api_shared_get(
                doc_id, current_agent="ag-a",
                principal=_principal({"level_cap": "summary"}))
            assert result.get("disclosure_level") == "summary"
            assert result.get("truncated") is True
            assert len(result["content"]) <= 200
            assert result["content"] == full[:200]
        finally:
            await ws.stop()

    anyio.run(main)


# D-3：scoped key level_cap=full → 全文，响应形状与修复前逐字一致
def test_d3_scoped_key_full_cap_unaffected(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws)
            full = await ws.get_doc_content(doc_id)
            result = await routes_shared.api_shared_get(
                doc_id, current_agent="ag-a",
                principal=_principal({"level_cap": "full"}))
            assert result == {"doc_id": doc_id, "content": full}
            assert SECRET in result["content"]
        finally:
            await ws.stop()

    anyio.run(main)


# D-4：普通 api_key（principal.scope 为 None）创建者读 → 全文不回归
def test_d4_plain_api_key_owner_full(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws)
            full = await ws.get_doc_content(doc_id)
            plain = types.SimpleNamespace(
                subject_id="ag-a", auth_mode="api_key", scope=None, scoped_key_id="")
            result = await routes_shared.api_shared_get(
                doc_id, current_agent="ag-a", principal=plain)
            assert result == {"doc_id": doc_id, "content": full}
        finally:
            await ws.stop()

    anyio.run(main)


# D-5：principal=None（NO_AUTH 开发态，无身份语义）→ 全文不回归
def test_d5_no_auth_principal_full(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws)
            full = await ws.get_doc_content(doc_id)
            result = await routes_shared.api_shared_get(
                doc_id, current_agent="ag-a", principal=None)
            assert result == {"doc_id": doc_id, "content": full}
        finally:
            await ws.stop()

    anyio.run(main)


# D-6：文档密级（trust_level=external）封顶——创建者本人也只得 metadata（min 语义）
def test_d6_doc_trust_level_caps_creator(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws, trust_level="external")
            result = await routes_shared.api_shared_get(
                doc_id, current_agent="ag-a", principal=None)
            assert "content" not in result, "external 密级文档对创建者同样只给元数据"
            assert result.get("disclosure_level") == "metadata"
        finally:
            await ws.stop()

    anyio.run(main)


# D-7：list——level_cap=none → 空列表；level_cap=metadata → 列表可见（本就是元数据）
def test_d7_list_level_cap(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            await _mk_doc(ws)
            r_none = await routes_shared.api_shared_list(
                current_agent="ag-a", principal=_principal({"level_cap": "none"}))
            assert r_none == {"docs": []}, r_none
            r_meta = await routes_shared.api_shared_list(
                current_agent="ag-a", principal=_principal({"level_cap": "metadata"}))
            assert len(r_meta["docs"]) == 1
        finally:
            await ws.stop()

    anyio.run(main)


# D-8：scoped key level_cap=none 读文档 → 403（与无权同 detail，T17 冻结口径）
def test_d8_scoped_key_none_cap_forbidden(env):
    ws, _db_path = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws)
            with pytest.raises(HTTPException) as exc_info:
                await routes_shared.api_shared_get(
                    doc_id, current_agent="ag-a",
                    principal=_principal({"level_cap": "none"}))
            assert exc_info.value.status_code == 403
        finally:
            await ws.stop()

    anyio.run(main)
