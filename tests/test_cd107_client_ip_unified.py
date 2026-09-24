# -*- coding: utf-8 -*-
"""tests/test_cd107_client_ip_unified.py — CD-107：客户端 IP 口径统一

目标：全仓只保留一套 IP 口径（CD-080 可信代理语义），下沉到 routes_common.py。
routes.py 不得再有独立实现；认证依赖（get_current_agent / get_current_principal）
与限速/审计共用同一 helper。

全部直调函数、伪造 ASGI scope，不起 HTTP、不绑端口。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes
import routes_common
from models import CONFIG


def _scope(peer, xff=None):
    """构造 ASGI scope：client=(peer, port) + 可选 x-forwarded-for 头。"""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    return {"type": "http", "client": (peer, 1234), "headers": headers}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """配置隔离 + 可信网缓存失效（模块级 cache 跨用例脏读防护）。

    配置字段真实名为 CONFIG.TRUSTED_PROXIES（config.yaml server.trusted_proxies
    扁平化到 Config），不是 SERVER_TRUSTED_PROXIES。
    """
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", [])
    # cache 变量随实现下沉到 routes_common；旧实现无此变量，hasattr 兼容先红阶段
    if hasattr(routes_common, "_TRUSTED_NETS_KEY"):
        monkeypatch.setattr(routes_common, "_TRUSTED_NETS_KEY", None)
    if hasattr(routes_common, "_TRUSTED_NETS_CACHE"):
        monkeypatch.setattr(routes_common, "_TRUSTED_NETS_CACHE", [])
    yield


def _spy_trusted_nets(monkeypatch):
    """Spy _trusted_proxy_nets：证明调用路径走了可信代理判定。

    断言方向调整（任务书 3.3）：返回值断言在旧口径下对部分输入恒绿
    （新旧实现对非可信 / 非法 XFF / 空列表返回相同 peer），单靠返回值
    打不中「两套口径的差异」。此处 spy 协作函数调用——旧实现完全不调用
    _trusted_proxy_nets → called 为空 → 断言红；新实现必调用 → 绿。
    """
    called = []
    orig = getattr(routes_common, "_trusted_proxy_nets", None)

    def _spy():
        called.append(1)
        return orig() if orig is not None else []

    monkeypatch.setattr(routes_common, "_trusted_proxy_nets", _spy, raising=False)
    return called


def test_trusted_proxy_honors_xff_head(monkeypatch):
    """1. 可信代理 + 合法 XFF → 返回 XFF 链首（旧口径不看 XFF，改动前红）。"""
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    scope = _scope("10.0.0.9", xff="203.0.113.7, 10.0.0.9")
    assert routes_common._scope_client_ip(scope) == "203.0.113.7"


def test_untrusted_peer_ignores_xff(monkeypatch):
    """2. 非可信代理 + XFF → 返回直连对端（XFF 被忽略）。"""
    called = _spy_trusted_nets(monkeypatch)
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    scope = _scope("192.168.1.5", xff="9.9.9.9")
    assert routes_common._scope_client_ip(scope) == "192.168.1.5"
    assert called, (
        "新口径必须调用 _trusted_proxy_nets 做可信判定"
        "（旧实现不调用 → 此断言改动前红）"
    )


def test_trusted_proxy_invalid_xff_falls_back(monkeypatch):
    """3. 可信代理 + 非法 XFF → 回落直连对端。"""
    called = _spy_trusted_nets(monkeypatch)
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    scope = _scope("10.0.0.9", xff="garbage")
    assert routes_common._scope_client_ip(scope) == "10.0.0.9"
    assert called, (
        "新口径必须调用 _trusted_proxy_nets 做可信判定"
        "（旧实现不调用 → 此断言改动前红）"
    )


def test_empty_trusted_or_unknown_peer(monkeypatch):
    """4. 可信代理列表为空 / unknown peer → 返回 peer，不抛异常。"""
    called = _spy_trusted_nets(monkeypatch)
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", [])
    scope = _scope("10.0.0.9", xff="203.0.113.7")
    assert routes_common._scope_client_ip(scope) == "10.0.0.9"
    monkeypatch.setattr(CONFIG, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    assert routes_common._scope_client_ip({"client": None}) == "unknown"
    assert routes_common._scope_client_ip({}) == "unknown"
    assert called, (
        "新口径必须调用 _trusted_proxy_nets 做可信判定"
        "（旧实现不调用 → 此断言改动前红）"
    )


def test_single_implementation_identity():
    """5. 归一实证：routes_common 与 routes 的 _scope_client_ip 是同一函数对象。

    修复前 routes.py 有自己的 def（不同对象）→ 断言红；修复后 routes 仅
    re-export → 同一对象 → 绿。此断言直接打中「只有一套实现」，非恒真。
    """
    assert routes_common._scope_client_ip is routes._scope_client_ip, (
        "routes 必须 re-export routes_common 的实现，不得持有独立 def"
    )
    assert routes_common._scope_client_ip.__module__ == "routes_common"
    assert routes._scope_client_ip.__module__ == "routes_common"
    assert callable(routes_common._direct_client_ip)
    assert routes_common._direct_client_ip is not routes_common._scope_client_ip
