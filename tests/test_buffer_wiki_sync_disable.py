# -*- coding: utf-8 -*-
"""终审断点 2：SYNC_HUB_DISABLE_WIKI_SYNC=1 禁用分支 UnboundLocalError 回归。

根因：buffer.py _wiki_sync_throttler 禁用分支不给 result 赋值，
随后通知段 `result.get("inbox_new", 0)` 抛 UnboundLocalError，
被外层 except 吞成每周期一条 "Wiki sync failed" 假告警。
修复：禁用分支补 `result = {"created":0,"updated":0,"skipped":0,"inbox_new":0}`。

口径：直调 throttler 一个节流周期（不起 Hub、不 import 真 wiki_sync——
sys.modules 注入假模块，避免 sklearn 重依赖）。
"""
import asyncio
import logging
import os
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hub_mixins.buffer import BufferMixin  # noqa: E402


class _Stub(BufferMixin):
    """最小 BufferMixin 宿主：只带 throttler 一个周期所需的属性。"""

    def __init__(self):
        self._running = True
        self._wiki_sync_pending = True
        self._last_wiki_sync = 0.0
        self._WIKI_SYNC_COOLDOWN = 0
        self._write_trace = []
        self.notifications = []

    async def create_notification(self, to_agent, kind, title, body="", source=""):
        self.notifications.append((to_agent, kind, title, body, source))


def _fake_wiki_sync(monkeypatch, result, calls):
    mod = types.ModuleType("wiki_sync")

    def _sync(force=False):
        calls.append(force)
        return result

    mod.sync = _sync
    monkeypatch.setitem(sys.modules, "wiki_sync", mod)


async def _run_cycles(stub, seconds=1.4):
    """跑 throttler：至少覆盖一个完整节流周期（内部 sleep 1.0s）。"""
    task = asyncio.create_task(stub._wiki_sync_throttler())
    await asyncio.sleep(seconds)
    stub._running = False
    try:
        await asyncio.wait_for(task, timeout=2.5)
    except asyncio.TimeoutError:
        task.cancel()
        raise AssertionError("throttler 未在预期内退出")


def test_disabled_flag_cycle_clean(monkeypatch, caplog):
    """禁用标志下一个节流周期：无异常、无 'Wiki sync failed' 假告警、不调 sync。"""
    monkeypatch.setenv("SYNC_HUB_DISABLE_WIKI_SYNC", "1")
    calls = []
    _fake_wiki_sync(monkeypatch, {"created": 9, "updated": 9, "skipped": 9,
                                  "inbox_new": 9}, calls)
    stub = _Stub()
    with caplog.at_level(logging.DEBUG, logger="xingshu.buffer"):
        asyncio.run(_run_cycles(stub))
    assert calls == [], "禁用分支不应调用 wiki_sync.sync"
    assert stub.notifications == [], "禁用分支不应触发收件箱通知"
    failed = [r for r in caplog.records
              if r.levelno >= logging.WARNING and "Wiki sync failed" in r.getMessage()]
    assert not failed, f"出现假告警: {[r.getMessage() for r in failed]}"


def test_enabled_flag_cycle_still_works(monkeypatch, caplog):
    """对照：未设禁用标志时 sync 正常调用，inbox_new>0 通知链路不断。"""
    monkeypatch.delenv("SYNC_HUB_DISABLE_WIKI_SYNC", raising=False)
    calls = []
    _fake_wiki_sync(monkeypatch, {"created": 1, "updated": 0, "skipped": 2,
                                  "inbox_new": 3}, calls)
    stub = _Stub()
    with caplog.at_level(logging.DEBUG, logger="xingshu.buffer"):
        asyncio.run(_run_cycles(stub))
    assert calls, "启用路径应调用 wiki_sync.sync"
    assert any(n[2] == "3 条 Wiki 新页面待审查" for n in stub.notifications)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
