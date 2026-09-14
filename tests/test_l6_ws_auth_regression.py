"""L6 WS 认证回归测试 — 空/错 token/身份不匹配拒绝（P1 首帧鉴权模型）

P1（D3）将 WS 认证从 query param api_key（close 4001）升级为首帧鉴权：
连接 accept 后首帧必须 {"type":"auth","token"}，3s 超时或校验失败 → close 4401。
认证成功前连接不入任何管理器。

回归背景：routes.py 原 `if not NO_AUTH and token:` 在空 token 时整个条件为 False，
跳过认证直接 accept → 空 token 可冒充任意 agent 收派单（认证绕过，安全 P0）。
P1 首帧模型从根上消除该问题：任何凭据都在 accept 后、注册前强制校验。
"""
import asyncio
import json
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import routes
import routes_common
from models import CONFIG


def _make_ws(auth_frame=None):
    """mock websocket：query_params 无 api_key（P1 不再用），首帧由调用方决定"""
    ws = MagicMock()
    ws.query_params = MagicMock()
    ws.query_params.get = lambda k, d="": "" if k == "api_key" else d
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    if auth_frame is not None:
        ws.receive_json = AsyncMock(return_value=auth_frame)
    else:
        # 无首帧（直接发业务帧/静默）→ 模拟非 auth 帧或超时
        ws.receive_json = AsyncMock(side_effect=asyncio.TimeoutError())
    return ws


def _make_db(path: str, api_key: str, agent_id: str):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE agents (agent_id TEXT, api_key TEXT)")
    conn.execute("INSERT INTO agents (agent_id, api_key) VALUES (?, ?)", (agent_id, api_key))
    conn.commit()
    conn.close()


def test_empty_token_rejected(tmp_path, monkeypatch):
    """空 token（首帧 token 为空）→ close 4401，绝不注册进 active_ws"""
    db = str(tmp_path / "t.db")
    _make_db(db, "secret-key", "agent-1")
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    monkeypatch.setattr(CONFIG, "HUB_TOKEN", "")
    monkeypatch.setattr(routes, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    ws = _make_ws({"type": "auth", "token": ""})
    asyncio.run(routes.ws_endpoint(ws, "agent-1"))
    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 4401
    assert "agent-1" not in routes.hub.active_ws  # 认证前零注册


def test_wrong_token_rejected(tmp_path, monkeypatch):
    """错 token（DB 无此 api_key，且非 hub_token）→ close 4401，不注册"""
    db = str(tmp_path / "t.db")
    _make_db(db, "secret-key", "agent-1")
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    monkeypatch.setattr(CONFIG, "HUB_TOKEN", "")
    monkeypatch.setattr(routes, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    ws = _make_ws({"type": "auth", "token": "wrong-key"})
    asyncio.run(routes.ws_endpoint(ws, "agent-1"))
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 4401
    assert "agent-1" not in routes.hub.active_ws


def test_token_agent_mismatch_rejected(tmp_path, monkeypatch):
    """api_key 属于 agent-2 却连 agent-1 → close 4401（strict_agent 防跨 agent 冒充）"""
    db = str(tmp_path / "t.db")
    _make_db(db, "secret-key", "agent-2")
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    monkeypatch.setattr(CONFIG, "HUB_TOKEN", "")
    monkeypatch.setattr(routes, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    ws = _make_ws({"type": "auth", "token": "secret-key"})
    asyncio.run(routes.ws_endpoint(ws, "agent-1"))
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 4401
    assert "agent-1" not in routes.hub.active_ws


def test_no_auth_frame_rejected(tmp_path, monkeypatch):
    """首帧不是 auth 帧（直接发业务帧）→ close 4401"""
    db = str(tmp_path / "t.db")
    _make_db(db, "secret-key", "agent-1")
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    monkeypatch.setattr(CONFIG, "HUB_TOKEN", "")
    monkeypatch.setattr(routes, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    ws = _make_ws({"type": "ping"})
    asyncio.run(routes.ws_endpoint(ws, "agent-1"))
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 4401
    assert "agent-1" not in routes.hub.active_ws


def _run_endpoint_and_check(ws, agent_id, db_path, hub_token):
    """连接存活期间断言注册，随后取消任务（模拟断连触发 finally 注销）"""
    routes.hub.active_ws.pop(agent_id, None)

    async def _driver():
        task = asyncio.create_task(routes.ws_endpoint(ws, agent_id))
        # 让协程跑到 receive 挂起点
        for _ in range(100):
            await asyncio.sleep(0.01)
            if agent_id in routes.hub.active_ws:
                break
        assert agent_id in routes.hub.active_ws  # 认证成功才注册
        # 取消任务 → 模拟断连 → finally 注销
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        assert agent_id not in routes.hub.active_ws  # 断连注销

    asyncio.run(_driver())


def test_correct_token_accepted(tmp_path, monkeypatch):
    """正确 api_key + 匹配 agent → 通过，注册进 active_ws"""
    db = str(tmp_path / "t.db")
    _make_db(db, "secret-key", "agent-1")
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    monkeypatch.setattr(CONFIG, "HUB_TOKEN", "")
    monkeypatch.setattr(routes, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    # receive_json：auth 帧后挂起（连接保持存活）
    calls = [{"type": "auth", "token": "secret-key"}]
    async def _recv():
        if calls:
            return calls.pop(0)
        await asyncio.Event().wait()  # 挂起 = 连接存活
    ws = MagicMock()
    ws.query_params = MagicMock()
    ws.query_params.get = lambda k, d="": "" if k == "api_key" else d
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=_recv)
    _run_endpoint_and_check(ws, "agent-1", db, "")


def test_hub_token_accepted(tmp_path, monkeypatch):
    """hub_token（部署级单 token）→ 通过（D1 无身份语义，信任路径声明）"""
    db = str(tmp_path / "t.db")
    _make_db(db, "secret-key", "agent-1")
    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    monkeypatch.setattr(CONFIG, "HUB_TOKEN", "hub-secret-token")
    monkeypatch.setattr(routes, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    calls = [{"type": "auth", "token": "hub-secret-token"}]
    async def _recv():
        if calls:
            return calls.pop(0)
        await asyncio.Event().wait()
    ws = MagicMock()
    ws.query_params = MagicMock()
    ws.query_params.get = lambda k, d="": "" if k == "api_key" else d
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=_recv)
    _run_endpoint_and_check(ws, "agent-1", db, "hub-secret-token")
