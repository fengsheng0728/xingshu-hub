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
import time
from contextvars import ContextVar

trace_id_var: ContextVar = ContextVar("xingshu_trace_id", default="")


def set_trace_id(tid: str):
    """设置当前上下文 trace_id（中间件在请求入口调用）。"""
    trace_id_var.set(tid or "")


def get_trace_id() -> str:
    """读取当前上下文 trace_id（日志/审计写入点调用）。"""
    return trace_id_var.get()


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
        # 附加字段（logger.info("msg", extra={...}) 传入的 dict）
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
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
