# -*- coding: utf-8 -*-
"""tests/test_cd083_request_id.py — CD-083：请求级关联 + 敏感字段脱敏 + 启动横幅

覆盖：
- X-Request-ID 中间件：入站回显 / 缺省生成 uuid4 hex / 注入字符白名单清洗 / 超长截断
- logfmt：request_id 进 JSON 日志字段；mask_sensitive 递归打码（api_key/token/password/secret）；
  JsonFormatter 对 extra_fields 统一脱敏
- main.startup_banner：含版本/host/port/TLS/auth 模式/registration/可信代理数，
  且不含 hub_token 明文
- routes._bg_task：后台任务入口生成内部 request id（bg-<name>-*）
"""
import asyncio
import io
import json
import logging
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logfmt import (JsonFormatter, log_event, mask_sensitive, sanitize_request_id,
                    set_request_id, get_request_id)


# ============ X-Request-ID 中间件（TestClient，NO_AUTH 测试态） ============

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from routes import app
    return TestClient(app)


def test_request_id_echoed(client):
    """入站带合法 X-Request-ID → 响应头原样回显。"""
    r = client.get("/health", headers={"X-Request-ID": "req-abc_XYZ123"})
    assert r.status_code == 200
    assert r.headers.get("x-request-id") == "req-abc_XYZ123"


def test_request_id_generated_when_absent(client):
    """入站无 X-Request-ID → 生成 uuid4 hex（32 位）并写回响应头。"""
    r = client.get("/health")
    rid = r.headers.get("x-request-id", "")
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid), \
        f"缺省应生成 uuid4 hex，实际 {rid!r}"


def test_request_id_injection_sanitized(client):
    """入站含 \\r\\n/空白等注入字符 → 白名单清洗（防日志注入/响应头拆分）。"""
    r = client.get("/health", headers={"X-Request-ID": "bad\r\ninj ect\tid"})
    rid = r.headers.get("x-request-id", "")
    assert rid == "badinjectid", f"注入字符应被白名单剥掉，实际 {rid!r}"


def test_request_id_overlong_truncated():
    """白名单清洗同时截断到 64 字符。"""
    assert sanitize_request_id("a" * 100) == "a" * 64
    assert sanitize_request_id("") == ""
    assert sanitize_request_id("!!!") == ""  # 清洗后为空 → 调用方生成 uuid4


# ============ logfmt：request_id 字段 + 敏感字段脱敏 ============

def _capture_logger():
    stream = io.StringIO()
    lg = logging.getLogger("cd083.test")
    lg.handlers = []
    h = logging.StreamHandler(stream)
    h.setFormatter(JsonFormatter())
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, stream


def test_json_log_carries_request_id():
    lg, stream = _capture_logger()
    set_request_id("rid-123")
    try:
        log_event(lg, "info", "hello")
    finally:
        set_request_id("")
    entry = json.loads(stream.getvalue())
    assert entry["request_id"] == "rid-123"


def test_log_event_masks_sensitive_fields():
    """log_event/JsonFormatter 输出前统一脱敏：敏感键值变 ***，非敏感字段原样。"""
    lg, stream = _capture_logger()
    log_event(lg, "info", "login", api_key="sekret-key", path="/api/v1/mem",
              nested={"hub_token": "tok-123", "note": "ok"})
    out = stream.getvalue()
    assert "sekret-key" not in out and "tok-123" not in out
    entry = json.loads(out)
    assert entry["api_key"] == "***"
    assert entry["nested"] == {"hub_token": "***", "note": "ok"}
    assert entry["path"] == "/api/v1/mem"


def test_mask_sensitive_recursive():
    data = {"password": "p", "items": [{"SECRET": "s"}, "plain"], "n": 1}
    masked = mask_sensitive(data)
    assert masked == {"password": "***", "items": [{"SECRET": "***"}, "plain"], "n": 1}
    assert data["password"] == "p", "不得改原对象（返回新结构）"


# ============ 后台任务内部 request id ============

def test_bg_task_generates_internal_request_id():
    import routes
    seen = {}

    async def probe():
        seen["rid"] = get_request_id()

    asyncio.run(routes._bg_task("probe", probe()))
    assert seen["rid"].startswith("bg-probe-"), f"后台任务应带内部 request id，实际 {seen!r}"


# ============ 启动横幅 ============

def _fake_cfg(**over):
    base = dict(AUTH_MODE="local", AUTH_REGISTRATION="guarded",
                TRUSTED_PROXIES=["10.0.0.0/8"], HUB_TOKEN="supersecret-token")
    base.update(over)
    return types.SimpleNamespace(**base)


def test_startup_banner_contains_key_items_and_masks_token():
    import main
    from models import HUB_VERSION
    b = main.startup_banner("0.0.0.0", 3060, False, _fake_cfg())
    assert HUB_VERSION in b and "0.0.0.0" in b and "3060" in b
    assert "tls=off" in b and "auth_mode=local" in b
    assert "registration=guarded" in b and "trusted_proxies=1" in b
    # 敏感值打码：hub_token 明文绝不出现在横幅
    assert "supersecret-token" not in b
    assert "hub_token=***" in b


def test_startup_banner_empty_token_not_leaked():
    import main
    b = main.startup_banner("127.0.0.1", 3060, True, _fake_cfg(HUB_TOKEN=""))
    assert "hub_token=(empty)" in b and "tls=on" in b
