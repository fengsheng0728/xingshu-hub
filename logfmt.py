"""P1 O1 可观测性（2026-08-04）：轻量结构化 JSON 日志 + trace_id 贯穿。

设计取舍：
- 不引入 structlog（依赖 + 全量改造成本高）；用标准 logging + JSONFormatter。
- 输出 stdout，由企业 ELK/Splunk 采集（内网标准做法，不自建日志存储）。
- 统一字段：ts / level / module / trace_id / principal / agent_id / msg。
- trace_id 由 routes.py 中间件生成/透传（X-Trace-Id header），全局 ContextVar 传递，
  审计事件（_log_event / disclosure _log_disclosure）读取同一 trace_id 落库——
  一次跨 Agent 越权可一条 ID 串 请求→规则判定→审计 全链路。
"""
import json
import logging
import re
import time
import uuid
from contextvars import ContextVar

trace_id_var: ContextVar = ContextVar("xingshu_trace_id", default="")

# CD-083（2026-09-23）：请求级关联 — X-Request-ID 与 trace_id 并列进日志上下文。
# trace_id 面向跨 Agent 审计串联；request_id 面向单次 HTTP 请求排障（响应头回显）。
request_id_var: ContextVar = ContextVar("xingshu_request_id", default="")

# 入站 X-Request-ID 字符白名单（防 \r\n 等控制字符注入日志/响应头）
_REQUEST_ID_RE = re.compile(r"[^A-Za-z0-9_-]")

# CD-083：敏感键打码 — 键名含 api_key/token/password/secret 的值一律替换（大小写不敏感）
_SENSITIVE_KEY_RE = re.compile(r"(api_key|token|password|secret)", re.IGNORECASE)
MASK_PLACEHOLDER = "***"


def set_trace_id(tid: str):
    """设置当前上下文 trace_id（中间件在请求入口调用）。"""
    trace_id_var.set(tid or "")


def get_trace_id() -> str:
    """读取当前上下文 trace_id（日志/审计写入点调用）。"""
    return trace_id_var.get()


def set_request_id(rid: str):
    """CD-083：设置当前上下文 request_id（X-Request-ID 中间件/后台任务入口调用）。"""
    request_id_var.set(rid or "")


def get_request_id() -> str:
    """CD-083：读取当前上下文 request_id（JSON 日志字段）。"""
    return request_id_var.get()


def sanitize_request_id(raw: str) -> str:
    """CD-083：入站 X-Request-ID 白名单清洗（仅 [A-Za-z0-9_-]，最长 64）。

    返回清洗后的 id；入站为空或清洗后为空 → 返回 ""（调用方生成 uuid4 hex）。
    """
    return _REQUEST_ID_RE.sub("", (raw or ""))[:64]


def rotate_request_id(prefix: str = "bg") -> str:
    """CD-108：后台循环每轮 tick 轮换 request id（返回新 id）。

    与 set_request_id 的差别只在「一行生成 + 写入」，供长跑循环在每轮开头调用，
    使同一次循环的不同 tick 在日志里可区分（此前一个循环只挂一个 id）。
    """
    rid = f"{prefix}-{uuid.uuid4().hex[:12]}"
    request_id_var.set(rid)
    return rid


def mask_sensitive(data):
    """CD-083：递归打码敏感键值 — 键名含 api_key/token/password/secret 的值替换为 ***。

    dict / list / tuple 递归下行；其余原样返回。用于日志/横幅等输出前脱敏。
    """
    if isinstance(data, dict):
        return {k: (MASK_PLACEHOLDER if _SENSITIVE_KEY_RE.search(str(k))
                    else mask_sensitive(v)) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask_sensitive(v) for v in data]
    return data


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器 — 单行输出，字段统一。"""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
        }
        tid = get_trace_id()
        if tid:
            entry["trace_id"] = tid
        rid = get_request_id()
        if rid:
            entry["request_id"] = rid
        # 附加字段（logger.info("msg", extra={...}) 传入的 dict）；
        # CD-083：输出前统一过敏感键打码（api_key/token/password/secret → ***）
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for k, v in mask_sensitive(extra).items():
                entry[k] = v
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """获取结构化 JSON logger（输出 stdout，自动附加 trace_id）。"""
    logger = logging.getLogger(name)
    if not getattr(logger, "_xingshu_json", False):
        # 只给根 logger 挂一次 handler；子 logger 传播到根
        _root = logging.getLogger("xingshu")
        if not getattr(_root, "_xingshu_json", False):
            _root.setLevel(logging.INFO)
            handler = logging.StreamHandler()  # stderr 默认；生产由容器采集
            handler.setFormatter(JsonFormatter())
            _root.addHandler(handler)
            _root._xingshu_json = True
        logger._xingshu_json = True
    return logger


def log_event(logger: logging.Logger, level: str, msg: str, **fields):
    """带 extra 字段的结构化日志（fields 进 JSON 输出）。"""
    extra = {"extra_fields": fields}
    getattr(logger, level)(msg, extra=extra)
