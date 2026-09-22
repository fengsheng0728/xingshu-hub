# -*- coding: utf-8 -*-
"""对外只读 scoped key：方法维度白名单 + 凭据端点硬拒绝 + WS 语义锁定（CD-069）

背景（2026-09-20，用户拍板「放出 API 后本地做后鉴权的限制收口」）：
现有 S1K scoped key 只有 endpoints（路径）一维白名单，对外交付「只读 key」时存在两个缺口：
  1. 同路径的读/写无法区分——endpoints=["/tasks"] 既放行 GET 也放行 POST；
  2. 受限主体（scoped key / 员工账号）只要绑定的 agent 是 manager/orchestrator，
     就能调 /api/v1/keys 自行签发新 key、或调 /api/v1/server/config 把 Hub 暴露到 0.0.0.0
     —— 对外交付的「受限 key」可以自我提权成全权。
本测试锁定修复后的语义（三态：方法白名单命中/不命中/未声明零迁移 + 凭据端点硬拒绝 + WS fail-closed）。

配方：tests/test_key_issue_e2e.py（guarded Hub subprocess + hub_cli 签发 + 真 HTTP）。
三铁律：① stdout/stderr DEVNULL（管道满卡死）② 轮询 /health（最长 40s）③ 末尾 kill + rmtree。
端口 3072（避开 3062 auth_matrix / 3063 ws_matrix / 3071 key_issue_e2e）。
"""
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB_PORT = 3072
HUB_TOKEN = "ro-scoped-hub-token"
MGR_AGENT = "ext-ro-mgr"


# ── 独立 guarded Hub ──


def _write_config(cfg_dir: str, cfg: dict) -> None:
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.yaml"), "w", encoding="utf-8") as f:
        yaml = __import__("yaml")
        yaml.dump(cfg, f, allow_unicode=True)


def _subprocess_env(cfg_dir: str, tmpdir: str) -> dict:
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = cfg_dir
    env["SYNC_HUB_CHROMA_PATH"] = os.path.join(tmpdir, "chroma_db")
    env.pop("SYNC_HUB_NO_AUTH", None)  # 确保鉴权开启（conftest 在 pytest 进程内 NO_AUTH=1）
    return env


@pytest.fixture(scope="module")
def guarded_hub():
    tmpdir = tempfile.mkdtemp(prefix="ro-scoped-")
    cfg_dir = os.path.join(tmpdir, "config")
    db_path = os.path.join(tmpdir, "guarded.db")
    _write_config(cfg_dir, {
        "server": {"host": "127.0.0.1", "port": HUB_PORT},
        "auth": {"enabled": True, "registration": "guarded", "hub_token": HUB_TOKEN},
        "database": {"path": db_path, "backup_enabled": False},
        "logging": {"level": "warning"},
        "ui": {"close_to_tray": True, "start_minimized": False},
    })
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=_subprocess_env(cfg_dir, tmpdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ok = False
    for _ in range(80):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{HUB_PORT}/health", timeout=2) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            continue
    if not ok:
        proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("guarded Hub failed to start within 40s")
    yield {"db_path": db_path, "tmpdir": tmpdir}
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


# ── 工具 ──


def _req(method: str, path: str, token: str = "", body: dict = None):
    """返回 (http_status, body_dict)；网络异常返回 (0, {})"""
    url = f"http://127.0.0.1:{HUB_PORT}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def _cli(args: list) -> dict:
    r = subprocess.run(
        [sys.executable, "hub_cli.py"] + args,
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    try:
        out = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        out = {"raw_stdout": r.stdout[:300]}
    out["_rc"] = r.returncode
    return out


def _db_row(db_path: str, sql: str, params: tuple = ()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _register_manager(cred: str) -> dict:
    """REST 重引导已预签发的 manager。

    两处关键语义（实测踩出）：
      ① register/bootstrap 在 hub_token 已配置时，中间件要求请求带 hub_token
         （T0-2 条件性豁免）——用 agent 自己的 api_key 打 register 会 401；
      ② guard 模式的 role 真相在服务端（CD-020），重注册不改 role → 角色由
         hub_cli agent create --role manager 预置。
    用 hub_token 走 register 的另一收益：_check_reregister_credential 接受 hub_token，
    并把该 agent 同步进 hub.agents 内存（role 门读内存，不读 DB）。"""
    code, res = _req("POST", "/api/v1/agents/register", token=HUB_TOKEN, body={
        "agent_id": MGR_AGENT, "agent_name": "对外只读测试主管", "role": "manager",
        "department": "proj-ext", "capabilities": [],
    })
    assert code in (200, 201), f"register manager 失败 {code}: {res}"
    return {"api_key": cred, "body": res}


def _issue_key(db_path: str, endpoints: str = "/memory,/tasks", extra: list = None) -> dict:
    args = ["key", "create", "--agent", MGR_AGENT,
            "--endpoints", endpoints,
            "--db", db_path] + (extra or [])
    cli = _cli(args)
    assert cli.get("status") == "created", f"key create 失败: {cli}"
    return cli


def _provision(db_path: str) -> dict:
    """模块级 Hub 跨用例共享：manager 身份幂等预建（CLI 建号 → REST 重引导进内存）"""
    row = _db_row(db_path, "SELECT api_key FROM agents WHERE agent_id = ?", (MGR_AGENT,))
    if row:
        cred = row["api_key"]
        if cred:
            return _register_manager(cred)
    cli = _cli(["agent", "create", "--id", MGR_AGENT, "--name", "对外只读测试主管",
                "--role", "manager", "--department", "proj-ext", "--db", db_path])
    assert cli.get("status") == "created", f"agent create 失败: {cli}"
    return _register_manager(cli["api_key"])


# ── 用例 ──


def test_cli_issue_methods_scope(guarded_hub):
    """签发闭环：--methods 落库 + 非法方法 fail-closed 400"""
    db_path = guarded_hub["db_path"]
    _provision(db_path)
    created = _issue_key(db_path, extra=["--methods", "GET,HEAD"])
    assert created["scope"].get("methods") == ["GET", "HEAD"], \
        f"--methods 应落库为列表: {created['scope']}"
    row = _db_row(db_path, "SELECT scope FROM agent_keys WHERE key_id = ?",
                  (created["key_id"],))
    assert json.loads(row["scope"])["methods"] == ["GET", "HEAD"], "methods 应持久化"

    bad = _cli(["key", "create", "--agent", MGR_AGENT, "--methods", "FOO",
                "--db", db_path])
    assert bad.get("code") == 400 and bad["_rc"] == 1, \
        f"非法方法值应 400 fail-closed: {bad}"


def test_readonly_key_http_matrix(guarded_hub):
    """只读 key 真 HTTP：GET 放行 / POST 被方法白名单拒 403"""
    db_path = guarded_hub["db_path"]
    admin = _provision(db_path)
    key = _issue_key(db_path, extra=["--methods", "GET,HEAD"])
    sk = key["key"]

    code, res = _req("GET", "/api/v1/tasks", token=sk)
    assert code == 200, f"只读 key 读端点应 200, 实际 {code}: {res}"

    code, res = _req("POST", "/api/v1/memory/search", token=sk,
                     body={"agent_id": MGR_AGENT, "query": "ro-probe"})
    assert code == 403, f"只读 key 调 POST 应 403（方法不在白名单）, 实际 {code}: {res}"
    assert "method" in json.dumps(res, ensure_ascii=False).lower(), \
        f"403 语义应指明方法维度: {res}"

    code, res = _req("POST", "/api/v1/knowledge", token=sk,
                     body={"title": "x", "content": "y", "tags": []})
    assert code == 403, f"只读 key 调写端点应 403, 实际 {code}: {res}"


def test_legacy_key_without_methods_zero_migration(guarded_hub):
    """零迁移：未声明 methods 的既有 key 行为不变（POST 读端点仍放行）"""
    db_path = guarded_hub["db_path"]
    _provision(db_path)
    key = _issue_key(db_path)  # 无 --methods
    assert not key["scope"].get("methods"), f"未声明时不应写入 methods: {key['scope']}"
    code, res = _req("POST", "/api/v1/memory/search", token=key["key"],
                     body={"agent_id": MGR_AGENT, "query": "legacy-probe"})
    assert code == 200, f"无 methods 的 key 应保持原行为 200, 实际 {code}: {res}"


def test_scoped_key_cannot_reach_credential_endpoints(guarded_hub):
    """防提权：受限主体（scoped key）一律不得触碰凭据/身份/暴露开关端点，
    即便其绑定身份是 manager、endpoints 白名单为空（=全部）也拒绝。"""
    db_path = guarded_hub["db_path"]
    admin = _provision(db_path)
    # endpoints 空 = 全部 + methods 空 = 全方法 → 最宽 scope，仍应被硬 deny
    wide = _issue_key(db_path, endpoints="", extra=["--methods", "GET,POST,DELETE"])
    sk = wide["key"]
    assert wide["scope"]["endpoints"] == [] and wide["scope"]["methods"], \
        f"前置条件：应为最宽 scope: {wide['scope']}"

    code, res = _req("GET", "/api/v1/keys", token=sk)
    assert code == 403, f"scoped key 列 key 应 403, 实际 {code}: {res}"
    code, res = _req("POST", "/api/v1/keys", token=sk,
                     body={"agent_id": MGR_AGENT, "scope": {}})
    assert code == 403, f"scoped key 自行签发（提权）应 403, 实际 {code}: {res}"
    code, res = _req("DELETE", "/api/v1/keys/key-000000000000", token=sk)
    assert code == 403, f"scoped key 吊销 key 应 403, 实际 {code}: {res}"
    code, res = _req("POST", "/api/v1/server/config", token=sk,
                     body={"lan_enabled": True})
    assert code == 403, f"scoped key 改暴露开关应 403, 实际 {code}: {res}"
    code, res = _req("GET", "/api/v1/access/accounts", token=sk)
    assert code == 403, f"scoped key 读员工账号应 403, 实际 {code}: {res}"

    # 对照：全权 manager key（非 scoped）不受影响，签发端点照常放行
    code, res = _req("GET", "/api/v1/keys", token=admin["api_key"])
    assert code == 200, f"全权 manager key 应可列 key, 实际 {code}: {res}"


def test_scoped_key_cannot_open_ws(guarded_hub):
    """WS fail-closed：sk- 受限 key 不能建任何 WS 连接（4401）"""
    websocket = pytest.importorskip("websocket")
    db_path = guarded_hub["db_path"]
    _provision(db_path)
    key = _issue_key(db_path, extra=["--methods", "GET,HEAD"])
    ws = websocket.create_connection(f"ws://127.0.0.1:{HUB_PORT}/ws/{MGR_AGENT}",
                                     timeout=5)
    try:
        ws.send(json.dumps({"type": "auth", "token": key["key"]}))
        time.sleep(0.6)
        code = _read_close_code(ws, timeout=1.5)
        assert code == 4401, f"sk- key 建 WS 应 4401, 实际 {code}"
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _read_close_code(ws, timeout=1.5):
    """非阻塞读服务端 close 帧关闭码（websocket-client 1.9 无 close_code 属性）"""
    try:
        ws.sock.settimeout(timeout)
    except Exception:
        return None
    try:
        while True:
            opcode, abnf = ws.recv_data_frame()
            if opcode == 0x8 and getattr(abnf, "data", None):
                raw = abnf.data
                if len(raw) >= 2:
                    return struct.unpack("!H", raw[:2])[0]
                return None
            if opcode is None:
                return None
    except Exception:
        return None


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-q"]))
