"""L3/L4/L5 transport layer unit tests"""
import pytest
import time


class FakeHub:
    """Minimal hub stub for testing transport tracking"""

    def __init__(self):
        self._pending_dispatches: dict = {}
        self._in_flight: dict = {}
        self._MAX_IN_FLIGHT = 8
        self._last_pong: dict = {}
        self._HEARTBEAT_INTERVAL = 30
        self._PONG_TIMEOUT = 90

    # ── L3: dispatch tracking ──
    def track_dispatch(self, session_id: str, envelope: dict):
        self._pending_dispatches.setdefault(session_id, []).append(envelope)

    def ack_dispatch(self, session_id: str, dispatch_id: str):
        if session_id in self._pending_dispatches:
            self._pending_dispatches[session_id] = [
                d for d in self._pending_dispatches[session_id] if d["id"] != dispatch_id
            ]

    def get_pending_dispatches(self, session_id: str, since_checkpoint_id: str = "") -> list:
        return self._pending_dispatches.get(session_id, [])

    # ── L4: backpressure ──
    def check_in_flight(self, agent_id: str) -> bool:
        return self._in_flight.get(agent_id, 0) < self._MAX_IN_FLIGHT

    def inc_in_flight(self, agent_id: str):
        self._in_flight[agent_id] = self._in_flight.get(agent_id, 0) + 1

    def dec_in_flight(self, agent_id: str):
        n = self._in_flight.get(agent_id, 0)
        if n > 0:
            self._in_flight[agent_id] = n - 1

    # ── L5: heartbeat ──
    def record_pong(self, agent_id: str):
        self._last_pong[agent_id] = time.time()

    def is_agent_timed_out(self, agent_id: str) -> bool:
        last = self._last_pong.get(agent_id, time.time())
        return (time.time() - last) > self._PONG_TIMEOUT


@pytest.fixture
def hub():
    return FakeHub()


# ═══ L3: dispatch tracking & replay ═══

class TestL3_DispatchTracking:
    """L3: track/ack/replay — 7 cases"""

    def test_l3_track_stores_envelope(self, hub):
        """L3-1: track_dispatch stores envelope per session"""
        hub.track_dispatch("s1", {"id": "d-001", "type": "dispatch", "payload": {"cmd": "run"}})
        assert len(hub._pending_dispatches["s1"]) == 1

    def test_l3_ack_removes_dispatch(self, hub):
        """L3-2: ack_dispatch removes specific envelope"""
        hub.track_dispatch("s1", {"id": "d-001"})
        hub.track_dispatch("s1", {"id": "d-002"})
        hub.ack_dispatch("s1", "d-001")
        remaining = hub.get_pending_dispatches("s1")
        assert len(remaining) == 1
        assert remaining[0]["id"] == "d-002"

    def test_l3_ack_nonexistent_noop(self, hub):
        """L3-3: ack_dispatch on nonexistent session doesn't crash"""
        hub.ack_dispatch("ghost", "d-999")
        assert "ghost" not in hub._pending_dispatches

    def test_l3_multisession_isolation(self, hub):
        """L3-4: sessions don't leak dispatches"""
        hub.track_dispatch("s1", {"id": "d-a"})
        hub.track_dispatch("s2", {"id": "d-b"})
        assert len(hub.get_pending_dispatches("s1")) == 1
        assert len(hub.get_pending_dispatches("s2")) == 1
        hub.ack_dispatch("s1", "d-a")
        assert len(hub.get_pending_dispatches("s1")) == 0
        assert len(hub.get_pending_dispatches("s2")) == 1

    def test_l3_empty_session_returns_empty(self, hub):
        """L3-5: get_pending on nonexistent session returns []"""
        assert hub.get_pending_dispatches("nonexistent") == []

    def test_l3_track_preserves_order(self, hub):
        """L3-6: dispatches are FIFO ordered"""
        for i in range(5):
            hub.track_dispatch("s1", {"id": f"d-{i}"})
        pending = hub.get_pending_dispatches("s1")
        assert pending[0]["id"] == "d-0"
        assert pending[4]["id"] == "d-4"

    def test_l3_partial_ack_maintains_order(self, hub):
        """L3-7: ack middle element, remaining order preserved"""
        for i in range(3):
            hub.track_dispatch("s1", {"id": f"d-{i}"})
        hub.ack_dispatch("s1", "d-1")
        pending = hub.get_pending_dispatches("s1")
        assert pending == [{"id": "d-0"}, {"id": "d-2"}]


# ═══ L4: backpressure ═══

class TestL4_Backpressure:
    """L4: in-flight counting — 5 cases"""

    def test_l4_initial_accepts(self, hub):
        """L4-1: new agent accepts dispatches"""
        assert hub.check_in_flight("agent-1") is True

    def test_l4_saturation_blocks(self, hub):
        """L4-2: at MAX_IN_FLIGHT, further dispatches blocked"""
        for _ in range(hub._MAX_IN_FLIGHT):
            hub.inc_in_flight("agent-1")
        assert hub.check_in_flight("agent-1") is False

    def test_l4_dec_reopens(self, hub):
        """L4-3: ack releases slot"""
        for _ in range(hub._MAX_IN_FLIGHT):
            hub.inc_in_flight("agent-1")
        hub.dec_in_flight("agent-1")
        assert hub.check_in_flight("agent-1") is True

    def test_l4_dec_below_zero(self, hub):
        """L4-4: dec underflow doesn't crash"""
        hub.dec_in_flight("agent-1")
        hub.dec_in_flight("agent-1")
        assert hub._in_flight.get("agent-1", 0) == 0

    def test_l4_per_agent_independent(self, hub):
        """L4-5: agents don't share in-flight counts"""
        for _ in range(hub._MAX_IN_FLIGHT):
            hub.inc_in_flight("agent-1")
        assert hub.check_in_flight("agent-1") is False
        assert hub.check_in_flight("agent-2") is True


# ═══ L5: heartbeat ═══

class TestL5_Heartbeat:
    """L5: ping/pong — 6 cases"""

    def test_l5_record_pong_updates_timestamp(self, hub):
        """L5-1: record_pong sets last_pong"""
        hub.record_pong("agent-1")
        assert "agent-1" in hub._last_pong

    def test_l5_fresh_agent_not_timed_out(self, hub):
        """L5-2: freshly recorded agent is not timed out"""
        hub.record_pong("agent-1")
        assert hub.is_agent_timed_out("agent-1") is False

    def test_l5_stale_agent_times_out(self, hub):
        """L5-3: agent with old pong times out"""
        hub._last_pong["agent-1"] = time.time() - 100  # 100s ago
        assert hub.is_agent_timed_out("agent-1") is True

    def test_l5_boundary_just_within_timeout(self, hub):
        """L5-4: pong at 89s ago is NOT timed out (< 90s)"""
        hub._last_pong["agent-1"] = time.time() - 89
        assert hub.is_agent_timed_out("agent-1") is False

    def test_l5_boundary_exactly_timeout(self, hub):
        """L5-5: pong at 91s ago IS timed out (> 90s)"""
        hub._last_pong["agent-1"] = time.time() - 91
        assert hub.is_agent_timed_out("agent-1") is True

    def test_l5_unknown_agent_defaults_alive(self, hub):
        """L5-6: agent never seen defaults to not timed out"""
        assert hub.is_agent_timed_out("never-seen") is False
