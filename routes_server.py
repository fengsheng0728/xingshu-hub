"""星枢 Sync Hub — 服务器配置 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_server")

import asyncio, json, os, sqlite3, time, uuid
import yaml
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from deps import check_windows_firewall, get_lan_ips
from models import CONFIG, HUB_VERSION
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
        logger.debug("routes_server silent-except(api_server_get_config): %s", _exc)
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
        "version": HUB_VERSION,
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
        checks["database"] = {"status": f"error: {type(e).__name__}"}

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
        checks["tasks"] = {"status": f"error: {type(e).__name__}"}

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
        checks["memory_pool"] = {"status": f"error: {type(e).__name__}"}

    # ── 磁盘空间 ──
    try:
        db_dir = os.path.dirname(os.path.abspath(CONFIG.DB_PATH)) or "."
        import shutil
        usage = shutil.disk_usage(db_dir)
        free_gb = round(usage.free / (1024 * 1024 * 1024), 2)
        checks["disk"] = {"free_gb": free_gb}
    except Exception as e:
        checks["disk"] = {"status": f"error: {type(e).__name__}"}

    # ── ChromaDB ──
    try:
        if hub._chroma_collection is not None:
            count = hub._chroma_collection.count()
            checks["chromadb"] = {"status": "ok", "documents": count}
        else:
            checks["chromadb"] = {"status": "disabled"}
    except Exception as e:
        checks["chromadb"] = {"status": f"error: {type(e).__name__}"}

    # ── db 门面观测面（D-10：慢查询护栏统计；观测面不许让 health 500，必须 try/except） ──
    try:
        checks["db_facade"] = db_facade.stats_snapshot()
    except Exception as e:
        checks["db_facade"] = {"status": f"error: {type(e).__name__}"}

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
    return {"status": "alive", "version": HUB_VERSION, "uptime_seconds": round(time.time() - hub._start_time, 1)}


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


# ============ CD-084：/metrics（Prometheus 文本格式，监控最后一公里可做半边） ============

def _metric_line(lines, name, value, help_text="", mtype="gauge", labels=None):
    """拼装一条 Prometheus 文本行（value=None 时只落 HELP/TYPE，不落数值行）。"""
    if help_text:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
    if value is None:
        return
    if labels:
        lab = ",".join(f'{k}="{str(v).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'
                       for k, v in labels.items())
        lines.append(f"{name}{{{lab}}} {value}")
    else:
        lines.append(f"{name} {value}")


@router.get("/metrics")
async def metrics():
    """CD-084 监控最后一公里（/metrics 半边）：Prometheus 文本格式，零依赖手写。

    只读既有观测口径，不新增采集：行计数（memory_pool/tasks/agents）、
    db_facade 慢查询护栏计数、ChromaDB degraded 标记与向量数、备份最近状态
    （复用 maintenance/backup-status 的磁盘扫描口径）、db 体积、死信积压。
    每个采集块独立 try/except——观测面不许 500（与 /health 的 db_facade 段同纪律）。
    免认证白名单登记在 routes.py AUTH_ALLOWLIST_PATHS（该文件归别的槽位，
    未登记前 /metrics 走全局认证，带 hub_token/api_key 即可抓取）。
    """
    lines = []

    # ── 行计数：memory_pool / tasks by status ──
    try:
        conn = hub._db()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM memory_pool")
        _metric_line(lines, "synchub_memories_total", c.fetchone()[0],
                     "memory_pool 行数")
        c.execute("SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status")
        _metric_line(lines, "synchub_tasks_total", None, "tasks 行数（按状态）")
        for row in c.fetchall():
            _metric_line(lines, "synchub_tasks_total", row["cnt"],
                         labels={"status": row["status"] or "unknown"})
        c.execute("PRAGMA page_count")
        page_count = c.fetchone()[0]
        c.execute("PRAGMA page_size")
        page_size = c.fetchone()[0]
        _metric_line(lines, "synchub_db_size_bytes", page_count * page_size,
                     "SQLite 主库体积（page_count*page_size）")
        # CD-084：死信积压（老库未迁移时表可能不存在 → 独立 try，缺失即跳过）
        try:
            c.execute("SELECT COUNT(*), COALESCE(SUM(retried=0),0) FROM dead_letters")
            dl_total, dl_pending = c.fetchone()
            _metric_line(lines, "synchub_dead_letters_total", dl_total,
                         "死信账本总行数（CD-084）")
            _metric_line(lines, "synchub_dead_letters_pending", dl_pending,
                         "未处理死信数（retried=0，CD-084）")
        except Exception as _exc:
            logger.debug("routes_server silent-except(metrics) dead_letters: %s", _exc)
        conn.close()
    except Exception as _exc:
        logger.debug("routes_server silent-except(metrics) db: %s", _exc)

    # ── Agent 在线 ──
    try:
        total_agents = len(hub.agents)
        online_agents = sum(1 for a in hub.agents.values() if a.get("status") == "online")
        _metric_line(lines, "synchub_agents_total", total_agents, "注册 Agent 总数")
        _metric_line(lines, "synchub_agents_online", online_agents, "在线 Agent 数")
    except Exception as _exc:
        logger.debug("routes_server silent-except(metrics) agents: %s", _exc)

    # ── db 门面慢查询护栏（台账 slow_query_ms 口径，只读计数器） ──
    try:
        snap = db_facade.stats_snapshot()
        _metric_line(lines, "synchub_db_calls_total", snap["calls"],
                     "db_facade 调用总数", mtype="counter")
        _metric_line(lines, "synchub_db_slow_queries_total", snap["slow_calls"],
                     "db_facade 慢查询累计（>= threshold_ms）", mtype="counter")
        _metric_line(lines, "synchub_db_slow_query_max_ms", snap["max_ms"],
                     "db_facade 单次最大耗时 ms")
        _metric_line(lines, "synchub_db_slow_query_threshold_ms", snap["threshold_ms"],
                     "慢查询阈值 ms（config database.slow_query_ms）")
    except Exception as _exc:
        logger.debug("routes_server silent-except(metrics) db_facade: %s", _exc)

    # ── ChromaDB degraded 标记 + 向量数（台账 chroma_max_vectors 口径配套） ──
    try:
        coll = getattr(hub, "_chroma_collection", None)
        _metric_line(lines, "synchub_chromadb_degraded", 1 if coll is None else 0,
                     "ChromaDB 降级标记（1=不可用）")
        if coll is not None:
            _metric_line(lines, "synchub_chromadb_documents", coll.count(),
                         "ChromaDB 向量数")
        _metric_line(lines, "synchub_chromadb_max_vectors",
                     getattr(CONFIG, "CHROMA_MAX_VECTORS", 50000),
                     "向量数水位阈值（config database.chroma_max_vectors）")
    except Exception as _exc:
        logger.debug("routes_server silent-except(metrics) chromadb: %s", _exc)

    # ── 备份最近状态（复用 maintenance/backup-status 磁盘扫描口径，不读进程内 ts） ──
    try:
        from routes_maintenance import api_backup_status
        bk = await api_backup_status()
        _metric_line(lines, "synchub_backup_files", bk.get("count", 0),
                     "备份文件份数")
        latest = bk.get("latest") or {}
        ts = 0
        if latest.get("mtime"):
            ts = int(datetime.fromisoformat(latest["mtime"]).timestamp())
        _metric_line(lines, "synchub_backup_last_success_timestamp_seconds", ts,
                     "最近备份 mtime（Unix 秒，0=无备份）")
    except Exception as _exc:
        logger.debug("routes_server silent-except(metrics) backup: %s", _exc)

    _metric_line(lines, "synchub_uptime_seconds", round(time.time() - hub._start_time, 1),
                 "Hub 运行时长秒")
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8")
