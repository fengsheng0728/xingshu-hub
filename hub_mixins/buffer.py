"""星枢 SyncHub — buffer Mixin"""
import logging
logger = logging.getLogger("xingshu.buffer")

import asyncio
import json
import hashlib
import time
import os
import sqlite3
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import numpy as np

from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong

class BufferMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def start_write_buffer(self):
        asyncio.create_task(self._write_buffer_worker())
        asyncio.create_task(self._trace_persist_worker())  # CD-017: trace 攒批持久化
        asyncio.create_task(self._wiki_sync_throttler())
        logger.info("write buffer started")


    async def _trace_persist_worker(self):
        """CD-017: buffer_log 持久化攒批 worker — 独立队列 + 单事务批量 INSERT，
        入队路径零阻塞；最终一致（允许延迟不允许缺漏）。"""
        while self._running:
            batch = []
            try:
                item = await asyncio.wait_for(self._trace_persist_queue.get(), timeout=0.5)
                batch.append(item)
            except asyncio.TimeoutError:
                continue
            while len(batch) < 100:
                try:
                    batch.append(self._trace_persist_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await asyncio.to_thread(self._persist_trace_batch, batch)


    async def _write_buffer_worker(self):
        while self._running:
            batch = []
            try:
                batch.append(await asyncio.wait_for(self._write_queue.get(), timeout=0.5))
            except asyncio.TimeoutError:
                continue
            while len(batch) < 20:
                try:
                    batch.append(self._write_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await asyncio.to_thread(self._batch_write_knowledge, batch)
            for _ in batch:
                self._write_queue.task_done()


    async def _wiki_sync_throttler(self):
        while self._running:
            if self._wiki_sync_pending:
                now = time.time()
                if now - self._last_wiki_sync >= self._WIKI_SYNC_COOLDOWN:
                    self._wiki_sync_pending = False
                    self._last_wiki_sync = now
                    try:
                        from wiki_sync import sync
                        # CD-017: sync() 内含 sklearn embedding 生成（CPU 密集持 GIL）——
                        # 必须在 to_thread 执行，否则阻塞事件循环饿死所有 HTTP 请求
                        if os.environ.get("SYNC_HUB_DISABLE_WIKI_SYNC") == "1":
                            logger.debug("wiki sync disabled by SYNC_HUB_DISABLE_WIKI_SYNC")
                        else:
                            result = await asyncio.to_thread(sync)
                            logger.debug(f"Wiki sync: +{result['created']} ~{result['updated']} ={result['skipped']}")
                        now_ts = datetime.now(timezone.utc).isoformat()
                        for trace in self._write_trace:
                            if trace.get("flushed_at") and not trace.get("synced_at"):
                                trace["synced_at"] = now_ts

                        # 收件箱新条目 → 通知 dashboard（管理员审查入口）
                        inbox_new = result.get("inbox_new", 0)
                        if inbox_new > 0:
                            await self.create_notification(
                                "__dashboard__", "wiki_inbox",
                                f"{inbox_new} 条 Wiki 新页面待审查",
                                body="新知识已进入收件箱，请审核后发布。",
                                source="wiki_sync")
                    except Exception as e:
                        logger.warning(f"Wiki sync failed: {e}")
            await asyncio.sleep(1.0)


    def _batch_write_knowledge(self, batch: list):
        """批量落库（CD-017: 同步 def — 在 to_thread 线程池执行，不占事件循环 GIL）"""
        if not batch:
            return
        t0 = time.time()
        try:
            with self._db() as conn:
                c = conn.cursor()
                c.execute("BEGIN IMMEDIATE")
                for action, payload in batch:
                    if action == "upsert":
                        e = payload
                        c.execute("""
                            INSERT OR REPLACE INTO knowledge_base
                            (entry_id, title, content, tags, links, category, importance,
                             created_by, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                                (SELECT created_at FROM knowledge_base WHERE entry_id = ?), ?),
                                ?)
                        """, (e["entry_id"], e["title"], e["content"], e["tags_json"], e["links_json"],
                              e["category"], e["importance"], e["created_by"],
                              e["entry_id"], e["created_at"], e["updated_at"]))
                    elif action == "delete":
                        c.execute("DELETE FROM knowledge_base WHERE entry_id = ?", (payload["entry_id"],))
                conn.commit()

            # 阶段3-P1: 影子双写（落库后镜像 git 仓库群，零阻塞入队）
            try:
                if self._shadow is not None:
                    import json as _json
                    for _action, _e in batch:
                        if _action == "upsert":
                            _tags = _e.get("tags_json") or "[]"
                            try:
                                _tags = _json.loads(_tags) if isinstance(_tags, str) else list(_tags)
                            except Exception:
                                _tags = []
                            self._shadow.submit("knowledge", {
                                "entry_id": _e["entry_id"],
                                "title": _e.get("title", ""),
                                "content": _e.get("content", ""),
                                "created_by": _e.get("created_by", ""),
                                "tags": _tags,
                                "date": (_e.get("updated_at") or "")[:10],
                            })
            except Exception:
                pass  # 影子失败不阻塞主链路（D4）

            latency_ms = (time.time() - t0) * 1000
            self._flush_count += 1
            self._total_flushed += len(batch)
            self._last_flush_at = time.time()
            self._flush_latencies.append(latency_ms)
            if len(self._flush_latencies) > 100:
                self._flush_latencies.pop(0)

            now = datetime.now(timezone.utc).isoformat()
            for item in batch:
                for trace in self._write_trace:
                    if trace.get("entry_id") == item[1].get("entry_id") and not trace.get("flushed_at"):
                        trace["flushed_at"] = now
                        trace["flush_latency_ms"] = round(latency_ms, 1)
            # CD-017: buffer_log flushed_at 批量更新合并为单事务（原来每 item 一次 commit，
            # 20 次/批在事件循环里同步 SQLite 写 → 阻塞）。语义不变：WAL replay 仍可恢复。
            try:
                with self._db() as conn2:
                    c2 = conn2.cursor()
                    c2.execute("BEGIN")
                    for item in batch:
                        c2.execute(
                            "UPDATE buffer_log SET flushed_at=?, flush_latency_ms=? WHERE entry_id=? AND flushed_at IS NULL",
                            (now, round(latency_ms, 1), item[1].get("entry_id")))
                    conn2.commit()
            except Exception as _exc:
                logger.debug("buffer silent-except @171: %s", _exc)

            logger.debug(f"batch write: {len(batch)} items, {latency_ms:.1f}ms")
        except Exception as e:
            logger.error(f"batch write failed: {e}")


    def _load_buffer_log(self):
        """启动时从 buffer_log 表恢复写入 trace 与累计计数（WAL replay）"""
        try:
            with self._db() as conn:
                c = conn.cursor()
                c.execute("""CREATE TABLE IF NOT EXISTS buffer_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT, agent_id TEXT, title TEXT, entry_id TEXT,
                    queued_at TEXT, flushed_at TEXT, synced_at TEXT,
                    flush_latency_ms REAL
                )""")
                # CD-017: entry_id 索引 — UPDATE flushed_at WHERE entry_id 全表扫在多档压测
                # 累积上万行后变慢（200 并发峰值延迟回升），索引后 UPDATE 走点查
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_buffer_log_entry ON buffer_log(entry_id, flushed_at)")
                c.execute("SELECT action, agent_id, title, entry_id, queued_at, flushed_at, synced_at, flush_latency_ms FROM buffer_log ORDER BY id DESC LIMIT 200")
                rows = c.fetchall()
                # 恢复最近 trace（倒序→正序）
                for r_ in reversed(rows):
                    self._write_trace.append({
                        "action": r_[0], "agent_id": r_[1], "title": r_[2],
                        "entry_id": r_[3], "queued_at": r_[4],
                        "flushed_at": r_[5], "synced_at": r_[6],
                        "flush_latency_ms": r_[7],
                    })
                # 恢复累计计数（已 flush 的条数）
                c.execute("SELECT COUNT(*), COUNT(flushed_at) FROM buffer_log")
                total, flushed = c.fetchone()
                self._total_flushed = flushed or 0
                self._flush_count = flushed or 0
            if flushed:
                logger.info(f"buffer: 持久化恢复 {flushed} 条已 flush 记录（buffer_log replay）")
        except Exception as e:
            logger.warning(f"buffer: buffer_log 恢复失败: {e}")


    def _record_trace(self, action: str, agent_id: str, title: str, entry_id: str):
        trace = {
            "action": action, "agent_id": agent_id, "title": title,
            "entry_id": entry_id,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "flushed_at": None, "synced_at": None, "flush_latency_ms": None,
        }
        self._write_trace.append(trace)
        if len(self._write_trace) > 200:
            self._write_trace.pop(0)
        # CD-017: buffer_log 持久化移出事件循环关键路径。
        # 压测验证：asyncio.to_thread 每请求一个线程池任务在 200 并发下形成任务风暴
        # （线程池默认 32 worker 全部抢 SQLite 写锁，busy_timeout 5000 等待），
        # 改用独立异步队列攒批持久化（见 _trace_persist_worker），入队路径零阻塞。
        try:
            self._trace_persist_queue.put_nowait(
                (action, agent_id, title, entry_id, trace["queued_at"]))
        except Exception:
            pass  # 队列满/未启动 → 仅内存 trace，不阻塞入队


    def _persist_trace_db(self, action: str, agent_id: str, title: str, entry_id: str, queued_at: str):
        """持久化到 buffer_log（WAL commit log）— 线程池执行，不阻塞事件循环"""
        try:
            with self._db() as conn:
                c = conn.cursor()
                c.execute(
                    "INSERT INTO buffer_log (action, agent_id, title, entry_id, queued_at) VALUES (?,?,?,?,?)",
                    (action, agent_id, title, entry_id, queued_at))
                conn.commit()
        except Exception:
            pass  # 持久化失败不阻塞入队


    def buffer_stats(self) -> dict:
        latencies = self._flush_latencies
        return {
            "queue_depth": self._write_queue.qsize(),
            "queue_max": self._write_queue.maxsize,
            "total_flushed": self._total_flushed,
            "flush_count": self._flush_count,
            "last_flush_at": self._last_flush_at,
            "avg_batch_size": round(self._total_flushed / max(1, self._flush_count), 1),
            "avg_flush_latency_ms": round(sum(latencies) / max(1, len(latencies)), 1),
            "p99_flush_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.99)] if len(latencies) > 10 else 0, 1),
            "wiki_sync_pending": self._wiki_sync_pending,
            "last_wiki_sync": self._last_wiki_sync,
            "sync_fallback_count": getattr(self, "_sync_fallback_count", 0),
        }


    def _enqueue_write(self, kind: str, payload: dict) -> str:
        """入队写入缓冲。队列满时降级直写（不阻塞 HTTP，不丢数据）。"""
        try:
            self._write_queue.put_nowait((kind, payload))
            return "queued"
        except asyncio.QueueFull:
            # 降级：绕过队列直接批写，记录计数（CD-017: to_thread 不阻塞事件循环）
            self._sync_fallback_count = getattr(self, "_sync_fallback_count", 0) + 1
            asyncio.create_task(asyncio.to_thread(self._batch_write_knowledge, [(kind, payload)]))
            logger.warning(
                f"buffer: queue full ({self._write_queue.maxsize}), fallback direct write ({kind})"
            )
            return "fallback"


    def recent_traces(self, limit: int = 50) -> list:
        return self._write_trace[-limit:]

    # ============ 企业知识库 ============


