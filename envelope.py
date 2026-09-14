"""
星枢 WS 传输层信封模块 · L0
结构化 envelope 替代平铺消息，根治 phase 7 type 碰撞。

envelope schema:
  type:      "dispatch" | "result" | "ack" | "ping" | "pong" | "hello"
  id:        <uuid4> 帧全局唯一
  session_id: <session id>
  via:       "human" | "automation"
  ts:        <ms timestamp>
  version:   1 (平铺旧格式) | 2 (信封新格式)
  payload:   <dict> 业务内容，结构由 type 决定

正交约束：
  payload 内字段名禁止与 envelope 字段名重叠。
  序列化时 envelope 层与 payload 层独立读写，不平铺合并。
"""

import json
import time
import uuid
from typing import Optional

ENVELOPE_FIELDS = {"type", "id", "session_id", "via", "ts", "version", "payload"}
RESERVED_PAYLOAD_KEYS = ENVELOPE_FIELDS - {"payload"}

# ── Construction ──

def envelope_dispatch(payload: dict, session_id: str = "", via: str = "human") -> dict:
    return _build("dispatch", payload, session_id, via)

def envelope_result(payload: dict, session_id: str = "", via: str = "human") -> dict:
    return _build("result", payload, session_id, via)

def envelope_ack(dispatch_id: str, session_id: str = "") -> dict:
    return _build("ack", {"dispatch_id": dispatch_id}, session_id, "human")

def envelope_ping() -> dict:
    return _build("ping", {}, "", "human")

def envelope_pong() -> dict:
    return _build("pong", {}, "", "human")

def envelope_hello(agent_id: str, last_checkpoint_id: str = "") -> dict:
    return _build("hello", {"agent_id": agent_id, "last_checkpoint_id": last_checkpoint_id}, "", "human")

def _build(type_: str, payload: dict, session_id: str, via: str) -> dict:
    _validate_payload_keys(payload)
    return {
        "type": type_,
        "id": uuid.uuid4().hex,
        "session_id": session_id,
        "via": via,
        "ts": int(time.time() * 1000),
        "version": 2,
        "payload": payload,
    }

# ── Parsing / Validation ──

def parse_envelope(raw: dict) -> Optional[dict]:
    """解析并验证信封。返回 None 表示无效/旧格式。"""
    if not isinstance(raw, dict):
        return None
    # Version check
    ver = raw.get("version", 1)
    if ver < 2:
        return None  # Legacy flat format — caller handles compat
    # Required fields
    if not all(k in raw for k in ("type", "id", "ts")):
        return None
    # Validate payload field hygiene
    payload = raw.get("payload", {})
    if isinstance(payload, dict):
        overlap = RESERVED_PAYLOAD_KEYS & set(payload.keys())
        if overlap:
            # Payload must not contain envelope field names
            return None
    return raw

def is_legacy_flat(raw: dict) -> bool:
    """检测旧格式平铺消息（version < 2 或缺少 envelope 必需字段）"""
    return raw.get("version", 1) < 2

def extract_payload(envelope: dict) -> dict:
    """从信封提取业务载荷"""
    return envelope.get("payload", {})

# ── Internal ──

def _validate_payload_keys(payload: dict):
    overlap = RESERVED_PAYLOAD_KEYS & set(payload.keys())
    if overlap:
        raise ValueError(
            f"Payload keys {overlap} conflict with envelope reserved fields. "
            f"Reserved: {RESERVED_PAYLOAD_KEYS}"
        )

# ── Serialization ──

def serialize(envelope: dict) -> str:
    return json.dumps(envelope, ensure_ascii=False)

def deserialize(raw: str) -> Optional[dict]:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
