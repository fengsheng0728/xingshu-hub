# -*- coding: utf-8 -*-
"""
tracing.py — LLM 调用级 trace 的 OTLP 导出封装（CD-085(c)）

设计（任务书 T11 硬性约束）：
  1. 可选依赖：未安装 OTel 时全模块降级 no-op，不抛异常、不改变任何调用点行为
  2. 默认关闭：开关未打开时 llm_span 返回惰性 no-op 上下文对象，零开销
  3. 数据边界：只记长度/token 用量/延迟/状态/模型名/provider/错误类型，
     禁止记录 prompt / response 全文（隐私与「可审计但不外泄」）
  4. 惰性初始化：import 期不做网络连接 / 读库 / 起线程，首次用到才建 provider

开关（两个都要，任一满足即开，且仍要求装了 OTel）：
  - env SYNC_HUB_LLM_TRACE=1
  - 或 env OTEL_EXPORTER_OTLP_ENDPOINT 非空（OTel 标准变量，存在即视为「要导出」）

后端：标准 OTLP exporter（opentelemetry-exporter-otlp-proto-http）；
endpoint 取 OTEL_EXPORTER_OTLP_ENDPOINT；service.name 取 SYNC_HUB_OTEL_SERVICE_NAME
（默认 "xingshu-sync-hub"）。任何 OTLP 接收端均可：本地 collector / Jaeger / 自托管 Langfuse。
"""
import os

try:  # 可选依赖：缺任一 OTel 组件即全模块降级
    from opentelemetry import trace as _otel_trace  # noqa: F401
    from opentelemetry.sdk.trace import TracerProvider as _TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor as _BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as _OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource as _Resource
    _OTEL_AVAILABLE = True
except Exception:  # ImportError 及任何初始化失败 → 降级
    _otel_trace = None
    _TracerProvider = None
    _BatchSpanProcessor = None
    _OTLPSpanExporter = None
    _Resource = None
    _OTEL_AVAILABLE = False

_TRACER_NAME = "xingshu-sync-hub"
_DEFAULT_SERVICE_NAME = "xingshu-sync-hub"

# span 属性键（数据边界：只记长度/token/延迟/状态/模型名/provider/错误类型）
ATTR_PROVIDER = "llm.provider"
ATTR_MODEL = "llm.model"
ATTR_LATENCY_MS = "llm.latency_ms"
ATTR_REQUEST_CHARS = "llm.request_chars"
ATTR_RESPONSE_CHARS = "llm.response_chars"
ATTR_PROMPT_TOKENS = "llm.prompt_tokens"
ATTR_COMPLETION_TOKENS = "llm.completion_tokens"
ATTR_STATUS = "llm.status"
ATTR_ERROR = "llm.error"

_tracer = None
_provider = None
_init_done = False


class _NoopSpan:
    """哑 span：所有写入均为 no-op。"""

    def set_attribute(self, key, value):
        pass

    def set_status(self, *args, **kwargs):
        pass

    def record_exception(self, *args, **kwargs):
        pass

    def end(self):
        pass


class _NoopSpanContext:
    """惰性 no-op 上下文管理器：__enter__ 返回哑对象，__exit__ 返回 False。"""

    def __enter__(self):
        return _NoopSpan()

    def __exit__(self, exc_type, exc, tb):
        return False


class _OtelSpanContext:
    """真 span 上下文管理器：__enter__ 建 span 并写基础属性，__exit__ end 且不吞异常。"""

    def __init__(self, name, provider, model, attrs):
        self._name = name
        self._provider = provider
        self._model = model
        self._attrs = attrs
        self._span = None

    def __enter__(self):
        try:
            tracer = _ensure_tracer()
            if tracer is None:
                return _NoopSpan()
            self._span = tracer.start_span(self._name)
            _safe_set(self._span, ATTR_PROVIDER, self._provider)
            _safe_set(self._span, ATTR_MODEL, self._model)
            for key, value in self._attrs.items():
                _safe_set(self._span, key, value)
            return self._span
        except Exception:
            self._span = None
            return _NoopSpan()

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._span is not None:
                self._span.end()
        except Exception:
            pass
        return False  # 不吞异常，控制流保持不变


def _safe_set(span, key, value):
    if span is None or value is None:
        return
    try:
        span.set_attribute(key, value)
    except Exception:
        pass


def llm_trace_enabled() -> bool:
    """读开关；未装 OTel 时恒 False（两条件与关系）。"""
    if not _OTEL_AVAILABLE:
        return False
    flag = os.environ.get("SYNC_HUB_LLM_TRACE", "").strip()
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return flag == "1" or bool(endpoint)


def llm_span(provider: str, model: str, **attrs):
    """LLM 调用级 span 上下文管理器；disabled / 降级时是 no-op。

    attrs 可含 span_name（默认 "llm.chat"）；其余键值写入 span attributes。
    """
    name = attrs.pop("span_name", "llm.chat")
    if not llm_trace_enabled():
        return _NoopSpanContext()
    return _OtelSpanContext(name, provider, model, attrs)


def record_llm_result(span, *, latency_ms, request_chars=None, response_chars=None,
                      prompt_tokens=None, completion_tokens=None, status="ok",
                      error=None) -> None:
    """把 LLM 调用结果写入 span attributes。

    数据边界（硬性）：只接受长度 / token 用量 / 延迟 / 状态 / 错误类型，
    不接受 prompt / response 文本字段。写入失败静默忽略，绝不影响调用点。
    """
    if span is None:
        return
    try:
        _safe_set(span, ATTR_LATENCY_MS, latency_ms)
        _safe_set(span, ATTR_REQUEST_CHARS, request_chars)
        _safe_set(span, ATTR_RESPONSE_CHARS, response_chars)
        _safe_set(span, ATTR_PROMPT_TOKENS, prompt_tokens)
        _safe_set(span, ATTR_COMPLETION_TOKENS, completion_tokens)
        _safe_set(span, ATTR_STATUS, status)
        _safe_set(span, ATTR_ERROR, error)
    except Exception:
        pass


def shutdown_tracing() -> None:
    """进程退出前 flush（main.py 可不必调用，但函数要有）。"""
    global _tracer, _provider, _init_done
    try:
        if _provider is not None:
            _provider.shutdown()
    except Exception:
        pass
    _tracer = None
    _provider = None
    _init_done = False


def _ensure_tracer():
    """惰性初始化：首次用到才建 provider / exporter（import 期零副作用）。"""
    global _tracer, _provider, _init_done
    if _init_done:
        return _tracer
    _init_done = True
    if not _OTEL_AVAILABLE:
        _tracer = None
        return None
    try:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        service_name = (
            os.environ.get("SYNC_HUB_OTEL_SERVICE_NAME", "").strip()
            or _DEFAULT_SERVICE_NAME
        )
        resource = _Resource.create({"service.name": service_name})
        provider = _TracerProvider(resource=resource)
        exporter_kwargs = {}
        if endpoint:
            exporter_kwargs["endpoint"] = endpoint
        exporter = _OTLPSpanExporter(**exporter_kwargs)
        provider.add_span_processor(_BatchSpanProcessor(exporter))
        _provider = provider
        _tracer = provider.get_tracer(_TRACER_NAME)
    except Exception:
        _provider = None
        _tracer = None
    return _tracer
