# -*- coding: utf-8 -*-
"""CD-094 终审补丁（2026-09-23）：WS 共享文档通道接入披露判定——进房/监听级别门。

实测漏洞：CD-094 只封了 REST 读出口（api_shared_get），WS 侧是洞——
/ws/shared/{doc_id} 进房只查 is_archived，任何有效 api_key 的 worker 连上
private 文档房间即可经 CRDT 同步拿全文；/ws/shared/watch/{doc_id} 同样无校验。

级别矩阵（与 routes_ws.py 头部注释同一口径）：
- CRDT 房（全文读写）：仅 FULL 放行；NONE/METADATA/SUMMARY → close 4403
- watch（preview ≤ 100 字）：NONE → close 4403；METADATA/SUMMARY/FULL 放行
- private 文档非 allowed_agents 成员：可见性硬门恒 NONE（同 REST can_access）→ 两通道一律拒
- hub_token 运维主体不判定直接放行（与 REST 读出口旁路同源）

覆盖：
- W-1 worker 连 private 文档 CRDT 房 → 4403，不进房
- W-2 worker 连 private 文档 watch → 4403，不进 watchers
- W-3 metadata cap scoped key 连 team 文档：CRDT 房 4403、watch 放行
- W-4 full 主体（创建者）进房不受影响（serve_websocket 被调用）
- W-5 级别门直测：hub_token 旁路 / 不存在 doc fail-closed NONE / summary cap 也拒进房

脚手架同 tests/test_shared_disclosure.py：临时库 + 临时 workspace 单例 +
直调 handler 协程（_ws_auth_accept_full patch 掉，principal 直接注入——
首帧鉴权本身由 test_l6_ws_auth_regression / test_ws_auth_matrix 覆盖）。
"""
import types
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

import db
import routes_shared
import routes_ws
from hub_core import hub
from models import CONFIG, DisclosureLevel
from shared_workspace import SharedWorkspace

SECRET = "营收密码-TopSecret-78245"  # 敏感标记：被拒主体绝不允许经 CRDT/watch 拿到


def _plain(agent_id):
    """普通 api_key 主体（无 scope）"""
    return types.SimpleNamespace(
        subject_id=agent_id, auth_mode="api_key", scope=None, scoped_key_id="")


def _scoped(agent_id, scope):
    """scoped key 主体（CD-094 的 level_cap 在 WS 门同样生效）"""
    return types.SimpleNamespace(
        subject_id=agent_id, auth_mode="api_key", scope=scope, scoped_key_id="key-x")


def _hub_token():
    return types.SimpleNamespace(
        subject_id="", auth_mode="hub_token", scope=None, scoped_key_id="")


def _fake_ws():
    """最小 websocket 替身：记录 close 码；receive_text 抬异常即退 watch 循环"""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_text = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=Exception("bye"))
    ws.query_params = MagicMock()
    ws.query_params.get = lambda k, d="": d
    return ws


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "shared_ws_disclosure.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db.init_db()
    ws = SharedWorkspace(db_path, store_dir=str(tmp_path / "store"))
    monkeypatch.setattr(routes_shared._sw, "workspace", ws)
    return ws, monkeypatch


def _patch_auth(monkeypatch, agent_id, principal):
    monkeypatch.setattr(
        routes_ws, "_ws_auth_accept_full",
        AsyncMock(return_value=(agent_id, principal)))


async def _mk_doc(ws, visibility="team", allowed_agents=None, creator="ag-owner"):
    doc = await ws.create_doc("营收机密文档", creator, visibility=visibility,
                              allowed_agents=allowed_agents or [])
    await ws.append_block(doc["doc_id"], SECRET + " 全文正文" * 30, creator)
    return doc["doc_id"]


# W-1：worker（普通 api_key）连 private 文档 CRDT 房 → close 4403，serve_websocket 未被调用
def test_w1_worker_private_room_rejected(env):
    ws, monkeypatch = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws, visibility="private")
            serve = AsyncMock()
            monkeypatch.setattr(ws, "serve_websocket", serve)
            _patch_auth(monkeypatch, "ag-x", _plain("ag-x"))
            fake = _fake_ws()
            await routes_ws.ws_shared(fake, doc_id)
            serve.assert_not_awaited()
            fake.close.assert_awaited_once()
            assert fake.close.await_args.kwargs.get("code") == 4403
        finally:
            await ws.stop()

    anyio.run(main)


# W-2：worker 连 private 文档 watch → close 4403，不注册进 watchers
def test_w2_worker_private_watch_rejected(env):
    ws, monkeypatch = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws, visibility="private")
            _patch_auth(monkeypatch, "ag-x", _plain("ag-x"))
            fake = _fake_ws()
            await routes_ws.ws_shared_watch(fake, doc_id)
            fake.close.assert_awaited_once()
            assert fake.close.await_args.kwargs.get("code") == 4403
            assert "ag-x" not in routes_ws._shared_watchers.get(doc_id, {})
            fake.send_text.assert_not_awaited()  # 一帧 preview/presence 都不该收到
        finally:
            await ws.stop()

    anyio.run(main)


# W-3：metadata cap scoped key 连 team 文档——CRDT 房 4403 拒绝、watch 放行
def test_w3_metadata_cap_team_room_rejected_watch_allowed(env):
    ws, monkeypatch = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws, visibility="team")
            principal = _scoped("ag-ext", {"level_cap": "metadata", "ws": True})
            _patch_auth(monkeypatch, "ag-ext", principal)

            # CRDT 房：协同 = 全文读写，metadata 级不该进 → 4403
            serve = AsyncMock()
            monkeypatch.setattr(ws, "serve_websocket", serve)
            fake = _fake_ws()
            await routes_ws.ws_shared(fake, doc_id)
            serve.assert_not_awaited()
            fake.close.assert_awaited_once()
            assert fake.close.await_args.kwargs.get("code") == 4403

            # watch：preview 100 字符合 metadata 级口径 → 放行（收到 presence 帧）
            fake2 = _fake_ws()
            await routes_ws.ws_shared_watch(fake2, doc_id)
            assert not any(
                c.kwargs.get("code") == 4403 for c in fake2.close.await_args_list)
            sent = [str(c.args[0]) for c in fake2.send_text.await_args_list if c.args]
            assert any("shared_presence" in s for s in sent), \
                "watch 放行后应收到 presence 帧，实际: %r" % (sent,)
        finally:
            await ws.stop()

    anyio.run(main)


# W-4：full 主体（创建者，普通 api_key 无 scope）进 CRDT 房不受影响
def test_w4_full_subject_room_unaffected(env):
    ws, monkeypatch = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws, visibility="team")
            _patch_auth(monkeypatch, "ag-owner", _plain("ag-owner"))
            serve = AsyncMock()
            monkeypatch.setattr(ws, "serve_websocket", serve)
            fake = _fake_ws()
            await routes_ws.ws_shared(fake, doc_id)
            serve.assert_awaited_once()  # FULL → 正常进房
            fake.close.assert_not_awaited()
        finally:
            await ws.stop()

    anyio.run(main)


# W-5：级别门直测——hub_token 旁路 / 不存在 doc fail-closed NONE / summary cap 拒进房但放行 watch
def test_w5_level_gate_matrix(env):
    ws, monkeypatch = env

    async def main():
        await ws.start()
        try:
            doc_id = await _mk_doc(ws, visibility="team")
            # hub_token 运维主体：不判定直接 FULL（不存在的 doc 也 FULL，
            # 保持 tests/test_ws_auth_matrix.py「不存在」语义不变）
            assert await routes_ws._shared_doc_ws_level(
                doc_id, "", _hub_token()) == DisclosureLevel.FULL
            assert await routes_ws._shared_doc_ws_level(
                "doc-not-exist", "", _hub_token()) == DisclosureLevel.FULL
            # 不存在的 doc + api_key 主体 → fail-closed NONE
            assert await routes_ws._shared_doc_ws_level(
                "doc-not-exist", "ag-x", _plain("ag-x")) == DisclosureLevel.NONE
            # summary cap scoped key：team 文档判 SUMMARY —— 房拒（4403）、watch 放
            # （规则 4.5：完全未知主体兜底 METADATA，到不了 SUMMARY；须登记为
            # worker 角色才能走规则 7 链尾的 published 提升 NONE→SUMMARY）
            monkeypatch.setitem(hub.agents, "ag-sum", {"role": "worker"})
            monkeypatch.setitem(hub.agents, "ag-owner", {"role": "worker"})
            principal = _scoped("ag-sum", {"level_cap": "summary", "ws": True})
            level = await routes_ws._shared_doc_ws_level(doc_id, "ag-sum", principal)
            assert level == DisclosureLevel.SUMMARY
            fake = _fake_ws()
            assert await routes_ws._shared_ws_gate(
                fake, doc_id, "ag-sum", principal, room=True) is False
            assert fake.close.await_args.kwargs.get("code") == 4403
            fake2 = _fake_ws()
            assert await routes_ws._shared_ws_gate(
                fake2, doc_id, "ag-sum", principal, room=False) is True
            fake2.close.assert_not_awaited()
        finally:
            await ws.stop()

    anyio.run(main)
