# -*- coding: utf-8 -*-
"""BatchB-T11 配对码加固测试（2026-09-09）

覆盖三条验收断言：
1. pairing_requested 事件 payload 不含 6 位配对码明文（secrets 随机源，行为不变）
2. pair/exchange 不在限速豁免内（TokenAuthMiddleware 判定区：其余 allowlist 仍豁免）
3. exchange 码校验失败按 IP 计数 → 达阈值退避 429（不再查库）；成功清零；退避指数递增
"""
import asyncio
import os
import re
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes_team
from hub_mixins.team import TeamMixin


# ═══════════ 配方：FakeHub（沿用 test_n1_gate 的 FakeHub + 临时 sqlite 模式） ═══════════

class FakeHub(TeamMixin):
    """TeamMixin + 临时 DB + 事件收集，仅实现 request_pairing 依赖的最小面"""

    def __init__(self, db_path):
        self._db_path = db_path
        self.hub_id = "hub-test"
        self.hostname = "test-host"
        self.events = []

    def _db(self):
        return sqlite3.connect(self._db_path)

    async def _log_event(self, event_type, agent_id, payload):
        self.events.append((event_type, agent_id, payload))


@pytest.fixture()
def hub():
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute(
        """CREATE TABLE pairing_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT, hub_id_a TEXT, agent_id_a TEXT,
            expires_at TEXT, used INTEGER DEFAULT 0, hub_id_b TEXT)"""
    )
    conn.commit()
    conn.close()
    h = FakeHub(tmp)
    yield h
    try:
        os.remove(tmp)
    except OSError:
        pass


# ═══════════ 断言①：配对码审计无明文 + secrets 随机源行为不变 ═══════════

def test_pairing_requested_audit_has_no_code(hub):
    result = asyncio.run(hub.request_pairing("agent-a"))
    code = result["pairing_code"]
    # 行为不变：6 位数字码、5 分钟 TTL 字段存在
    assert re.fullmatch(r"\d{6}", code), f"配对码应为 6 位数字: {code!r}"
    assert result["expires_at"]
    # 审计事件存在，但 payload 不含配对码明文
    pairing_events = [e for e in hub.events if e[0] == "pairing_requested"]
    assert len(pairing_events) == 1
    _, agent_id, payload = pairing_events[0]
    assert agent_id == "agent-a"
    assert code not in str(payload), f"配对码明文泄露进审计 payload: {payload}"


def test_pairing_code_uses_secrets_source():
    """源码断言：request_pairing 不再使用 random.randint，改用 secrets.randbelow"""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "hub_mixins", "team.py"),
        encoding="utf-8",
    ).read()
    assert "random.randint" not in src
    assert "secrets.randbelow" in src


# ═══════════ 断言②：pair/exchange 参与限速（其余 allowlist 豁免不变） ═══════════

def _run_middleware(path, ip, monkeypatch, limit):
    """直接驱动 TokenAuthMiddleware 的 ASGI 调用，返回 (status, reached_app)"""
    import routes

    monkeypatch.setattr(routes, "NO_AUTH", False)  # conftest 全局 NO_AUTH=1，此处需真实门卫
    monkeypatch.setattr(routes.CONFIG, "RATE_LIMIT_PER_IP", limit)

    reached = []

    async def inner_app(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = routes.TokenAuthMiddleware(inner_app)
    scope = {"type": "http", "path": path, "headers": [], "client": (ip, 12345)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, bool(reached)


def test_pair_exchange_participates_in_rate_limit(monkeypatch):
    import routes
    routes._rate_hits.clear()
    ip = "10.66.0.1"
    # 前 2 次放行（到达内层 app），第 3 次超限 429
    assert _run_middleware("/api/v1/team/pair/exchange", ip, monkeypatch, 2) == (200, True)
    assert _run_middleware("/api/v1/team/pair/exchange", ip, monkeypatch, 2) == (200, True)
    status, reached = _run_middleware("/api/v1/team/pair/exchange", ip, monkeypatch, 2)
    assert status == 429 and not reached


def test_other_allowlist_paths_still_exempt(monkeypatch):
    import routes
    routes._rate_hits.clear()
    ip = "10.66.0.2"
    # /health 不在 RATE_LIMITED_ALLOWLIST → 限速豁免不变，远超阈值也放行
    for _ in range(5):
        status, reached = _run_middleware("/health", ip, monkeypatch, 2)
        assert status == 200 and reached


def test_rate_limited_allowlist_constant():
    import routes
    assert "/api/v1/team/pair/exchange" in routes.RATE_LIMITED_ALLOWLIST
    # 认证豁免保持原样：pair/exchange 仍在认证 allowlist 中
    assert "/api/v1/team/pair/exchange" in routes.AUTH_ALLOWLIST_PATHS


# ═══════════ 断言③：exchange 失败计数锁定/退避 + 成功清零 ═══════════

class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, host):
        self.client = _FakeClient(host)


class _ExchangeHub:
    """stub _handle_pair_exchange：记录调用次数，按 mode 返回成功/失败"""

    def __init__(self, mode="fail"):
        self.mode = mode
        self.calls = 0

    async def _handle_pair_exchange(self, code, req):
        self.calls += 1
        if self.mode == "fail":
            return {"error": "配对码无效"}
        return {"dh_public": "ab", "encrypted_key": "cd", "nonce": "ef", "agent_id_a": "a"}


@pytest.fixture(autouse=True)
def _reset_pair_state(monkeypatch):
    """每个用例前清零退避内存态（模块级 dict）"""
    routes_team._pair_fails.clear()
    routes_team._pair_ban_until.clear()
    routes_team._pair_backoff_sec.clear()
    yield
    routes_team._pair_fails.clear()
    routes_team._pair_ban_until.clear()
    routes_team._pair_backoff_sec.clear()


def _exchange(hub_stub, ip="10.77.0.1", code="123456"):
    return asyncio.run(
        routes_team.api_team_pair_exchange({"code": code}, _FakeRequest(ip))
    )


def test_exchange_lockout_after_threshold(monkeypatch):
    stub = _ExchangeHub("fail")
    monkeypatch.setattr(routes_team, "hub", stub)
    ip = "10.77.0.1"
    # 连续失败达阈值（5 次）→ 第 5 次触发退避
    for i in range(routes_team._PAIR_FAIL_MAX):
        r = _exchange(stub, ip)
        assert r == {"error": "配对码无效"}, f"第 {i+1} 次应正常校验"
    assert stub.calls == routes_team._PAIR_FAIL_MAX
    # 退避期内：直接 429，不再查库（stub 调用数不增）
    r = _exchange(stub, ip)
    assert getattr(r, "status_code", None) == 429, f"退避期应 429，实得 {r!r}"
    assert stub.calls == routes_team._PAIR_FAIL_MAX, "退避期内不应再调 _handle_pair_exchange"
    assert routes_team._pair_ip_banned(ip)


def test_exchange_success_clears_failures(monkeypatch):
    ip = "10.77.0.2"
    fail_stub = _ExchangeHub("fail")
    ok_stub = _ExchangeHub("ok")
    monkeypatch.setattr(routes_team, "hub", fail_stub)
    # 先失败 4 次（阈值 5，未触发退避）
    for _ in range(4):
        _exchange(fail_stub, ip)
    # 一次成功 → 计数清零
    monkeypatch.setattr(routes_team, "hub", ok_stub)
    r = _exchange(ok_stub, ip)
    assert "error" not in r
    assert routes_team._pair_fails.get(ip, []) == []
    # 再失败 4 次仍不触发退避（若未清零，累计 8 次早已锁定）
    monkeypatch.setattr(routes_team, "hub", fail_stub)
    for _ in range(4):
        r = _exchange(fail_stub, ip)
        assert r == {"error": "配对码无效"}
    assert not routes_team._pair_ip_banned(ip)


def test_exchange_backoff_doubles(monkeypatch):
    stub = _ExchangeHub("fail")
    monkeypatch.setattr(routes_team, "hub", stub)
    ip = "10.77.0.3"
    # 第一轮：5 次失败 → 退避 60s
    for _ in range(routes_team._PAIR_FAIL_MAX):
        _exchange(stub, ip)
    first_until = routes_team._pair_ban_until[ip]
    import time as _t
    assert first_until - _t.time() > routes_team._PAIR_BACKOFF_BASE_SEC - 2
    # 模拟退避期结束
    routes_team._pair_ban_until[ip] = _t.time() - 1
    assert not routes_team._pair_ip_banned(ip)
    # 第二轮：再 5 次失败 → 退避翻倍 120s
    for _ in range(routes_team._PAIR_FAIL_MAX):
        _exchange(stub, ip)
    second_until = routes_team._pair_ban_until[ip]
    assert second_until - _t.time() > routes_team._PAIR_BACKOFF_BASE_SEC * 2 - 2


def test_exchange_missing_code_unaffected(monkeypatch):
    """缺 code → 直接 error，不进入 IP 计数（参数错误不算爆破尝试）"""
    stub = _ExchangeHub("fail")
    monkeypatch.setattr(routes_team, "hub", stub)
    r = _exchange(stub, code="")
    assert r == {"error": "code 必填"}
    assert stub.calls == 0
    assert routes_team._pair_fails == {}
