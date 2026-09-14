"""星枢 Sync Hub — 路由共享依赖（Phase 2 拆分）。

routes.py 与各 routes_*.py 子模块共同引用的模块级依赖：
NO_AUTH 开关、auth_provider 惰性单例、Depends 认证注入。

外部兼容：其他模块仍以 `from routes import NO_AUTH / _auth_provider / ...`
引用——routes.py 会 re-export 本模块的这些名字，语义与拆分前完全一致。
"""
import asyncio
import os
import sqlite3
import time

from fastapi import HTTPException, Request

from models import CONFIG
from auth_provider import Principal, get_auth_provider

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
    return _auth_provider().authenticate(token, client_ip) is not None


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
    principal = _auth_provider().authenticate(token, _scope_client_ip(request.scope))
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
    return _auth_provider().authenticate(token, _scope_client_ip(request.scope))


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
