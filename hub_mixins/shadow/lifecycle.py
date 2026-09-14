# -*- coding: utf-8 -*-
"""ShadowWriter 生命周期/队列/统计/告警组（3-2c 自 shadow.py 逐字搬运）。"""
import logging
import threading

logger = logging.getLogger("xingshu.shadow")


class LifecycleMixin:
    # ── 生命周期 ──
    def start(self):
        if not self.enabled or self._thread is not None:
            return
        # G1 批1：启动 replay——崩溃前未完成的 pending 行重入队补镜像
        self._replay_pending()
        self._start_worker()
        self._start_watchdog()
        logger.info("ShadowWriter 启动（影子双写 enabled）")

    def _start_worker(self):
        """（重）建 worker 线程。threading 线程不可 restart——看门狗重启走这里。"""
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="shadow-writer",
                                        daemon=True)
        self._thread.start()

    def _start_watchdog(self):
        """G1 批2：独立 daemon 巡检线程（worker 死后无法自检，只能体外巡检）。"""
        if self._watchdog is not None:
            return
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                          name="shadow-watchdog", daemon=True)
        self._watchdog.start()

    def _watchdog_loop(self):
        while not self._stop.wait(self._watchdog_interval):
            try:
                self._watchdog_check()
            except Exception:
                logger.exception("影子看门狗巡检异常（降级）")

    def _watchdog_check(self):
        """worker 死亡（enabled 且非停止中）→ 重建 Thread 重启 + 计数 + 告警。"""
        t = self._thread
        if t is None or t.is_alive() or self._stop.is_set():
            return
        try:
            t.join(timeout=0)  # reap 死线程
        except Exception as _exc:
            logger.debug("shadow silent-except @274: %s", _exc)
        if self._stop.is_set():
            # 与 stop() 竞态兜底：巡检中途收到停止信号则放弃重启
            return
        self.stats["watchdog_restarts"] += 1
        logger.warning("影子 worker 线程死亡，看门狗重启（第 %d 次）",
                       self.stats["watchdog_restarts"])
        # 先重启后告警：告警钩子的惰性首次 import 有秒级开销，不能阻塞恢复
        self._start_worker()
        self._alert("watchdog_restart",
                    "restarts=%d" % self.stats["watchdog_restarts"])

    def stop(self, flush: bool = True):
        """停止 worker；flush=True 时把剩余队列写完。"""
        if self._thread is None:
            return
        self._stop.set()
        if flush:
            self._drain_once()
        self._thread.join(timeout=5)
        self._thread = None
        if self._watchdog is not None:
            self._watchdog.join(timeout=2)
            self._watchdog = None
        self._pend_close()
        logger.info("ShadowWriter 停止（flush=%s）", flush)

    # ── 入队 ──
    def submit(self, kind: str, payload: dict):
        """线程安全入队，零阻塞。enabled=false 或该 kind 未开 → no-op。

        G1 批1：入队前先 INSERT shadow_pending（status=pending）——崩溃后可 replay。
        INSERT 失败只记 stats 不阻塞，条目仍入队镜像（仅失去崩溃保护，D4 红线）。
        队内条目为 (kind, payload, pending_id)，pending_id=None 表示无 pending 行。
        """
        if not self.enabled or not self._switches.get(kind):
            return
        try:
            pending_id = self._pending_insert(kind, payload)
            with self._qlock:
                self._q.append((kind, payload, pending_id))
            self.stats["submitted"] += 1
        except Exception:
            self.stats["failures"] += 1

    def queue_depth(self) -> int:
        with self._qlock:
            return len(self._q)

    def stats_snapshot(self) -> dict:
        """G1 批2：stats 全量 + 运行态快照（stats 端点用，routes_maintenance）。

        pending_incomplete = shadow_pending 表 status='pending' 行数
        （含内存队列未 flush 部分——两边同源于 submit，以表为准更贴近
        「已落库未镜像」语义）。
        """
        pending_incomplete = 0
        if self._pending_db:
            try:
                with self._pend_lock:
                    pending_incomplete = self._pend_conn().execute(
                        "SELECT COUNT(*) FROM shadow_pending WHERE status='pending'"
                    ).fetchone()[0]
            except Exception:
                self._pend_close()
        t = self._thread
        return {"enabled": self.enabled, "stats": dict(self.stats),
                "kind_count": dict(self._kind_count),
                "queue_depth": self.queue_depth(),
                "pending_incomplete": pending_incomplete,
                "daemon_alive": bool(t is not None and t.is_alive())}

    # ── 告警钩子（G1 批2：去抖封装在 hub_mixins.notifications）──
    def _alert(self, reason: str, detail: str = ""):
        """连续失败告警（阈值/去抖由 notifications.shadow_alert 封装）。静默降级。"""
        try:
            from hub_mixins.notifications import shadow_alert
            shadow_alert(reason, detail)
        except Exception as _exc:
            logger.warning("shadow silent-except @353: %s", _exc)

    def _alert_reset(self, reason: str):
        try:
            from hub_mixins.notifications import shadow_alert_reset
            shadow_alert_reset(reason)
        except Exception as _exc:
            logger.warning("shadow silent-except @360: %s", _exc)
