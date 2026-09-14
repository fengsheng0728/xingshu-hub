"""星枢 Sync Hub — 服务器配置 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_server")

import asyncio, json, os, sqlite3, time, uuid
import yaml
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from deps import check_windows_firewall, get_lan_ips
from models import CONFIG
import db_facade
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
    principal_is_privileged,
)

router = APIRouter()

@router.get("/api/v1/server/config")
async def api_server_get_config():
    """读取服务器配置（host、port、auth、ui.new 灰度开关）"""
    config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
    config_path = os.path.join(config_dir, "config.yaml")
    host, port = "0.0.0.0", 3060
    auth_enabled = True
    ui_new = False
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        host = cfg.get("server", {}).get("host", "0.0.0.0")
        port = cfg.get("server", {}).get("port", 3060)
        auth_enabled = cfg.get("auth", {}).get("enabled", True)
        ui_new = bool(cfg.get("ui", {}).get("new", False))
    except Exception as _exc:
        logger.debug("routes_server silent-except @40: %s", _exc)
    return {
        "host": host,
        "port": port,
        "lan_enabled": host == "0.0.0.0",
        "auth_enabled": auth_enabled,
        "ui_new": ui_new,
        "ui_new_available": os.path.isfile("./dashboard_dist/index.html"),
        "network": await asyncio.to_thread(get_lan_ips),
    }


class ServerConfigUpdate(BaseModel):
    lan_enabled: bool = True
    ui_new: Optional[bool] = None  # U1 灰度开关；None=不改动


@router.post("/api/v1/server/config")
async def api_server_update_config(cfg: ServerConfigUpdate, request: Request):
    """更新服务器配置（lan_enabled 切换 0.0.0.0 ↔ 127.0.0.1；ui_new 新控制台灰度）

    T15 角色门：hub_token 或 role ∈ (manager, orchestrator) 才放行，否则 403——
    任意有效 worker key 改 lan_enabled 会把 Hub 暴露到 0.0.0.0（S8/S9 审查发现）。
    NO_AUTH 开发模式无身份语义，与全局中间件一致保持放行。
    GET /server/config 只读端点不动（dashboard 打开页面要读配置）。
    """
    if not NO_AUTH and not principal_is_privileged(request.scope.get("principal")):
        raise HTTPException(
            status_code=403,
            detail="需要 manager/orchestrator 角色或 hub_token 才能修改服务器配置",
        )
    config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
    config_path = os.path.join(config_dir, "config.yaml")
    os.makedirs(config_dir, exist_ok=True)
    # 读取现有配置
    full_config = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            full_config = yaml.safe_load(f) or {}
    # 更新
    full_config.setdefault("server", {})
    full_config["server"]["host"] = "0.0.0.0" if cfg.lan_enabled else "127.0.0.1"
    if cfg.ui_new is not None:
        full_config.setdefault("ui", {})
        full_config["ui"]["new"] = bool(cfg.ui_new)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(full_config, f, allow_unicode=True, default_flow_style=False)
    new_host = full_config["server"]["host"]
    return {
        "status": "ok",
        "host": new_host,
        "lan_enabled": new_host == "0.0.0.0",
        "ui_new": bool(full_config.get("ui", {}).get("new", False)),
        "restart_required": True,
    }


@router.get("/health")
async def health():
    """深度健康检查：数据库、任务队列、内存池、磁盘、ChromDB、运行时长"""
    checks = {
        "status": "ok",
        "version": "2.0.0",
        "uptime_seconds": round(time.time() - hub._start_time, 1),
    }

    # ── Agent 统计 ──
    total_agents = len(hub.agents)
    online_agents = sum(1 for a in hub.agents.values() if a.get("status") == "online")
    checks["agents"] = {"total": total_agents, "online": online_agents}

    # ── 数据库连接 + WAL 模式检查 ──
    try:
        conn = hub._db()
        c = conn.cursor()
        c.execute("SELECT 1")
        c.execute("PRAGMA journal_mode")
        wal_mode = c.fetchone()[0]
        c.execute("PRAGMA page_count")
        page_count = c.fetchone()[0]
        c.execute("PRAGMA page_size")
        page_size = c.fetchone()[0]
        db_size_mb = round((page_count * page_size) / (1024 * 1024), 2)
        conn.close()
        checks["database"] = {
            "status": "ok",
            "wal_mode": wal_mode,
            "size_mb": db_size_mb,
        }
    except Exception as e:
        checks["database"] = {"status": f"error: {e}"}

    # ── 任务队列健康 ──
    try:
        conn = hub._db()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status")
        by_status = {row["status"]: row["cnt"] for row in c.fetchall()}
        # 卡住的任务：assigned/in_progress 超过 24 小时未更新
        c.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('assigned','in_progress') "
            "AND updated_at < datetime('now', '-1 day')"
        )
        stale_tasks = c.fetchone()[0]
        conn.close()
        checks["tasks"] = {
            "by_status": by_status,
            "stale_tasks": stale_tasks,  # 超过1天没动的任务
        }
    except Exception as e:
        checks["tasks"] = {"status": f"error: {e}"}

    # ── 内存池统计 ──
    try:
        conn = hub._db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM memory_pool")
        total_memories = c.fetchone()[0]
        c.execute(
            "SELECT COUNT(*) FROM memory_pool WHERE created_at > datetime('now', '-1 hour')"
        )
        recent_memories = c.fetchone()[0]
        conn.close()
        checks["memory_pool"] = {
            "total": total_memories,
            "recent_1h": recent_memories,
        }
    except Exception as e:
        checks["memory_pool"] = {"status": f"error: {e}"}

    # ── 磁盘空间 ──
    try:
        db_dir = os.path.dirname(os.path.abspath(CONFIG.DB_PATH)) or "."
        import shutil
        usage = shutil.disk_usage(db_dir)
        free_gb = round(usage.free / (1024 * 1024 * 1024), 2)
        checks["disk"] = {"free_gb": free_gb}
    except Exception as e:
        checks["disk"] = {"status": f"error: {e}"}

    # ── ChromaDB ──
    try:
        if hub._chroma_collection is not None:
            count = hub._chroma_collection.count()
            checks["chromadb"] = {"status": "ok", "documents": count}
        else:
            checks["chromadb"] = {"status": "disabled"}
    except Exception as e:
        checks["chromadb"] = {"status": f"error: {e}"}

    # ── db 门面观测面（D-10：慢查询护栏统计；观测面不许让 health 500，必须 try/except） ──
    try:
        checks["db_facade"] = db_facade.stats_snapshot()
    except Exception as e:
        checks["db_facade"] = {"status": f"error: {e}"}

    # ── 整体状态判定 ──
    has_errors = any(
        isinstance(v, dict) and v.get("status", "").startswith("error")
        for v in [checks.get("database", {}), checks.get("chromadb", {})]
    )
    if has_errors:
        checks["status"] = "degraded"

    # ── 网络信息 ──
    try:
        checks["network"] = await asyncio.to_thread(get_lan_ips)
        checks["firewall"] = await asyncio.to_thread(check_windows_firewall, 3060)
    except Exception:
        checks["network"] = {"error": "获取失败"}
        checks["firewall"] = {"checked": False}

    # ── 告警汇总 ──
    warnings = []
    if isinstance(checks.get("database"), dict) and "error" in str(checks["database"].get("status", "")):
        warnings.append("数据库异常")

    disk = checks.get("disk", {})
    if isinstance(disk, dict) and disk.get("free_gb", 999) < 5:
        warnings.append(f"磁盘不足（剩余 {disk.get('free_gb', 0)} GB）")

    agents = checks.get("agents", {})
    if isinstance(agents, dict) and agents.get("total", 0) > 0:
        offline_pct = (agents["total"] - agents.get("online", 0)) / agents["total"]
        if offline_pct >= 0.5:
            warnings.append(f"Agent 大面积掉线（{round(offline_pct*100)}% offline）")

    fw = checks.get("firewall", {})
    if isinstance(fw, dict) and fw.get("status") == "warning":
        warnings.append("防火墙可能未放行端口")

    if warnings:
        checks["warnings"] = warnings
        if checks["status"] == "ok":
            checks["status"] = "degraded"

    return checks


# ============ P1 O1 可观测性：存活/就绪探针（2026-08-04） ============
# /healthz 存活：进程活着即 200 — 对接 K8s liveness / LB
# /readyz  就绪：SQLite SELECT 1 + ChromaDB（degraded 标记）→ 未就绪 503
# 两者分离是内网 K8s/负载均衡接入的基本要求。免鉴权（探针属于 allowlist 语义）。

@router.get("/healthz")
async def healthz():
    """存活探针 — 进程活着即 200。"""
    return {"status": "alive", "version": "2.0.0", "uptime_seconds": round(time.time() - hub._start_time, 1)}


@router.get("/readyz")
async def readyz():
    """就绪探针 — SQLite 可读 + ChromaDB 未降级 → 200；否则 503。
    复用现有 degraded 标记：_chroma_collection is None = ChromaDB 不可用。
    """
    ready = True
    problems = []

    # SQLite 探活
    try:
        conn = hub._db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception as e:
        ready = False
        problems.append(f"sqlite: {type(e).__name__}")

    # ChromaDB 就绪（degraded 标记）
    try:
        if getattr(hub, "_chroma_collection", None) is None:
            ready = False
            problems.append("chromadb: degraded/unavailable")
    except Exception as e:
        ready = False
        problems.append(f"chromadb: {type(e).__name__}")

    status_code = 200 if ready else 503
    body = {"status": "ready" if ready else "not_ready", "checks": problems}
    return JSONResponse(content=body, status_code=status_code)


# ============ 写入缓冲遥测 API ============

@router.get("/api/v1/buffer/stats")
async def api_buffer_stats(current_agent: str = Depends(get_current_agent)):
    """写入缓冲实时统计：队列深度、落库延迟、同步状态"""
    return {"status": "ok", **hub.buffer_stats()}


@router.get("/api/v1/buffer/trace")
async def api_buffer_trace(
    limit: int = 20,
    current_agent: str = Depends(get_current_agent),
):
    """最近写入跟踪：谁写了什么 → 何时入队 → 何时落库 → 何时同步"""
    traces = hub.recent_traces(limit)
    return {
        "status": "ok",
        "count": len(traces),
        "buffer_stats": hub.buffer_stats(),
        "traces": traces,
    }


# ═══ 技能/插件目录 ═══
@router.get("/api/v1/skills/catalog")
async def api_skills_catalog():
    """返回 Hub 端技能目录（社区精选）"""
    import json, os
    catalog_path = os.path.join(os.path.dirname(__file__), "skills_catalog.json")
    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)
        return {"catalog": catalog}
    except Exception:
        return {"catalog": []}
