# -*- coding: utf-8 -*-
"""ShadowWriter 崩溃一致性 shadow_pending 组（3-2c 自 shadow.py 逐字搬运）。"""
import datetime
import json
import logging
import sqlite3

from .common import _PEND_DDL, _PENDING_MAX_ATTEMPTS

logger = logging.getLogger("xingshu.shadow")


class PendingMixin:
    # ── shadow_pending WAL（G1 批1：崩溃一致性，方案A）──
    # 与业务写入不同事务：submit 挂钩本就在业务 commit 之后，独立连接即可。
    # 所有 DB 操作失败静默降级（D4），绝不阻塞主链路。

    def _pend_conn(self):
        """共享连接（须在 _pend_lock 内调用）。防御性建表兜底（正式迁移在 db.py v6）。"""
        if self._pend_db_conn is None:
            conn = sqlite3.connect(self._pending_db, check_same_thread=False)
            conn.execute(_PEND_DDL)
            conn.commit()
            self._pend_db_conn = conn
        return self._pend_db_conn

    def _pend_close(self):
        with self._pend_lock:
            if self._pend_db_conn is not None:
                try:
                    self._pend_db_conn.close()
                except Exception as _exc:
                    logger.debug("shadow silent-except @531: %s", _exc)
                self._pend_db_conn = None

    def _pending_insert(self, kind: str, payload: dict):
        """submit 前落 pending 行（status=pending），返回行 id。

        失败只记 stats 返回 None——条目照常入队镜像，仅失去崩溃保护。
        """
        if not self._pending_db:
            return None
        try:
            with self._pend_lock:
                cur = self._pend_conn().execute(
                    "INSERT INTO shadow_pending (kind, payload) VALUES (?, ?)",
                    (kind, json.dumps(payload, ensure_ascii=False, default=str)))
                self._pend_conn().commit()
                return cur.lastrowid
        except Exception:
            self._pend_close()
            # 不计 failures（其语义不变：只计镜像写入失败），单独计数
            self.stats["pending_insert_failed"] += 1
            logger.warning("shadow_pending INSERT 失败（降级，本条无崩溃保护）kind=%s", kind)
            return None

    def _pending_mark_done(self, ids):
        """flush 成功后批量软标记 status='done'（不 DELETE，留可审计账）。"""
        ids = [i for i in ids if i]
        if not ids or not self._pending_db:
            return
        try:
            qs = ",".join("?" * len(ids))
            with self._pend_lock:
                self._pend_conn().execute(
                    f"UPDATE shadow_pending SET status='done' WHERE id IN ({qs})", ids)
                self._pend_conn().commit()
        except Exception:
            self._pend_close()
            logger.warning("shadow_pending 软标记失败（降级）ids=%s", ids[:5])

    def _pending_note_failure(self, ids):
        """flush 失败：attempts+1；达到上限的行标记 failed 并计 stats。"""
        ids = [i for i in ids if i]
        if not ids or not self._pending_db:
            return
        try:
            qs = ",".join("?" * len(ids))
            with self._pend_lock:
                self._pend_conn().execute(
                    "UPDATE shadow_pending SET attempts=attempts+1, "
                    "status=CASE WHEN attempts+1>=? THEN 'failed' ELSE status END "
                    f"WHERE id IN ({qs}) AND status='pending'",
                    [_PENDING_MAX_ATTEMPTS, *ids])
                n_failed = self._pend_conn().execute(
                    f"SELECT COUNT(*) FROM shadow_pending WHERE id IN ({qs}) "
                    "AND status='failed'", ids).fetchone()[0]
                self._pend_conn().commit()
            if n_failed:
                self.stats["pending_failed"] += n_failed
                logger.warning("shadow_pending %d 行重试超 %d 次标记 failed",
                               n_failed, _PENDING_MAX_ATTEMPTS)
                # G1 批2：批级失败落审计（shadow_pending failed 行已留账，
                # 补一条 audit_log 行级哈希链记录）
                self._audit_batch_failed(ids, n_failed)
        except Exception:
            self._pend_close()
            logger.warning("shadow_pending 失败计数异常（降级）ids=%s", ids[:5])

    def _audit_batch_failed(self, ids, n_failed: int):
        """G1 批2：批级失败落审计——attempts 超限标 failed 的行写 audit_log。

        复用现有审计写入模式（audit_chain.AuditChain 行级哈希链，entry_type=
        shadow_batch_failed）。无 audit_db_path / 无 audit_log 表（独立测试库）
        → 静默降级（D4：可见性建设不得阻塞主链路）。
        """
        if not self._audit_db_path:
            return
        try:
            from audit_chain import AuditChain
            AuditChain(self._audit_db_path).append(
                "shadow_batch_failed", "shadow_pending",
                ",".join(str(i) for i in ids[:20]),
                {"pending_ids": [int(i) for i in ids[:50]],
                 "failed": n_failed,
                 "max_attempts": _PENDING_MAX_ATTEMPTS,
                 "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
        except Exception:
            logger.warning("影子失败批审计落账异常（降级，不阻塞主链路）")

    def _replay_pending(self):
        """启动 replay：status='pending' 且 attempts 未超限的行重入内存队列。

        幂等保证：_append_index 同 id 去重 + vault md 确定性路径覆盖写 +
        git 同内容重复提交得 nothing to commit（视为成功）。
        """
        if not self._pending_db:
            return
        try:
            with self._pend_lock:
                rows = self._pend_conn().execute(
                    "SELECT id, kind, payload FROM shadow_pending "
                    "WHERE status='pending' AND attempts<? ORDER BY id",
                    (_PENDING_MAX_ATTEMPTS,)).fetchall()
                n_failed = self._pend_conn().execute(
                    "SELECT COUNT(*) FROM shadow_pending WHERE status='failed'"
                ).fetchone()[0]
            replayed = 0
            for pid, kind, payload_json in rows:
                if not self._switches.get(kind):
                    continue
                try:
                    payload = json.loads(payload_json)
                except Exception:
                    payload = {}
                with self._qlock:
                    self._q.append((kind, payload, pid))
                replayed += 1
            self.stats["pending_replayed"] += replayed
            if n_failed:
                # 表内 failed 行数即累计值（行只进不出），直接对齐避免重复计数
                self.stats["pending_failed"] = n_failed
            if replayed or n_failed:
                logger.info("影子 pending replay：%d 条重入队，%d 条 failed 留账",
                            replayed, n_failed)
        except Exception:
            self._pend_close()
            logger.exception("影子 pending replay 失败（降级，不阻塞启动）")
