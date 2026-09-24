# -*- coding: utf-8 -*-
"""CD-104：watch 通道 preview 按 watcher 逐人剥离（不再一份帧发给所有人）。

口径（验收方定死）：
- FULL / SUMMARY → 原文前 100 字（现状，零回归）
- METADATA       → preview="" + preview_stripped=true（元数据级不含正文）
- NONE           → 不推该 watcher（fail-closed 兜底）
- shared_presence（无 preview 键）→ ≥METADATA 一律原样发，不加 preview_stripped

脚手架：fake websocket（记录 send_text raw）+ fake workspace（get_doc_meta /
can_access 可控）+ 直调 _broadcast_shared_update 协程。不绑端口、不起 Hub。
"""
import json
import time
import types

import anyio
import pytest

import routes_shared
from hub_core import hub
from models import DisclosureLevel

DOC_ID = "doc-cd104"
PREVIEW = "P" * 100  # 模拟调用方 text[:100]


class FakeWS:
    """最小 websocket 替身：记录 send_text 收到的 raw 文本。"""

    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_text(self, msg):
        self.sent.append(msg)

    async def close(self, code=None, reason=""):
        self.closed.append((code, reason))


class FakeWorkspace:
    """get_doc_meta / can_access 可控的 workspace 替身。"""

    def __init__(self, meta, access=None):
        self._meta = meta
        self._access = access or {}

    async def get_doc_meta(self, doc_id):
        if self._meta and self._meta.get("doc_id") == doc_id:
            return self._meta
        return None

    async def can_access(self, doc_id, agent_id):
        return bool(self._access.get(agent_id, False))


def _plain(agent_id):
    return types.SimpleNamespace(
        subject_id=agent_id, auth_mode="api_key", scope=None, scoped_key_id="")


def _scoped(agent_id, scope):
    return types.SimpleNamespace(
        subject_id=agent_id, auth_mode="api_key", scope=scope, scoped_key_id="key-x")


def _team_meta():
    return {
        "doc_id": DOC_ID, "created_by": "ag-owner", "visibility": "team",
        "allowed_agents": [], "trust_level": "internal", "title": "CD-104",
    }


def _update_event():
    return {
        "type": "shared_update", "doc_id": DOC_ID,
        "agent_id": "ag-writer", "preview": PREVIEW,
    }


def _presence_event():
    return {
        "type": "shared_presence", "doc_id": DOC_ID,
        "agent_id": "ag-writer", "joined": True,
    }


def _reg(agent_id, ws, principal):
    """按任务书 3.2(a) 目标契约注册 3 元组 (websocket, joined_at, principal)。"""
    watchers = routes_shared._shared_watchers.setdefault(DOC_ID, {})
    watchers[agent_id] = (ws, time.time(), principal)


def _frames(ws):
    return [json.loads(s) for s in ws.sent]


@pytest.fixture()
def env(monkeypatch):
    routes_shared._shared_watchers.clear()
    monkeypatch.setattr(
        routes_shared._sw, "workspace", FakeWorkspace(_team_meta()))
    monkeypatch.setitem(hub.agents, "ag-owner", {"role": "worker"})
    monkeypatch.setitem(hub.agents, "ag-sum", {"role": "worker"})
    yield monkeypatch
    routes_shared._shared_watchers.clear()


# ① 核心判据：同一 doc 同一次广播，FULL 与 METADATA 两帧内容不同
def test_1_per_watcher_frames_differ_full_vs_metadata(env):
    monkeypatch = env

    async def main():
        ws_full, ws_meta = FakeWS(), FakeWS()
        # ag-owner = 创建者 → 规则1 FULL；ag-meta = level_cap metadata → METADATA
        _reg("ag-owner", ws_full, _plain("ag-owner"))
        _reg("ag-meta", ws_meta, _scoped("ag-meta", {"level_cap": "metadata", "ws": True}))
        await routes_shared._broadcast_shared_update(DOC_ID, _update_event())

        assert len(ws_full.sent) == 1 and len(ws_meta.sent) == 1
        f_full, f_meta = _frames(ws_full)[0], _frames(ws_meta)[0]
        # 两帧必须不同（若偷懒统一发一份，此断言必红）
        assert ws_full.sent[0] != ws_meta.sent[0], \
            "逐人剥离未生效：FULL 与 METADATA 收到同一份 msg"
        assert f_full["preview"] == PREVIEW
        assert "preview_stripped" not in f_full
        assert f_meta["preview"] == ""
        assert f_meta["preview_stripped"] is True
        # 其余字段保持不变
        for f in (f_full, f_meta):
            assert f["type"] == "shared_update"
            assert f["doc_id"] == DOC_ID
            assert f["agent_id"] == "ag-writer"
            assert set(f["online"]) == {"ag-owner", "ag-meta"}

    anyio.run(main)


# ② SUMMARY 与 FULL 同口径：非空 preview（100 字）
def test_2_summary_gets_nonempty_preview(env):
    monkeypatch = env

    async def main():
        ws_sum, ws_full = FakeWS(), FakeWS()
        _reg("ag-sum", ws_sum, _scoped("ag-sum", {"level_cap": "summary", "ws": True}))
        _reg("ag-owner", ws_full, _plain("ag-owner"))
        await routes_shared._broadcast_shared_update(DOC_ID, _update_event())

        f_sum = _frames(ws_sum)[0]
        f_full = _frames(ws_full)[0]
        assert f_sum["preview"] == PREVIEW
        assert len(f_sum["preview"]) == 100
        assert "preview_stripped" not in f_sum
        assert f_sum["preview"] == f_full["preview"]  # 同口径

    anyio.run(main)


# ③ NONE 主体不推（fail-closed 兜底）
def test_3_none_watcher_not_pushed(env):
    monkeypatch = env

    async def main():
        ws_none, ws_full = FakeWS(), FakeWS()
        _reg("ag-none", ws_none, _scoped("ag-none", {"level_cap": "none", "ws": True}))
        _reg("ag-owner", ws_full, _plain("ag-owner"))
        await routes_shared._broadcast_shared_update(DOC_ID, _update_event())

        assert ws_none.sent == [], "NONE 级 watcher 不该收到任何帧"
        assert len(ws_full.sent) == 1  # 其余正常 watcher 照常

    anyio.run(main)


# ④ 判定抛异常 → fail-closed 跳过该 watcher，其余照常
def test_4_level_exception_skips_watcher(env):
    monkeypatch = env

    async def main():
        ws_bad, ws_full = FakeWS(), FakeWS()
        _reg("ag-bad", ws_bad, _plain("ag-bad"))
        _reg("ag-owner", ws_full, _plain("ag-owner"))

        orig = routes_shared._shared_doc_ws_level

        async def _boom(doc_id, agent_id, principal):
            if agent_id == "ag-bad":
                raise RuntimeError("judge boom")
            return await orig(doc_id, agent_id, principal)

        monkeypatch.setattr(routes_shared, "_shared_doc_ws_level", _boom)
        await routes_shared._broadcast_shared_update(DOC_ID, _update_event())

        assert ws_bad.sent == [], "判定抛异常的 watcher 不该收到帧"
        assert len(ws_full.sent) == 1
        assert _frames(ws_full)[0]["preview"] == PREVIEW

    anyio.run(main)


# ⑤ presence 帧不受剥离影响：≥METADATA 均收到，且不含 preview_stripped
def test_5_presence_unaffected_by_stripping(env):
    monkeypatch = env

    async def main():
        ws_full, ws_sum, ws_meta, ws_none = FakeWS(), FakeWS(), FakeWS(), FakeWS()
        _reg("ag-owner", ws_full, _plain("ag-owner"))
        _reg("ag-sum", ws_sum, _scoped("ag-sum", {"level_cap": "summary", "ws": True}))
        _reg("ag-meta", ws_meta, _scoped("ag-meta", {"level_cap": "metadata", "ws": True}))
        _reg("ag-none", ws_none, _scoped("ag-none", {"level_cap": "none", "ws": True}))
        await routes_shared._broadcast_shared_update(DOC_ID, _presence_event())

        assert len(ws_full.sent) == 1
        assert len(ws_sum.sent) == 1
        assert len(ws_meta.sent) == 1
        assert ws_none.sent == []
        for ws in (ws_full, ws_sum, ws_meta):
            f = _frames(ws)[0]
            assert f["type"] == "shared_presence"
            assert "preview" not in f
            assert "preview_stripped" not in f, "presence 帧不得附带 preview_stripped"
            assert f["agent_id"] == "ag-writer"

    anyio.run(main)


# ⑥ 元组结构回归：3 元组注册后 _close_shared_watchers 仍正确关闭并返回计数
def test_6_close_shared_watchers_with_3tuple(env):
    monkeypatch = env

    async def main():
        ws_a, ws_b = FakeWS(), FakeWS()
        _reg("ag-owner", ws_a, _plain("ag-owner"))
        _reg("ag-meta", ws_b, _scoped("ag-meta", {"level_cap": "metadata", "ws": True}))
        n = await routes_shared._close_shared_watchers(DOC_ID, reason="test close")

        assert n == 2
        for ws in (ws_a, ws_b):
            assert ws.closed, "watcher 应被 close"
            assert ws.closed[0][0] == 4404
            raw = json.loads(ws.sent[0])
            assert raw["type"] == "shared_archived"
            assert raw["reason"] == "test close"
        assert DOC_ID not in routes_shared._shared_watchers or \
            not routes_shared._shared_watchers.get(DOC_ID)

    anyio.run(main)
