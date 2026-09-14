"""L0 信封分层 — 过关用例"""
import pytest
import json
from envelope import (
    envelope_dispatch, envelope_result, envelope_ack, envelope_ping, envelope_pong,
    envelope_hello, parse_envelope, is_legacy_flat, extract_payload,
    serialize, deserialize, ENVELOPE_FIELDS, RESERVED_PAYLOAD_KEYS,
)


class TestL0T1_FieldHygiene:
    """L0-T1: payload 含同名 envelope 字段时互不覆盖"""

    def test_payload_cannot_contain_type(self):
        with pytest.raises(ValueError):
            envelope_dispatch({"type": "malicious"})

    def test_payload_cannot_contain_id(self):
        with pytest.raises(ValueError):
            envelope_result({"id": "fake"})

    def test_payload_cannot_contain_session_id(self):
        with pytest.raises(ValueError):
            envelope_dispatch({"session_id": "hijack"})

    def test_payload_cannot_contain_via(self):
        with pytest.raises(ValueError):
            envelope_result({"via": "automation"})

    def test_payload_cannot_contain_ts(self):
        with pytest.raises(ValueError):
            envelope_dispatch({"ts": 0})

    def test_payload_cannot_contain_version(self):
        with pytest.raises(ValueError):
            envelope_result({"version": 2})

    def test_parse_rejects_payload_with_envelope_keys(self):
        raw = {
            "type": "result", "id": "abc", "session_id": "s1",
            "via": "human", "ts": 123, "version": 2,
            "payload": {"type": "malicious", "id": "hijack"},
        }
        assert parse_envelope(raw) is None

    def test_clean_payload_passes(self):
        env = envelope_dispatch({"event": "test", "data": "hello"})
        parsed = parse_envelope(env)
        assert parsed is not None
        assert extract_payload(parsed) == {"event": "test", "data": "hello"}


class TestL0T2_SerializationRoundtrip:
    """L0-T2: 序列化往返 — envelope 层与 payload 层独立"""

    def test_dispatch_roundtrip(self):
        env = envelope_dispatch({"cmd": "run", "args": {}}, session_id="s42", via="human")
        text = serialize(env)
        parsed = deserialize(text)
        assert parsed is not None
        result = parse_envelope(parsed)
        assert result is not None
        assert result["type"] == "dispatch"
        assert result["session_id"] == "s42"
        assert result["via"] == "human"
        assert extract_payload(result) == {"cmd": "run", "args": {}}

    def test_result_roundtrip(self):
        env = envelope_result({"reply": "ok", "status": "success"})
        text = serialize(env)
        result = parse_envelope(deserialize(text))
        assert result is not None
        assert result["type"] == "result"

    def test_ack_roundtrip(self):
        env = envelope_ack("dispatch-123")
        text = serialize(env)
        result = parse_envelope(deserialize(text))
        assert extract_payload(result) == {"dispatch_id": "dispatch-123"}

    def test_heartbeat_roundtrip(self):
        ping = envelope_ping()
        assert parse_envelope(ping) is not None
        pong = envelope_pong()
        assert parse_envelope(pong) is not None

    def test_hello_roundtrip(self):
        hello = envelope_hello("agent-1", "ckpt-42")
        result = parse_envelope(hello)
        assert extract_payload(result)["agent_id"] == "agent-1"
        assert extract_payload(result)["last_checkpoint_id"] == "ckpt-42"


class TestL0T3_LegacyDetection:
    """L0-T3: 旧格式检测 + 兼容"""

    def test_legacy_flat_detected(self):
        legacy = {"type": "automation.run", "job_id": 1, "instruction": "test"}
        assert is_legacy_flat(legacy)
        assert parse_envelope(legacy) is None

    def test_v2_envelope_not_legacy(self):
        env = envelope_dispatch({"event": "test"})
        assert not is_legacy_flat(env)
        assert parse_envelope(env) is not None

    def test_legacy_with_version_field(self):
        legacy2 = {"type": "push", "data": {}, "version": 1}
        assert is_legacy_flat(legacy2)
        assert parse_envelope(legacy2) is None


class TestL0T4_IdUniqueness:
    """L0-T4: 每个信封 id 全局唯一"""

    def test_ids_are_unique(self):
        ids = set()
        for _ in range(100):
            env = envelope_dispatch({"test": True})
            ids.add(env["id"])
        assert len(ids) == 100

    def test_ids_are_strings(self):
        env = envelope_result({"ok": True})
        assert isinstance(env["id"], str)
        assert len(env["id"]) > 0


class TestL0T5_Timestamp:
    """L0-T5: ts 毫秒时间戳递增"""

    def test_ts_is_ms_epoch(self):
        import time
        now = int(time.time() * 1000)
        env = envelope_ping()
        assert abs(env["ts"] - now) < 5000  # within 5 seconds

    def test_ts_increases(self):
        t1 = envelope_ping()["ts"]
        t2 = envelope_ping()["ts"]
        assert t2 >= t1
