# -*- coding: utf-8 -*-
"""ShadowWriter 组合类（3-2c：五 mixin 组合，__init__ 仅在此处）。"""
import collections
import threading

from .common import _WATCHDOG_INTERVAL
from .lifecycle import LifecycleMixin
from .merge import MergeMixin
from .pending import PendingMixin
from .scan import ScanMixin
from .write import WriteMixin


class ShadowWriter(LifecycleMixin, PendingMixin, WriteMixin, MergeMixin, ScanMixin):
    """影子双写器。绑定 DataTrunk；DataTrunk.enabled=false 时全部 no-op。"""

    def __init__(self, data_trunk, audit_db_path: str = "", pending_db_path: str = "",
                 watchdog_interval: float = _WATCHDOG_INTERVAL):
        self.dt = data_trunk
        # P2 交付4：审计库路径（audit_log 哈希链）。空 = 不写 chain-head.jsonl
        self._audit_db_path = audit_db_path
        # G1 批1：shadow_pending WAL 表所在库。缺省回落 audit_db_path
        # （hub_core 传入的正是主库 CONFIG.DB_PATH，无需改调用方）；空 = 无崩溃保护
        self._pending_db = pending_db_path or audit_db_path or ""
        self._pend_lock = threading.Lock()
        self._pend_db_conn = None
        shadow = getattr(data_trunk, "shadow", None) or {}
        self._switches = {k: bool(shadow.get(k, False)) for k in
                          ("memory", "knowledge", "wiki", "shared")}
        self.enabled = bool(getattr(data_trunk, "enabled", False)) and any(self._switches.values())
        self._q = collections.deque()
        self._qlock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        # G1 批2：看门狗巡检线程 + 连续失败计数（告警阈值用）
        self._watchdog = None
        self._watchdog_interval = watchdog_interval
        self._consec_fail = 0
        # G1 批1：新增 pending_replayed/pending_failed/pending_insert_failed
        # G1 批2：新增 watchdog_restarts/last_failure_at/last_failure_reason
        # （向后兼容，failures 等原键语义不动）
        self.stats = {"submitted": 0, "flushed": 0, "failures": 0,
                      "last_flush_at": 0.0,
                      "pending_replayed": 0, "pending_failed": 0,
                      "pending_insert_failed": 0,
                      "watchdog_restarts": 0, "last_failure_at": 0.0,
                      "last_failure_reason": ""}
        self._kind_count = {"memory": 0, "knowledge": 0, "wiki": 0, "shared": 0}
        # P2 交付1：已开通的 (branch, agent) 组合缓存，避免每条数据重复 ensure_branch
        self._opened = set()
        # P2 交付2：id → 真相源定位 {branch, path, trunk_commit, branch_commit, ts}
        self._origins = {}
