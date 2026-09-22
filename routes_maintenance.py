"""星枢 Sync Hub — 维护 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_maintenance")

import asyncio

from fastapi import APIRouter, Depends, Request

from db import get_lan_ips, check_windows_firewall
from deps import CONFIG
from hub_core import hub
from routes_common import get_current_agent, require_ops_privilege, log_ops_trigger

router = APIRouter()


@router.get("/api/v1/maintenance/db-stats")
async def api_db_stats():
    """数据库统计（各表行数 + 总大小）"""
    return await hub._db_stats()


@router.post("/api/v1/maintenance/cleanup")
async def api_force_cleanup(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """手动触发数据清理（CD-061：补 Depends 认证 + 重运维统一门 + 触发审计）"""
    await require_ops_privilege(request, "/api/v1/maintenance/cleanup", current_agent)
    before = await hub._db_stats()
    result = await hub.force_cleanup()
    counts = {}
    for table in ("memory_pool", "events", "tasks"):
        after = result.get(table) if isinstance(result, dict) else None
        counts[table] = {
            "before": before.get(table, "unavailable"),
            "after": after if after is not None else "unavailable",
        }
    await log_ops_trigger("/api/v1/maintenance/cleanup", current_agent, counts)
    return result


@router.get("/api/v1/maintenance/shadow-stats")
async def api_shadow_stats():
    """G1 批2：影子双写 stats 全量 + pending 未完成计数 + worker 存活状态。

    影子未启用（enabled=false / 未构造）时返回 enabled=False 占位，
    与影子「增强不是依赖」语义一致——端点本身永远可用。
    """
    w = getattr(hub, "_shadow", None)
    if w is None:
        # CD-047(L3): 占位响应同样带 switches 键（只增键，既有键与结构不变）
        return {"enabled": False, "stats": None, "kind_count": {},
                "queue_depth": 0, "pending_incomplete": 0,
                "daemon_alive": False, "switches": {}}
    return w.stats_snapshot()


@router.get("/api/v1/maintenance/network")
async def api_network_info():
    """本机网络信息（LAN IP + 防火墙状态）"""
    return {
        "network": await asyncio.to_thread(get_lan_ips),
        "firewall": await asyncio.to_thread(check_windows_firewall, 3060),
    }


@router.post("/api/v1/maintenance/shadow-reconcile")
async def api_shadow_reconcile(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """T31: 手动触发影子档案对账（孤儿/跨天重复归档）。走 CD-061 运维门 + ops_trigger 审计。"""
    await require_ops_privilege(request, "/api/v1/maintenance/shadow-reconcile", current_agent)
    from hub_mixins.shadow.reconcile import reconcile_shadow_archives
    stats = await asyncio.to_thread(
        reconcile_shadow_archives, hub.data_trunk,
        db_path=getattr(hub, "_audit_db_path", None) or getattr(CONFIG, "DB_PATH", None))
    await log_ops_trigger("/api/v1/maintenance/shadow-reconcile", current_agent, stats)
    return stats


@router.get("/api/v1/maintenance/backup-status")
async def api_backup_status():
    """U2 O3：备份状态 — 扫描 backups 目录（最近备份/数量/保留策略）。

    以磁盘实际备份文件为准（跨重启可信），而非进程内的 _last_backup_ts。
    """
    import os
    import yaml
    from datetime import datetime, timezone

    config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
    backup_cfg = {}
    try:
        with open(os.path.join(config_dir, "config.yaml"), "r", encoding="utf-8") as f:
            backup_cfg = (yaml.safe_load(f) or {}).get("database", {})
    except Exception as _exc:
        logger.debug("routes_maintenance silent-except(api_backup_status): %s", _exc)

    backup_dir = os.path.join(config_dir, "backups")
    files = []
    if os.path.isdir(backup_dir):
        for fname in os.listdir(backup_dir):
            if fname.startswith("sync_hub.") and fname.endswith(".db"):
                st = os.stat(os.path.join(backup_dir, fname))
                files.append({
                    "file": fname,
                    "size_mb": round(st.st_size / (1024 * 1024), 2),
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                })
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {
        "enabled": backup_cfg.get("backup_enabled", True),
        "keep_days": backup_cfg.get("backup_interval_days", 7),
        "dir": backup_dir,
        "count": len(files),
        "latest": files[0] if files else None,
    }
