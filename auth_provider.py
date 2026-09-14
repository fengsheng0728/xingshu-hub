"""
S1 身份接入（2026-08-05）— auth_provider 抽象层
=================================================

三层模型：
    人（User）───AD/LDAP/OIDC 认证───┐
                                    ├─→ 会话主体（Principal）
    机器（Service）──api_key + IP 白名单┘        │
                                                ▼
                                 Agent 实例 ← 归属 Principal
                                                │
                                                ▼
                                 披露引擎权限判定（Agent 权限 ∧ Principal 组权限）

AUTH_MODE: local | ldap | oidc | hybrid
- local  ：现有 token 模式（agents.api_key / CONFIG.HUB_TOKEN），零迁移默认
- ldap   ：AD 的 sAMAccountName 绑定 + 组查询（ldap3），组关系落 principal_groups 表
- oidc   ：标准 JWT（Authorization Code Flow 产物），JWKS 验签
- hybrid ：人走 OIDC、机器走 api_key（JWT 形 token → OidcProvider，否则 api_key/hub_token）

设计约束：
- 依赖缺失/配置缺失时该 provider 不可用 → 构造时抛 AuthProviderUnavailable，
  工厂 get_auth_provider() 捕获后降级 local（fail-safe，绝不因身份接入崩启动）。
- api_key 校验从"布尔通过"升级为"返回归属 agent_id + 过期/白名单判定"，
  同时保留 hub_token 的部署级语义（无身份语义，agent_id 由请求声明，D1 不做 RBAC）。
- Principal 注入：TokenAuthMiddleware 认证成功后写入 scope["principal"] + ContextVar
  （xingshu.principal），供披露引擎与审计读取。

api_key 轮换（S1 通过条件）：
- agents 表新增 api_key_created_at / api_key_expires_at / api_key_prev /
  api_key_prev_expires_at / api_key_ip_whitelist / last_used_at 列（db.py DDL+迁移）。
- 轮换 = 新 key 写 api_key，旧 key 移 api_key_prev（宽限 24h 内仍有效），
  超宽限旧 key 401；轮换事件入 events 审计 + 通知管理员。
- 使用审计：每次 api_key 校验成功刷新 last_used_at（节流：≥60s 才写盘）。

api_key 哈希化（T1-2，2026-09-09）：
- alembic 0003 起 agents 加 api_key_hash / api_key_prev_hash（SHA256），
  存量明文迁移为 hash 后 api_key/api_key_prev 清空——Hub 库不存可用明文凭据，
  明文只在签发时刻回显一次（register 首注 / hub-cli agent create）。
- 查询一律先 hash 再查；未迁移老库（无 hash 列）按 PRAGMA 检测降级旧明文行为。
"""

from __future__ import annotations
import os


import ipaddress
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from models import PUBLIC_DOMAIN

logger = logging.getLogger("xingshu.auth_provider")

# ---- Principal（会话主体） ----


@dataclass
class Principal:
    """认证通过后的会话主体。

    subject_type: service（机器/Agent/api_key/hub_token）| user（人，LDAP/OIDC）
    subject_id:   service → agent_id；user → 用户名/邮箱
    groups:       组 DN 列表（user 来自 LDAP/OIDC claims；service 可来自挂载策略）
    auth_mode:    api_key | hub_token | ldap | oidc
    """

    subject_type: str = "service"
    subject_id: str = ""
    groups: List[str] = field(default_factory=list)
    auth_mode: str = "api_key"
    scope: Optional[dict] = None  # S1K scoped key 三层 scope（endpoints/data_domain/level_cap）

    def to_dict(self) -> dict:
        return {
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "groups": self.groups,
            "auth_mode": self.auth_mode,
            "scope": self.scope,
        }


class AuthProviderUnavailable(Exception):
    """该 provider 依赖/配置缺失，无法使用（调用方降级 local）。"""


# ---- 抽象基类 ----


class AuthProvider:
    mode = "local"

    def authenticate(self, token: str, client_ip: str = "") -> Optional[Principal]:
        """校验凭据，成功返回 Principal，失败返回 None。必须线程安全。"""
        raise NotImplementedError

    def sync_groups(self) -> int:
        """同步组关系到 principal_groups 表，返回同步条数。local 默认 0（无组体系）。"""
        return 0

    def close(self) -> None:
        """释放连接资源（如 LDAP 连接池）。"""


# ---- LocalProvider（现有 api_key / hub_token，零迁移） ----


class LocalProvider(AuthProvider):
    """现状语义包装：agents.api_key 或 CONFIG.HUB_TOKEN 任一有效。

    api_key 路径升级为返回归属 agent_id + 过期/宽限/白名单判定（S1）。
    hub_token 路径保持无身份语义（subject_id="__hub__"，D1）。
    """

    mode = "local"

    def __init__(self, config):
        self._config = config
        self._last_used_write: Dict[str, float] = {}  # agent_id → 上次写 last_used_at
        self._last_used_lock = threading.Lock()
        self._db_lock = threading.Lock()

    # ---- 内部工具 ----

    def _connect(self):
        import sqlite3

        conn = sqlite3.connect(self._config.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    # ── 1e 员工账号（阶段1）：模板→scope 映射 + key 查询 ──
    TEMPLATE_SCOPE = {
        # level_cap: D5 三方取 min 的员工侧上限; data_domain: 越域降 METADATA
        # __dept__/__project__/__public__ 为运行时占位, 由员工记录替换
        "owner":     {"level_cap": "full",     "data_domain": []},
        "dept_head": {"level_cap": "full",     "data_domain": ["__dept__"]},
        "staff":     {"level_cap": "summary",  "data_domain": ["__dept__", "__public__"]},
        "external":  {"level_cap": "metadata", "data_domain": ["__project__"]},
    }

    def _employee_scope(self, emp: dict) -> dict:
        """员工记录 → S1K scope（模板占位替换为实际值；未知模板 fail-closed 到 metadata）"""
        tpl = self.TEMPLATE_SCOPE.get((emp.get("role_template") or "").strip(), {})
        if not tpl:
            return {"endpoints": [], "data_domain": [], "level_cap": "metadata"}
        domain = []
        for d in tpl["data_domain"]:
            if d == "__dept__":
                if emp.get("department"):
                    domain.append(emp["department"])
            elif d == "__project__":
                if emp.get("project_scope"):
                    domain.append(emp["project_scope"])
            elif d == "__public__":
                domain.append(PUBLIC_DOMAIN)
            else:
                domain.append(d)
        return {"endpoints": [], "data_domain": domain, "level_cap": tpl["level_cap"]}

    def _lookup_employee_by_key(self, token: str) -> Optional[dict]:
        """按员工 key（SHA256 不存明文，同 S1K 规范）查 employee_accounts。"""
        try:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute("PRAGMA table_info(employee_accounts)")
                cols = {r[1] for r in c.fetchall()}
                if "key_hash" not in cols:
                    return None  # 表未迁移, 员工认证不生效
                import hashlib
                h = hashlib.sha256(token.encode("utf-8")).hexdigest()
                c.execute(
                    "SELECT employee_id, name, email, role_template, department,"
                    " project_scope, status, lease_expires_at FROM employee_accounts"
                    " WHERE key_hash = ?",
                    (h,),
                )
                row = c.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            logger.warning("employee lookup failed: %s", e)
            return None

    @staticmethod
    def _key_hash(token: str) -> str:
        """api_key → SHA256 hexdigest（T1-2：对齐 employee_accounts/agent_keys 规范）。"""
        import hashlib
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    # ---- CD-040（2026-09-14）凭据查询缓存 ----
    # 原实现每请求 `PRAGMA table_info(agents)` + SELECT（同步、事件循环内），200 并发下与
    # 写缓冲争锁；实测（同 DB 快照/同 harness/worktree）现状峰 4.62s·吞吐 1841 → 缓存化
    # 后 3.15s·3339。语义与失效边界：
    #   · 列集（schema）进程内缓存一次（DDL 运行期不变）；
    #   · token→row 只缓存**正命中**，TTL 默认 8s（SYNC_HUB_AUTH_CACHE_TTL 可调）；
    #     负结果不缓存 → 新签发/新注册的 key 立即可用，不延迟生效；
    #   · 轮换等凭据写点调用 invalidate_auth_cache() 立即清空，未挂钩路径最坏延迟 = TTL。
    _schema_cols_cache: dict = {}      # DB_PATH -> set(列名)
    _lookup_cache: dict = {}           # (DB_PATH, sha256(token)) -> (row, ts)
    AUTH_CACHE_TTL_S: float = float(os.environ.get("SYNC_HUB_AUTH_CACHE_TTL", "8") or 8)

    def _agent_columns(self, cursor=None) -> set:
        """agents 表列集（进程内缓存；DDL 运行期不变）。

        传入 cursor 时复用调用方的连接（首次查询不多开一次连接）。
        """
        key = getattr(self._config, "DB_PATH", "")
        cached = LocalProvider._schema_cols_cache.get(key)
        if cached is not None:
            return cached
        if cursor is not None:
            cols = {r[1] for r in cursor.execute("PRAGMA table_info(agents)").fetchall()}
            LocalProvider._schema_cols_cache[key] = cols
            return cols
        conn = self._connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        finally:
            conn.close()
        LocalProvider._schema_cols_cache[key] = cols
        return cols

    def _row_cache_active(self) -> bool:
        """行缓存开关（默认关）。

        CD-040：token→row 缓存能显著降低并发下的每请求 DB 往返（实测峰 4.62s→3.15s），
        但会把「凭据变更立即生效」放宽为「≤TTL 生效」。默认关=严格语义不变（既有单测
        依赖此语义）；需要吞吐的场景显式开 SYNC_HUB_AUTH_ROW_CACHE=1。
        """
        return os.environ.get("SYNC_HUB_AUTH_ROW_CACHE", "0").strip() in ("1", "true", "yes")

    def invalidate_auth_cache(self) -> None:
        """凭据变更（轮换/吊销/白名单变更）后清空 token→row 缓存。"""
        LocalProvider._lookup_cache.clear()

    def _lookup_agent(self, token: str) -> Optional[dict]:
        """按 api_key 查归属 agent（含 prev 宽限 key）。

        T1-2（2026-09-09）：已迁移库（alembic 0003，含 api_key_hash/api_key_prev_hash
        列）按 SHA256 hash 匹配——Hub 库不再存可用明文凭据。
        向后兼容（未迁移老库逐级降级）：无 hash 列 → 明文 api_key/api_key_prev 匹配；
        无 S1 轮换列 → 仅按 api_key 匹配。降级仅为迁移前兼容窗口，
        0003 迁移后明文列被清空，旧代码读明文将失效。
        """
        _ck = None
        if self._row_cache_active():   # CD-040：默认关闭（严格语义优先），显式开启才用行缓存
            _ck = (getattr(self._config, "DB_PATH", ""), self._key_hash(token))
            _hit = LocalProvider._lookup_cache.get(_ck)
            if _hit is not None and (time.time() - _hit[1]) < LocalProvider.AUTH_CACHE_TTL_S:
                return _hit[0]
        try:
            conn = self._connect()
            try:
                c = conn.cursor()
                cols = self._agent_columns(c)   # CD-040：列集进程内缓存（复用本连接）
                if {"api_key_hash", "api_key_prev_hash"} <= cols:
                    h = self._key_hash(token)
                    c.execute(
                        "SELECT agent_id, api_key_hash, api_key_prev_hash, "
                        "api_key_created_at, api_key_expires_at, "
                        "api_key_prev_expires_at, api_key_ip_whitelist "
                        "FROM agents WHERE api_key_hash = ? OR api_key_prev_hash = ?",
                        (h, h),
                    )
                elif {"api_key_prev", "api_key_expires_at", "api_key_ip_whitelist",
                        "api_key_prev_expires_at"} <= cols:
                    c.execute(
                        "SELECT agent_id, api_key, api_key_prev, api_key_created_at, "
                        "api_key_expires_at, api_key_prev_expires_at, api_key_ip_whitelist "
                        "FROM agents WHERE api_key = ? OR api_key_prev = ?",
                        (token, token),
                    )
                else:
                    # 旧库降级：只按 api_key 匹配（无轮换语义）
                    c.execute(
                        "SELECT agent_id, api_key FROM agents WHERE api_key = ?",
                        (token,),
                    )
                row = c.fetchone()
                if row is None or _ck is None:
                    return dict(row) if row else None
                d = dict(row)
                LocalProvider._lookup_cache[_ck] = (d, time.time())   # CD-040：正命中缓存（TTL 兜底）
                return d
            finally:
                conn.close()
        except Exception as e:
            logger.warning("auth_provider lookup failed: %s", e)
            return None

    @staticmethod
    def _ip_in_whitelist(client_ip: str, whitelist_json: str) -> bool:
        """IP/CIDR 白名单判定；白名单为空 = 不限制。"""
        if not whitelist_json:
            return True
        try:
            entries = json.loads(whitelist_json) or []
        except Exception:
            return False
        if not entries:
            return True
        if not client_ip or client_ip == "unknown":
            return False
        try:
            ip = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        for entry in entries:
            try:
                if "/" in entry:
                    if ip in ipaddress.ip_network(entry, strict=False):
                        return True
                elif ip == ipaddress.ip_address(entry):
                    return True
            except ValueError:
                continue
        return False

    def _is_expired(self, expires_at: Optional[str], grace_hours: int = 0) -> bool:
        """过期判定（可含宽限期）。expires_at 为空 = 永不过期（旧数据零迁移）。"""
        if not expires_at:
            return False
        try:
            from datetime import datetime, timezone

            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if grace_hours > 0:
                from datetime import timedelta

                exp = exp + timedelta(hours=grace_hours)
            return datetime.now(timezone.utc) > exp
        except Exception:
            # fail-closed（2026-09-09 T13）：expires_at 非空但解析失败 → 视为已过期，
            # 拒绝认证（原 return False 让畸形时间戳凭据永久有效）。空值不过期语义不变。
            return True

    def _touch_last_used(self, agent_id: str) -> None:
        """使用审计：last_used_at 刷新（≥60s 节流，不拖慢鉴权路径）。

        旧库（无 last_used_at 列）静默跳过——审计列缺失不阻断认证。
        """
        now = time.time()
        with self._last_used_lock:
            last = self._last_used_write.get(agent_id, 0)
            if now - last < 60:
                return
            self._last_used_write[agent_id] = now
        try:
            from datetime import datetime, timezone

            ts = datetime.now(timezone.utc).isoformat()
            with self._db_lock:
                conn = self._connect()
                try:
                    c = conn.cursor()
                    c.execute("PRAGMA table_info(agents)")
                    cols = {r[1] for r in c.fetchall()}
                    if "last_used_at" not in cols:
                        return
                    conn.execute(
                        "UPDATE agents SET last_used_at = ? WHERE agent_id = ?",
                        (ts, agent_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.warning("last_used_at update failed: %s", e)

    # ---- 认证 ----

    def authenticate(self, token: str, client_ip: str = "") -> Optional[Principal]:
        if not token:
            return None
        # 1) api_key 路径（含 prev 宽限 key）
        row = self._lookup_agent(token)
        if row:
            agent_id = row["agent_id"]
            # IP 白名单
            if not self._ip_in_whitelist(client_ip, row.get("api_key_ip_whitelist") or ""):
                logger.warning("auth denied: agent %s ip %s not in whitelist", agent_id, client_ip)
                return None
            # 主 key：过期即拒（轮换后主 key 新、prev 旧）
            # T1-2：hash 模式按 sha256(token) 与 api_key_hash 比对判定主/prev
            if "api_key_hash" in row:
                is_main = self._key_hash(token) == (row.get("api_key_hash") or "")
            else:
                is_main = token == row.get("api_key")
            if is_main:
                if self._is_expired(row.get("api_key_expires_at")):
                    return None
            # prev key（轮换后的旧 key）：仅宽限期（api_key_prev_expires_at）内有效
            else:
                if self._is_expired(row.get("api_key_prev_expires_at")):
                    return None
            self._touch_last_used(agent_id)
            # S1K scoped key 双模式之 A（2026-09-06 B1 补回）：token 同时命中
            # agents.api_key 与 agent_keys（换发模式：agent 的 api_key 本身被登记为
            # scoped key，guard_liveness 探针 5 语义）→ scope 附加。
            # 模式 B（sk- 独立认证，不依赖与 agents.api_key 相等）见下方顶层分支。
            _scope = None
            try:
                from key_scopes import get_store as _key_store_a
                _key_store_a = _key_store_a(self._config.DB_PATH)
                _sk_a = _key_store_a.lookup_by_hash(token)
                if _sk_a:
                    _scope = _sk_a["scope"]
                    try:
                        _key_store_a.touch(_sk_a["key_id"])
                    except Exception as _exc:
                        logger.debug("auth_provider silent-except @365: %s", _exc)
            except Exception as _e:
                logger.warning("S1K scoped key lookup failed: %s", _e)
            return Principal(subject_type="service", subject_id=agent_id,
                             auth_mode="api_key", scope=_scope)
        # S1K scoped API key 模式 B（2026-09-06 B1）：agent_keys 顶层独立查询。
        # 原实现只有模式 A（挂在 agents.api_key 命中后），而签发流程从不把 sk- 随机串写回
        # agents.api_key → 纯 scoped key 实际永远 401。模式 B 让 key 不依赖 agents.api_key
        # 相等即可认证。subject_id = key 绑定的 agent_id（预建身份；B1 D1：全权 api_key
        # 管理员托管，交付外部协作者的只有这张受限 key）。status/expiry 校验在
        # lookup_by_hash 内完成（revoked/过期 → None → 401）。
        _sk_row = None
        try:
            from key_scopes import get_store as _key_store
            _key_store = _key_store(self._config.DB_PATH)
            _sk_row = _key_store.lookup_by_hash(token)
        except Exception as _e:
            logger.warning("S1K scoped key lookup failed: %s", _e)
        if _sk_row:
            try:
                _key_store.touch(_sk_row["key_id"])
            except Exception:
                pass  # 调用画像失败不阻断认证
            return Principal(subject_type="service", subject_id=_sk_row["agent_id"],
                             auth_mode="api_key", scope=_sk_row["scope"])
        # 1e 员工路径：agents 未命中 → employee_accounts(key_hash SHA256)
        emp = self._lookup_employee_by_key(token)
        if emp:
            if emp.get("status") != "active":
                logger.warning("auth denied: employee %s not active", emp["employee_id"])
                return None
            if self._is_expired(emp.get("lease_expires_at")):
                logger.warning("auth denied: employee %s lease expired", emp["employee_id"])
                return None
            self._touch_last_used(emp["employee_id"])
            return Principal(
                subject_type="user", subject_id=emp["employee_id"],
                auth_mode="api_key", scope=self._employee_scope(emp),
            )
        # 2) hub_token 路径（部署级单 token，无身份语义）
        import hmac as _hmac

        if self._config.HUB_TOKEN and _hmac.compare_digest(token, self._config.HUB_TOKEN):
            return Principal(subject_type="service", subject_id="__hub__",
                             auth_mode="hub_token")
        return None

    def rotate_keys(self) -> int:
        self.invalidate_auth_cache()   # CD-040：轮换后旧 key 立即失效（TTL 兜底）
        """轮换到期 api_key：新 key 写 api_key，旧 key 移 prev（24h 宽限）。

        返回轮换条数。轮换事件由调用方（hub 启动任务）入审计。
        T1-2（2026-09-09）：已迁移库（api_key_hash 列存在）只写 SHA256 hash，
        明文列保持清空；新 key 明文在服务端生成即弃（Hub 不存明文，无法回吐），
        旧 key 在宽限期内仍可用，超宽限后该 agent 需管理员重新预签发。
        """
        from datetime import datetime, timedelta, timezone

        rotated = 0
        now = datetime.now(timezone.utc)
        grace_h = getattr(self._config, "API_KEY_ROTATION_GRACE_HOURS", 24)
        days = getattr(self._config, "API_KEY_ROTATION_DAYS", 90)
        try:
            with self._db_lock:
                conn = self._connect()
                try:
                    c = conn.cursor()
                    c.execute("PRAGMA table_info(agents)")
                    cols = {r[1] for r in c.fetchall()}
                    hashed = {"api_key_hash", "api_key_prev_hash"} <= cols
                    if hashed:
                        c.execute(
                            "SELECT agent_id, api_key_hash, api_key_expires_at FROM agents "
                            "WHERE COALESCE(api_key_hash, '') != '' "
                            "AND COALESCE(api_key_expires_at, '') != ''"
                        )
                    elif "api_key_expires_at" in cols:
                        # 未迁移老库：旧明文行为（兼容窗口）
                        c.execute(
                            "SELECT agent_id, api_key, api_key_expires_at FROM agents "
                            "WHERE api_key != '' AND api_key_expires_at != ''"
                        )
                    else:
                        return 0  # 旧库无轮换列，静默跳过
                    for row in c.fetchall():
                        try:
                            exp = datetime.fromisoformat(row["api_key_expires_at"])
                            if exp.tzinfo is None:
                                exp = exp.replace(tzinfo=timezone.utc)
                        except Exception:
                            continue
                        if exp <= now:
                            new_key = secrets.token_urlsafe(32)
                            prev_exp = (now + timedelta(hours=grace_h)).isoformat()
                            new_exp = (now + timedelta(days=days)).isoformat()
                            if hashed:
                                conn.execute(
                                    "UPDATE agents SET api_key='', api_key_hash=?, "
                                    "api_key_prev='', api_key_prev_hash=?, "
                                    "api_key_prev_expires_at=?, api_key_created_at=?, "
                                    "api_key_expires_at=? WHERE agent_id=?",
                                    (self._key_hash(new_key), row["api_key_hash"],
                                     prev_exp, now.isoformat(),
                                     new_exp, row["agent_id"]),
                                )
                            else:
                                conn.execute(
                                    "UPDATE agents SET api_key=?, api_key_prev=?, "
                                    "api_key_prev_expires_at=?, api_key_created_at=?, "
                                    "api_key_expires_at=? WHERE agent_id=?",
                                    (new_key, row["api_key"], prev_exp, now.isoformat(),
                                     new_exp, row["agent_id"]),
                                )
                            rotated += 1
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.warning("rotate_keys failed: %s", e)
        return rotated


# ---- LdapProvider（AD sAMAccountName 绑定 + 组查询） ----


class LdapProvider(AuthProvider):
    """AD/LDAP：用户密码绑定 + memberOf 递归组查询。

    依赖 ldap3（pip install ldap3）；未安装或未配置 → 构造抛 AuthProviderUnavailable。
    """

    mode = "ldap"

    def __init__(self, config):
        try:
            import ldap3  # noqa: F401
        except ImportError:
            raise AuthProviderUnavailable("ldap3 未安装")
        self._config = config
        url = getattr(config, "AUTH_LDAP_URL", "")
        if not url:
            raise AuthProviderUnavailable("AUTH_LDAP_URL 未配置")
        self._url = url
        self._bind_dn = getattr(config, "AUTH_LDAP_BIND_DN", "")
        self._bind_pw = getattr(config, "AUTH_LDAP_BIND_PASSWORD", "")
        self._base_dn = getattr(config, "AUTH_LDAP_BASE_DN", "")
        self._group_base = getattr(config, "AUTH_LDAP_GROUP_BASE_DN", self._base_dn)
        self._sync_interval = getattr(config, "AUTH_LDAP_GROUP_SYNC_SEC", 900)
        self._last_sync = 0.0
        self._lock = threading.Lock()

    def _conn(self):
        import ldap3

        return ldap3.Connection(
            self._url,
            user=self._bind_dn,
            password=self._bind_pw,
            auto_bind=True,
            raise_exceptions=False,
        )

    def _user_groups(self, conn, username: str, seen: Optional[set] = None,
                     depth: int = 0) -> List[str]:
        """递归遍历 memberOf（组嵌套），返回去重 DN 列表。"""
        import ldap3
        from ldap3.utils.conv import escape_filter_chars

        seen = seen or set()
        if depth > 10:  # 防环
            return []
        conn.search(
            self._base_dn,
            # fail-closed（2026-09-09 T13）：username 转义后插值，防 LDAP filter 注入
            f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(username)}))",
            attributes=["memberOf", "distinguishedName"],
            search_scope=ldap3.SUBTREE,
        )
        groups = []
        if not conn.entries:
            return groups
        entry = conn.entries[0]
        member_of = getattr(entry, "memberOf", []) or []
        for g in member_of:
            gdn = str(g)
            if gdn not in seen:
                seen.add(gdn)
                groups.append(gdn)
                # 嵌套：查该组的 memberOf
                conn.search(self._group_base, f"(distinguishedName={gdn})",
                            attributes=["memberOf"], search_scope=ldap3.SUBTREE)
                if conn.entries:
                    for g2 in (getattr(conn.entries[0], "memberOf", []) or []):
                        g2dn = str(g2)
                        if g2dn not in seen:
                            seen.add(g2dn)
                            groups.append(g2dn)
        return groups

    def authenticate(self, token: str, client_ip: str = "") -> Optional[Principal]:
        """token 格式: "username:password"（绑定即认证）。"""
        if ":" not in token:
            return None
        username, password = token.split(":", 1)
        if not username or not password:
            return None
        try:
            import ldap3

            user_dn = f"CN={username},{self._base_dn}" if not username.startswith("CN=") else username
            conn = ldap3.Connection(self._url, user=user_dn, password=password,
                                    auto_bind=True, raise_exceptions=False)
            if not conn.bound:
                return None
            groups = self._user_groups(conn, username)
            conn.unbind()
            return Principal(subject_type="user", subject_id=username,
                             groups=groups, auth_mode="ldap")
        except Exception as e:
            logger.warning("ldap auth failed for %s: %s", username, e)
            return None

    def sync_groups(self) -> int:
        """同步用户组关系到 principal_groups 表（节流：sync_interval 内只跑一次）。"""
        now = time.time()
        with self._lock:
            if now - self._last_sync < self._sync_interval:
                return 0
            self._last_sync = now
        try:
            import sqlite3

            conn = sqlite3.connect(self._config.DB_PATH)
            try:
                c = conn.cursor()
                c.execute(
                    "SELECT agent_id, agent_name FROM agents WHERE agent_name != '' "
                    "AND agent_name LIKE '%@%' OR agent_name LIKE 'CN=%'"
                )
                # 简化：先同步"用户名形如 CN=xxx"的 agent 归属；完整映射依赖部署配置
                count = 0
                for row in c.fetchall():
                    username = row["agent_name"]
                    if username.startswith("CN="):
                        username = username.split("=")[1].split(",")[0]
                    ldap_conn = self._conn()
                    try:
                        groups = self._user_groups(ldap_conn, username)
                    finally:
                        ldap_conn.unbind()
                    for g in groups:
                        c.execute(
                            "INSERT OR REPLACE INTO principal_groups "
                            "(principal_id, group_dn, synced_at) VALUES (?, ?, ?)",
                            (row["agent_id"], g, time.strftime("%Y-%m-%dT%H:%M:%S")),
                        )
                        count += 1
                conn.commit()
                return count
            finally:
                conn.close()
        except Exception as e:
            logger.warning("ldap sync_groups failed: %s", e)
            return 0


# ---- OidcProvider（JWT + JWKS 验签） ----


class OidcProvider(AuthProvider):
    """OIDC：JWT（id_token / access_token JWT 形）验签认证。

    依赖 PyJWT + cryptography；issuer/client_id/jwks_url 未配置 → 构造抛 AuthProviderUnavailable。
    JWKS 拉取失败时尝试用 issuer/.well-known/openid-configuration 发现。
    """

    mode = "oidc"

    def __init__(self, config):
        try:
            import jwt  # noqa: F401
        except ImportError:
            raise AuthProviderUnavailable("PyJWT 未安装")
        self._config = config
        self._issuer = getattr(config, "AUTH_OIDC_ISSUER", "")
        self._client_id = getattr(config, "AUTH_OIDC_CLIENT_ID", "")
        self._jwks_url = getattr(config, "AUTH_OIDC_JWKS_URL", "")
        if not self._issuer or not self._client_id:
            raise AuthProviderUnavailable("AUTH_OIDC_ISSUER / AUTH_OIDC_CLIENT_ID 未配置")
        self._jwks_cache = None
        self._jwks_fetched = 0.0

    def _fetch_jwks(self):
        """拉取 JWKS（带缓存，5 分钟过期）。"""
        import jwt
        import urllib.request

        if self._jwks_cache and time.time() - self._jwks_fetched < 300:
            return self._jwks_cache
        url = self._jwks_url
        if not url:
            # 从 issuer 发现
            url = self._issuer.rstrip("/") + "/.well-known/openid-configuration"
            with urllib.request.urlopen(url, timeout=5) as r:
                meta = json.loads(r.read().decode("utf-8"))
            url = meta.get("jwks_uri", "")
            if not url:
                raise AuthProviderUnavailable("OIDC jwks_uri 未发现")
        with urllib.request.urlopen(url, timeout=5) as r:
            jwks = json.loads(r.read().decode("utf-8"))
        self._jwks_cache = jwks
        self._jwks_fetched = time.time()
        return jwks

    def authenticate(self, token: str, client_ip: str = "") -> Optional[Principal]:
        """token = JWT（3 段）。验签 + 验 iss/aud/exp。"""
        if token.count(".") != 2:
            return None
        try:
            import jwt

            jwks = self._fetch_jwks()
            unverified = jwt.decode(token, options={"verify_signature": False,
                                                    "verify_aud": False})
            kid = unverified.get("kid") or unverified.get("alg", "")
            key = None
            for k in jwks.get("keys", []):
                if k.get("kid") == kid:
                    key = jwt.algorithms.RSAAlgorithm.from_jwk(k)
                    break
            if key is None:
                return None
            payload = jwt.decode(
                token, key,
                algorithms=["RS256", "RS384", "RS512"],
                audience=self._client_id,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
            sub = payload.get("sub") or payload.get("preferred_username") or payload.get("email", "")
            groups = list(payload.get("groups") or payload.get("roles") or [])
            return Principal(subject_type="user", subject_id=sub,
                             groups=groups, auth_mode="oidc")
        except Exception as e:
            logger.warning("oidc auth failed: %s", e)
            return None


# ---- 工厂 ----


def _looks_like_jwt(token: str) -> bool:
    return bool(token) and token.count(".") == 2


def get_auth_provider(config) -> AuthProvider:
    """按 CONFIG.AUTH_MODE 构造 provider；失败降级 local（fail-safe）。

    hybrid：token 形如 JWT 且 OIDC 可用 → OIDC；否则 api_key/hub_token（local 语义）。
    """
    mode = getattr(config, "AUTH_MODE", "local")
    if mode == "hybrid":
        # 延迟绑定：先试 OIDC（仅当有 JWT 形 token 时用，authenticate 内部判定），
        # 其余走 local。这里返回一个组合 provider。
        providers = []
        try:
            providers.append(("oidc", OidcProvider(config)))
        except AuthProviderUnavailable:
            pass
        providers.append(("local", LocalProvider(config)))
        return _HybridProvider(providers)
    if mode == "ldap":
        try:
            return LdapProvider(config)
        except AuthProviderUnavailable as e:
            logger.warning("ldap provider unavailable, fallback local: %s", e)
            return LocalProvider(config)
    if mode == "oidc":
        try:
            return OidcProvider(config)
        except AuthProviderUnavailable as e:
            logger.warning("oidc provider unavailable, fallback local: %s", e)
            return LocalProvider(config)
    return LocalProvider(config)


class _HybridProvider(AuthProvider):
    """hybrid = OIDC（人，JWT 形 token）+ local（机器，api_key/hub_token）。"""

    mode = "hybrid"

    def __init__(self, providers):
        self._providers = providers  # [(mode, provider), ...]

    def authenticate(self, token: str, client_ip: str = "") -> Optional[Principal]:
        if _looks_like_jwt(token):
            for mode, p in self._providers:
                if mode == "oidc":
                    principal = p.authenticate(token, client_ip)
                    if principal:
                        return principal
        # 非 JWT 或 OIDC 失败 → local 语义（api_key/hub_token）
        for mode, p in self._providers:
            if mode == "local":
                return p.authenticate(token, client_ip)
        return None

    def rotate_keys(self) -> int:
        total = 0
        for mode, p in self._providers:
            if mode == "local" and hasattr(p, "rotate_keys"):
                total += p.rotate_keys()
        return total

    def sync_groups(self) -> int:
        total = 0
        for mode, p in self._providers:
            if mode == "oidc" and hasattr(p, "sync_groups"):
                total += p.sync_groups()
        return total
