"""L7: 传输层审计 — dispatch/result/ack 全帧落库"""
import logging
logger = logging.getLogger("xingshu.transport_audit")

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

AUDIT_DIR = "audit"
TRANSPORT_AUDIT_FILE = os.path.join(AUDIT_DIR, "transport.jsonl")

# S2：jsonl 滚动链（窗口 hash 挂 audit_log 主链；DB 不可用静默跳过）
_rolling_chain = None

def _get_rolling_chain():
    global _rolling_chain
    if _rolling_chain is None:
        try:
            from audit_chain import JsonlRollingChain
            db_path = ""
            try:
                from models import CONFIG
                db_path = CONFIG.DB_PATH
            except Exception:
                db_path = os.environ.get("SYNC_HUB_DB", "")
            if db_path:
                _rolling_chain = JsonlRollingChain(db_path, TRANSPORT_AUDIT_FILE, "transport.jsonl")
        except Exception:
            _rolling_chain = None
    return _rolling_chain


def _ensure_dir():
    os.makedirs(AUDIT_DIR, exist_ok=True)


def log_transport_frame(direction: str, envelope: dict):
    """L7: 记录传输层帧到审计日志。direction: 'in' | 'out'"""
    _ensure_dir()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "type": envelope.get("type", "?"),
        "id": envelope.get("id", ""),
        "session_id": envelope.get("session_id", ""),
        "via": envelope.get("via", "?"),
        "envelope_ts": envelope.get("ts", 0),
        "payload_keys": list(envelope.get("payload", {}).keys()) if isinstance(envelope.get("payload"), dict) else [],
    }
    try:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # S2：滚动链统一写入（写文件 + 窗口锚定）；链不可用时降级原样写
        ch = _get_rolling_chain()
        if ch:
            ch.append_line(line)
        else:
            with open(TRANSPORT_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        # L7-T3: audit write failure must not block business
        _fallback_local(entry)


def _fallback_local(entry: dict):
    """L7-T3: 降级 — 写本地文件"""
    fallback = f"audit/transport_fallback_{int(time.time())}.jsonl"
    try:
        with open(fallback, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as _exc:
        logger.warning("transport_audit silent-except @69: %s", _exc)


def log_dispatch_out(envelope: dict):
    log_transport_frame("out", envelope)


def log_dispatch_in(envelope: dict):
    log_transport_frame("in", envelope)


def log_result_out(envelope: dict):
    log_transport_frame("out", envelope)


def log_ack_in(envelope: dict):
    log_transport_frame("in", envelope)


def log_ping_pong(direction: str, envelope: dict):
    """L7: ping/pong frames also audited"""
    log_transport_frame(direction, envelope)
