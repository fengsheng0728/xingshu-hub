# -*- coding: utf-8 -*-
"""tests/test_cd116_loop_request_id.py — CD-116：4 处未过 _bg_task 的长跑循环补 tick 级 request id

覆盖：
1. _trace_persist_worker / _write_buffer_worker / _wiki_sync_throttler / _sweeper_loop
   跑 ≥2 轮 → 每轮 tick 轮换 request id（≥2 个不同值 + 正确前缀）
2. _wiki_sync_throttler 未触发同步时也轮换 id
3. 不改控制流：_running / _stopping 置位后循环在有限轮内退出
4. 无回归：CD-108 与 CD-116 两套 tick 机制不互斥
5. 结构断言：inspect.getsource 确认 4 个函数体内有 rotate_request_id( 调用

注入方式（避免 while 真循环挂死）：monkeypatch 队列 get / 循环内 asyncio.sleep，
第 N 轮把 self._running（或 _stopping）置 False，让循环自退。
"""
import asyncio
import inspect
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logfmt import get_request_id, set_request_id

N_TICKS = 2


class _Sentinel(Exception):
    pass


def _collect_and_stop(rids, stub, flag_name, n=N_TICKS, stop_value=False):
    """记录当前 request id；满 n 轮后置退出标志（_running 用 False，_stopping 用 True）。"""
    rids.append(get_request_id())
    if len(rids) >= n:
        setattr(stub, flag_name, stop_value)


def _assert_ticks(rids, prefix):
    """两侧可区分：有 tick → ≥2 个不同值 + 前缀；没 tick → 全同（断言失败）。"""
    assert len(rids) >= N_TICKS, f"应收集到 ≥{N_TICKS} 轮，实际 {len(rids)}"
    uniq = set(rids)
    assert len(uniq) >= N_TICKS, (
        f"request id 应 ≥{N_TICKS} 个不同值（有 tick 才会轮换），实际全同: {rids}"
    )
    for r in rids:
        assert r.startswith(prefix), f"id 应带 {prefix} 前缀: {r}"


# ============ 1-2. _trace_persist_worker / _write_buffer_worker ============

class _QueueStub:
    """假队列：get() 记录当前 request id 后抛 TimeoutError（驱动 continue 路径）。"""

    def __init__(self, rids, stub, flag_name, n=N_TICKS):
        self._rids = rids
        self._stub = stub
        self._flag = flag_name
        self._n = n

    async def get(self):
        _collect_and_stop(self._rids, self._stub, self._flag, self._n)
        raise asyncio.TimeoutError()

    def get_nowait(self):
        raise asyncio.QueueEmpty()

    def task_done(self):
        pass


def test_trace_persist_worker_rotates_each_tick(monkeypatch):
    from hub_mixins.buffer import BufferMixin

    rids = []
    stub = SimpleNamespace(_running=True)
    stub._trace_persist_queue = _QueueStub(rids, stub, "_running")

    async def _run():
        set_request_id("INIT-NO-TICK")
        try:
            await BufferMixin._trace_persist_worker(stub)
        finally:
            set_request_id("")

    asyncio.run(_run())
    _assert_ticks(rids, "trace-persist-")


def test_write_buffer_worker_rotates_each_tick(monkeypatch):
    from hub_mixins.buffer import BufferMixin

    rids = []
    stub = SimpleNamespace(_running=True)
    stub._write_queue = _QueueStub(rids, stub, "_running")

    async def _run():
        set_request_id("INIT-NO-TICK")
        try:
            await BufferMixin._write_buffer_worker(stub)
        finally:
            set_request_id("")

    asyncio.run(_run())
    _assert_ticks(rids, "write-buffer-")


# ============ 3. _wiki_sync_throttler（含「未触发同步也轮换」） ============

def test_wiki_sync_throttler_rotates_even_without_sync(monkeypatch):
    from hub_mixins.buffer import BufferMixin

    rids = []
    sync_calls = []
    stub = SimpleNamespace(
        _running=True,
        _wiki_sync_pending=False,
        _last_wiki_sync=0.0,
        _WIKI_SYNC_COOLDOWN=0,
        _write_trace=[],
    )

    async def fake_sleep(sec):
        _collect_and_stop(rids, stub, "_running")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setitem(sys.modules, "wiki_sync",
                        SimpleNamespace(sync=lambda *a, **k: sync_calls.append(1)))

    async def _run():
        set_request_id("INIT-NO-TICK")
        try:
            await BufferMixin._wiki_sync_throttler(stub)
        finally:
            set_request_id("")

    asyncio.run(_run())
    _assert_ticks(rids, "wiki-sync-")
    assert sync_calls == [], "本轮未触发同步（_wiki_sync_pending=False），但仍应轮换 id"


# ============ 4. _sweeper_loop ============

def test_sweeper_loop_rotates_each_tick(monkeypatch):
    from models import CONFIG
    from shared_workspace import SharedWorkspace

    rids = []
    stub = SimpleNamespace(_stopping=False)
    sweep_calls = []

    async def fake_sweep():
        sweep_calls.append(1)

    async def fake_sleep(sec):
        _collect_and_stop(rids, stub, "_stopping", stop_value=True)

    stub.sweep_once = fake_sweep
    monkeypatch.setattr(CONFIG, "WORKSPACE_SWEEP_INTERVAL_SEC", 1)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def _run():
        set_request_id("INIT-NO-TICK")
        try:
            await SharedWorkspace._sweeper_loop(stub)
        finally:
            set_request_id("")

    asyncio.run(_run())
    _assert_ticks(rids, "ws-sweeper-")


# ============ 5. 不改控制流：退出条件仍生效 ============

@pytest.mark.parametrize("kind", [
    "trace-persist", "write-buffer", "wiki-sync", "ws-sweeper",
])
def test_exit_condition_still_terminates(monkeypatch, kind):
    """置退出标志后循环在有限轮内退出，不挂死（整体包 wait_for 超时守护）。"""
    from hub_mixins.buffer import BufferMixin
    from shared_workspace import SharedWorkspace

    rids = []

    if kind in ("trace-persist", "write-buffer"):
        stub = SimpleNamespace(_running=True)
        q = _QueueStub(rids, stub, "_running", n=1)
        if kind == "trace-persist":
            stub._trace_persist_queue = q
            coro_fn = lambda: BufferMixin._trace_persist_worker(stub)
        else:
            stub._write_queue = q
            coro_fn = lambda: BufferMixin._write_buffer_worker(stub)
    elif kind == "wiki-sync":
        stub = SimpleNamespace(
            _running=True, _wiki_sync_pending=False,
            _last_wiki_sync=0.0, _WIKI_SYNC_COOLDOWN=0, _write_trace=[],
        )

        async def fake_sleep(sec):
            _collect_and_stop(rids, stub, "_running", n=1)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        coro_fn = lambda: BufferMixin._wiki_sync_throttler(stub)
    else:
        from models import CONFIG
        stub = SimpleNamespace(_stopping=False)

        async def fake_sweep():
            pass

        async def fake_sleep(sec):
            _collect_and_stop(rids, stub, "_stopping", n=1, stop_value=True)

        stub.sweep_once = fake_sweep
        monkeypatch.setattr(CONFIG, "WORKSPACE_SWEEP_INTERVAL_SEC", 1)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        coro_fn = lambda: SharedWorkspace._sweeper_loop(stub)

    async def _run():
        await asyncio.wait_for(coro_fn(), timeout=2.0)

    asyncio.run(_run())  # 挂死则 wait_for 超时 → 测试失败
    assert rids, "至少应完成 1 轮 tick"


# ============ 6. 无回归：CD-108 与 CD-116 tick 机制不互斥 ============

def test_cd108_and_cd116_ticks_do_not_conflict(monkeypatch):
    """交叉验证：CD-108 的 _keepalive_ping 与 CD-116 的 _trace_persist_worker
    各自轮换自己的前缀，ContextVar 不互相覆盖语义。"""
    from hub_mixins.buffer import BufferMixin
    from hub_mixins.maintenance import MaintenanceMixin

    keepalive_rids = []
    persist_rids = []

    async def fake_sleep(sec):
        keepalive_rids.append(get_request_id())
        if len(keepalive_rids) >= 3:
            raise _Sentinel()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def _run_keepalive():
        fake = SimpleNamespace(
            _running=True, _HEARTBEAT_INTERVAL=30,
            active_ws={}, _lock=asyncio.Lock(),
        )
        try:
            with pytest.raises(_Sentinel):
                await MaintenanceMixin._keepalive_ping(fake)
        finally:
            set_request_id("")

    asyncio.run(_run_keepalive())
    monkeypatch.undo()

    stub = SimpleNamespace(_running=True)
    stub._trace_persist_queue = _QueueStub(persist_rids, stub, "_running")

    async def _run_persist():
        try:
            await BufferMixin._trace_persist_worker(stub)
        finally:
            set_request_id("")

    asyncio.run(_run_persist())

    ka_ticks = keepalive_rids[1:]  # 第 1 轮 sleep 时尚未 rotate（CD-108 形态）
    assert len(set(ka_ticks)) >= 2, f"CD-108 keepalive tick 应轮换: {ka_ticks}"
    for r in ka_ticks:
        assert r.startswith("keepalive-"), f"CD-108 前缀应为 keepalive-: {r}"

    _assert_ticks(persist_rids, "trace-persist-")
    # 两侧前缀互不相同 → 两套机制独立
    assert not any(r.startswith("trace-persist-") for r in ka_ticks)
    assert not any(r.startswith("keepalive-") for r in persist_rids)


# ============ 7. 结构断言：函数体内确实有 rotate_request_id( 调用 ============

@pytest.mark.parametrize("import_name,attr,funcname,prefix", [
    ("hub_mixins.buffer", "BufferMixin", "_trace_persist_worker", "trace-persist"),
    ("hub_mixins.buffer", "BufferMixin", "_write_buffer_worker", "write-buffer"),
    ("hub_mixins.buffer", "BufferMixin", "_wiki_sync_throttler", "wiki-sync"),
    ("shared_workspace", "SharedWorkspace", "_sweeper_loop", "ws-sweeper"),
])
def test_loop_body_calls_rotate_request_id(import_name, attr, funcname, prefix):
    mod = __import__(import_name, fromlist=[attr])
    cls = getattr(mod, attr)
    src = inspect.getsource(getattr(cls, funcname))
    assert "rotate_request_id(" in src, (
        f"{import_name}:{funcname} 循环体内未找到 rotate_request_id(...) 调用"
    )
    assert f'"{prefix}"' in src or f"'{prefix}'" in src, (
        f"{funcname} 内未找到 prefix={prefix} 的 rotate_request_id 调用"
    )
