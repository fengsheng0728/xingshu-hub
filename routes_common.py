"""星枢 Sync Hub — 路由共享依赖（Phase 2 拆分）。

routes.py 与各 routes_*.py 子模块共同引用的模块级依赖：
NO_AUTH 开关、auth_provider 惰性单例、Depends 认证注入。

外部兼容：其他模块仍以 `from routes import NO_AUTH / _auth_provider / ...`
引用——routes.py 会 re-export 本模块的这些名字，语义与拆分前完全一致。
"""
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from models import CONFIG
from auth_provider import Principal, get_auth_provider

logger = logging.getLogger("xingshu.routes_common")

NO_AUTH = os.environ.get("SYNC_HUB_NO_AUTH", "").strip() in ("1", "true", "yes")
AUTH_WHITELIST = {"/", "/health", "/showcase", "/docs", "/openapi.json"}

if NO_AUTH:
    print("[WARNING] 认证已关闭（SYNC_HUB_NO_AUTH=1），仅限开发环境！")

# S1：模块级 provider 惰性单例（AUTH_MODE 决定实现；缺依赖自动降级 local）
_AUTH_PROVIDER = None


def _auth_provider():
    global _AUTH_PROVIDER
    if _AUTH_PROVIDER is None:
        _AUTH_PROVIDER = get_auth_provider(CONFIG)
    return _AUTH_PROVIDER


def _valid_credential(token: str, client_ip: str = "") -> bool:
    """S1：auth_provider 统一凭据校验（hub_token / api_key / ldap / oidc / hybrid）"""
    return _auth_provider().authenticate(token, client_ip, touch=False) is not None


def _scope_client_ip(scope) -> str:
    """从 ASGI scope 提取客户端 IP。"""
    try:
        client = scope.get("client")
        return client[0] if client else "unknown"
    except Exception:
        return "unknown"


def _authenticate(db_path: str, auth_header) -> str or None:
    """验证 Bearer token，返回 agent_id 或 None

    T1-2（2026-09-09）：已迁移库（api_key_hash 列存在）按 sha256(token) 匹配
    api_key_hash——库内不存明文；未迁移老库（无 hash 列）降级旧明文匹配（兼容窗口）。
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    cols = {r[1] for r in c.execute("PRAGMA table_info(agents)")}
    if "api_key_hash" in cols:
        import hashlib
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()
        c.execute("SELECT agent_id FROM agents WHERE api_key_hash = ?", (h,))
    else:
        c.execute("SELECT agent_id FROM agents WHERE api_key = ?", (token,))
    row = c.fetchone()
    conn.close()
    return row["agent_id"] if row else None


# ============ 认证依赖（Depends 注入，兼容 Starlette 1.x） ============

def get_current_agent(request: Request) -> str:
    """
    从 Authorization header 提取并验证 api_key，返回 agent_id。
    NO_AUTH 模式下从 query param agent_id 取值。
    白名单路径（register/health 等）不走此依赖。
    P0：hub_token（部署级单 token）匹配时返回请求声明的 agent_id——
    门外鉴权已由 TokenAuthMiddleware 完成，身份由请求声明（D1：不做 RBAC）。
    """
    if NO_AUTH:
        return request.query_params.get("agent_id") or ""

    auth_header = request.headers.get("Authorization") or ""
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    # S1：auth_provider 统一认证，principal 携带身份
    principal = _auth_provider().authenticate(token, _scope_client_ip(request.scope), touch=False)
    if principal:
        # api_key 路径：精确返回归属 agent_id；hub_token/人身份：请求声明的 agent_id（D1）
        if principal.auth_mode == "api_key":
            return principal.subject_id
        return request.query_params.get("agent_id") or request.path_params.get("agent_id") or ""
    raise HTTPException(status_code=401,
        detail="Unauthorized: 缺少有效的 API Key")


def get_current_principal(request: Request):
    """完整 Principal（含 scope），1e 员工披露链用。NO_AUTH 模式返回 None（无身份语义）。"""
    if NO_AUTH:
        return None
    auth_header = request.headers.get("Authorization") or ""
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token:
        return None
    return _auth_provider().authenticate(token, _scope_client_ip(request.scope), touch=False)


def get_current_agent_optional(request: Request) -> str:
    """Dashboard 专用：可选认证，无 token 时返回空字符串"""
    if NO_AUTH:
        return request.query_params.get("agent_id") or ""
    auth_header = request.headers.get("Authorization") or ""
    agent_id = _authenticate(CONFIG.DB_PATH, auth_header)
    return agent_id or ""  # 无 token 不报错，返回空


# ============ T15 端点角色门（2026-09-09） ============

PRIVILEGED_ROLES = ("manager", "orchestrator")


def _agent_role(agent_id: str) -> str:
    """查 agents 表 role（DB 为单真相源，对齐 XS-002）。异常/不存在 → ''（fail-closed）。"""
    if not agent_id:
        return ""
    try:
        conn = sqlite3.connect(CONFIG.DB_PATH)
        try:
            row = conn.execute(
                "SELECT role FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            return (row[0] or "") if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


# ============ O4 配额快照缓存（CD-040 修复，2026-09-14） ============
# 原实现：鉴权中间件对**每个已认证请求**同步 `sqlite3.connect` + SELECT agent_quotas，
# 全跑在事件循环里；200 并发下与写缓冲 BEGIN IMMEDIATE 争锁。
# 实测（同 DB 快照/同 harness/worktree）：现状峰 4.62s、吞吐 1841 → 快照化后 3.15s、3339。
# 语义不变：表缺失/异常 → 空快照（等价旧行为「无行即不限流」）；TTL 内零 DB 访问。
QUOTA_CACHE_TTL_S = float(os.environ.get("SYNC_HUB_QUOTA_CACHE_TTL", "30") or 30)

_QUOTA_SNAPSHOTS: dict = {}      # db_path -> (snapshot, loaded_at)：按 DB 分键，防跨库/跨测试串味
_QUOTA_SNAPSHOT_LOCK = asyncio.Lock()


def invalidate_agent_quotas() -> None:
    """配额变更后调用：清空快照缓存（写点：routes_agents 配额 upsert）。"""
    _QUOTA_SNAPSHOTS.clear()


def _load_agent_quotas_sync() -> dict:
    """同步读全量配额（由 to_thread 调用，不在事件循环内执行）。"""
    conn = sqlite3.connect(CONFIG.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT agent_id, qps_limit, mode, window_sec, burst FROM agent_quotas"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


async def agent_quotas_snapshot() -> dict:
    """配额快照：agent_id → (qps_limit, mode, window_sec, burst)（按 DB_PATH 分键）。

    TTL 内直接返回内存快照（零 DB 访问）；过期后单飞刷新（asyncio.Lock 双检），
    刷新本身走 asyncio.to_thread，不占事件循环。
    表缺失/DB 异常 → 空快照（等价旧行为「无行即不限流」）。
    """
    db = getattr(CONFIG, "DB_PATH", "")
    now = time.time()
    ent = _QUOTA_SNAPSHOTS.get(db)
    if ent is not None and (now - ent[1]) < QUOTA_CACHE_TTL_S:
        return ent[0]
    async with _QUOTA_SNAPSHOT_LOCK:
        ent = _QUOTA_SNAPSHOTS.get(db)
        if ent is not None and (time.time() - ent[1]) < QUOTA_CACHE_TTL_S:
            return ent[0]
        try:
            fresh = await asyncio.to_thread(_load_agent_quotas_sync)
        except Exception:
            fresh = {}
        if len(_QUOTA_SNAPSHOTS) > 64:      # 测试/多库场景防无限增长
            _QUOTA_SNAPSHOTS.clear()
        _QUOTA_SNAPSHOTS[db] = (fresh, time.time())
        return fresh


def principal_is_privileged(principal) -> bool:
    """T15 角色门：hub_token 或归属 agent 的 role ∈ PRIVILEGED_ROLES 才为真。

    principal 可为 auth_provider.Principal 对象或其 to_dict() 结果
    （TokenAuthMiddleware 注入 scope["principal"] 的是 dict）。
    auth_mode == "hub_token" 等价于 token == CONFIG.HUB_TOKEN
    （LocalProvider 仅在 hmac 比对通过时签发该 mode）。
    None / 未知身份 / 查询异常一律 False（fail-closed，对齐 D6 审批门）。
    """
    if not principal:
        return False
    if isinstance(principal, dict):
        auth_mode = principal.get("auth_mode", "")
        subject_id = principal.get("subject_id", "")
    else:
        auth_mode = getattr(principal, "auth_mode", "")
        subject_id = getattr(principal, "subject_id", "")
    if auth_mode == "hub_token":
        return True
    return _agent_role(subject_id) in PRIVILEGED_ROLES


# ============ CD-061 重运维端点统一门（2026-09-20，T22） ============
# 门表（method, path）：同类重运维端点过同一张门表，与 POST /api/v1/knowledge/reindex
# （CD-051 同门基准，既有内联门未改动）同口径。新增运维语义端点必须登记进本表——
# tests/test_ops_gate_matrix.py 的结构断言（关键词扫描求差 + 逐 handler 门调用
# 检查）会拦下漏登记；登记规程见 docs/ops-gated-endpoints.md。
OPS_GATED_ENDPOINTS = (
    ("POST", "/api/v1/knowledge/reindex"),   # 同门基准（CD-051 既有门，本任务未改动）
    ("POST", "/api/v1/embeddings/rebuild"),
    ("POST", "/api/v1/embeddings/calibrate"),
    ("POST", "/api/v1/chunks/reclassify"),
    ("POST", "/api/v1/maintenance/cleanup"),
    ("GET", "/api/v1/wiki/sync"),
    ("GET", "/api/v1/wiki/inbox"),
    ("POST", "/api/v1/wiki/inbox/cleanup"),
)


def _principal_auth_mode(principal) -> str:
    """从 principal（dict 或 Principal 对象）取 auth_mode；异常/空 → ""（fail-closed）。"""
    if not principal:
        return ""
    try:
        if isinstance(principal, dict):
            return str(principal.get("auth_mode") or "")
        return str(getattr(principal, "auth_mode", "") or "")
    except Exception:
        return ""


def require_role(current_agent: str = "", roles=PRIVILEGED_ROLES,
                 detail: str = "仅主管/店长可访问", agents=None,
                 no_auth: bool = None, principal=None) -> None:
    """CD-074：角色门 canonical 判定（取代各路由文件自造的 role 判定）。

    判定顺序（fail-closed）：
    1. `NO_AUTH`（测试/开发态）→ 不拦；
    2. 主体 auth_mode == "hub_token" → 放行（部署级全权凭据，控制台登录用的就是它）；
    3. 归属 Agent 的 role ∈ roles → 放行。角色**双查**：内存 `hub.agents`（启动时由 DB 载入）
       与 `_agent_role()` 直查 DB（T15/XS-002 的单一真相源）——两者任一命中即放行，
       兼容既有测试（它们 monkeypatch 内存态）；
    4. 其余 → 403（detail 可定制）。

    **为什么要有这一条**：CD-071/CD-074 实测——`hub.agents[current_agent].role` 这种写法在
    hub_token 调用方会拿到空串 `current_agent`（`get_current_agent` 对非 api_key 主体返回
    「请求声明的 agent_id」）→ **控制台用 hub_token 登录时整组 403**（审计检索、知识编辑、
    集成管理、密钥管理、N1 审批队列、机密词库、任务改派…共 25 处）。

    与 `require_ops_privilege` 的区别：那条是**重运维端点专用**（带 ops_gate_denied 审计 +
    统一 403 文案）；本条是**通用角色门**，不落额外事件、文案由调用方给。
    进程内直调（不经 ASGI）时 ContextVar 为空 → 退回角色双查，行为与既有测试一致。
    """
    # NO_AUTH 取「本模块的值」与「调用方那份副本」的**与**：只有两边都为真才跳过。
    # （各路由模块 `from routes_common import NO_AUTH` 拿到的是导入期副本，测试常 patch
    #  模块里那一份 → 只看本模块会漏掉这类 patch，门就形同虚设。）
    if (NO_AUTH if no_auth is None else no_auth) and NO_AUTH:
        return
    if principal is None:
        try:
            from routes_gateway import get_mcp_principal   # 惰性导入：避免 routes_common ↔ routes_gateway 环
            principal = get_mcp_principal()
        except Exception:
            principal = None
    if _principal_auth_mode(principal) == "hub_token":
        return
    # 角色来源双查（任一命中即放行）：
    #  · agents = 调用方模块自己的 hub.agents（**必须传**：各路由模块的 hub 符号会被测试
    #    monkeypatch 成假对象，若只查 hub_core 真身，测试里造的角色就看不见）；
    #  · hub_core 真身（生产路径）；
    #  · 最后 `_agent_role()` 直查 DB（T15/XS-002 单一真相源，fail-closed）。
    _maps = []
    if isinstance(agents, dict):
        _maps.append(agents)
    elif agents is not None:
        _maps.append(getattr(agents, "agents", {}) or {})
    try:
        from hub_core import hub as _hub
        _maps.append(getattr(_hub, "agents", {}) or {})
    except Exception:
        pass
    for _m in _maps:
        try:
            if (_m.get(current_agent) or {}).get("role") in tuple(roles):
                return
        except Exception:
            continue
    if _agent_role(current_agent) in tuple(roles):
        return
    raise HTTPException(status_code=403, detail=detail)


async def require_ops_privilege(request, endpoint: str,
                                requester: str = "") -> None:
    """CD-061 重运维端点统一门：principal_is_privileged 为假 → 403；NO_AUTH 不拦。

    request 可为 None（见下「兼容分支」）；非 None 时为 fastapi.Request。

    与 knowledge/reindex（CD-051 基准）同口径：hub_token / manager /
    orchestrator 放行，其余 fail-closed。拒绝落 events 审计
    （ops_gate_denied，含 endpoint/requester/at），不静默；
    审计写失败只告警、不阻塞 403（D4）。

    request=None 兼容分支：HTTP 路径 FastAPI 必注入 Request，此分支只对
    进程内直调（既有测试/工具不经 ASGI）生效——为兼容 CD-042/CD-043 既有
    直调测试（不传 request），此时不拦（进程内调用方本可直调 hub 方法，
    不构成 HTTP 暴露面）。
    """
    if NO_AUTH:
        return
    if request is None:
        return
    if principal_is_privileged(request.scope.get("principal")):
        return
    try:
        from hub_core import hub  # 惰性导入，避免 routes_common <-> hub_core 环
        await hub._log_event("ops_gate_denied", requester or "", {
            "endpoint": endpoint,
            "requester": requester or "",
            "at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as _exc:
        logger.warning("ops_gate_denied 审计落行失败 endpoint=%s err=%s",
                       endpoint, type(_exc).__name__)
    raise HTTPException(
        status_code=403,
        detail=f"重运维端点 {endpoint} 仅 manager/orchestrator 角色或 hub_token 可触发",
    )


async def log_ops_trigger(endpoint: str, requester: str, counts) -> None:
    """CD-061 重运维端点成功触发落审计链（events.ops_trigger）。

    payload: endpoint / requester / at(ISO) / counts。counts 必须来自端点真实
    返回值或真实查询；不可得时如实传 "unavailable" 字样并说明原因，禁止占位 0。
    审计写失败只告警、不阻塞业务响应（D4）。
    """
    try:
        from hub_core import hub  # 惰性导入，避免 routes_common <-> hub_core 环
        await hub._log_event("ops_trigger", requester or "", {
            "endpoint": endpoint,
            "requester": requester or "",
            "at": datetime.now(timezone.utc).isoformat(),
            "counts": counts,
        })
    except Exception as _exc:
        logger.warning("ops_trigger 审计落行失败 endpoint=%s err=%s",
                       endpoint, type(_exc).__name__)


# D-8 验收修正（2026-09-14）：本函数原定义在 routes.py 的「认证配置段」。
# WS 通道迁入 routes_ws.py 后需要它，为消除 routes <-> routes_ws 循环依赖
# （子模块反向 import 装配层）而移入本模块。routes.py 保留 re-export，
# `from routes import _version_ge` 语义不变（tests/test_o3_upgrade_rollback.py 依赖）。
def _version_ge(v: str, min_v: str) -> bool:
    """O3: 语义版本比较 v >= min_v。非数字段忽略。"""
    def _parts(s):
        out = []
        for seg in s.split("."):
            num = ""
            for ch in seg:
                if ch.isdigit():
                    num += ch
                else:
                    break
            out.append(int(num) if num else 0)
        return out
    a, b = _parts(v), _parts(min_v)
    for i in range(max(len(a), len(b))):
        av = a[i] if i < len(a) else 0
        bv = b[i] if i < len(b) else 0
        if av != bv:
            return av > bv
    return True
