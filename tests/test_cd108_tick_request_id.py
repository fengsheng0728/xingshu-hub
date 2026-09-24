# -*- coding: utf-8 -*-
"""tests/test_cd108_tick_request_id.py — CD-108：后台循环 tick 级 request id 轮换

覆盖：
1. rotate_request_id 行为：连续调用返回不同 id、前缀正确、get_request_id() 等于最后一次返回值
2. AST 结构断言：五个长跑循环体里都出现 rotate_request_id(...) 调用（防复发）
3. tick 级轮换真行为：monkeypatch asyncio.sleep，跑 _keepalive_ping 两轮，
   断言两轮 get_request_id() 不同且都带 keepalive- 前缀
"""
import ast
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logfmt import get_request_id, set_request_id

ROOT = Path(__file__).resolve().parent.parent


class _Sentinel(Exception):
    pass


# ============ 1. rotate_request_id 行为 ============

def test_rotate_request_id_distinct_prefix_and_get():
    from logfmt import rotate_request_id
    try:
        rid1 = rotate_request_id("keepalive")
        rid2 = rotate_request_id("keepalive")
        assert rid1 != rid2, f"连续两次应返回不同 id: {rid1} vs {rid2}"
        assert rid1.startswith("keepalive-"), f"rid1 应带 keepalive- 前缀: {rid1}"
        assert rid2.startswith("keepalive-"), f"rid2 应带 keepalive- 前缀: {rid2}"
        assert get_request_id() == rid2
    finally:
        set_request_id("")


def test_rotate_request_id_custom_prefix():
    from logfmt import rotate_request_id
    try:
        rid = rotate_request_id("cleanup-loop")
        assert rid.startswith("cleanup-loop-"), f"应带 cleanup-loop- 前缀: {rid}"
        assert get_request_id() == rid
    finally:
        set_request_id("")


# ============ 2. AST 结构断言：五处循环体都有 rotate_request_id 调用 ============

def _find_func(filepath: Path, funcname: str):
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == funcname:
            return node
    raise AssertionError(f"{filepath.name} 中未找到函数 {funcname}")


def _has_rotate_call(func_node) -> bool:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "rotate_request_id":
                return True
            if isinstance(f, ast.Attribute) and f.attr == "rotate_request_id":
                return True
    return False


@pytest.mark.parametrize("relpath,funcname", [
    ("hub_mixins/maintenance.py", "_cleanup_loop"),
    ("hub_mixins/maintenance.py", "_keepalive_ping"),
    ("routes_automation.py", "automation_scheduler"),
    ("integrations/registry.py", "integration_scheduler"),
    ("routes_audit.py", "anchor_export_loop"),
])
def test_loop_body_calls_rotate_request_id(relpath, funcname):
    fp = ROOT / relpath
    func_node = _find_func(fp, funcname)
    assert _has_rotate_call(func_node), (
        f"{relpath}:{funcname} 循环体内未找到 rotate_request_id(...) 调用"
    )


# ============ 3. tick 级轮换真行为：_keepalive_ping 两轮 id 不同 ============

def test_keepalive_ping_rotates_request_id_each_tick(monkeypatch):
    from hub_mixins.maintenance import MaintenanceMixin

    rids_at_sleep = []

    async def fake_sleep(sec):
        rids_at_sleep.append(get_request_id())
        if len(rids_at_sleep) >= 3:
            raise _Sentinel()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def _run():
        fake = SimpleNamespace(
            _running=True,
            _HEARTBEAT_INTERVAL=30,
            active_ws={},
            _lock=asyncio.Lock(),
        )
        set_request_id("bg-keepalive-INIT0000")
        try:
            with pytest.raises(_Sentinel):
                await MaintenanceMixin._keepalive_ping(fake)
            result = get_request_id()
        finally:
            set_request_id("")
        return result

    final_rid = asyncio.run(_run())

    assert len(rids_at_sleep) == 3
    # rids_at_sleep[0] = 初始 id（第 1 轮 sleep 时，尚未 rotate）
    # rids_at_sleep[1] = 第 1 轮 rotate 后的 id（第 2 轮 sleep 时）
    # rids_at_sleep[2] = 第 2 轮 rotate 后的 id（第 3 轮 sleep 时，随后抛哨兵）
    rid_tick1 = rids_at_sleep[1]
    rid_tick2 = rids_at_sleep[2]
    assert rid_tick1 != rid_tick2, f"两轮 tick request id 应不同: {rid_tick1} vs {rid_tick2}"
    assert rid_tick1.startswith("keepalive-"), f"tick1 id 应带 keepalive- 前缀: {rid_tick1}"
    assert rid_tick2.startswith("keepalive-"), f"tick2 id 应带 keepalive- 前缀: {rid_tick2}"
    assert final_rid == rid_tick2, "退出时 get_request_id() 应等于最后一次轮换值"
