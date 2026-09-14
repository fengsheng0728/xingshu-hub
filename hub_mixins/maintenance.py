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
from notifications import notifications
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong

class MaintenanceMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def _run_backup(self):
        """启动时备份数据库（基于 config.yaml 中的 backup 配置）
        内置冷却：距上次备份 < 1 小时则跳过，防止快速重启产生大量备份。
        """
        import shutil
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

            keep_days = backup_cfg.get("backup_interval_days", 7)
            backup_dir = os.path.join(config_dir, "backups")
            os.makedirs(backup_dir, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
            src = CONFIG.DB_PATH
            dst = os.path.join(backup_dir, f"sync_hub.{timestamp}.db")
            shutil.copy2(src, dst)
            logger.info(f"数据库备份完成: {dst}")

            # 清理过期备份
            cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
            for fname in os.listdir(backup_dir):
                if fname.startswith("sync_hub.") and fname.endswith(".db"):
                    fpath = os.path.join(backup_dir, fname)
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        logger.info(f"清理过期备份: {fname}")
        except Exception as e:
            logger.warning(f"数据库备份失败: {e}")


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
            async with self._lock:
                for aid in list(self.active_ws.keys()):
                    try:
                        ws = self.active_ws[aid]
                        ping = envelope_ping()
                        await ws.send_text(serialize(ping))
                        log_ping_pong('out', ping)
                    except Exception as _exc:
                        logger.debug("maintenance silent-except @164: %s", _exc)


    async def _cleanup_loop(self):
        """心跳超时清理 + 定时备份（每30分钟）"""
        backup_counter = 0
        while self._running:
            await asyncio.sleep(120)  # 2 分钟心跳检查（99 人以内足够）
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
                                    logger.debug("maintenance silent-except @196: %s", _exc)
                                del self.active_ws[aid]
                    except Exception as _exc:
                        logger.debug("maintenance silent-except @199: %s", _exc)

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
                logger.debug("maintenance silent-except @246: %s", _exc)
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
                    logger.warning("maintenance silent-except @262: %s", _exc)
        except Exception as e:
            self.logger.warning("rotate_expired_keys failed: %s", e)


