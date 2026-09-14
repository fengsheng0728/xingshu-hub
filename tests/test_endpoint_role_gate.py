# -*- coding: utf-8 -*-
"""BatchB-T15 端点角色门测试（2026-09-09）

覆盖验收断言：
1. POST /api/v1/server/config：hub_token → 放行；manager agent key → 放行；
   worker agent key → 403；无凭据 → 401（TokenAuthMiddleware 行为不变）；
   NO_AUTH 开发模式无身份语义 → 放行（与全局中间件一致）
2. /ws/dashboard、/ws/buffer 收严：worker principal → close 4401；
   hub_token / manager → 接受
3. 回归：require_privileged 默认 False → /ws/{agent_id} 等非特权通道行为不变

纯单测：直接驱动路由函数 / _ws_auth_accept / TokenAuthMiddleware，
不起真实 Hub，不连 3060，不写运行时数据（config 写入临时目录）。
"""
import asyncio
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes
import routes_common
import routes_server
from auth_provider import Principal


# ═══════════ 配方 ═══════════

class _FakeRequest:
    """最小 Request stub：路由只读 request.scope"""

    def __init__(self, principal=None):
        self.scope = {}
        if principal is not None:
            self.scope["principal"] = principal


class _FakeWS:
    """最小 WebSocket stub：accept/首帧/close 记录"""

    def __init__(self, token):
        self._frame = {"type": "auth", "token": token}
        self.query_params = {}
        self.client = None  # _client_ip → "unknown"
        self.accepted = False
        self.close_code = None
        self.close_reason = ""

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        return self._frame

    async def close(self, code=1000, reason=""):
        self.close_code = code
        self.close_reason = reason


class _StubProvider:
    """按 token → Principal 映射的 stub auth_provider"""

    def __init__(self, mapping):
        self._mapping = mapping

    def authenticate(self, token, client_ip=""):
        return self._mapping.get(token)


def _ws_run(ws, hint="", strict_agent=False, require_privileged=False):
    return asyncio.run(routes._ws_auth_accept(
        ws, hint, strict_agent=strict_agent, require_privileged=require_privileged))


@pytest.fixture()
def privileged_env(monkeypatch):
    """真实门卫语义：NO_AUTH=False + token→principal 映射 + 可控 role 查询"""
    monkeypatch.setattr(routes, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    monkeypatch.setattr(routes_server, "NO_AUTH", False)
    mapping = {
        "hub-tok": Principal(subject_type="service", subject_id="__hub__",
                             auth_mode="hub_token"),
        "mgr-key": Principal(subject_type="service", subject_id="mgr-1",
                             auth_mode="api_key"),
        "wkr-key": Principal(subject_type="service", subject_id="wkr-1",
                             auth_mode="api_key"),
    }
    monkeypatch.setattr(routes, "_auth_provider", lambda: _StubProvider(mapping))
    monkeypatch.setattr(routes_common, "_auth_provider", lambda: _StubProvider(mapping))
    roles = {"mgr-1": "manager", "wkr-1": "worker"}
    monkeypatch.setattr(routes_common, "_agent_role",
                        lambda agent_id: roles.get(agent_id, ""))
    return mapping


# ═══════════ ① POST /api/v1/server/config 角色门 ═══════════

def test_config_post_hub_token_allowed(privileged_env, tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_HUB_CONFIG_DIR", str(tmp_path))
    req = _FakeRequest({"auth_mode": "hub_token", "subject_id": "__hub__"})
    result = asyncio.run(routes_server.api_server_update_config(
        routes_server.ServerConfigUpdate(lan_enabled=False), req))
    assert result["status"] == "ok"
    assert result["lan_enabled"] is False
    # 配置确实写入临时目录（不碰真实 config/）
    assert os.path.isfile(os.path.join(str(tmp_path), "config.yaml"))


def test_config_post_manager_allowed(privileged_env, tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_HUB_CONFIG_DIR", str(tmp_path))
    req = _FakeRequest({"auth_mode": "api_key", "subject_id": "mgr-1"})
    result = asyncio.run(routes_server.api_server_update_config(
        routes_server.ServerConfigUpdate(lan_enabled=True), req))
    assert result["status"] == "ok"
    assert result["lan_enabled"] is True


def test_config_post_worker_403(privileged_env, tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_HUB_CONFIG_DIR", str(tmp_path))
    req = _FakeRequest({"auth_mode": "api_key", "subject_id": "wkr-1"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_server.api_server_update_config(
            routes_server.ServerConfigUpdate(lan_enabled=True), req))
    assert exc.value.status_code == 403
    assert "manager/orchestrator" in exc.value.detail
    # 403 前置：不得写配置
    assert not os.path.isfile(os.path.join(str(tmp_path), "config.yaml"))


def test_config_post_no_credential_401_middleware(privileged_env, monkeypatch):
    """无凭据 → TokenAuthMiddleware 401（中间件行为不变，请求到不了路由）"""
    routes._rate_hits.clear()
    reached = []

    async def inner_app(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = routes.TokenAuthMiddleware(inner_app)
    scope = {"type": "http", "path": "/api/v1/server/config",
             "headers": [], "client": ("10.77.0.1", 12345), "method": "POST"}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 401
    assert not reached


def test_config_post_no_auth_mode_bypass(tmp_path, monkeypatch):
    """NO_AUTH 开发模式无身份语义 → 放行（dashboard 配置页 dev 路径不断）"""
    # conftest 全局 NO_AUTH=1，routes_server 模块级常量即为 True，无需 patch
    assert routes_server.NO_AUTH is True
    monkeypatch.setenv("SYNC_HUB_CONFIG_DIR", str(tmp_path))
    req = _FakeRequest()  # scope 无 principal
    result = asyncio.run(routes_server.api_server_update_config(
        routes_server.ServerConfigUpdate(lan_enabled=False), req))
    assert result["status"] == "ok"


# ═══════════ ② /ws/dashboard、/ws/buffer 收严 ═══════════

def test_ws_privileged_worker_rejected_4401(privileged_env):
    ws = _FakeWS("wkr-key")
    result = _ws_run(ws, "__dashboard__", require_privileged=True)
    assert result is None
    assert ws.close_code == 4401


def test_ws_privileged_hub_token_accepted(privileged_env):
    ws = _FakeWS("hub-tok")
    result = _ws_run(ws, "__dashboard__", require_privileged=True)
    assert result == "__dashboard__"
    assert ws.close_code is None


def test_ws_privileged_manager_accepted(privileged_env):
    ws = _FakeWS("mgr-key")
    result = _ws_run(ws, "buffer-monitor", require_privileged=True)
    assert result == "mgr-1"
    assert ws.close_code is None


def test_ws_privileged_unknown_subject_rejected(privileged_env):
    """api_key 有效但 agents 表查不到 role（fail-closed）→ 4401"""
    ws = _FakeWS("wkr-key")
    monkeypatch_roles_empty = lambda agent_id: ""
    import routes_common as rc
    orig = rc._agent_role
    rc._agent_role = monkeypatch_roles_empty
    try:
        result = _ws_run(ws, "__dashboard__", require_privileged=True)
    finally:
        rc._agent_role = orig
    assert result is None
    assert ws.close_code == 4401


# ═══════════ ③ 回归：非特权通道行为不变 ═══════════

def test_ws_default_not_privileged_worker_accepted(privileged_env):
    """require_privileged 默认 False → worker 照常通过（/ws/{agent_id} 零变化）"""
    ws = _FakeWS("wkr-key")
    result = _ws_run(ws, "wkr-1", strict_agent=True)  # 与 /ws/{agent_id} 调用形态一致
    assert result == "wkr-1"
    assert ws.close_code is None


def test_ws_dashboard_buffer_callsite_wiring():
    """源码断言：dashboard/buffer 两处调用点已传 require_privileged=True，
    /ws/{agent_id}（strict_agent 路径）未传"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "routes_ws.py"),
               encoding="utf-8").read()
    assert '_ws_auth_accept(websocket, "__dashboard__", require_privileged=True)' in src
    assert '_ws_auth_accept(websocket, "buffer-monitor", require_privileged=True)' in src
    assert '_ws_auth_accept(websocket, agent_id, strict_agent=True)' in src


# ═══════════ helper 直测 ═══════════

def test_principal_is_privileged_helper(privileged_env):
    assert routes_common.principal_is_privileged(None) is False
    assert routes_common.principal_is_privileged({}) is False
    assert routes_common.principal_is_privileged(
        {"auth_mode": "hub_token", "subject_id": "__hub__"}) is True
    assert routes_common.principal_is_privileged(
        Principal(subject_id="mgr-1", auth_mode="api_key")) is True
    assert routes_common.principal_is_privileged(
        Principal(subject_id="wkr-1", auth_mode="api_key")) is False
