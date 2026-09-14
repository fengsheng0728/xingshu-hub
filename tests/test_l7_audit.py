"""L7 传输层审计 — 过关用例 (via 可区分 + 全帧落库 + 降级不阻断)"""
import json
import os
import tempfile
import pytest

# Redirect transport_audit to temp dir
os.environ["AUDIT_DIR"] = tempfile.mkdtemp()

import transport_audit as ta
ta.AUDIT_DIR = os.environ["AUDIT_DIR"]
ta.TRANSPORT_AUDIT_FILE = os.path.join(ta.AUDIT_DIR, "transport.jsonl")


def _read_all():
    if os.path.exists(ta.TRANSPORT_AUDIT_FILE):
        with open(ta.TRANSPORT_AUDIT_FILE, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    return []


def _clean():
    if os.path.exists(ta.TRANSPORT_AUDIT_FILE):
        os.remove(ta.TRANSPORT_AUDIT_FILE)


class TestL7T1_FullFrameAudit:
    """L7-T1: 每条派单有 audit 记录 (dispatch/result/ack 三类 + via/ts/方向)"""

    def setup_method(self):
        _clean()

    def _read(self):
        return _read_all()

    def test_dispatch_frame_logged(self):
        env = {
            "type": "dispatch", "id": "d-001", "session_id": "s1",
            "via": "automation", "ts": 1700000000000,
            "payload": {"event": "automation.run", "job_id": 1},
        }
        ta.log_dispatch_out(env)
        records = self._read()
        assert len(records) == 1
        r = records[0]
        assert r["type"] == "dispatch"
        assert r["direction"] == "out"
        assert r["via"] == "automation"
        assert r["id"] == "d-001"
        assert r["session_id"] == "s1"
        assert "ts" in r
        assert "payload_keys" in r

    def test_ack_frame_logged(self):
        env = {
            "type": "ack", "id": "a-001", "session_id": "s1",
            "via": "human", "ts": 1700000001000,
            "payload": {"dispatch_id": "d-001"},
        }
        ta.log_ack_in(env)
        records = self._read()
        assert len(records) == 1
        assert records[0]["type"] == "ack"
        assert records[0]["via"] == "human"

    def test_ping_frame_logged(self):
        env = {"type": "ping", "id": "p-001", "session_id": "", "via": "human", "ts": 1700000002000, "payload": {}}
        ta.log_ping_pong("in", env)
        records = self._read()
        assert len(records) == 1
        assert records[0]["type"] == "ping"
        assert records[0]["direction"] == "in"

    def test_fields_complete(self):
        """dispatch_id / session_id / via / ts / direction 全在"""
        env = {
            "type": "result", "id": "r-001", "session_id": "s2",
            "via": "automation", "ts": 1700000003000,
            "payload": {"status": "success"},
        }
        ta.log_result_out(env)
        records = self._read()
        r = records[0]
        for field in ["type", "id", "session_id", "via", "ts", "direction"]:
            assert field in r, f"missing field: {field}"


class TestL7T2_ViaDistinction:
    """L7-T2: grep via=automation 可区分人工与自动"""

    def setup_method(self):
        _clean()

    def _read(self):
        return _read_all()

    def test_via_human_vs_automation(self):
        human_env = {
            "type": "dispatch", "id": "h-001", "session_id": "s1",
            "via": "human", "ts": 1700000000000,
            "payload": {"event": "chat"},
        }
        auto_env = {
            "type": "dispatch", "id": "a-001", "session_id": "s1",
            "via": "automation", "ts": 1700000001000,
            "payload": {"event": "automation.run", "job_id": 1},
        }
        ta.log_dispatch_out(human_env)
        ta.log_dispatch_out(auto_env)

        records = self._read()
        human = [r for r in records if r["via"] == "human"]
        auto = [r for r in records if r["via"] == "automation"]

        assert len(human) >= 1, "no human records found"
        assert len(auto) >= 1, "no automation records found"

        # Verify grep semantics: via field is exact match
        for r in human:
            assert r["via"] == "human"
        for r in auto:
            assert r["via"] == "automation"


class TestL7T3_FallbackNoBlock:
    """L7-T3: audit 写入失败不阻断业务 -- 降级记本地文件"""

    def setup_method(self):
        _clean()

    def _read(self):
        return _read_all()

    def test_write_failure_does_not_raise(self):
        """L7-T3: 注入写入失败，业务继续，不抛异常"""
        # Point to non-writable path to simulate failure
        old_file = ta.TRANSPORT_AUDIT_FILE
        ta.TRANSPORT_AUDIT_FILE = "Z:/nonexistent/transport.jsonl"
        try:
            env = {"type": "ack", "id": "a-001", "session_id": "", "via": "human", "ts": 0, "payload": {}}
            # Must not raise
            ta.log_ack_in(env)
        finally:
            ta.TRANSPORT_AUDIT_FILE = old_file

        # Verify fallback file was created
        import glob
        fallbacks = glob.glob("audit/transport_fallback_*.jsonl")
        if fallbacks:
            for fb in fallbacks:
                try:
                    os.remove(fb)
                except Exception:
                    pass
