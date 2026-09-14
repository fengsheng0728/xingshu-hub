"""
星枢 Sync Hub — FastAPI 路由
"""
import logging
logger = logging.getLogger("xingshu.routes")

import asyncio, json, os, sys, time, sqlite3, yaml
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from contextlib import asynccontextmanager
from routes_automation import register as _register_automation, automation_scheduler
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Depends
from transport_audit import log_transport_frame, log_dispatch_in, log_result_out, log_ack_in, log_ping_pong
from envelope import envelope_result, envelope_dispatch, envelope_ack, envelope_ping, envelope_pong, parse_envelope, is_legacy_flat, extract_payload, serialize
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import uuid
from db import get_lan_ips, check_windows_firewall
from models import CONFIG
from hub_core import hub
from models import HubAgentConfig, DisclosureRules
from hub_core import hub_agent
from notifications import notifications
from logfmt import set_trace_id, get_trace_id
from auth_provider import get_auth_provider, Principal

# Phase 2：共享认证依赖已拆分至 routes_common（此处 re-export 保持外部引用兼容：
# `from routes import NO_AUTH/_auth_provider/_version_ge/...` 语义不变）
from routes_common import (
    agent_quotas_snapshot,  # CD-040: O4 配额改用内存快照（不再每请求同步查库）
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
    principal_is_privileged, _version_ge,
)
# Phase 2 拆分：共享工作区 helper 移入 routes_shared（WS 处理器保留在本文件，运行时引用）
from routes_shared import _ws, _shared_watchers, _broadcast_shared_update


# ============ FastAPI 应用 ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    await hub._restore_agents()
    asyncio.create_task(hub._cleanup_loop())
    asyncio.create_task(hub._keepalive_ping())
    await hub.start_write_buffer()  # 写入缓冲 + Wiki 自动同步
    # 启动时执行一次数据库备份
    asyncio.create_task(hub._run_backup())
    asyncio.create_task(hub._run_cleanup())
    asyncio.create_task(automation_scheduler(hub))
    # XS-004：审计锚定外发（启动即推一次 + 按 AUDIT_ANCHOR_INTERVAL 周期推送；
    # 默认空 URL 列表 = 休眠，仅写本地快照）
    from routes_audit import anchor_export_loop
    asyncio.create_task(anchor_export_loop())
    # 集成层（§七）：拉取调度循环
    from integrations.registry import integration_scheduler
    from routes_integrations import registry as _integrations_registry
    asyncio.create_task(integration_scheduler(hub, _integrations_registry))
    # 共享工作区
    from shared_workspace import SharedWorkspace
    import shared_workspace
    from models import CONFIG
    _ws = SharedWorkspace(CONFIG.DB_PATH, store_dir="./shared_store",
                          shadow=getattr(hub, "_shadow", None))
    await _ws.start()
    shared_workspace.workspace = _ws
    # Electron 模式：通知主进程就绪
    if os.environ.get("SYNC_HUB_ELECTRON") == "1":
        print("READY", flush=True)
    yield
    # 关闭：影子双写器 flush 剩余队列（D4 不阻塞，尽力而为）
    try:
        if getattr(hub, "_shadow", None) is not None:
            hub._shadow.stop(flush=True)
    except Exception as _exc:
        logger.debug("routes silent-except @72: %s", _exc)


app = FastAPI(
    title="Hermes Sync Hub - Progressive Disclosure",
    description="写入隔离 + 渐进式披露架构 · 适用 99 人以内小公司",
    version="2.0.0",
    lifespan=lifespan,
)
# _register_automation called at bottom
def _bundle_dir(name: str) -> str:
    """静态资源目录解析: cwd 优先, PyInstaller 解包目录(sys._MEIPASS)兜底。

    打包修复 2026-09-06: 原 './dashboard' 相对 cwd 硬解析, exe 启动(任意 cwd)时
    dashboard 在 _MEIPASS 内 → RuntimeError 启动即崩(7/15 产物同样从未跑通过,
    验证即抓)。源码态 cwd=仓库根行为不变。
    """
    for base in (os.getcwd(), getattr(sys, "_MEIPASS", "")):
        p = os.path.join(base, name) if base else ""
        if p and os.path.isdir(p):
            return p
    return os.path.join(os.getcwd(), name)


app.mount("/static", StaticFiles(directory=_bundle_dir("dashboard")), name="static")

# Phase U1：新控制台（hub_ui 构建产物）与旧页回退挂载
# 灰度：config.yaml ui.new=true 时 / 直出 dashboard_dist/index.html（见 dashboard() 路由）
_ui_dist = _bundle_dir("dashboard_dist")
if os.path.isdir(_ui_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(_ui_dist, "assets")), name="ui-assets")
app.mount("/legacy", StaticFiles(directory=_bundle_dir("dashboard"), html=True), name="legacy")  # 旧 6 页保留一个版本周期

# MCP Server (SSE) — Wiki 知识库 MCP 端点
from mcp_server import mcp
app.mount("/mcp", mcp.sse_app())

# ============ 认证配置（P4） ============
import hmac as _hmac
import json as _json

# 部署级 token 鉴权中间件（P0 安全底线）：
# - 除 allowlist 外所有 HTTP 请求必须带有效凭据（hub_token 或 agents.api_key）
# - hub_token 读自 config.yaml auth.hub_token（CONFIG.HUB_TOKEN）
# - NO_AUTH=1（开发/测试）时全放行
# - /static、/docs 等挂载/文档路径放行；/mcp 已移出豁免（T0-3，CD-013），走统一认证
AUTH_ALLOWLIST_PREFIXES = ("/static", "/assets", "/legacy", "/docs", "/openapi.json")
# /health 存活探针；6 页面壳（T0-3 无 token 可打开输入界面，页面内 fetch 才鉴权）；
# register/bootstrap 为引导端点（只发新 api_key，不读业务数据）——无 key 死锁豁免，
# 配 hub_token 后注册请求带 token 同样通过（D2 防的是业务端点泄露）；
# team/proxy/disclose 函数内自校验 remote_api_key（team_members 表）——自认证端点豁免，
# P2 升级为 AES-GCM 加密信道时在其内部叠加；
# team/pair/exchange 配对握手端点——用 6 位配对码自认证（跨 Hub 调用，无本地 api_key）
AUTH_ALLOWLIST_PATHS = {
    "/health", "/healthz", "/readyz", "/", "/showcase", "/knowledge", "/chat", "/report", "/wiki", "/team",
    "/api/v1/agents/register", "/api/v1/agents/bootstrap",
    "/api/v1/team/proxy/disclose",
    "/api/v1/team/pair/exchange",
}

# T11：认证豁免但参与限速的 allowlist 路径——pair/exchange 用 6 位码自认证，
# 免限速会被爆破（100 万码空间），必须走每 IP 限速；认证豁免（下方 allowlist 放行）保持原样
RATE_LIMITED_ALLOWLIST = ("/api/v1/team/pair/exchange",)


def _endpoint_allowed(path: str, allowed: List[str]) -> bool:
    """S1K scoped key endpoints 白名单判定（纯函数，T14 自中间件内嵌闭包提取）。

    仅两种命中（/api/v1 前缀先归一剥离）：
      1. 精确匹配：/mem == /mem
      2. 边界匹配：a=`/mem` 命中 `/mem/foo`（`a.rstrip("/") + "/"` 前缀）
    禁止裸 `startswith(a)` 前缀匹配——a=`/mem` 会错误放行 `/memory/*`（T14/S6 越权）。
    """
    _norm = path
    if _norm.startswith("/api/v1"):
        _norm = _norm[len("/api/v1"):]
    return any(
        _norm == a or _norm.startswith(a.rstrip("/") + "/")
        for a in allowed
    )


class TokenAuthMiddleware:
    """纯 ASGI 中间件 — 统一门卫。WS 通道由 P1 首帧鉴权单独处理。
    P0 S4（2026-08-04）：叠加每 IP 滑动窗口限速（默认 1000 req/s 可配，
    远高于 200 并发压测基线，仅防误配置脚本打爆；超限 429 + 审计）。
    P1 O1（2026-08-04）：叠加 trace_id 贯穿——生成/透传 X-Trace-Id，
    注入 ContextVar 供日志与审计读取，一次跨 Agent 事件一条 ID 串全链路。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        # P1 O1：trace_id 生成/透传（header 优先，否则 uuid4 短码）
        tid = ""
        for name, value in scope.get("headers", []):
            if name == b"x-trace-id":
                tid = value.decode("latin-1", "replace")[:64]
                break
        if not tid:
            tid = uuid.uuid4().hex[:16]
        set_trace_id(tid)

        if NO_AUTH:
            return await self.app(scope, receive, send)

        # P0 S4：每 IP 限速（健康/静态/文档路径豁免——探针与资源加载不受限；
        # T11：RATE_LIMITED_ALLOWLIST 中的路径虽免认证但参与限速）
        if CONFIG.RATE_LIMIT_PER_IP > 0 and (path in RATE_LIMITED_ALLOWLIST
                                             or not (path in AUTH_ALLOWLIST_PATHS
                                                     or path.startswith(AUTH_ALLOWLIST_PREFIXES))):
            if not await _rate_limit_ok(scope, path):
                return await _send_429(send, scope)

        if path in AUTH_ALLOWLIST_PATHS or path.startswith(AUTH_ALLOWLIST_PREFIXES):
            # 引导端点特殊规则：hub_token 已配置时必须带 hub_token——
            # 否则任何人可无 token bootstrap 注册新 agent 绕过门卫（T0-2 抓出）
            if path in ("/api/v1/agents/register", "/api/v1/agents/bootstrap") and CONFIG.HUB_TOKEN:
                token = _extract_bearer(scope)
                if not token or not _hmac.compare_digest(token, CONFIG.HUB_TOKEN):
                    return await _send_401(send)
            return await self.app(scope, receive, send)

        # 提取 Authorization header
        token = _extract_bearer(scope)

        if not token:
            return await _send_401(send)
        # S1：auth_provider 统一认证（api_key / hub_token / ldap / oidc / hybrid）
        principal = _auth_provider().authenticate(token, _scope_client_ip(scope))
        if principal is None:
            return await _send_401(send)
        # principal 注入请求上下文（披露引擎/审计读取）
        scope["principal"] = principal.to_dict()
        # S1K scoped key：endpoints 白名单过滤（空 = 全部；前缀匹配，/api/v1 前缀归一）
        _psk = getattr(principal, "scope", None) or {}
        _allowed = _psk.get("endpoints") or []
        if _allowed:
            if not _endpoint_allowed(path, _allowed):
                return await _send_403(send, "scoped key: endpoint 不在白名单")
        # O4：按 Agent 配额（reject/throttle/alert_only 三态）
        if not await _agent_quota_ok(scope, principal.subject_id, path):
            return await _send_429(send, scope)
        return await self.app(scope, receive, send)


# ---- P0 S4：REST 每 IP 限速（滑动窗口，内存计数） ----

_rate_hits: Dict[str, list] = {}  # ip → [ts, ...] 滑动窗口
_RATE_LOCK = asyncio.Lock()


async def _rate_limit_ok(scope, path: str) -> bool:
    """每 IP 滑动窗口限速；超限返回 False（调用方发 429）。"""
    global _rate_hits
    ip = _scope_client_ip(scope)
    limit = CONFIG.RATE_LIMIT_PER_IP
    window = 1.0
    now = time.time()
    async with _RATE_LOCK:
        hits = [t for t in _rate_hits.get(ip, []) if now - t < window]
        if len(hits) >= limit:
            _rate_hits[ip] = hits
            # 审计（异步任务，不阻塞限速判定）
            asyncio.create_task(_audit_rate_limit(ip, path))
            return False
        hits.append(now)
        _rate_hits[ip] = hits
        return True


_agent_quota_hits: Dict[str, list] = {}  # agent_id → [ts, ...] 滑动窗口
_AGENT_QUOTA_LOCK = asyncio.Lock()


async def _agent_quota_ok(scope, agent_id: str, path: str) -> bool:
    """O4：按 Agent 配额滑动窗口限流。返回 False → 调用方发 429。

    agent_quotas 表配置：
      mode=alert_only → 超限仅审计，不拦（默认，零影响上线）
      mode=reject     → 超限 429
      mode=throttle   → 超限后延迟 0.5s 再放行（软限）
    表不存在/无行 → 默认 alert_only（不阻断）。
    """
    global _agent_quota_hits
    # CD-040（2026-09-14）：原实现每请求同步 sqlite3.connect + SELECT agent_quotas（事件循环内），
    # 200 并发下与写缓冲 BEGIN IMMEDIATE 争锁——实测峰 4.62s / 吞吐 1841；改走
    # routes_common.agent_quotas_snapshot（进程内快照，TTL 30s，过期经 to_thread 刷新）后
    # 实测 3.15s / 3339。语义不变：无该 agent 行 → 不限流。
    row = (await agent_quotas_snapshot()).get(agent_id)
    if row is None:
        return True
    qps_limit, mode, window_sec, burst = row
    now = time.time()
    async with _AGENT_QUOTA_LOCK:
        hits = [t for t in _agent_quota_hits.get(agent_id, []) if now - t < (window_sec or 1.0)]
        # 阈值 = 每秒限额 × 窗口；burst 为超限后审计前的容忍增量
        limit = max(1, int((qps_limit or 50) * (window_sec or 1.0)))
        over = len(hits) >= limit
        hits.append(now)
        _agent_quota_hits[agent_id] = hits[-200:]  # 防无限增长
    if not over:
        return True
    # 超限：按 mode 处理
    asyncio.create_task(_audit_agent_quota(agent_id, path, mode))
    if mode == "reject":
        return False
    if mode == "throttle":
        await asyncio.sleep(0.5)
    return True  # alert_only / throttle 放行


async def _audit_agent_quota(agent_id: str, path: str, mode: str):
    """O4：配额超限审计。"""
    try:
        from audit_chain import AuditChain
        AuditChain(CONFIG.DB_PATH).append(
            "agent_quota_exceeded", "audit_log", agent_id,
            {"path": path, "mode": mode})
    except Exception as _exc:
        logger.warning("routes silent-except @300: %s", _exc)


async def _send_429(send, scope):
    """发送 429 Too Many Requests。"""
    body = _json.dumps({"detail": "Too Many Requests: 触发限速，请降低请求频率"}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 429,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"retry-after", b"1"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _audit_rate_limit(ip: str, path: str):
    """限速超限事件入审计（不阻塞主流程）。"""
    try:
        await hub._log_event("rate_limit_hit", "__http_gate__", {
            "ip": ip, "path": path, "limit_per_ip": CONFIG.RATE_LIMIT_PER_IP})
    except Exception as _exc:
        logger.warning("routes silent-except @345: %s", _exc)


def _extract_bearer(scope) -> str:
    """从 ASGI scope headers 提取 Bearer token"""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            raw = value.decode("latin-1")
            if raw.startswith("Bearer "):
                return raw[7:]
            break
    return ""


async def _send_403(send, detail: str = "Forbidden"):
    """发送统一 403 JSON 响应（scoped key 端点过滤等）"""
    body = _json.dumps({"detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [(b"content-type", b"application/json; charset=utf-8")],
    })
    await send({"type": "http.response.body", "body": body})


async def _send_401(send):
    """发送统一 401 JSON 响应"""
    body = _json.dumps(
        {"detail": "Unauthorized: 缺少有效的 Hub Token 或 API Key"}
    ).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [(b"content-type", b"application/json; charset=utf-8")],
    })
    await send({"type": "http.response.body", "body": body})



# （NO_AUTH / AUTH_WHITELIST / _authenticate / get_current_agent 已移至 routes_common，
#  顶部 re-export 保持外部引用兼容）


# ============ M3: Memory Pool 增强 ============

from pydantic import BaseModel as PydanticBase, Field as PydanticField
from typing import Optional as Opt


# ============ 披露审批端点 ============


# ============ 任务生命周期端点（P6） ============


# ============ 通知 API（P7） ============


# 通知/私聊端点已拆分至 routes_notifications.py（文件底部 include_router 注册，路径与行为不变）

# （get_current_agent_optional 已移至 routes_common）















# 知识库 / Wiki / 收件箱审查端点已拆分至 routes_knowledge.py / routes_wiki.py
# （文件底部 include_router 注册，路径与行为不变）


# 知识库 / Wiki / 收件箱审查端点已拆分至 routes_knowledge.py / routes_wiki.py；
# 维护端点已拆分至 routes_maintenance.py（文件底部 include_router 注册，路径不变）






# ============ 服务器配置 API ============


# ============ Hub Agent API（LLM 披露审计引擎） ============


# ---------- LangChain 聊天端点 ----------


# ============ 维护 API ============


# ---------- 日报 ----------


# ============ M2: 会话摘要归档 API ============


# ═══════════════════════════════════════════════════════════════
#  P0-2: 内网组队协同 API (2026-07-28 注册)
# ═══════════════════════════════════════════════════════════════




# ═══ 跨 Hub 代理披露 (TC5 核心用例) ═══


_register_automation(app, hub, get_current_agent)




# _ensure_config 和 __main__ 已移至 main.py，统一入口


# ═══════════════════════════════════════════════════════════
# H3 chunk 检索（附录 E v1.4，2026-08-06）
# 防拼接滑窗挂载点：跨请求 24h 累计 >50% → 降级 + 审计（E.3）
# parent_hint 只给布尔量，不给 total_chunks（防结构泄露）
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# H4 汇入管道 + E.7 重判定（附录 E v1.4，2026-08-06）
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# K1 embedding 升级（附录 F 2026-08-06）
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# K3 机密词库管理（附录 F 2026-08-06）
# 词库本身是敏感信息：查看/导出/更新全走 manager/orchestrator 审批门
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# K2 实体审查队列（附录 F v1.7，2026-08-06）
# 铁律 2：LLM 幻觉实体不直接进图谱，review 放行后才进 knowledge_base
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# S1K scoped API key（2026-08-07）— 随时限制 key 访问范围
# create 需 manager+（审批）；revoke 60s 内全端点失效；rotate 90 天轮换
# ═══════════════════════════════════════════════════════════




# D-8（3-2b）：WS/静态页/dashboard 端点与私有 helper 已迁入
# routes_ws.py / routes_pages.py / routes_dashboard.py
#（/health、/healthz、/readyz、buffer 遥测、skills/catalog 追加进 routes_server.py），
# 此处 re-export 保持 `from routes import X` / `routes.X` 外部引用语义不变（同一对象，非复制品）
from routes_ws import (
    ws_endpoint, ws_dashboard, ws_buffer_stats, ws_shared_watch, ws_shared,
    _ws_auth_accept,
)
from routes_pages import _read_static_html, _STATIC_HTML_CACHE, _ui_new_enabled
from routes_pages import router as _pages_router
from routes_ws import router as _ws_router
from routes_dashboard import router as _dashboard_router
# ═══════════════════════════════════════════════════════════════
# Phase 2: APIRouter 子模块挂载（拆分自本文件的端点组，路径与行为不变。
# 与原位置的注册顺序差异仅影响路由表次序；经核查各组路径前缀互不重叠，
# 无 {param} 模式冲突）
# ═══════════════════════════════════════════════════════════════
from routes_notifications import router as _notifications_router
from routes_knowledge import router as _knowledge_router
from routes_wiki import router as _wiki_router
from routes_maintenance import router as _maintenance_router

app.include_router(_notifications_router)
app.include_router(_knowledge_router)
app.include_router(_wiki_router)
app.include_router(_maintenance_router)
from routes_audit import router as _audit_router
from routes_agents import router as _agents_router
from routes_federation import router as _federation_router
from routes_memory import router as _memory_router
from routes_tasks import router as _tasks_router
from routes_disclosure import router as _disclosure_router
from routes_server import router as _server_router
from routes_hubagent import router as _hubagent_router
from routes_report import router as _report_router
from routes_sessions import router as _sessions_router
from routes_team import router as _team_router
from routes_shared import router as _shared_router
from routes_pipeline import router as _pipeline_router
from routes_keys import router as _keys_router
from routes_access import router as _access_router
from routes_integrations import router as _integrations_router
from routes_n1 import router as _n1_router
from routes_gateway import router as _gateway_router
app.include_router(_audit_router)
app.include_router(_agents_router)
app.include_router(_federation_router)
app.include_router(_memory_router)
app.include_router(_tasks_router)
app.include_router(_disclosure_router)
app.include_router(_server_router)
app.include_router(_hubagent_router)
app.include_router(_report_router)
app.include_router(_sessions_router)
app.include_router(_team_router)
app.include_router(_shared_router)
app.include_router(_pipeline_router)
app.include_router(_keys_router)
app.include_router(_access_router)
app.include_router(_integrations_router)
app.include_router(_n1_router)
app.include_router(_gateway_router)
app.include_router(_pages_router)
app.include_router(_ws_router)
app.include_router(_dashboard_router)


# P0: 部署级 token 鉴权中间件 — 挂在最外层，所有 HTTP 请求先过门卫
app.add_middleware(TokenAuthMiddleware)