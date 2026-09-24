# -*- coding: utf-8 -*-
"""
CD-085(c)：LLM 调用级 trace 的 OTLP 导出 —— 单测

约束对齐任务书 T11：
  - 禁止安装 OTel：全部用「注入假模块 / 阻断 import」验证，不 pip install
  - 禁止真实外呼 LLM：httpx 全 mock
  - 禁止记录 prompt/response 全文：入参与 span 属性双侧断言
  - 例 2（装了但开关关）与例 3（装了+开关开）结果必须可区分（False vs True）

覆盖 7 例（任务书 3.4 要求 ≥6）：
  1. 未装 OTel 时降级 no-op
  2. 装了但开关关 → 默认关闭
  3. 装了 + SYNC_HUB_LLM_TRACE=1 → True + span 属性落盘
  4. OTEL_EXPORTER_OTLP_ENDPOINT 单独存在也能开
  5. 数据边界：record_llm_result 不含 prompt/response 文本字段
  6. 埋点不改控制流（hub_agent 上一层 mock client.post）
  7. 开关开但 OTel 未装 → False（两条件与关系）
"""
import asyncio
import importlib
import inspect
import sqlite3
import sys
import types

import pytest

import tracing  # noqa: F401  先红：ModuleNotFoundError: No module named 'tracing'


# ────────────────────────── 假 OTel 基建 ──────────────────────────

_OTEL_MODULE_NAMES = (
    "opentelemetry",
    "opentelemetry.trace",
    "opentelemetry.sdk",
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.trace.export",
    "opentelemetry.sdk.resources",
    "opentelemetry.exporter",
    "opentelemetry.exporter.otlp",
    "opentelemetry.exporter.otlp.proto",
    "opentelemetry.exporter.otlp.proto.http",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
)


class _FakeSpan:
    def __init__(self, name: str):
        self.name = name
        self.attributes = {}
        self.ended = False
        self.status = None
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, *args, **kwargs):
        self.status = args

    def record_exception(self, exc, *args, **kwargs):
        self.exceptions.append(exc)

    def end(self):
        self.ended = True


class _FakeTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, *args, **kwargs):
        sp = _FakeSpan(name)
        self.spans.append(sp)
        return sp


class _FakeTracerProvider:
    last = None

    def __init__(self, **kwargs):
        self.resource = kwargs.get("resource")
        self.processors = []
        self.shutdown_called = False
        self._tracer = _FakeTracer()
        _FakeTracerProvider.last = self

    def add_span_processor(self, processor):
        self.processors.append(processor)

    def get_tracer(self, name, *args, **kwargs):
        return self._tracer

    def shutdown(self):
        self.shutdown_called = True


class _FakeBatchSpanProcessor:
    def __init__(self, exporter):
        self.exporter = exporter

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class _FakeOTLPSpanExporter:
    last_kwargs = None

    def __init__(self, **kwargs):
        _FakeOTLPSpanExporter.last_kwargs = kwargs

    def shutdown(self):
        pass


class _FakeResource:
    def __init__(self, attrs=None):
        self.attributes = dict(attrs or {})

    @classmethod
    def create(cls, attrs):
        return cls(attrs)


def _block_otel(monkeypatch):
    """模拟「未安装 OTel」：sys.modules 相关项设 None → import 即 ImportError。"""
    for name in _OTEL_MODULE_NAMES:
        monkeypatch.setitem(sys.modules, name, None)


def _install_fake_otel(monkeypatch):
    """模拟「装了 OTel」：注入假模块（不 pip install）。"""
    fake_otel = types.ModuleType("opentelemetry")
    fake_trace = types.ModuleType("opentelemetry.trace")
    fake_sdk = types.ModuleType("opentelemetry.sdk")
    fake_sdk_trace = types.ModuleType("opentelemetry.sdk.trace")
    fake_sdk_trace_export = types.ModuleType("opentelemetry.sdk.trace.export")
    fake_sdk_resources = types.ModuleType("opentelemetry.sdk.resources")
    fake_exporter = types.ModuleType("opentelemetry.exporter")
    fake_otlp = types.ModuleType("opentelemetry.exporter.otlp")
    fake_otlp_proto = types.ModuleType("opentelemetry.exporter.otlp.proto")
    fake_otlp_http = types.ModuleType("opentelemetry.exporter.otlp.proto.http")
    fake_otlp_http_te = types.ModuleType(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter")

    fake_otel.trace = fake_trace
    fake_sdk_trace.TracerProvider = _FakeTracerProvider
    fake_sdk_trace_export.BatchSpanProcessor = _FakeBatchSpanProcessor
    fake_sdk_resources.Resource = _FakeResource
    fake_otlp_http_te.OTLPSpanExporter = _FakeOTLPSpanExporter

    mapping = {
        "opentelemetry": fake_otel,
        "opentelemetry.trace": fake_trace,
        "opentelemetry.sdk": fake_sdk,
        "opentelemetry.sdk.trace": fake_sdk_trace,
        "opentelemetry.sdk.trace.export": fake_sdk_trace_export,
        "opentelemetry.sdk.resources": fake_sdk_resources,
        "opentelemetry.exporter": fake_exporter,
        "opentelemetry.exporter.otlp": fake_otlp,
        "opentelemetry.exporter.otlp.proto": fake_otlp_proto,
        "opentelemetry.exporter.otlp.proto.http": fake_otlp_http,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter": fake_otlp_http_te,
    }
    for name, mod in mapping.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _fresh_tracing(monkeypatch, otel: str):
    """重新加载 tracing 模块，按 otel='blocked'|'fake' 决定依赖可见性。"""
    monkeypatch.delenv("SYNC_HUB_LLM_TRACE", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("SYNC_HUB_OTEL_SERVICE_NAME", raising=False)
    if otel == "blocked":
        _block_otel(monkeypatch)
    else:
        _install_fake_otel(monkeypatch)
    sys.modules.pop("tracing", None)
    return importlib.import_module("tracing")


def _clear_trace_switches(monkeypatch):
    monkeypatch.delenv("SYNC_HUB_LLM_TRACE", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)


# ────────────────────────── 1. 未装 OTel 降级 ──────────────────────────

def test_1_degrade_without_otel(monkeypatch):
    tr = _fresh_tracing(monkeypatch, otel="blocked")
    _clear_trace_switches(monkeypatch)

    assert tr.llm_trace_enabled() is False

    with tr.llm_span("deepseek", "deepseek-chat") as sp:
        # 哑对象：不抛即可，且 record 不抛
        tr.record_llm_result(sp, latency_ms=1.0, status="ok")
    assert True  # with 正常退出


# ────────────────────────── 2. 装了但默认关 ──────────────────────────

def test_2_installed_but_switch_off(monkeypatch):
    tr = _fresh_tracing(monkeypatch, otel="fake")
    _clear_trace_switches(monkeypatch)

    assert tr.llm_trace_enabled() is False


# ────────────────────────── 3. 装了 + 开关开 ──────────────────────────

def test_3_installed_and_switch_on(monkeypatch):
    tr = _fresh_tracing(monkeypatch, otel="fake")
    monkeypatch.setenv("SYNC_HUB_LLM_TRACE", "1")
    _FakeTracerProvider.last = None

    assert tr.llm_trace_enabled() is True

    with tr.llm_span("deepseek", "deepseek-chat", span_name="llm.chat") as sp:
        tr.record_llm_result(
            sp,
            latency_ms=12.5,
            request_chars=120,
            response_chars=80,
            prompt_tokens=15,
            completion_tokens=7,
            status="ok",
        )

    attrs = sp.attributes
    assert attrs["llm.provider"] == "deepseek"
    assert attrs["llm.model"] == "deepseek-chat"
    assert attrs["llm.latency_ms"] == 12.5
    assert attrs["llm.request_chars"] == 120
    assert attrs["llm.response_chars"] == 80
    assert attrs["llm.prompt_tokens"] == 15
    assert attrs["llm.completion_tokens"] == 7
    assert attrs["llm.status"] == "ok"
    assert "llm.error" not in attrs
    assert sp.name == "llm.chat"
    assert sp.ended is True


# ────────────────────────── 4. OTLP endpoint 单独可开 ──────────────────────────

def test4_otlp_endpoint_alone_enables(monkeypatch):
    tr = _fresh_tracing(monkeypatch, otel="fake")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert tr.llm_trace_enabled() is True

    with tr.llm_span("openai", "gpt-4o-mini") as sp:
        tr.record_llm_result(sp, latency_ms=3.0, status="ok")
    assert sp.attributes["llm.status"] == "ok"


# ────────────────────────── 5. 数据边界 ──────────────────────────

def test_5_data_boundary_no_prompt_response_text(monkeypatch):
    tr = _fresh_tracing(monkeypatch, otel="fake")
    monkeypatch.setenv("SYNC_HUB_LLM_TRACE", "1")

    sig = inspect.signature(tr.record_llm_result)
    param_names = set(sig.parameters)
    # 只允许长度/token/延迟/状态/模型名/provider/错误类型；不含任何 prompt/response 文本入参
    assert param_names == {
        "span", "latency_ms", "request_chars", "response_chars",
        "prompt_tokens", "completion_tokens", "status", "error",
    }
    forbidden_params = {
        "prompt", "response", "messages", "content",
        "prompt_text", "response_text", "request_text",
        "request_body", "response_body", "input", "output",
    }
    assert not (param_names & forbidden_params)

    with tr.llm_span("deepseek", "deepseek-chat") as sp:
        tr.record_llm_result(sp, latency_ms=1.0, request_chars=10,
                             response_chars=20, prompt_tokens=1, completion_tokens=2)
    forbidden_attr_keys = {
        "llm.prompt", "llm.response", "llm.prompt_text", "llm.response_text",
        "llm.messages", "llm.content", "llm.request_body", "llm.response_body",
    }
    assert not (set(sp.attributes) & forbidden_attr_keys)


# ────────────────────────── 6. 埋点不改控制流 ──────────────────────────

class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def _mock_httpx_post(monkeypatch, *, exc=None, resp=None):
    import httpx

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            if exc is not None:
                raise exc
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


def _make_hub_agent(tmp_path, monkeypatch):
    import hub_agent

    monkeypatch.delenv("SYNC_HUB_LLM_API_KEY", raising=False)
    db_path = str(tmp_path / "hub_trace.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hub_agent_config "
        "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )
    rows = {
        "provider": "deepseek",
        "api_key": "test-key-123456",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "temperature": "0.3",
        "enabled": "true",
        "auto_approve": "false",
    }
    for k, v in rows.items():
        conn.execute(
            "INSERT OR REPLACE INTO hub_agent_config (key, value, updated_at) VALUES (?, ?, '')",
            (k, v),
        )
    conn.commit()
    conn.close()
    return hub_agent.HubAgent(db_path)


def test_6_instrumentation_preserves_control_flow(monkeypatch, tmp_path):
    """hub_agent 上一层：异常照常被原外层 handler 接到、正常返回值不变；record 收到 status。"""
    import hub_agent
    tr_mod = hub_agent.tracing  # 必须补在 hub_agent 实际引用的 tracing 模块上

    agent = _make_hub_agent(tmp_path, monkeypatch)

    record_calls = []

    def _spy(span, **kwargs):
        record_calls.append(kwargs)

    monkeypatch.setattr(tr_mod, "record_llm_result", _spy)

    # ── 异常路径：原异常仍经原外层 handler 变成 error dict（控制流未变）──
    record_calls.clear()
    _mock_httpx_post(monkeypatch, exc=ValueError("boom-llm"))
    result = asyncio.run(agent.test_connection())
    assert result["status"] == "error"
    assert "boom-llm" in result["error"]  # 原异常信息原样带出（raise 后被原 except 接住）
    assert len(record_calls) == 1
    assert record_calls[0]["status"] == "error"
    assert record_calls[0]["error"] == "ValueError"

    # ── 正常路径：返回值结构不变 ──
    record_calls.clear()
    ok_resp = _FakeResp(
        status_code=200,
        json_data={"choices": [{"message": {"content": "pong"}}], "usage": {}},
        text='{"choices":[]}',
    )
    _mock_httpx_post(monkeypatch, resp=ok_resp)
    result = asyncio.run(agent.test_connection())
    assert result["status"] == "ok"
    assert result["model"] == "deepseek-chat"
    assert result["provider"] == "deepseek"
    assert result["response"] == "pong"
    assert len(record_calls) == 1
    assert record_calls[0]["status"] == "ok"
    assert record_calls[0]["error"] is None


# ────────────────────────── 7. 开关开但 OTel 未装 ──────────────────────────

def test_7_switch_on_without_otel_is_false(monkeypatch):
    tr = _fresh_tracing(monkeypatch, otel="blocked")
    monkeypatch.setenv("SYNC_HUB_LLM_TRACE", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")

    assert tr.llm_trace_enabled() is False


# ────────────────────────── 8/9. routes_report 埋点（CD-085c 5/5） ──────────────────────────

def _setup_report_env(monkeypatch):
    """routes_report api_daily_report 调用环境：假 hub_agent / 假 stats / 假 company name。"""
    import routes_report
    import hub_agent as hub_agent_mod

    class _FakeHubAgent:
        def is_configured(self):
            return True

        def _get_config(self):
            return {
                "provider": "deepseek",
                "api_key": "test-key-123456",
                "api_base": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
            }

    monkeypatch.setattr(routes_report, "hub_agent", _FakeHubAgent())
    monkeypatch.setattr(hub_agent_mod, "_get_company_name", lambda: "TestCo")
    monkeypatch.setattr(routes_report, "_daily_report_stats_sync", lambda: {
        "date": "2026-09-24",
        "tasks": {"by_status": {}, "done": 1, "failed": 0, "active": 0},
        "memories": 0,
        "disclosures": {"total": 0, "pending": 0},
        "agents": {"online": 1, "total": 1},
        "top_tags": [],
        "knowledge_base": 0,
    })
    return routes_report


def test_8_report_llm_trace_success(monkeypatch):
    """routes_report chat/completions 成功路径 → 假 span 收到预期 attributes（span_name=llm.report）。"""
    tr = _fresh_tracing(monkeypatch, otel="fake")
    monkeypatch.setenv("SYNC_HUB_LLM_TRACE", "1")
    _FakeTracerProvider.last = None

    routes_report = _setup_report_env(monkeypatch)
    monkeypatch.setattr(routes_report, "tracing", tr, raising=False)

    ok_resp = _FakeResp(
        status_code=200,
        json_data={"choices": [{"message": {"content": "今日一切正常。"}}]},
        text='{"choices":[{"message":{"content":"今日一切正常。"}}]}',
    )
    _mock_httpx_post(monkeypatch, resp=ok_resp)

    result = asyncio.run(routes_report.api_daily_report())

    assert result["status"] == "ok"
    assert result["summary"] == "今日一切正常。"

    provider = _FakeTracerProvider.last
    assert provider is not None, "应创建 FakeTracerProvider（埋点未触发）"
    spans = provider._tracer.spans
    assert len(spans) == 1, f"应恰好 1 个 span，实际 {len(spans)}"
    sp = spans[0]
    assert sp.name == "llm.report"
    assert sp.attributes["llm.provider"] == "deepseek"
    assert sp.attributes["llm.model"] == "deepseek-chat"
    assert sp.attributes["llm.status"] == "ok"
    assert sp.attributes["llm.request_chars"] > 0
    assert sp.ended is True


def test_9_report_llm_trace_failure_preserves_control_flow(monkeypatch):
    """routes_report chat/completions 失败路径 → status=error，外层行为不变（summary 形态）。"""
    tr = _fresh_tracing(monkeypatch, otel="fake")
    monkeypatch.setenv("SYNC_HUB_LLM_TRACE", "1")
    _FakeTracerProvider.last = None

    routes_report = _setup_report_env(monkeypatch)
    monkeypatch.setattr(routes_report, "tracing", tr, raising=False)

    _mock_httpx_post(monkeypatch, exc=ValueError("boom-report"))

    result = asyncio.run(routes_report.api_daily_report())

    # 控制流不变：外层 handler 捕获异常，summary 仍为固定字符串形态
    assert result["summary"].startswith("(AI 摘要生成失败: "), \
        f"summary 形态应不变，实际: {result['summary']!r}"
    assert "boom-report" in result["summary"]

    # 埋点记录 status=error
    provider = _FakeTracerProvider.last
    assert provider is not None, "应创建 FakeTracerProvider（埋点未触发）"
    spans = provider._tracer.spans
    assert len(spans) == 1, f"应恰好 1 个 span，实际 {len(spans)}"
    sp = spans[0]
    assert sp.name == "llm.report"
    assert sp.attributes["llm.status"] == "error"
    assert sp.attributes["llm.error"] == "ValueError"
    assert sp.ended is True
