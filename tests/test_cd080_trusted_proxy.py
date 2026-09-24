# -*- coding: utf-8 -*-
"""tests/test_cd080_trusted_proxy.py — CD-080：X-Forwarded-For 伪造绕过限流修复

口径：
- 默认 TRUSTED_PROXIES 为空 = 不信任任何代理头：伪造 XFF 不改变限速记账 IP（不能绕过）。
- 直连对端命中 server.trusted_proxies（CIDR/IP）后才采信 XFF 链首（=原始客户端）；
  XFF 链首非法值回落对端 IP。

直调 routes._scope_client_ip / routes._rate_limit_ok（伪造 ASGI scope），不起 HTTP。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes
from models import CONFIG


def _scope(peer: str = "1.2.3.4", xff: str = None) -> dict:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    return {"type": "http", "client": (peer, 12345), "headers": headers,
            "path": "/api/v1/x"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """限速窗口与可信代理配置隔离。"""
    routes._rate_hits.clear()
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", [])
    yield
    routes._rate_hits.clear()


def test_xff_ignored_when_no_trusted_proxies():
    """默认空可信列表：XFF 一律不采信，ClientIP = 连接对端。"""
    assert routes._scope_client_ip(_scope(peer="1.2.3.4", xff="9.9.9.9")) == "1.2.3.4"


def test_xff_honored_from_trusted_proxy(monkeypatch):
    """对端在可信列表内 → 采信 XFF 链首（原始客户端）。"""
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    scope = _scope(peer="10.0.0.9", xff="5.6.7.8, 10.0.0.9")
    assert routes._scope_client_ip(scope) == "5.6.7.8"


def test_xff_ignored_from_untrusted_peer(monkeypatch):
    """对端不在可信列表 → XFF 不采信。"""
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    assert routes._scope_client_ip(_scope(peer="192.168.1.5", xff="9.9.9.9")) == "192.168.1.5"


def test_xff_invalid_value_falls_back_to_peer(monkeypatch):
    """可信代理转发但 XFF 链首非法 → 回落对端 IP（fail-closed）。"""
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.1/32"])
    assert routes._scope_client_ip(_scope(peer="10.0.0.1", xff="not-an-ip")) == "10.0.0.1"


def test_xff_absent_from_trusted_proxy_uses_peer(monkeypatch):
    """可信代理但没带 XFF → 对端 IP。"""
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.1"])
    assert routes._scope_client_ip(_scope(peer="10.0.0.1")) == "10.0.0.1"


def test_forged_xff_does_not_bypass_rate_limit(monkeypatch):
    """核心回归：默认配置下伪造 XFF 换「IP」刷请求 → 仍按真实对端记账，超限 429。"""
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", 2)
    for i in range(2):
        ok = asyncio.run(routes._rate_limit_ok(
            _scope(peer="1.2.3.4", xff=f"10.1.1.{i}"), "/api/v1/mem"))
        assert ok, f"第 {i + 1} 次请求不应被限"
    ok = asyncio.run(routes._rate_limit_ok(
        _scope(peer="1.2.3.4", xff="10.1.1.99"), "/api/v1/mem"))
    assert not ok, "伪造 XFF 换 IP 不得绕过每 IP 限速"


def test_rate_limit_keyed_by_real_client_behind_trusted_proxy(monkeypatch):
    """可信代理之后：限速按 XFF 解析出的真实客户端分桶（不同客户端互不占额度）。"""
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", 1)
    scope_a = _scope(peer="10.0.0.9", xff="5.6.7.8")
    scope_b = _scope(peer="10.0.0.9", xff="5.6.7.9")
    assert asyncio.run(routes._rate_limit_ok(scope_a, "/api/v1/mem"))
    # 同一真实客户端第二次 → 超限
    assert not asyncio.run(routes._rate_limit_ok(scope_a, "/api/v1/mem"))
    # 另一真实客户端仍有独立额度
    assert asyncio.run(routes._rate_limit_ok(scope_b, "/api/v1/mem"))
