# -*- coding: utf-8 -*-
"""CD-117（2026-09-24）：免认证联邦端点加回每 IP 限速 —— 验收测试。

背景：CD-110 把 /api/v1/wiki/import、/api/v1/wiki/export 与既有
/api/v1/federation/snapshot 加进 AUTH_ALLOWLIST_PREFIXES（认证豁免 + 端点函数内
双通道自认证），但限速中间件对 allowlist 路径直接跳过 → 匿名方可高频打、
每次触发认证失败路径并落一行 _log_deny（拒绝路径写入放大）。
方案①（用户拍板）：限速与认证是两件事，豁免认证 ≠ 豁免限速 —— 给这几条加回限速。

口径（对齐 test_pairing_hardening T11 配方 + test_cd110 的 NO_AUTH 语义）：
  - fastapi.testclient.TestClient（进程内 ASGI 调用，不起真实 Hub、不绑端口）
  - monkeypatch routes.NO_AUTH=False（中间件限速路径）+ routes_common.NO_AUTH=False
    （wiki 端点自认证生效——匿名请求被拒也计入限速次数）+
    routes_federation.NO_AUTH=False（snapshot 端点自认证）
  - monkeypatch CONFIG.RATE_LIMIT_PER_IP=<小值>
  - 清 routes._rate_hits（滑动窗口内存态）
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes             # noqa: E402
import routes_common      # noqa: E402
import routes_federation  # noqa: E402
from models import CONFIG  # noqa: E402

LIMIT = 3
HITS = 5


@pytest.fixture()
def gate(monkeypatch):
    """鉴权语义环境：NO_AUTH=False + 清限速窗口。
    TestClient 不用 with（不起 lifespan 备份/调度循环，同 test_guard_liveness 配方）。
    """
    from fastapi.testclient import TestClient
    monkeypatch.setattr(routes, "NO_AUTH", False)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    monkeypatch.setattr(routes_federation, "NO_AUTH", False)
    routes._rate_hits.clear()
    return TestClient(routes.app)


def _codes(client, method, path, n=HITS, **kwargs):
    """连打 n 次，返回状态码序列（供断言消息与报告观测用）。"""
    out = []
    for _ in range(n):
        r = getattr(client, method)(path, **kwargs)
        out.append(r.status_code)
    return out


# ═══ 1. /api/v1/wiki/import 会被限速 ═══

def test_wiki_import_rate_limited(gate, monkeypatch):
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", LIMIT)
    codes = _codes(gate, "post", "/api/v1/wiki/import", json={"pages": {}})
    assert 429 in codes, (
        f"阈值 {LIMIT}、连打 {HITS} 次应至少一次 429，实测序列: {codes}")


# ═══ 2. /api/v1/wiki/export 同上 ═══

def test_wiki_export_rate_limited(gate, monkeypatch):
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", LIMIT)
    codes = _codes(gate, "get", "/api/v1/wiki/export")
    assert 429 in codes, (
        f"阈值 {LIMIT}、连打 {HITS} 次应至少一次 429，实测序列: {codes}")


# ═══ 3. /api/v1/federation/snapshot/agents 同上 ═══

def test_federation_snapshot_rate_limited(gate, monkeypatch):
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", LIMIT)
    codes = _codes(gate, "get", "/api/v1/federation/snapshot/agents")
    assert 429 in codes, (
        f"阈值 {LIMIT}、连打 {HITS} 次应至少一次 429，实测序列: {codes}")


# ═══ 4. 对照：/docs 仍不限速 ═══

def test_docs_still_exempt(gate, monkeypatch):
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", LIMIT)
    codes = _codes(gate, "get", "/docs")
    assert 429 not in codes, (
        f"探针/文档路径应保持限速豁免，实测序列: {codes}")


# ═══ 5. 对照：pair/exchange 仍受限（T11 不回归） ═══

def test_pair_exchange_still_rate_limited(gate, monkeypatch):
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", LIMIT)
    codes = _codes(gate, "post", "/api/v1/team/pair/exchange", json={})
    assert 429 in codes, (
        f"T11 既有行为不回归：pair/exchange 应仍受限，实测序列: {codes}")


# ═══ 6. 限速关闭时全部放行 ═══

def test_rate_limit_disabled_all_pass(gate, monkeypatch):
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", 0)
    codes = _codes(gate, "post", "/api/v1/wiki/import", json={"pages": {}})
    assert 429 not in codes, (
        f"RATE_LIMIT_PER_IP=0 应全部放行，实测序列: {codes}")


# ═══ 7. RATE_LIMITED_ALLOWLIST 成员断言（防误删哨兵） ═══

def test_rate_limited_allowlist_membership():
    """恰好包含这 4 条——多/少都要报，给后来者防误删哨兵。"""
    expected = {
        "/api/v1/team/pair/exchange",
        "/api/v1/wiki/import",
        "/api/v1/wiki/export",
        "/api/v1/federation/snapshot",
    }
    actual = set(routes.RATE_LIMITED_ALLOWLIST)
    assert actual == expected, (
        f"RATE_LIMITED_ALLOWLIST 应恰好包含这 4 条 {sorted(expected)}，"
        f"实际 {sorted(actual)}（多/少都视为不一致）")


# ═══ 8. 超限审计仍落 ═══

def test_rate_limit_audit_event(monkeypatch):
    """超限审计：_audit_rate_limit → hub._log_event("rate_limit_hit", "__http_gate__", …)。

    可行性说明：_audit_rate_limit 经 asyncio.create_task 异步触发，TestClient
    请求返回后不保证 task 已完成；此处直调 _rate_limit_ok + await sleep(0) 刷任务，
    断言假 hub 收到 rate_limit_hit —— 与用例 1-6 的 TestClient 路径互补（那里验 429
    状态码，这里验审计副作用）。
    """
    routes._rate_hits.clear()
    monkeypatch.setattr(CONFIG, "RATE_LIMIT_PER_IP", 1)

    class _FakeHub:
        def __init__(self):
            self.events = []

        async def _log_event(self, event_type, agent_id, payload):
            self.events.append((event_type, agent_id, payload))

    fake = _FakeHub()
    monkeypatch.setattr(routes, "hub", fake)
    scope = {"type": "http", "path": "/api/v1/wiki/import",
             "headers": [], "client": ("10.99.0.8", 12345)}

    async def _scenario():
        assert await routes._rate_limit_ok(scope, "/api/v1/wiki/import") is True
        assert await routes._rate_limit_ok(scope, "/api/v1/wiki/import") is False
        await asyncio.sleep(0.05)

    asyncio.run(_scenario())
    hits = [e for e in fake.events if e[0] == "rate_limit_hit"]
    assert hits, f"超限应落 rate_limit_hit 审计，实收: {fake.events}"
    assert hits[0][1] == "__http_gate__"
    assert hits[0][2].get("path") == "/api/v1/wiki/import"
