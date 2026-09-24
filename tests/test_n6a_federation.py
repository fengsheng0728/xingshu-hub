# -*- coding: utf-8 -*-
"""N6a 轻量互备验收测试（2026-08-05）

覆盖：
1. export_snapshot：4 种 kind 导出 + agents 快照**排除 api_key 列**（安全关键）
2. import_snapshot：upsert 落库（主权威覆盖备）
3. 单向：只拉不推（无写回逻辑）
4. 端到端：起两个测试 Hub（主 3071/备 3072）→ 备拉主快照 → 备库数据一致
"""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（tests/ 上一级）
sys.path.insert(0, str(_ROOT))

from federation_sync import export_snapshot, import_snapshot, AGENTS_SNAPSHOT_EXCLUDE


def _make_db(path, tables):
    conn = sqlite3.connect(path)
    for sql in tables:
        conn.execute(sql)
    conn.commit()
    conn.close()


AGENTS_TABLE = """CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY, agent_name TEXT, department TEXT,
    capabilities TEXT, role TEXT, managed_agents TEXT,
    disclosure_policy TEXT, endpoint TEXT, registered_at TEXT,
    last_heartbeat TEXT, status TEXT, api_key TEXT,
    api_key_created_at TEXT, api_key_expires_at TEXT,
    api_key_prev TEXT, api_key_prev_expires_at TEXT,
    api_key_ip_whitelist TEXT, last_used_at TEXT)"""

MEMORY_TABLE = """CREATE TABLE memory_pool (
    memory_id TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL,
    memory_key TEXT, content TEXT, summary TEXT, embedding BLOB,
    importance REAL, tags TEXT, disclosure_level TEXT,
    disclosure_scope TEXT, allowed_viewers TEXT, created_at TEXT,
    access_count INTEGER DEFAULT 0, last_accessed TEXT,
    kind TEXT DEFAULT 'fact', source_session_id TEXT DEFAULT '',
    confidence REAL DEFAULT 1.0, source_type TEXT DEFAULT 'user',
    updated_at TEXT DEFAULT NULL,
    trust_level TEXT DEFAULT 'internal',
    source_agent_id TEXT DEFAULT '', tainted_at TEXT DEFAULT '')"""

KNOWLEDGE_TABLE = """CREATE TABLE knowledge_base (
    entry_id TEXT PRIMARY KEY, title TEXT, content TEXT, tags TEXT,
    links TEXT, category TEXT, importance REAL, created_by TEXT,
    created_at TEXT, updated_at TEXT, embedding BLOB)"""


# ============ 1. 导出 ============

def test_export_agents_excludes_api_key():
    """agents 快照必须排除 api_key 相关列（备 Hub 拿不到主 Hub 密钥）。"""
    tmp = tempfile.mkdtemp(prefix="n6a-")
    db = os.path.join(tmp, "t.db")
    _make_db(db, [AGENTS_TABLE])
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO agents (agent_id, agent_name, role, status, api_key) "
        "VALUES ('a1', '测试', 'worker', 'online', 'SECRET-KEY-123')")
    conn.commit()
    conn.close()

    snap = export_snapshot(db, "agents")
    assert snap["status"] == "ok"
    assert snap["count"] == 1
    row = snap["rows"][0]
    assert "api_key" not in row, "快照不得含 api_key"
    assert row["agent_id"] == "a1"
    assert row["agent_name"] == "测试"
    for k in AGENTS_SNAPSHOT_EXCLUDE:
        assert k not in row, f"快照不得含 {k}"
    os.remove(db)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_export_memory_and_knowledge():
    tmp = tempfile.mkdtemp(prefix="n6a-")
    db = os.path.join(tmp, "t.db")
    _make_db(db, [MEMORY_TABLE, KNOWLEDGE_TABLE])
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content) VALUES ('m1','a1','k1','内容')")
    conn.execute(
        "INSERT INTO knowledge_base (entry_id, title, content) VALUES ('e1','标题','正文')")
    conn.commit()
    conn.close()

    m = export_snapshot(db, "memory")
    assert m["status"] == "ok" and m["count"] == 1 and m["rows"][0]["memory_id"] == "m1"
    k = export_snapshot(db, "knowledge")
    assert k["status"] == "ok" and k["count"] == 1 and k["rows"][0]["entry_id"] == "e1"
    bad = export_snapshot(db, "nope")
    assert bad["status"] == "error"
    os.remove(db)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ============ 2. 导入（主权威覆盖备） ============

def test_import_upsert_overwrites():
    """备库已有同主键数据 → 主快照覆盖（无冲突解决，主为权威）。"""
    tmp = tempfile.mkdtemp(prefix="n6a-")
    db = os.path.join(tmp, "t.db")
    _make_db(db, [AGENTS_TABLE])
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO agents (agent_id, agent_name, role, status, api_key) VALUES ('a1','旧名','worker','offline','OLD-KEY')")
    conn.commit()
    conn.close()

    rows = [{"agent_id": "a1", "agent_name": "新名", "role": "manager", "status": "online"}]
    r = import_snapshot(db, "agents", rows)
    assert r["status"] == "ok" and r["imported"] == 1
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT agent_name, role, status FROM agents WHERE agent_id='a1'").fetchone()
    conn.close()
    assert row == ("新名", "manager", "online"), f"主权威应覆盖备: {row}"
    os.remove(db)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_import_inserts_new():
    tmp = tempfile.mkdtemp(prefix="n6a-")
    db = os.path.join(tmp, "t.db")
    _make_db(db, [MEMORY_TABLE])
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content) VALUES ('m1','a1','k1','旧')")
    conn.commit()
    conn.close()

    rows = [{"memory_id": "m2", "owner_agent_id": "a1", "memory_key": "k2", "content": "新"}]
    r = import_snapshot(db, "memory", rows)
    assert r["status"] == "ok" and r["imported"] == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM memory_pool").fetchone()[0] == 2
    conn.close()
    os.remove(db)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ============ 2b. 导入列名白名单（授权门禁修复轮） ============

def test_import_rejects_unknown_columns():
    """远端 rows 含目标表 PRAGMA table_info 之外的列 → 整行拒绝（防伪造列名注入）。"""
    tmp = tempfile.mkdtemp(prefix="n6a-")
    db = os.path.join(tmp, "t.db")
    _make_db(db, [AGENTS_TABLE])

    rows = [{"agent_id": "a9", "agent_name": "x", "role": "worker",
             "status": "online", 'evil"; DROP TABLE agents;--': "1"}]
    r = import_snapshot(db, "agents", rows)
    assert r["status"] == "ok" and r["imported"] == 0, f"未知列行应被拒: {r}"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM agents").fetchone()[0] == 0
    conn.close()
    os.remove(db)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_import_valid_columns_still_quoted():
    """合法列名正常导入（双引号包裹不影响语义）。"""
    tmp = tempfile.mkdtemp(prefix="n6a-")
    db = os.path.join(tmp, "t.db")
    _make_db(db, [AGENTS_TABLE])
    rows = [{"agent_id": "a8", "agent_name": "合法", "role": "worker", "status": "online"}]
    r = import_snapshot(db, "agents", rows)
    assert r["status"] == "ok" and r["imported"] == 1
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT agent_name FROM agents WHERE agent_id='a8'").fetchone()
    conn.close()
    assert row == ("合法",)
    os.remove(db)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ============ 3. 端到端（双 Hub 真实拉取） ============

def test_e2e_pull_from_peer():
    """起主 Hub(3071) + 备 Hub(3072) → 备拉主 agents 快照 → 备库一致。"""
    tmp = tempfile.mkdtemp(prefix="n6a-e2e-")
    hub = None

    def start_hub(port, db_name):
        cfg_dir = os.path.join(tmp, f"cfg-{port}")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(
                f"server:\n  port: {port}\n  host: 127.0.0.1\n"
                f"database:\n  path: {os.path.join(tmp, db_name).replace(chr(92), '/')}\n"
                f"  backup_enabled: False\n"
            )
        env = dict(os.environ)
        env["SYNC_HUB_CONFIG_DIR"] = cfg_dir
        env.pop("SYNC_HUB_NO_AUTH", None)
        return subprocess.Popen(
            [sys.executable, "main.py"], cwd=str(_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ), os.path.join(tmp, db_name)

    def wait_health(port, timeout=45):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    return True
            except Exception:
                time.sleep(0.5)
        return False

    try:
        hub, db_main = start_hub(3071, "main.db")
        hub2, db_backup = start_hub(3072, "backup.db")
        assert wait_health(3071) and wait_health(3072), "双 Hub 未就绪"

        # 主 Hub 注册一个 agent（拿 api_key）
        req = urllib.request.Request(
            "http://127.0.0.1:3071/api/v1/agents/register",
            data=json.dumps({"agent_id": "fed-a", "agent_name": "联邦A"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            reg = json.loads(r.read().decode())
        api_key = reg.get("api_key", "")

        # 安全收编（授权门禁修复轮）：快照端点仅 manager/orchestrator 可拉。
        # 注册默认 role=worker → 先验证 worker 被 403 拒绝，再提升为 manager 验证放行。
        req = urllib.request.Request(
            "http://127.0.0.1:3071/api/v1/federation/snapshot/agents",
            headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                snap = json.loads(r.read().decode())
            raise AssertionError(f"worker 不应能拉快照: {snap}")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"worker 拉快照应 403，实得 {e.code}"

        conn = sqlite3.connect(db_main)
        conn.execute("UPDATE agents SET role='manager' WHERE agent_id='fed-a'")
        conn.commit()
        conn.close()

        # 备 Hub 直接调快照端点拉取（模拟 pull_from_peer 的网络路径）
        req = urllib.request.Request(
            "http://127.0.0.1:3071/api/v1/federation/snapshot/agents",
            headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            snap = json.loads(r.read().decode())
        assert snap["status"] == "ok", f"快照拉取失败: {snap}"
        assert snap["count"] >= 1
        assert all("api_key" not in row for row in snap["rows"]), "快照泄露 api_key!"

        # 落备库
        from federation_sync import import_snapshot
        imp = import_snapshot(db_backup, "agents", snap["rows"])
        assert imp["status"] == "ok" and imp["imported"] >= 1
        conn = sqlite3.connect(db_backup)
        row = conn.execute("SELECT agent_id, agent_name FROM agents WHERE agent_id='fed-a'").fetchone()
        conn.close()
        assert row == ("fed-a", "联邦A"), f"备库应同步到: {row}"
    finally:
        if hub:
            hub.kill()
        if hub2:
            hub2.kill()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ============ 4. 快照端点双通道认证（联邦快照链断裂修复轮） ============
#
# 背景：pair exchange 落库的 remote_api_key 是 secrets.token_urlsafe(32) 随机串，
# 只写 team_members 表，从不注册进 agents/agent_keys——快照端点走标准认证
# （get_current_agent）必然 401，再叠加 manager/orchestrator 角色门 → 双门永拒，
# 合法跨 Hub 快照拉取从未工作过。
# 修复口径（对齐 /team/proxy/disclose 既成模式）：路由层认证豁免
# （routes.AUTH_ALLOWLIST_PREFIXES 前缀），端点函数内双通道自认证——
# (a) 标准凭据：hub_token 或 manager/orchestrator → 放行；
# (b) 配对凭据：team_members.remote_api_key 命中且 revoked_at IS NULL → 放行；
# 两路皆败 → 401（无效凭据）/403（身份有效但无权限），不泄露配对存在性。


def _start_hub(tmp, port, db_name):
    """起一个独立测试 Hub（临时 config 指向临时 DB，鉴权开启）。"""
    cfg_dir = os.path.join(tmp, f"cfg-{port}")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(
            f"server:\n  port: {port}\n  host: 127.0.0.1\n"
            f"database:\n  path: {os.path.join(tmp, db_name).replace(chr(92), '/')}\n"
            f"  backup_enabled: False\n"
        )
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = cfg_dir
    env.pop("SYNC_HUB_NO_AUTH", None)  # 确保鉴权开启——本组用例验的就是认证通道
    return subprocess.Popen(
        [sys.executable, "main.py"], cwd=str(_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ), os.path.join(tmp, db_name)


def _wait_health(port, timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def _get_snapshot(port, kind, token):
    """GET 快照端点，返回 (http_status, body_or_None)。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/federation/snapshot/{kind}",
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def _insert_team_member(db, api_key, revoked=False, remote_hub_id="hub-peer"):
    """直写一条 team_members 配对记录（模拟 pair exchange 落库）。"""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO team_members (local_agent_id, remote_hub_id, remote_hub_url,"
        " remote_agent_id, remote_api_key, paired_at, key_expires_at, revoked_at)"
        " VALUES ('local-a', ?, 'http://192.168.1.2:3060', 'remote-b', ?,"
        " '2026-01-01T00:00:00+00:00', '2027-01-01T00:00:00+00:00', ?)",
        (remote_hub_id, api_key, "2026-06-01T00:00:00+00:00" if revoked else None))
    conn.commit()
    conn.close()


def test_snapshot_path_in_auth_allowlist_prefixes():
    """快照端点已登记认证豁免前缀（端点内双通道自认证兜底）。"""
    import routes
    assert "/api/v1/federation/snapshot" in routes.AUTH_ALLOWLIST_PREFIXES
    # /federation/pull 是本地触发动作，保持全局认证 + 角色门，不得进豁免
    assert "/api/v1/federation/pull" not in routes.AUTH_ALLOWLIST_PATHS
    assert not any("/api/v1/federation/pull".startswith(p)
                   for p in routes.AUTH_ALLOWLIST_PREFIXES)


def test_snapshot_pairing_credential_dual_channel():
    """配对凭据可拉快照；未配对随机串 401；revoked 成员 401/403；manager key 仍通。"""
    tmp = tempfile.mkdtemp(prefix="n6a-dual-")
    port = 3073
    proc = None
    PAIR_KEY = "pair-active-" + "a" * 32
    REVOKED_KEY = "pair-revoked-" + "b" * 31
    try:
        proc, db = _start_hub(tmp, port, "hub.db")
        assert _wait_health(port), "Hub 未就绪"

        _insert_team_member(db, PAIR_KEY, revoked=False, remote_hub_id="hub-peer-a")
        _insert_team_member(db, REVOKED_KEY, revoked=True, remote_hub_id="hub-peer-b")

        # 1. 配对凭据（team_members.remote_api_key，未撤销）→ 放行
        code, body = _get_snapshot(port, "agents", PAIR_KEY)
        assert code == 200, f"配对凭据拉快照应 200，实得 {code}"
        assert body["status"] == "ok"
        assert all("api_key" not in row for row in body["rows"]), "快照泄露 api_key!"

        # 2. 未配对随机串 → 401
        code, _ = _get_snapshot(port, "agents", "unpaired-" + "x" * 32)
        assert code == 401, f"未配对随机串应 401，实得 {code}"

        # 3. revoked 配对成员 → 401/403（不泄露存在性即可）
        code, _ = _get_snapshot(port, "agents", REVOKED_KEY)
        assert code in (401, 403), f"revoked 成员应 401/403，实得 {code}"

        # 4. manager 标准凭据仍通（角色门通道不回归）
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/agents/register",
            data=json.dumps({"agent_id": "fed-mgr", "agent_name": "主管"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            mgr_key = json.loads(r.read().decode())["api_key"]
        conn = sqlite3.connect(db)
        conn.execute("UPDATE agents SET role='manager' WHERE agent_id='fed-mgr'")
        conn.commit()
        conn.close()
        code, body = _get_snapshot(port, "agents", mgr_key)
        assert code == 200 and body["status"] == "ok", f"manager 拉快照应通，实得 {code}"

        # 5. 无 token → 401（豁免≠匿名放行，端点自认证兜底）
        code, _ = _get_snapshot(port, "agents", "")
        assert code == 401, f"无 token 应 401，实得 {code}"
    finally:
        if proc:
            proc.kill()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
