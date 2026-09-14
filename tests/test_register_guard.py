"""OGA: auth.registration open|guarded 注册准入受管模式（独立进程真实 Hub）

配方来源：tests/test_auth_matrix.py（8-01 鉴权矩阵）的独立 Hub subprocess 结构。
三铁律（继承自该配方）：
  1. subprocess stdout/stderr 用 DEVNULL（管道满会卡死；T1 进程秒退、必须读 stderr 的除外）
  2. 轮询 /health 就绪（最长 40s）
  3. 末尾 kill + rmtree 清理

覆盖：
  T1 启动校验：guarded + 空 hub_token → main.py 退出码非 0, stderr 含 [FATAL]
  T2 无 token → 401（中间件强制 hub_token）
  T3 错 token → 401
  T4 未预签 agent_id → 403 且不建号
  T5 hub_cli agent create 预签发后 bootstrap → 200, api_key 与 CLI 输出一致
  T6 角色不自封：bootstrap 声明 manager, 服务端保持 CLI 预置 worker
  T7 幂等重引导：api_key 与首次一致
  T8-T10 open 模式（T0-2）：已存在 agent_id 无凭据重注册/重引导 → 403；
      带旧 key 重注册 → 200 幂等；新号无凭据注册 → 200
  T11 guarded 模式：无凭据 bootstrap 已存在 agent_id → 401（中间件），不吐 key
禁止 monkeypatch Hub 内部——guarded 语义走真实 HTTP + 真实 main.py 启动。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB_PORT = 3068
OPEN_HUB_PORT = 3067
HUB_TOKEN = "oga-test-hub-token"


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


def _req(method: str, path: str, token: str = "", body: dict = None,
         port: int = HUB_PORT):
    """返回 (http_status, body_dict)；网络异常返回 (0, {})"""
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def _db_role_and_key(db_path: str, agent_id: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT role, api_key FROM agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
        return (row["role"], row["api_key"]) if row else None
    finally:
        conn.close()


def test_t1_guarded_without_hub_token_refuses_start():
    """T1: registration=guarded 但 hub_token 为空 → 拒绝启动, 退出码非 0, stderr 含 [FATAL]"""
    tmpdir = tempfile.mkdtemp(prefix="oga-t1-")
    try:
        cfg_dir = os.path.join(tmpdir, "config")
        _write_config(cfg_dir, {
            "server": {"host": "127.0.0.1", "port": 3069},
            "auth": {"enabled": True, "registration": "guarded", "hub_token": ""},
            "database": {"path": os.path.join(tmpdir, "t1.db"), "backup_enabled": False},
            "logging": {"level": "warning"},
        })
        # 进程秒退（uvicorn.run 之前 sys.exit）, PIPE 读 stderr 不会卡死
        r = subprocess.run(
            [sys.executable, "main.py"],
            cwd=REPO_ROOT, env=_subprocess_env(cfg_dir, tmpdir),
            capture_output=True, text=True, timeout=120,
        )
        assert r.returncode != 0, f"guarded+空 hub_token 应拒绝启动, 实际 exit={r.returncode}"
        assert "[FATAL]" in r.stderr, f"stderr 缺 [FATAL]: {r.stderr[-500:]}"
        assert "auth.registration=guarded" in r.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="module")
def guarded_hub():
    """起 guarded 测试 Hub（registration=guarded + hub_token + 独立 DB + 3068）"""
    tmpdir = tempfile.mkdtemp(prefix="oga-guarded-")
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
    # 等健康（最长 40s）
    ok = False
    for _ in range(80):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{HUB_PORT}/health", timeout=2) as r:
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


def test_t2_register_no_token_401(guarded_hub):
    """T2: guarded Hub 上无 Authorization 调 register → 401（中间件强制 hub_token）"""
    code, _ = _req("POST", "/api/v1/agents/register", token="",
                   body={"agent_id": "oga-no-token", "agent_name": "x"})
    assert code == 401, f"expect 401, got {code}"


def test_t3_register_wrong_token_401(guarded_hub):
    """T3: Bearer wrong-token → 401"""
    code, _ = _req("POST", "/api/v1/agents/register", token="wrong-token",
                   body={"agent_id": "oga-bad-token", "agent_name": "x"})
    assert code == 401, f"expect 401, got {code}"


def test_t4_unprovisioned_register_bootstrap_403(guarded_hub):
    """T4: 带 hub_token 但 agent_id 未预签发 → register/bootstrap 均 403, 且库中不建号"""
    body = {"agent_id": "oga-ghost", "agent_name": "幽灵", "role": "manager"}
    code, reg = _req("POST", "/api/v1/agents/register", token=HUB_TOKEN, body=body)
    assert code == 403, f"register expect 403, got {code}: {reg}"
    code, boot = _req("POST", "/api/v1/agents/bootstrap", token=HUB_TOKEN, body=body)
    assert code == 403, f"bootstrap expect 403, got {code}: {boot}"
    assert _db_role_and_key(guarded_hub["db_path"], "oga-ghost") is None, \
        "未预签发 agent 不得建号"


def test_t5_t6_t7_provisioned_bootstrap(guarded_hub):
    """T5 预签发后引导成功 / T6 角色不自封 / T7 幂等重引导"""
    db_path = guarded_hub["db_path"]
    # T5a: 管理员 CLI 预签发（必须显式 --db, 否则走 CONFIG.DB_PATH）
    r = subprocess.run(
        [sys.executable, "hub_cli.py", "agent", "create",
         "--id", "oga-provisioned", "--name", "预建", "--role", "worker",
         "--db", db_path],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"hub_cli agent create 失败: {r.stderr[-300:]}"
    cli = json.loads(r.stdout)
    assert cli["status"] == "created" and cli["api_key"], f"CLI 输出异常: {cli}"

    # T5b: 带 hub_token bootstrap → 200, api_key == CLI 预签发 key
    code, boot = _req("POST", "/api/v1/agents/bootstrap", token=HUB_TOKEN,
                      body={"agent_id": "oga-provisioned", "agent_name": "预建"})
    assert code == 200, f"bootstrap expect 200, got {code}: {boot}"
    assert boot["status"] == "bootstrapped"
    assert boot["api_key"] == cli["api_key"], "bootstrap 必须保留 CLI 预签发的 api_key"
    assert isinstance(boot.get("workspace"), dict), "workspace 缺失"

    # T6: bootstrap 声明 role=manager → 200 但服务端角色保持 CLI 预置 worker（不自封）
    code, boot2 = _req("POST", "/api/v1/agents/bootstrap", token=HUB_TOKEN,
                       body={"agent_id": "oga-provisioned", "agent_name": "预建",
                             "role": "manager"})
    assert code == 200, f"re-bootstrap expect 200, got {code}: {boot2}"
    role, key = _db_role_and_key(db_path, "oga-provisioned")
    assert role == "worker", f"guarded 下角色以服务端预置值为准, 实际 role={role}"

    # T7: 幂等重引导 → 200 且 api_key 与首次一致
    assert boot2["api_key"] == cli["api_key"], "重引导 api_key 必须幂等不变"
    assert key == cli["api_key"]


# ============ T0-2: open 模式重注册身份窃取防护（路由层凭据校验） ============
# 缺口核证（2026-09-09）：open 模式（hub_token 空）下中间件对 register/bootstrap
# 直接放行，hub_core.register 对已存在 agent_id 无凭据回吐旧 api_key。
# 修复在 routes_agents.py 路由层：已存在 agent_id 必须出示该 agent api_key 或 hub_token。

@pytest.fixture(scope="module")
def open_hub():
    """起 open 测试 Hub（registration=open + hub_token 空 + 独立 DB + 3067）"""
    tmpdir = tempfile.mkdtemp(prefix="oga-open-")
    cfg_dir = os.path.join(tmpdir, "config")
    db_path = os.path.join(tmpdir, "open.db")
    _write_config(cfg_dir, {
        "server": {"host": "127.0.0.1", "port": OPEN_HUB_PORT},
        "auth": {"enabled": True, "registration": "open", "hub_token": ""},
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
                    f"http://127.0.0.1:{OPEN_HUB_PORT}/health", timeout=2) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            continue
    if not ok:
        proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("open Hub failed to start within 40s")
    yield {"db_path": db_path, "tmpdir": tmpdir}
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_t8_open_reregister_no_credential_403(open_hub):
    """T8: open 模式——注册 agentA 拿 key 后，无凭据重注册/重引导同 agent_id → 403 且不吐 key"""
    body = {"agent_id": "t8-open-victim", "agent_name": "受害者A"}
    code, reg = _req("POST", "/api/v1/agents/register", body=body, port=OPEN_HUB_PORT)
    assert code == 200, f"首次注册 expect 200, got {code}: {reg}"
    assert reg.get("api_key"), "首次注册必须下发 api_key"

    code, reg2 = _req("POST", "/api/v1/agents/register", body=body, port=OPEN_HUB_PORT)
    assert code == 403, f"无凭据重注册 expect 403, got {code}: {reg2}"
    assert "api_key" not in reg2, "403 响应不得回吐 api_key"

    code, boot = _req("POST", "/api/v1/agents/bootstrap", body=body, port=OPEN_HUB_PORT)
    assert code == 403, f"无凭据重引导 expect 403, got {code}: {boot}"
    assert "api_key" not in boot, "403 响应不得回吐 api_key"


def test_t9_open_reregister_with_own_key_200_idempotent(open_hub):
    """T9: open 模式——带 agentA 旧 key 重注册 → 200 且 api_key 与首次一致（幂等）"""
    body = {"agent_id": "t9-open-agent", "agent_name": "持有者B"}
    code, reg = _req("POST", "/api/v1/agents/register", body=body, port=OPEN_HUB_PORT)
    assert code == 200, f"首次注册 expect 200, got {code}: {reg}"
    key1 = reg["api_key"]

    code, reg2 = _req("POST", "/api/v1/agents/register", token=key1,
                      body=body, port=OPEN_HUB_PORT)
    assert code == 200, f"带旧 key 重注册 expect 200, got {code}: {reg2}"
    assert reg2["api_key"] == key1, "重注册 api_key 必须幂等不变"

    # 错 key（别人的/伪造的）→ 403
    code, _ = _req("POST", "/api/v1/agents/register", token="forged-key",
                   body=body, port=OPEN_HUB_PORT)
    assert code == 403, f"伪造 key 重注册 expect 403, got {code}"


def test_t10_open_new_agent_no_credential_200(open_hub):
    """T10: open 模式——不存在的 agent_id 无凭据注册 → 200（新号不阻塞）"""
    code, reg = _req("POST", "/api/v1/agents/register",
                     body={"agent_id": "t10-open-new", "agent_name": "新号C"},
                     port=OPEN_HUB_PORT)
    assert code == 200, f"新号注册 expect 200, got {code}: {reg}"
    assert reg.get("api_key"), "新号注册必须下发 api_key"


def test_t11_guarded_bootstrap_existing_no_credential_denied(guarded_hub):
    """T11: guarded 模式——无凭据 bootstrap 已存在 agent_id → 拒绝（中间件 401，先于路由层 403）。

    与 T2（register 无 token → 401）等价语义的 bootstrap 侧覆盖：
    guarded 下中间件对 register/bootstrap 强制 hub_token，无凭据请求到不了路由层，
    统一 401；关键是不得 200、不得回吐 api_key。
    """
    db_path = guarded_hub["db_path"]
    r = subprocess.run(
        [sys.executable, "hub_cli.py", "agent", "create",
         "--id", "t11-provisioned", "--name", "预建D", "--role", "worker",
         "--db", db_path],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"hub_cli agent create 失败: {r.stderr[-300:]}"
    cli_key = json.loads(r.stdout)["api_key"]

    code, boot = _req("POST", "/api/v1/agents/bootstrap", token="",
                      body={"agent_id": "t11-provisioned", "agent_name": "预建D"})
    assert code == 401, f"guarded 无凭据 bootstrap expect 401, got {code}: {boot}"
    assert "api_key" not in boot, "拒绝响应不得回吐 api_key"
    # 已存在 agent 的 key 未被改写
    assert _db_role_and_key(db_path, "t11-provisioned")[1] == cli_key


# ============ T1-2: api_key 哈希化（alembic 0003 后的库形态，open 模式 e2e） ============
# 库先经 `alembic upgrade head` 建出含 api_key_hash 的 schema 再启动 Hub——
# 与生产「先升代码再跑迁移」相反方向（直接落到终态 schema），覆盖 hash 全链路：
# 首注下发明文一次、库内只存 hash、明文 key 可认证、重引导不回吐明文。

HASH_HUB_PORT = 3066


@pytest.fixture(scope="module")
def hashed_hub():
    """起 hash 模式测试 Hub（库先 alembic upgrade head → 含 api_key_hash 列）"""
    tmpdir = tempfile.mkdtemp(prefix="t12-hashed-")
    cfg_dir = os.path.join(tmpdir, "config")
    db_path = os.path.join(tmpdir, "hashed.db")
    # 先在空库上跑到 head（含 0003 hash 列），再让 Hub 在该库上启动
    env = dict(os.environ, SYNC_HUB_DB=db_path)
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"alembic upgrade head 失败: {r.stderr[-300:]}"
    _write_config(cfg_dir, {
        "server": {"host": "127.0.0.1", "port": HASH_HUB_PORT},
        "auth": {"enabled": True, "registration": "open", "hub_token": ""},
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
                    f"http://127.0.0.1:{HASH_HUB_PORT}/health", timeout=2) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            continue
    if not ok:
        proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("hashed Hub failed to start within 40s")
    yield {"db_path": db_path, "tmpdir": tmpdir}
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_t12_hash_mode_register_store_hash_only(hashed_hub):
    """T12a: hash 模式首注——响应回显明文一次；库内 api_key 清空、api_key_hash=sha256(明文)"""
    import hashlib
    body = {"agent_id": "t12-hash-agent", "agent_name": "哈希A"}
    code, reg = _req("POST", "/api/v1/agents/register", body=body, port=HASH_HUB_PORT)
    assert code == 200, f"首注 expect 200, got {code}: {reg}"
    key = reg.get("api_key", "")
    assert key, "首注必须下发 api_key（明文仅此一次）"
    conn = sqlite3.connect(hashed_hub["db_path"])
    row = conn.execute(
        "SELECT api_key, api_key_hash FROM agents WHERE agent_id='t12-hash-agent'"
    ).fetchone()
    conn.close()
    assert row[0] == "", f"库内 api_key 明文列必须为空, got {row[0]!r}"
    assert row[1] == hashlib.sha256(key.encode()).hexdigest(), "api_key_hash 必须是明文的 sha256"

    # 明文 key 可认证（200），错 key 401
    code, _ = _req("POST", "/api/v1/agents/t12-hash-agent/heartbeat",
                   token=key, body={}, port=HASH_HUB_PORT)
    assert code == 200, f"正确 key 认证 expect 200, got {code}"
    code, _ = _req("POST", "/api/v1/agents/t12-hash-agent/heartbeat",
                   token="wrong-key", body={}, port=HASH_HUB_PORT)
    assert code == 401, f"错 key expect 401, got {code}"


def test_t12_hash_mode_reregister_no_plaintext_echo(hashed_hub):
    """T12b: 重注册/重引导——出示旧 key 放行（200）但响应不再含明文 api_key"""
    import hashlib
    # 新 agent 走完整闭环（t12-hash-agent 的首注明文 T12a 已断言）
    body2 = {"agent_id": "t12-hash-agent-b", "agent_name": "哈希B"}
    code, reg = _req("POST", "/api/v1/agents/register", body=body2, port=HASH_HUB_PORT)
    assert code == 200 and reg.get("api_key"), f"首注 expect 200+key, got {code}: {reg}"
    key_b = reg["api_key"]

    # 带旧 key 重注册 → 200，响应无明文（空串）
    code, reg2 = _req("POST", "/api/v1/agents/register", token=key_b,
                      body=body2, port=HASH_HUB_PORT)
    assert code == 200, f"带旧 key 重注册 expect 200, got {code}: {reg2}"
    assert not reg2.get("api_key"), "重注册响应不得回吐明文 api_key"
    # 无凭据重注册 → 403（T0-2 在 hash 列上仍生效）
    code, _ = _req("POST", "/api/v1/agents/register", body=body2, port=HASH_HUB_PORT)
    assert code == 403, f"无凭据重注册 expect 403, got {code}"
    # 带旧 key 重引导 → 200，响应无明文；且 hash 未被改写（幂等）
    code, boot = _req("POST", "/api/v1/agents/bootstrap", token=key_b,
                      body=body2, port=HASH_HUB_PORT)
    assert code == 200, f"带旧 key 重引导 expect 200, got {code}: {boot}"
    assert not boot.get("api_key"), "重引导响应不得回吐明文 api_key"
    conn = sqlite3.connect(hashed_hub["db_path"])
    row = conn.execute(
        "SELECT api_key, api_key_hash FROM agents WHERE agent_id='t12-hash-agent-b'"
    ).fetchone()
    conn.close()
    assert row[0] == "" and row[1] == hashlib.sha256(key_b.encode()).hexdigest(), \
        "重引导后 hash 必须幂等不变、明文列保持空"
