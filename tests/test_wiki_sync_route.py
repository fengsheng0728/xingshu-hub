# -*- coding: utf-8 -*-
"""CD-042（2026-09-14）：/api/v1/wiki/sync 不再阻塞调用方。

背景：全量同步在 wiki 页数多时单次 20s+（实测 6429 页 23-24s），旧实现直接在 async handler
里同步调用 → 调用方（控制台按钮）必然无响应/超时。
现：默认路径走 `asyncio.to_thread`（返回体不变，向后兼容）；`background=1` 后台任务（单飞）
+ `/api/v1/wiki/sync/status` 轮询；控制台按钮用后台路径。
"""
import asyncio
import time

import pytest

import routes_common
import routes_wiki
import wiki_sync

_EMPTY = {"created": 0, "updated": 0, "skipped": 0, "errors": []}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    routes_wiki._wiki_sync_state.update({
        "running": False, "started_at": None, "last_finished_at": None,
        "last_result": None, "last_error": None})
    yield
    routes_wiki._wiki_sync_state.update({
        "running": False, "started_at": None, "last_finished_at": None,
        "last_result": None, "last_error": None})


def test_default_path_keeps_payload_and_uses_thread(monkeypatch):
    """默认（同步语义）路径：返回体与旧实现一致（向后兼容），且经 to_thread 执行。"""
    seen = {}

    def fake_sync(dry_run=False, force=False, federate=False):
        seen["args"] = (dry_run, force, federate)
        seen["thread"] = None
        import threading
        seen["thread"] = threading.current_thread().name
        return {"created": 2, "updated": 1, "skipped": 3, "errors": [], "inbox_new": 4}

    monkeypatch.setattr(wiki_sync, "sync", fake_sync)
    main_thread = __import__("threading").current_thread().name
    out = asyncio.run(routes_wiki.api_wiki_sync())
    assert out["status"] == "ok" and out["created"] == 2 and out["inbox_new"] == 4
    assert seen["args"] == (False, False, False)
    assert seen["thread"] != main_thread, "同步应在线程池执行，不占事件循环"


def test_background_returns_immediately(monkeypatch):
    """background=1：立即返回 started，状态置 running；结束后落 last_result。"""
    def slow_sync(dry_run=False, force=False, federate=False):
        time.sleep(0.05)
        return {"created": 1, "inbox_new": 7}

    monkeypatch.setattr(wiki_sync, "sync", slow_sync)

    async def _run():
        t0 = time.time()
        out = await routes_wiki.api_wiki_sync(background=True)
        dt = time.time() - t0
        mid_running = routes_wiki._wiki_sync_state["running"]
        await asyncio.sleep(0.5)
        return out, dt, mid_running

    out, dt, mid_running = asyncio.run(_run())
    assert out == {"status": "ok", "started": True, "already_running": False}
    assert dt < 0.05, "后台模式必须立即返回，实测 %.3fs" % dt
    assert mid_running is True, "触发后状态应为 running"
    st = routes_wiki._wiki_sync_state
    assert st["running"] is False and st["last_result"] == {"created": 1, "inbox_new": 7}
    assert st["last_finished_at"]


def test_background_single_flight(monkeypatch):
    """单飞：同步进行中重复触发 → already_running（不叠加第二次全量同步）。"""
    def slow_sync(dry_run=False, force=False, federate=False):
        time.sleep(0.25)
        return dict(_EMPTY)

    monkeypatch.setattr(wiki_sync, "sync", slow_sync)

    async def _run():
        first = await routes_wiki.api_wiki_sync(background=True)
        second = await routes_wiki.api_wiki_sync(background=True)
        await asyncio.sleep(0.6)
        return first, second

    first, second = asyncio.run(_run())
    assert first["started"] is True and first["already_running"] is False
    assert second["started"] is False and second["already_running"] is True


def test_background_failure_recorded(monkeypatch):
    """后台任务异常不得逃逸到事件循环：落 last_error，running 复位。"""
    def boom(dry_run=False, force=False, federate=False):
        raise RuntimeError("wiki boom")

    monkeypatch.setattr(wiki_sync, "sync", boom)

    async def _run():
        await routes_wiki.api_wiki_sync(background=True)
        await asyncio.sleep(0.4)

    asyncio.run(_run())
    st = routes_wiki._wiki_sync_state
    assert st["running"] is False
    assert "wiki boom" in (st["last_error"] or "")


def test_status_endpoint_shape():
    """状态端点：直接反映 _wiki_sync_state（供控制台轮询）。"""
    routes_wiki._wiki_sync_state.update({"running": True, "last_error": "e-x"})
    out = asyncio.run(routes_wiki.api_wiki_sync_status())
    assert out["status"] == "ok" and out["running"] is True and out["last_error"] == "e-x"
