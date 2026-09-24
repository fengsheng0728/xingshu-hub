# -*- coding: utf-8 -*-
"""CD-0xx（终审断点 1）：披露审批端点 hub_token 误伤 403 回归。

根因：routes_disclosure 的 approve/deny 调 require_role 时未传 principal，
hub_token 主体经 get_current_agent 解析为 ""（非 api_key 主体），
require_role 拿空串查 agents 角色 → 403。修复对照 routes_pipeline 端点「显式声明
request 参数并透传 request.scope["principal"]」的既成写法：
端点加 request: Request 参数并透传 request.scope["principal"]。

口径：端点函数直调（不起 TestClient、不绑端口，同 test_dead_letters 配方）；
NO_AUTH=False 由 monkeypatch 显式覆盖（conftest 默认 NO_AUTH=1）。
"""
import asyncio
import os
import sys

import pytest
from fastapi import HTTPException

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import routes_disclosure  # noqa: E402
from hub_mixins.disclosure_ops import DisclosureOpsMixin  # noqa: E402


class _FakeReq:
    """最小 Request 替身：require_role 只读 request.scope['principal']。"""

    def __init__(self, principal=None):
        self.scope = {"principal": principal}


@pytest.fixture()
def _no_auth_off(monkeypatch):
    monkeypatch.setattr(routes_disclosure, "NO_AUTH", False)


@pytest.fixture()
def _capture_approve(monkeypatch):
    """截获下游 hub.approve_disclosure_request（不触真实 Hub/DB）。"""
    calls = {}

    async def _fake(self, request_id, approver_id):
        calls["approve"] = (request_id, approver_id)
        return {"status": "approved"}

    async def _fake_deny(self, request_id, approver_id, reason):
        calls["deny"] = (request_id, approver_id, reason)
        return {"status": "denied"}

    # CD-114：类级打桩（实例级会给单例留下永久实例属性，遮蔽后续类级 monkeypatch）
    monkeypatch.setattr(DisclosureOpsMixin, "approve_disclosure_request", _fake)
    # CD-114：类级打桩（实例级会给单例留下永久实例属性，遮蔽后续类级 monkeypatch）
    monkeypatch.setattr(DisclosureOpsMixin, "deny_disclosure_request", _fake_deny)
    return calls


def test_approve_hub_token_not_403(_no_auth_off, _capture_approve):
    """hub_token 主体（current_agent=""）调 approve：require_role 放行，不再 403。"""
    req = _FakeReq({"auth_mode": "hub_token", "subject_id": "__hub__"})
    # hub_token 主体的 current_agent 为空串，approver_id 声明同一身份（空串）
    r = asyncio.run(routes_disclosure.api_approve_disclosure(
        "r-1", "", req, current_agent=""))
    assert r == {"status": "approved"}
    assert _capture_approve["approve"] == ("r-1", "")


def test_deny_hub_token_not_403(_no_auth_off, _capture_approve):
    req = _FakeReq({"auth_mode": "hub_token", "subject_id": "__hub__"})
    r = asyncio.run(routes_disclosure.api_deny_disclosure(
        "r-2", "", req, deny_reason="不批", current_agent=""))
    assert r == {"status": "denied"}
    assert _capture_approve["deny"] == ("r-2", "", "不批")


def test_approve_worker_principal_still_403(_no_auth_off, _capture_approve):
    """门没有因修复而松开：普通 worker（api_key 主体、无特权角色）→ 仍 403。"""
    req = _FakeReq({"auth_mode": "api_key", "subject_id": "worker-1"})
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_disclosure.api_approve_disclosure(
            "r-3", "worker-1", req, current_agent="worker-1"))
    assert ei.value.status_code == 403
    assert "approve" not in _capture_approve


def test_approve_no_principal_still_403(_no_auth_off, _capture_approve):
    """无 principal（修复前形态）+ 未知 agent → 仍 403（fail-closed 不变）。"""
    req = _FakeReq(None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_disclosure.api_approve_disclosure(
            "r-4", "", req, current_agent=""))
    assert ei.value.status_code == 403
