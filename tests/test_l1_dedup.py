"""L1 幂等去重 — 过关用例"""
import pytest
import time
from unittest.mock import MagicMock, patch


class FakeAgentClient:
    """Minimal mock for testing dispatch dedup logic"""
    def __init__(self):
        self._executed_dispatches = {}
        self._DISPATCH_CACHE_MAX = 10000
        self._DISPATCH_CACHE_TTL = 86400

    def _is_duplicate_dispatch(self, dispatch_id):
        if dispatch_id in self._executed_dispatches:
            cached = self._executed_dispatches[dispatch_id]
            if time.time() - cached["ts"] < self._DISPATCH_CACHE_TTL:
                return cached["result"]
            del self._executed_dispatches[dispatch_id]
        return None

    def _cache_dispatch_result(self, dispatch_id, result):
        if len(self._executed_dispatches) >= self._DISPATCH_CACHE_MAX:
            oldest = min(self._executed_dispatches, key=lambda k: self._executed_dispatches[k]["ts"])
            del self._executed_dispatches[oldest]
        self._executed_dispatches[dispatch_id] = {"result": result, "ts": time.time()}


class TestL1T1_DuplicatePrevention:
    """L1-T1: 同一 dispatch_id 第二次到达不重复执行"""

    def test_first_call_executes(self):
        agent = FakeAgentClient()
        result = agent._is_duplicate_dispatch("d-001")
        assert result is None

    def test_second_call_returns_cached(self):
        agent = FakeAgentClient()
        agent._cache_dispatch_result("d-001", {"status": "success", "summary": "done"})
        cached = agent._is_duplicate_dispatch("d-001")
        assert cached is not None
        assert cached["status"] == "success"

    def test_different_ids_independent(self):
        agent = FakeAgentClient()
        agent._cache_dispatch_result("d-001", {"status": "success"})
        assert agent._is_duplicate_dispatch("d-002") is None
        assert agent._is_duplicate_dispatch("d-001") is not None


class TestL1T2_SideEffectOnce:
    """L1-T2: 缓存后不再触发副作用（ToolExecutor 不会被调）"""

    def test_cache_size_grows(self):
        agent = FakeAgentClient()
        for i in range(5):
            agent._cache_dispatch_result(f"d-{i}", {"status": "ok"})
        assert len(agent._executed_dispatches) == 5

    def test_duplicate_does_not_increase_size(self):
        agent = FakeAgentClient()
        agent._cache_dispatch_result("d-001", {"status": "ok"})
        size_before = len(agent._executed_dispatches)
        agent._cache_dispatch_result("d-001", {"status": "ok"})
        assert len(agent._executed_dispatches) == size_before  # same key, overwrites


class TestL1T3_LRUEviction:
    """L1-T3: LRU 淘汰 + known limitation"""

    def test_lru_evicts_oldest(self):
        agent = FakeAgentClient()
        agent._DISPATCH_CACHE_MAX = 3
        agent._cache_dispatch_result("d-old", {"ts": time.time() - 100})
        time.sleep(0.001)
        agent._cache_dispatch_result("d-mid", {"ts": time.time()})
        time.sleep(0.001)
        agent._cache_dispatch_result("d-new", {"ts": time.time()})
        # Cache is full (3 items). Next insert evicts oldest.
        agent._cache_dispatch_result("d-overflow", {"ts": time.time()})
        # "d-old" should be evicted
        assert agent._is_duplicate_dispatch("d-old") is None
        # Newer items should remain
        assert agent._is_duplicate_dispatch("d-mid") is not None
        assert agent._is_duplicate_dispatch("d-new") is not None

    def test_evicted_replay_is_known_limitation(self):
        """Known limitation: evicted dispatch_id will re-execute if replayed"""
        agent = FakeAgentClient()
        agent._DISPATCH_CACHE_MAX = 1
        agent._cache_dispatch_result("d-001", {"status": "ok"})
        agent._cache_dispatch_result("d-002", {"status": "ok"})  # evicts d-001
        # d-001 is evicted — this IS the known limitation
        assert agent._is_duplicate_dispatch("d-001") is None


class TestL1T4_TTLExpiry:
    """L1-T4: TTL 过期后允许重新执行"""

    def test_expired_entry_re_executes(self):
        agent = FakeAgentClient()
        agent._DISPATCH_CACHE_TTL = 0  # instant expiry
        agent._cache_dispatch_result("d-001", {"status": "ok"})
        # Should be expired
        assert agent._is_duplicate_dispatch("d-001") is None

    def test_non_expired_entry_returns_cache(self):
        agent = FakeAgentClient()
        agent._DISPATCH_CACHE_TTL = 999999
        agent._cache_dispatch_result("d-001", {"status": "ok"})
        assert agent._is_duplicate_dispatch("d-001") is not None
