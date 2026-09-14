# -*- coding: utf-8 -*-
"""S1 身份接入验收测试（2026-08-05）

覆盖：
1. LocalProvider：api_key 精确归属 / 过期拒绝 / IP 白名单 / 轮换（新旧 key 宽限）/ hub_token
2. 工厂：oidc 缺配置降级 local；hybrid JWT 路由
3. disclosure 组交集规则（规则 2b）
4. TokenAuthMiddleware principal 注入（HTTP 层）
"""
import json
import os
import secrets
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

from models import Config
from auth_provider import LocalProvider, OidcProvider, get_auth_provider, Principal


def _make_agents_db(path: str, rows: list):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE agents (
            agent_id TEXT PRIMARY KEY, api_key TEXT, api_key_prev TEXT,
            api_key_created_at TEXT, api_key_expires_at TEXT,
            api_key_prev_expires_at TEXT, api_key_ip_whitelist TEXT, last_used_at TEXT)"""
    )
    for r in rows:
        conn.execute(
            "INSERT INTO agents (agent_id, api_key, api_key_created_at, api_key_expires_at,"
            " api_key_ip_whitelist) VALUES (?, ?, ?, ?, ?)",
            (r[0], r[1], r[2], r[3], r[4]),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def local_cfg():
    tmp = tempfile.mktemp(suffix=".db")
    _make_agents_db(tmp, [])
    cfg = Config()
    cfg.DB_PATH = tmp
    cfg.HUB_TOKEN = ""
    yield cfg
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_api_key_authenticate_returns_agent_id(local_cfg):
    key = secrets.token_urlsafe(32)
    conn = sqlite3.connect(local_cfg.DB_PATH)
    conn.execute("INSERT INTO agents (agent_id, api_key) VALUES (?, ?)", ("ag1", key))
    conn.commit()
    conn.close()
    p = LocalProvider(local_cfg)
    r = p.authenticate(key, "127.0.0.1")
    assert r is not None
    assert r.subject_id == "ag1"
    assert r.subject_type == "service"
    assert r.auth_mode == "api_key"


def test_api_key_unknown_rejected(local_cfg):
    p = LocalProvider(local_cfg)
    assert p.authenticate("bad-key", "127.0.0.1") is None


def test_expired_api_key_rejected(local_cfg):
    key = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(local_cfg.DB_PATH)
    conn.execute(
        "INSERT INTO agents (agent_id, api_key, api_key_expires_at) VALUES (?, ?, ?)",
        ("ag1", key, exp),
    )
    conn.commit()
    conn.close()
    p = LocalProvider(local_cfg)
    assert p.authenticate(key, "127.0.0.1") is None


def test_ip_whitelist_enforced(local_cfg):
    key = secrets.token_urlsafe(32)
    conn = sqlite3.connect(local_cfg.DB_PATH)
    conn.execute(
        "INSERT INTO agents (agent_id, api_key, api_key_ip_whitelist) VALUES (?, ?, ?)",
        ("ag1", key, json.dumps(["10.0.0.0/8", "192.168.1.5"])),
    )
    conn.commit()
    conn.close()
    p = LocalProvider(local_cfg)
    assert p.authenticate(key, "10.1.2.3") is not None
    assert p.authenticate(key, "192.168.1.5") is not None
    assert p.authenticate(key, "8.8.8.8") is None


def test_rotation_new_key_valid_old_key_grace(local_cfg):
    """轮换：到期 → 新 key 可用，旧 key 宽限期（24h）内仍可用，超宽限拒绝。"""
    key = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = sqlite3.connect(local_cfg.DB_PATH)
    conn.execute(
        "INSERT INTO agents (agent_id, api_key, api_key_expires_at) VALUES (?, ?, ?)",
        ("ag1", key, exp),
    )
    conn.commit()
    conn.close()
    p = LocalProvider(local_cfg)
    # 到期 key 拒绝
    assert p.authenticate(key, "127.0.0.1") is None
    # 轮换
    assert p.rotate_keys() == 1
    conn = sqlite3.connect(local_cfg.DB_PATH)
    row = conn.execute("SELECT api_key, api_key_prev FROM agents WHERE agent_id='ag1'").fetchone()
    new_key, old_key = row
    conn.close()
    assert new_key != key
    # 新 key 有效
    assert p.authenticate(new_key, "127.0.0.1") is not None
    # 旧 key 宽限内有效
    assert p.authenticate(old_key, "127.0.0.1") is not None
    # 超宽限：模拟 25h 后
    past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    conn = sqlite3.connect(local_cfg.DB_PATH)
    conn.execute("UPDATE agents SET api_key_prev_expires_at=? WHERE agent_id='ag1'", (past,))
    conn.commit()
    conn.close()
    assert p.authenticate(old_key, "127.0.0.1") is None
    assert p.authenticate(new_key, "127.0.0.1") is not None


def test_hub_token_principal(local_cfg):
    local_cfg.HUB_TOKEN = "hub-secret-1"
    p = LocalProvider(local_cfg)
    r = p.authenticate("hub-secret-1", "127.0.0.1")
    assert r is not None
    assert r.subject_id == "__hub__"
    assert r.auth_mode == "hub_token"


def test_rotation_disabled_never_expires():
    cfg = Config()
    cfg.DB_PATH = tempfile.mktemp(suffix=".db")
    cfg.API_KEY_ROTATION_DAYS = 0
    key = secrets.token_urlsafe(32)
    _make_agents_db(cfg.DB_PATH, [])
    conn = sqlite3.connect(cfg.DB_PATH)
    conn.execute("INSERT INTO agents (agent_id, api_key) VALUES (?, ?)", ("ag1", key))
    conn.commit()
    conn.close()
    p = LocalProvider(cfg)
    assert p.authenticate(key, "127.0.0.1") is not None
    assert p.rotate_keys() == 0
    os.remove(cfg.DB_PATH)


def test_is_expired_fail_closed(local_cfg):
    """T13 ①：_is_expired fail-closed —— 非空但畸形时间戳 → 已过期（拒绝）。"""
    p = LocalProvider(local_cfg)
    # 畸形 → True（fail-closed）
    assert p._is_expired("not-a-date") is True
    # 空值 → False（永不过期旧数据语义回归）
    assert p._is_expired("") is False
    assert p._is_expired(None) is False
    # 合法过去 → True；合法未来 → False（回归）
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert p._is_expired(past) is True
    assert p._is_expired(future) is False


def test_ldap_filter_injection_escaped(local_cfg):
    """T13 ②：LdapProvider._user_groups 的 sAMAccountName filter 注入字符被转义。"""
    from auth_provider import LdapProvider

    local_cfg.AUTH_LDAP_URL = "ldap://127.0.0.1:389"  # 不真实连接：fake conn 直接喂 _user_groups
    p = LdapProvider(local_cfg)

    class FakeConn:
        def __init__(self):
            self.filters = []
            self.entries = []  # 无命中 → _user_groups 记录 filter 后返回 []

        def search(self, base, filt, attributes=None, search_scope=None):
            self.filters.append(filt)
            return True

    # 注入式 username：filter 值部分无裸注入字符，转义序列出现
    # （注意：filter 骨架 `(...)(...)` 本身含 ")("，故断言整串等于转义后的期望值）
    fc = FakeConn()
    p._user_groups(fc, 'a)(|(uid=*')
    f = fc.filters[0]
    assert f == "(&(objectClass=user)(sAMAccountName=a\\29\\28|\\28uid=\\2a))", \
        f"注入字符未转义: {f}"
    # 正常 username → filter 形态不变（回归）
    fc2 = FakeConn()
    p._user_groups(fc2, "zhang.san")
    assert fc2.filters[0] == "(&(objectClass=user)(sAMAccountName=zhang.san))"


def test_factory_oidc_missing_config_falls_back_local():
    cfg = Config()
    cfg.DB_PATH = tempfile.mktemp(suffix=".db")
    _make_agents_db(cfg.DB_PATH, [])
    cfg.AUTH_MODE = "oidc"
    p = get_auth_provider(cfg)
    assert isinstance(p, LocalProvider)
    os.remove(cfg.DB_PATH)


def test_factory_hybrid_jwt_routes_to_oidc():
    """hybrid：JWT 形 token → OIDC 尝试；非 JWT → local。"""
    cfg = Config()
    cfg.DB_PATH = tempfile.mktemp(suffix=".db")
    _make_agents_db(cfg.DB_PATH, [])
    cfg.AUTH_MODE = "hybrid"
    # 无 OIDC 配置 → hybrid 降级为纯 local，但 JWT 形 token 也不该被 local 误认
    p = get_auth_provider(cfg)
    # JWT 形（3 段）但无签名 → OIDC 失败 → None（不会落到 local 误放行）
    assert p.authenticate("eyJ.a.b", "127.0.0.1") is None
    os.remove(cfg.DB_PATH)


# ---- T1-2（2026-09-09）：api_key 哈希化模式（alembic 0003 后的库形态） ----

def _make_agents_db_hashed(path: str):
    """0003 后的 agents 表形态：api_key/api_key_prev 清空，hash 列持证。"""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE agents (
            agent_id TEXT PRIMARY KEY, api_key TEXT DEFAULT '', api_key_prev TEXT DEFAULT '',
            api_key_hash TEXT, api_key_prev_hash TEXT,
            api_key_created_at TEXT, api_key_expires_at TEXT,
            api_key_prev_expires_at TEXT, api_key_ip_whitelist TEXT, last_used_at TEXT)"""
    )
    conn.commit()
    conn.close()


def _sha(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.fixture()
def hashed_cfg():
    tmp = tempfile.mktemp(suffix=".db")
    _make_agents_db_hashed(tmp)
    cfg = Config()
    cfg.DB_PATH = tmp
    cfg.HUB_TOKEN = ""
    yield cfg
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_hash_mode_authenticate_by_hash(hashed_cfg):
    """hash 模式：明文 key 认证命中（先 hash 再查），库内无明文。"""
    key = secrets.token_urlsafe(32)
    conn = sqlite3.connect(hashed_cfg.DB_PATH)
    conn.execute("INSERT INTO agents (agent_id, api_key, api_key_hash) VALUES (?, '', ?)",
                 ("ag1", _sha(key)))
    conn.commit()
    conn.close()
    p = LocalProvider(hashed_cfg)
    r = p.authenticate(key, "127.0.0.1")
    assert r is not None and r.subject_id == "ag1" and r.auth_mode == "api_key"
    # 错 key → None（401 语义）
    assert p.authenticate("wrong-key", "127.0.0.1") is None
    # 库内明文列为空
    conn = sqlite3.connect(hashed_cfg.DB_PATH)
    assert conn.execute("SELECT api_key FROM agents WHERE agent_id='ag1'").fetchone()[0] == ""
    conn.close()


def test_hash_mode_expired_and_whitelist(hashed_cfg):
    """hash 模式：主 key 过期拒绝；IP 白名单语义不变。"""
    key = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(hashed_cfg.DB_PATH)
    conn.execute(
        "INSERT INTO agents (agent_id, api_key, api_key_hash, api_key_expires_at,"
        " api_key_ip_whitelist) VALUES (?, '', ?, ?, ?)",
        ("ag1", _sha(key), exp, json.dumps(["10.0.0.0/8"])),
    )
    conn.commit()
    conn.close()
    p = LocalProvider(hashed_cfg)
    assert p.authenticate(key, "10.1.2.3") is None  # 过期
    # 未过期 + 白名单内 → 通过；白名单外 → 拒绝
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(hashed_cfg.DB_PATH)
    conn.execute("UPDATE agents SET api_key_expires_at=? WHERE agent_id='ag1'", (future,))
    conn.commit()
    conn.close()
    assert p.authenticate(key, "10.1.2.3") is not None
    assert p.authenticate(key, "8.8.8.8") is None


def test_hash_mode_rotation_writes_hash_only(hashed_cfg):
    """hash 模式轮换：到期 → prev_hash=旧 hash、api_key_hash=新 hash、明文列清空，
    旧 key 宽限内可认证、超宽限 401；全程库内无明文。"""
    key = secrets.token_urlsafe(32)
    old_hash = _sha(key)
    exp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = sqlite3.connect(hashed_cfg.DB_PATH)
    conn.execute(
        "INSERT INTO agents (agent_id, api_key, api_key_hash, api_key_expires_at)"
        " VALUES (?, '', ?, ?)",
        ("ag1", old_hash, exp),
    )
    conn.commit()
    conn.close()
    p = LocalProvider(hashed_cfg)
    assert p.authenticate(key, "127.0.0.1") is None  # 到期拒
    assert p.rotate_keys() == 1
    conn = sqlite3.connect(hashed_cfg.DB_PATH)
    row = conn.execute(
        "SELECT api_key, api_key_prev, api_key_hash, api_key_prev_hash"
        " FROM agents WHERE agent_id='ag1'").fetchone()
    conn.close()
    api_key_col, prev_col, new_hash, prev_hash = row
    assert api_key_col == "" and prev_col == "", "轮换后明文列必须为空"
    assert prev_hash == old_hash, "旧 key hash 应移入 prev_hash"
    assert new_hash and new_hash != old_hash
    # 旧 key 宽限内有效；超宽限拒绝
    assert p.authenticate(key, "127.0.0.1") is not None
    past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    conn = sqlite3.connect(hashed_cfg.DB_PATH)
    conn.execute("UPDATE agents SET api_key_prev_expires_at=? WHERE agent_id='ag1'", (past,))
    conn.commit()
    conn.close()
    assert p.authenticate(key, "127.0.0.1") is None


# ---- disclosure 组交集（规则 2b） ----

@pytest.fixture()
def disclosure_env():
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute(
        """CREATE TABLE principal_groups (
            principal_id TEXT NOT NULL, group_dn TEXT NOT NULL,
            synced_at TEXT, PRIMARY KEY (principal_id, group_dn))"""
    )
    conn.commit()
    conn.close()
    cfg = Config()
    cfg.DB_PATH = tmp
    yield tmp, cfg
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_disclosure_group_intersection(disclosure_env):
    """同组 requester 看到 SUMMARY，不同组仍 NONE（规则 2b）。"""
    tmp, cfg = disclosure_env
    conn = sqlite3.connect(tmp)
    conn.execute(
        "INSERT INTO principal_groups (principal_id, group_dn) VALUES (?, ?)",
        ("req-a", "CN=Finance,OU=Dept,DC=corp,DC=local"),
    )
    conn.execute(
        "INSERT INTO principal_groups (principal_id, group_dn) VALUES (?, ?)",
        ("own-b", "CN=Finance,OU=Dept,DC=corp,DC=local"),
    )
    conn.execute(
        "INSERT INTO principal_groups (principal_id, group_dn) VALUES (?, ?)",
        ("req-x", "CN=Ops,OU=Dept,DC=corp,DC=local"),
    )
    conn.commit()
    conn.close()

    # disclosure.py 用模块级 CONFIG.DB_PATH —— 临时指向测试库
    import disclosure as disclosure_mod
    orig = disclosure_mod.CONFIG.DB_PATH
    disclosure_mod.CONFIG.DB_PATH = tmp
    try:
        from disclosure import DisclosureEngine
        from models import DisclosureLevel

        class FakeHub:
            def __init__(self):
                self._disclosure_policy = {}
                self.agents = {
                    "req-a": {"role": "worker", "department": ""},
                    "own-b": {"role": "worker", "department": ""},
                    "req-x": {"role": "worker", "department": ""},
                }

            def _db(self):
                # T2-2 对齐: 披露引擎复用 hub._db 连接工厂
                from models import CONFIG
                import sqlite3 as _s

                conn = _s.connect(CONFIG.DB_PATH)
                conn.row_factory = _s.Row
                conn.execute("PRAGMA busy_timeout = 5000")
                return conn

        engine = DisclosureEngine(FakeHub())
        memory = {
            "owner_agent_id": "own-b",
            "disclosure_level": "summary",
            "allowed_viewers": "[]",
            "content": "机密财务数据",
        }
        # 同组 → SUMMARY（规则 2b 在规则 7 前命中）
        level = engine._calculate_disclosure_level(
            memory, "req-a", {}, DisclosureLevel.SUMMARY)
        assert level.value == "summary"
        # 不同组 → NONE
        level2 = engine._calculate_disclosure_level(
            memory, "req-x", {}, DisclosureLevel.SUMMARY)
        assert level2.value == "none"
    finally:
        disclosure_mod.CONFIG.DB_PATH = orig


def test_disclosure_no_groups_zero_impact(disclosure_env):
    """无组数据 → 规则 2b 跳过，原规则链行为不变（worker 隔离 NONE）。"""
    tmp, cfg = disclosure_env
    import disclosure as disclosure_mod
    orig = disclosure_mod.CONFIG.DB_PATH
    disclosure_mod.CONFIG.DB_PATH = tmp
    try:
        from disclosure import DisclosureEngine
        from models import DisclosureLevel

        class FakeHub:
            def __init__(self):
                self._disclosure_policy = {}
                self.agents = {
                    "req-a": {"role": "worker", "department": ""},
                    "own-b": {"role": "worker", "department": ""},
                }

            def _db(self):
                # T2-2 对齐: 披露引擎复用 hub._db 连接工厂
                from models import CONFIG
                import sqlite3 as _s

                conn = _s.connect(CONFIG.DB_PATH)
                conn.row_factory = _s.Row
                conn.execute("PRAGMA busy_timeout = 5000")
                return conn

        engine = DisclosureEngine(FakeHub())
        memory = {
            "owner_agent_id": "own-b",
            "disclosure_level": "summary",
            "allowed_viewers": "[]",
            "content": "x",
        }
        level = engine._calculate_disclosure_level(memory, "req-a", {}, DisclosureLevel.SUMMARY)
        assert level.value == "none"  # 无组 → 规则 7 worker 隔离仍生效
    finally:
        disclosure_mod.CONFIG.DB_PATH = orig
