# -*- coding: utf-8 -*-
"""审计 outbox（CD-045，2026-09-17）— 事务内事件行 + 后台消费者异步落审计链

为什么：写路径曾在 SQLite 事务体内、commit 之前直接 audit_memory() 追加哈希链。
审计追加成功而 commit 失败回滚时，不可改的链上永久留下"从未发生的写入"。

修法（拍板设计，不得自行更改）：事务内只写 event_outbox 事件行（与业务数据
原子提交，同生共死）；后台消费者按 id 顺序消费 → 调 audit_memory() 落链；
失败 attempts+1 留 pending，达上限标 failed 并告警，重启自动 replay。

事件表是通用事件日志（带 event_type），后续影子镜像事件（CD-047）复用同表。
本模块不复制审计逻辑——审计只能由 audit.memory_audit.audit_memory 落盘。

CD-046（2026-09-17）：新增 vector_index 事件类型（向量索引一致性补偿）+
enqueue_after_commit()（业务已提交之后的独立入队）。向量写路径改为"业务提交
后执行，失败落补偿事件"；消费者回调 vector_fn 用库内 embedding blob 重灌索引。

CD-047（2026-09-17）：新增 shadow_mirror 事件类型（影子镜像事件驱动化）+
shadow_fn 注入。事件只记 memory_id（不打包快照）；消费者回调 shadow_fn
从库内读最新值再 submit 影子——消灭"commit 后 submit 前"的崩溃窗口，
快照陈旧问题也随之消失。
"""
import json
import logging
import sqlite3
import threading

logger = logging.getLogger("xingshu.outbox")

OUTBOX_MAX_ATTEMPTS = 5     # 与 shadow 的 _PENDING_MAX_ATTEMPTS 同量级
OUTBOX_BATCH = 100
OUTBOX_INTERVAL_SEC = 0.5   # drain 间隔：把"审计可见延迟"压到亚秒级


def enqueue(conn, event_type: str, payload: dict) -> None:
    """事务内写事件行。conn 为调用方的业务事务连接/游标。

    不得自建连接、不得 commit、不得吞异常——异常向上抛，让业务事务一起回滚
    （fail-closed：数据与审计同生共死）。
    """
    conn.execute(
        "INSERT INTO event_outbox (event_type, payload) VALUES (?, ?)",
        (event_type, json.dumps(payload, ensure_ascii=False, default=str)),
    )


# enqueue_after_commit 失败计数（观测用；业务已提交，失败只能告警不能反悔）
_ENQUEUE_AFTER_COMMIT_FAILED = 0


async def enqueue_after_commit(event_type: str, payload: dict) -> bool:
    """业务已提交之后的独立入队（CD-046 向量补偿等"提交后副作用"用）。

    与 enqueue() 的差别：enqueue 必须在业务事务内（同事务原子提交）；
    本函数用于业务已提交、无法回滚之后的补偿入队——经 db_facade.execute
    单独 INSERT + 自动提交。失败只 logger.warning + 计数，**不抛**
    （业务已提交，不能反悔）。返回是否入队成功。
    """
    global _ENQUEUE_AFTER_COMMIT_FAILED
    try:
        import db_facade  # 函数内延迟 import：保持本模块 stdlib-only，避免环
        await db_facade.execute(
            "INSERT INTO event_outbox (event_type, payload) VALUES (?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False, default=str)))
        return True
    except Exception as exc:
        _ENQUEUE_AFTER_COMMIT_FAILED += 1
        logger.warning("enqueue_after_commit 失败（%s）: %s: %s",
                       event_type, type(exc).__name__, str(exc)[:200])
        return False


class OutboxConsumer:
    """后台消费者：顺序消费 event_outbox → 落审计链。失败留 pending 自动重试。"""

    def __init__(self, db_path: str, audit_fn=None, vector_fn=None,
                 shadow_fn=None, shadow_delete_fn=None, shadow_archive_fn=None):
        self._db_path = db_path
        self._audit_fn = audit_fn  # None = 懒加载 audit.memory_audit.audit_memory
        self._vector_fn = vector_fn  # CD-046: vector_index 事件回调（同步函数）
        self._shadow_fn = shadow_fn  # CD-047: shadow_mirror 事件回调（同步函数）
        self._shadow_delete_fn = shadow_delete_fn  # T31: shadow_delete 事件回调
        self._shadow_archive_fn = shadow_archive_fn  # T31: shadow_archive 事件回调
        self._conn = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._drained = 0
        self._last_error = ""

    def _resolve_audit_fn(self):
        if self._audit_fn is not None:
            return self._audit_fn
        from audit.memory_audit import audit_memory
        return audit_memory

    def _ensure_conn(self):
        if self._conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout = 5000")
            self._conn = conn
        return self._conn

    def _drop_conn(self):
        # 连接异常后丢弃，下次 drain 重建（不许让线程死）
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="outbox-consumer", daemon=True)
        self._thread.start()

    def stop(self, flush=True):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5)
            self._thread = None
        if flush:
            self._drain_once()  # 停止前最后再 drain 一次

    def _loop(self):
        while not self._stop.is_set():
            try:
                # 有积压时连续 drain（否则一轮 = OUTBOX_BATCH/INTERVAL，
                # 高峰写入下事件表会越积越深）；空表即回到等待节奏。
                while not self._stop.is_set() and self._drain_once():
                    pass
            except Exception:
                # D4 降级：审计消费者失败不得阻塞主链路，线程不死
                logger.exception("outbox drain 异常（已降级，线程继续）")
            self._stop.wait(OUTBOX_INTERVAL_SEC)

    def _drain_once(self) -> bool:
        """消费一轮 pending 事件（可单独调用，测试用确定性 drain）。

        返回本轮是否消费了任何一行（含失败行）。
        """
        try:
            with self._lock:
                conn = self._ensure_conn()
                rows = conn.execute(
                    "SELECT id, event_type, payload, attempts FROM event_outbox "
                    "WHERE status='pending' AND attempts < ? "
                    "ORDER BY id LIMIT ?",
                    (OUTBOX_MAX_ATTEMPTS, OUTBOX_BATCH),
                ).fetchall()
                for rid, event_type, payload, attempts in rows:
                    handler = None
                    if event_type == "memory_audit":
                        def _audit_job(p=payload):
                            self._resolve_audit_fn()(
                                **json.loads(p), raise_on_error=True)
                        handler = _audit_job
                    elif event_type == "vector_index":
                        if self._vector_fn is None:
                            # CD-046：有 vector_index 事件但未注入 vector_fn →
                            # 标 failed + 告警，不许静默跳过、不许标 done
                            err = "vector_index 事件但消费者未注入 vector_fn"
                            conn.execute(
                                "UPDATE event_outbox SET status='failed',"
                                " last_error=? WHERE id=?", (err, rid))
                            conn.commit()
                            logger.error("outbox 事件 %d %s", rid, err)
                            self._last_error = err
                            continue
                        def _vector_job(p=payload):
                            self._vector_fn(json.loads(p))  # 同步调用
                        handler = _vector_job
                    elif event_type == "shadow_mirror":
                        if self._shadow_fn is None:
                            # CD-047：有 shadow_mirror 事件但未注入 shadow_fn →
                            # 标 failed + 告警，不许静默跳过、不许标 done
                            err = "shadow_mirror 事件但消费者未注入 shadow_fn"
                            conn.execute(
                                "UPDATE event_outbox SET status='failed',"
                                " last_error=? WHERE id=?", (err, rid))
                            conn.commit()
                            logger.error("outbox 事件 %d %s", rid, err)
                            self._last_error = err
                            continue
                        def _shadow_job(p=payload):
                            self._shadow_fn(json.loads(p))  # 同步调用
                        handler = _shadow_job
                    elif event_type == "shadow_delete":
                        if self._shadow_delete_fn is None:
                            err = "shadow_delete 事件但消费者未注入 shadow_delete_fn"
                            conn.execute(
                                "UPDATE event_outbox SET status='failed',"
                                " last_error=? WHERE id=?", (err, rid))
                            conn.commit()
                            logger.error("outbox 事件 %d %s", rid, err)
                            self._last_error = err
                            continue
                        def _shadow_delete_job(p=payload):
                            self._shadow_delete_fn(json.loads(p))
                        handler = _shadow_delete_job
                    elif event_type == "shadow_archive":
                        if self._shadow_archive_fn is None:
                            err = "shadow_archive 事件但消费者未注入 shadow_archive_fn"
                            conn.execute(
                                "UPDATE event_outbox SET status='failed',"
                                " last_error=? WHERE id=?", (err, rid))
                            conn.commit()
                            logger.error("outbox 事件 %d %s", rid, err)
                            self._last_error = err
                            continue
                        def _shadow_archive_job(p=payload):
                            self._shadow_archive_fn(json.loads(p))
                        handler = _shadow_archive_job
                    else:
                        # 未知事件类型：直接标 failed + 告警，不许静默跳过
                        err = f"unknown event_type: {event_type}"
                        conn.execute(
                            "UPDATE event_outbox SET status='failed',"
                            " last_error=? WHERE id=?", (err, rid))
                        conn.commit()
                        logger.error("outbox 事件 %d %s", rid, err)
                        self._last_error = err
                        continue
                    try:
                        handler()
                    except Exception as exc:
                        err = f"{type(exc).__name__}: {str(exc)[:200]}"
                        self._last_error = err
                        new_attempts = attempts + 1
                        if new_attempts >= OUTBOX_MAX_ATTEMPTS:
                            conn.execute(
                                "UPDATE event_outbox SET attempts=?,"
                                " last_error=?, status='failed' WHERE id=?",
                                (new_attempts, err, rid))
                            conn.commit()
                            logger.error(
                                "outbox 事件 %d 重试 %d 次达上限标记 failed: %s",
                                rid, new_attempts, err)
                        else:
                            conn.execute(
                                "UPDATE event_outbox SET attempts=?,"
                                " last_error=? WHERE id=?",
                                (new_attempts, err, rid))
                            conn.commit()
                            logger.warning(
                                "outbox 事件 %d 消费失败（attempts=%d）: %s",
                                rid, new_attempts, err)
                        continue
                    conn.execute(
                        "UPDATE event_outbox SET status='done' WHERE id=?",
                        (rid,))
                    conn.commit()
                    self._drained += 1
                return bool(rows)
        except Exception as exc:
            # 连接级故障：记日志、丢连接（下次重建），不让线程退出
            self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning("outbox drain 连接级异常（下次重建连接）: %s",
                           self._last_error)
            self._drop_conn()
            return False

    def stats_snapshot(self) -> dict:
        snap = {"pending": 0, "done": 0, "failed": 0,
                "last_error": self._last_error, "drained": self._drained,
                "by_type": {}}  # by_type: pending 积压按 event_type 分组计数
        try:
            with self._lock:
                conn = self._ensure_conn()
                for status in ("pending", "done", "failed"):
                    snap[status] = conn.execute(
                        "SELECT COUNT(*) FROM event_outbox WHERE status=?",
                        (status,)).fetchone()[0]
                for et, n in conn.execute(
                        "SELECT event_type, COUNT(*) FROM event_outbox"
                        " WHERE status='pending' GROUP BY event_type"):
                    snap["by_type"][et] = n
        except Exception as exc:
            snap["last_error"] = (
                self._last_error or f"{type(exc).__name__}: {exc}")
            self._drop_conn()
        return snap
