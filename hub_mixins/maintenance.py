"""星枢 SyncHub — maintenance Mixin"""
import logging
logger = logging.getLogger("xingshu.maintenance")

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

from deps import CONFIG
from logfmt import rotate_request_id
from notifications import notifications
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong

class MaintenanceMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def _run_backup(self):
        """启动/周期备份（CD-077，2026-09-23 运维轮）

        基线的裸拷主库文件（把 DB 文件直接复制走）有两个洞：
          ① WAL 模式下新提交还在 `-wal` 里，裸拷主库文件拿到的是旧快照；
          ② 只拷 DB，`chroma_db/` 不在备份里 → 恢复时向量索引与记忆池对不上。
        改为复用 `hub_cli.cmd_backup`（VACUUM INTO 一致性快照 + ChromaDB 目录 + marker/manifest）——
        **同一份实现两处调用**，不再维护两份备份逻辑（手动 CLI 与自动备份口径一致）。

        观测（同轮收口「失败静默」）：失败落 `events(backup_failed)` 可机器判读的痕迹 + logger.error。
        通知渠道未验证（CD-018），故只作附加路径，落链才是判据。

        内置冷却：距上次备份 < 1 小时则跳过，防止快速重启产生大量备份。
        """
        try:
            # 冷却检查：1 小时内不重复备份
            now_ts = datetime.now(timezone.utc).timestamp()
            if hasattr(self, '_last_backup_ts') and (now_ts - self._last_backup_ts) < 3600:
                return
            self._last_backup_ts = now_ts

            import yaml
            config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
            with open(os.path.join(config_dir, "config.yaml"), "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            backup_cfg = cfg.get("database", {})
            if not backup_cfg.get("backup_enabled", True):
                return

            # 保留天数：新字段 backup_keep_days 优先，回退既有 backup_interval_days（历史字段名
            # 语义就是保留天数，CD-077 保留向后兼容），最后默认 7 天。
            keep_days = backup_cfg.get("backup_keep_days",
                                       backup_cfg.get("backup_interval_days", 7))
            backup_dir = os.path.join(config_dir, "backups")
            os.makedirs(backup_dir, exist_ok=True)

            # 复用 CLI 的同一实现（VACUUM INTO + chroma 目录 + marker/manifest 对齐）
            # to_thread：cmd_backup 是同步重 IO（VACUUM INTO 全库快照 + copytree），
            # 直调会把事件循环串行化（CD-017 同类教训），卸载到线程执行。
            import hub_cli
            result = await asyncio.to_thread(
                hub_cli.cmd_backup, backup_dir, CONFIG.DB_PATH, CONFIG.CHROMA_PATH)
            logger.info(f"数据库备份完成: {result.get('sqlite')}")

            # 清理过期备份（覆盖新形态产物：db / manifest.json / chroma_db）
            self._cleanup_old_backups(backup_dir, keep_days)
        except Exception as e:
            logger.error(f"数据库备份失败({type(e).__name__}): {e}")
            # CD-077：失败必须留可机器判读的痕迹，不再静默（参照 hub_core._log_event 口径）
            try:
                await self._log_event(
                    "backup_failed", "__system__",
                    {"error": str(e), "error_type": type(e).__name__,
                     "backup_dir": os.path.join(
                         os.environ.get("SYNC_HUB_CONFIG_DIR", "./config"), "backups")},
                )
            except Exception as log_exc:
                logger.error(f"backup_failed 事件落链失败: {log_exc}")

    def _cleanup_old_backups(self, backup_dir: str, keep_days: int = 7) -> None:
        """清理过期备份产物（CD-077）：只动本实现自己创建的东西。

        - 命名匹配：`sync_hub.*.db` / `manifest.json` / `chroma_db/`（目录）
        - 判据：mtime 早于 now - keep_days*86400
        - **外来文件一律不碰**（如运维手工放进来的 manual_important.db、user_notes/）
        - 清理失败只告警，绝不影响备份主流程
        """
        cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
        try:
            names = os.listdir(backup_dir)
        except OSError as e:
            logger.warning(f"备份目录不可读，跳过清理: {e}")
            return
        for fname in names:
            fpath = os.path.join(backup_dir, fname)
            try:
                if os.path.getmtime(fpath) >= cutoff:
                    continue
                if fname.startswith("sync_hub.") and fname.endswith(".db"):
                    os.remove(fpath)
                elif fname == "manifest.json":
                    os.remove(fpath)
                elif fname == "chroma_db" and os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
                else:
                    continue  # 非本实现产物：不碰
                logger.info(f"清理过期备份: {fname}")
            except OSError as e:
                logger.warning(f"清理过期备份失败({fname}): {e}")


    async def _run_cleanup(self):
        """启动时清理过期数据"""
        try:
            with self._db() as conn:
                c = conn.cursor()

                # 清理过期事件
                cutoff_events = (datetime.now(timezone.utc).timestamp()
                                 - CONFIG.RETENTION_EVENTS_DAYS * 86400)
                c.execute(
                    "DELETE FROM events WHERE timestamp < ?",
                    (datetime.fromtimestamp(cutoff_events, tz=timezone.utc).isoformat(),),
                )
                events_deleted = c.rowcount

                # 清理过期记忆（低重要性 + 长期未访问）
                cutoff_mem = (datetime.now(timezone.utc).timestamp()
                              - CONFIG.RETENTION_MEMORY_DAYS * 86400)
                c.execute(
                    "DELETE FROM memory_pool WHERE created_at < ? AND importance < 0.5",
                    (datetime.fromtimestamp(cutoff_mem, tz=timezone.utc).isoformat(),),
                )
                mem_deleted = c.rowcount

                # CD-064 残余①：memory_pool_fts 是外部内容表
                # （content='memory_pool'）且全库无触发器，直删主表只会留孤儿词元
                # （memory_search 回查主表过滤，语义不脏但索引持续漂移）；
                # 批量清理后整表 rebuild 兜平——幂等、成本 O(行数)，
                # 清理本身是启停期低频动作。表缺失（极老库）
                # 或 FTS 不可用则跳过，不连累 events/memory/tasks 清理。
                if mem_deleted:
                    try:
                        c.execute("INSERT INTO memory_pool_fts(memory_pool_fts) VALUES('rebuild')")
                        logger.info(f"memory FTS 重建（配合清理 {mem_deleted} 条）")
                    except sqlite3.OperationalError as fts_err:
                        logger.warning(f"memory FTS rebuild 跳过: {fts_err}")

                # 清理已完成/已取消的旧任务
                cutoff_tasks = (datetime.now(timezone.utc).timestamp()
                                - CONFIG.RETENTION_TASKS_DAYS * 86400)
                c.execute(
                    "DELETE FROM tasks WHERE status IN ('completed','cancelled','failed') AND updated_at < ?",
                    (datetime.fromtimestamp(cutoff_tasks, tz=timezone.utc).isoformat(),),
                )
                tasks_deleted = c.rowcount

                conn.commit()

            # CD-021: gateway_read_log 读审计保留清理——独立 try(表缺失/异常不连累
            # events/memory/tasks 清理;created_at 是 datetime('now') 无 T 格式,
            # cutoff 用同格式字符串比较,避免 ISO T 分隔符的字典序偏差)
            readlog_deleted = 0
            try:
                with self._db() as conn:
                    c = conn.cursor()
                    cutoff_rl = (datetime.now(timezone.utc)
                                 - timedelta(days=CONFIG.RETENTION_READLOG_DAYS)) \
                        .strftime("%Y-%m-%d %H:%M:%S")
                    c.execute("DELETE FROM gateway_read_log WHERE created_at < ?",
                              (cutoff_rl,))
                    readlog_deleted = c.rowcount
                    conn.commit()
            except Exception as e:
                logger.warning(f"gateway_read_log 清理失败: {e}")

            if events_deleted or mem_deleted or tasks_deleted or readlog_deleted:
                logger.info(
                    f"数据清理完成: 事件 {events_deleted}, 记忆 {mem_deleted}, 任务 {tasks_deleted}, 读审计 {readlog_deleted}"
                )
        except Exception as e:
            logger.warning(f"数据清理失败: {e}")


    async def force_cleanup(self) -> dict:
        """手动触发清理（通过 API 调用）"""
        await self._run_cleanup()
        return await self._db_stats()


    async def _db_stats(self) -> dict:
        """获取数据库统计"""
        with self._db() as conn:
            c = conn.cursor()
            stats = {}
            for table in ["memory_pool", "events", "tasks", "disclosure_log", "disclosure_requests"]:
                try:
                    c.execute(f"SELECT COUNT(*) FROM {table}")
                    stats[table] = c.fetchone()[0]
                except Exception:
                    stats[table] = -1
            try:
                c.execute("PRAGMA page_count")
                pc = c.fetchone()[0]
                c.execute("PRAGMA page_size")
                ps = c.fetchone()[0]
                stats["db_size_mb"] = round(pc * ps / (1024 * 1024), 2)
            except Exception:
                stats["db_size_mb"] = -1
        return stats


    async def _keepalive_ping(self):
        """P0-FIX: 每30s向活跃WS连接发送ping，维持心跳"""
        while self._running:
            await asyncio.sleep(self._HEARTBEAT_INTERVAL)  # 30s
            rotate_request_id("keepalive")  # CD-108：每轮 tick 轮换 request id
            async with self._lock:
                for aid in list(self.active_ws.keys()):
                    try:
                        ws = self.active_ws[aid]
                        ping = envelope_ping()
                        await ws.send_text(serialize(ping))
                        log_ping_pong('out', ping)
                    except Exception as _exc:
                        logger.debug("maintenance silent-except(_keepalive_ping): %s", _exc)


    async def _cleanup_loop(self):
        """心跳超时清理 + 定时备份（每30分钟）"""
        backup_counter = 0
        while self._running:
            await asyncio.sleep(120)  # 2 分钟心跳检查（99 人以内足够）
            rotate_request_id("cleanup-loop")  # CD-108：每轮 tick 轮换 request id
            backup_counter += 1
            # 每 30 分钟备份一次 (30×60/120 = 15 cycles)
            if backup_counter >= 15:
                asyncio.create_task(self._run_backup())
                backup_counter = 0
            # S1：api_key 到期轮换（每 30 分钟查一次，幂等；轮换事件入审计）
            if getattr(CONFIG, "API_KEY_ROTATION_DAYS", 0) > 0:
                asyncio.create_task(self._rotate_expired_keys())
            async with self._lock:
                now = datetime.now(timezone.utc)
                offline_ids = []
                for aid, info in self.agents.items():
                    try:
                        last = datetime.fromisoformat(info["last_heartbeat"])
                    except ValueError:
                        continue
                    try:
                        if (now - last).total_seconds() > CONFIG.HEARTBEAT_TIMEOUT:
                            info["status"] = "offline"
                            offline_ids.append(aid)
                            if aid in self.active_ws:
                                try:
                                    await self.active_ws[aid].close()
                                except Exception as _exc:
                                    logger.debug("maintenance silent-except(_cleanup_loop): %s", _exc)
                                del self.active_ws[aid]
                    except Exception as _exc:
                        logger.debug("maintenance silent-except(_cleanup_loop): %s", _exc)

                if offline_ids:
                    with self._db() as conn:
                        c = conn.cursor()
                        for aid in offline_ids:
                            c.execute(
                                "UPDATE agents SET status = 'offline' WHERE agent_id = ?",
                                (aid,),
                            )
                            # P2B: 通知该 Agent 的 manager
                            agent_info = self.agents.get(aid, {})
                            manager_list = []
                            for mid, minfo in self.agents.items():
                                if aid in minfo.get("managed_agents", []):
                                    manager_list.append(mid)
                            for mid in manager_list:
                                await notifications.notify(mid, {
                                    "type": "agent_offline",
                                    "agent_id": aid,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                })
                            # U2: dashboard 通道同步掉线事件（Ops Agent 在线 / Activity 事件流）
                            await notifications.broadcast_dashboard({
                                "type": "agent_offline",
                                "agent_id": aid,
                            })
                        conn.commit()

    # ── 内网组队 ──


    async def _rotate_expired_keys(self):
        """S1：api_key 到期轮换（幂等，30 分钟粒度）。轮换成功 → 事件审计 + 通知管理员。

        语义：LocalProvider.rotate_keys() 把到期主 key 移入 prev（24h 宽限），
        新 key 写主位；Agent 端下次 bootstrap 拿到新 key，宽限期内旧 key 仍可用，
        不会因轮换即断连（评审 D6 要求）。轮换事件本身入审计（谁轮换/何时）。
        """
        try:
            from auth_provider import LocalProvider
            provider = None
            # 复用 routes 的 provider（local/hybrid 内含轮换能力）
            try:
                from routes import _auth_provider
                provider = _auth_provider()
            except Exception as _exc:
                logger.debug("maintenance silent-except(_rotate_expired_keys): %s", _exc)
            if provider is None:
                provider = LocalProvider(CONFIG)
            if not hasattr(provider, "rotate_keys"):
                return
            rotated = provider.rotate_keys()
            if rotated > 0:
                await self._log_event("api_key_rotation", "__system__", {
                    "rotated": rotated, "reason": "expired"})
                try:
                    await self.create_notification(
                        "__dashboard__", "security",
                        f"api_key 轮换: {rotated} 个 Agent",
                        f"检测到 {rotated} 个 Agent 的 api_key 到期，已自动轮换（旧 key 24h 宽限）",
                        source="key_rotation")
                except Exception as _exc:
                    logger.warning("maintenance silent-except(_rotate_expired_keys): %s", _exc)
        except Exception as e:
            self.logger.warning("rotate_expired_keys failed: %s", e)


