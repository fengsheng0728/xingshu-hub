"""B1: scoped key 签发 + 外部消费方独立认证 E2E（独立进程真实 guarded Hub，2026-09-06）

背景：S1K scoped key 机制（agent_keys 表 + routes_keys 签发 + 中间件 endpoints 过滤 +
disclosure level_cap 叠加）2026-08-07 已就位，但认证层从未打通——authenticate() 把
agent_keys 查询挂在 agents.api_key 命中之后，而 sk- 随机串永远不等于任何 agents.api_key
（_lookup_agent 精确匹配必 miss）→ scoped key 实际永远 401。本测试验证 B1 修复后
外部协作者全链：CLI 预签发 → sk- key 独立 HTTP 认证 → endpoints 白名单 403 →
level_cap 内容剥离 → 吊销即失效。

配方来源：tests/test_register_guard.py（guarded Hub subprocess）。
三铁律：① stdout/stderr DEVNULL（管道满卡死）② 轮询 /health（最长 40s）③ 末尾 kill + rmtree。
B1 决策（D1/D2，见 9-06 汇报）：外部消费方 = hub_cli agent create 预建 worker 身份，
全权 api_key 管理员托管不交付，给外部人的只有 scoped key；WS 协作者场景（9-05 场景 A）
留 B2。data_domain 叠加层已由 XS-001（2026-09-08）落地读时派生：记忆域 = 显式
department 键 → owner 部门（agents/employee_accounts）→ 空 = 公共区，判定 fail-closed
（域外一律降 METADATA，见 tests/test_xs001_data_domain.py），CD-025 对应项关闭。
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
HUB_PORT = 3071
HUB_TOKEN = "b1-test-hub-token"
EXT_AGENT = "ext-collab"
MARKER_FULL = "EXT-SECRET-FULLTEXT-7f3a"   # content 全文标记（cap 后不得出现）
MARKER_SUM = "EXT-SUMMARY-9b1c"             # summary 标记（cap 到 summary 后应出现）


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
    """跑 hub_cli.py，返回 (returncode, parsed_stdout)"""
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


@pytest.fixture(scope="module")
def guarded_hub():
    """起 guarded 测试 Hub（registration=guarded + hub_token + 独立 DB + 3071）"""
    tmpdir = tempfile.mkdtemp(prefix="b1-guarded-")
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


def _provision(db_path: str) -> dict:
    """预建外部协作者 worker 身份（幂等：模块级 Hub 跨测试共享），
    返回其全权 api_key（管理员托管，仅用于种子写入）"""
    row = _db_row(db_path, "SELECT api_key FROM agents WHERE agent_id = ?", (EXT_AGENT,))
    if row:
        return {"api_key": row["api_key"], "pre_existing": True}
    cli = _cli(["agent", "create", "--id", EXT_AGENT,
                "--name", "外部协作者", "--role", "worker",
                "--department", "proj-alpha", "--db", db_path])
    assert cli.get("status") == "created", f"agent create 失败: {cli}"
    return {"api_key": cli["api_key"]}


def _issue_key(db_path: str, extra: list = None) -> dict:
    args = ["key", "create", "--agent", EXT_AGENT,
            "--endpoints", "/memory/disclose,/gateway/read",
            "--data-domain", "proj-alpha", "--level-cap", "summary",
            "--db", db_path] + (extra or [])
    cli = _cli(args)
    assert cli.get("status") == "created", f"key create 失败: {cli}"
    return cli


def test_b1_cli_issue_roundtrip_semantics(guarded_hub):
    """签发闭环：key create 明文仅一次 + scope 落库 + list 画像 + 负向语义"""
    db_path = guarded_hub["db_path"]
    _provision(db_path)
    created = _issue_key(db_path)
    key_id, sk = created["key_id"], created["key"]
    assert sk.startswith("sk-"), f"scoped key 应带 sk- 前缀: {sk[:12]}..."
    assert created["scope"]["level_cap"] == "summary"
    assert "/gateway/read" in created["scope"]["endpoints"]
    assert created["agent_name"] == "外部协作者"

    # DB: key_hash=SHA256 不存明文 + created_by=hub-cli + active
    row = _db_row(db_path,
                  "SELECT key_hash, created_by, status FROM agent_keys WHERE key_id = ?",
                  (key_id,))
    assert row is not None and row["key_hash"] != sk, "明文不得落库"
    assert row["key_hash"] == __import__("hashlib").sha256(sk.encode()).hexdigest()
    assert row["created_by"] == "hub-cli" and row["status"] == "active"

    # list：不含明文
    listed = _cli(["key", "list", "--db", db_path])
    assert listed["status"] == "ok"
    hit = [k for k in listed["keys"] if k["key_id"] == key_id]
    assert hit and "key_hash" not in hit[0] and "key" not in hit[0]

    # 负向：agent 不存在 → 404；level_cap 非法 → 400
    ghost = _cli(["key", "create", "--agent", "ghost-nobody", "--db", db_path])
    assert ghost.get("code") == 404 and ghost["_rc"] == 1
    badcap = _cli(["key", "create", "--agent", EXT_AGENT,
                   "--level-cap", "topsecret", "--db", db_path])
    assert badcap.get("code") == 400 and badcap["_rc"] == 1
    # 负向：吊销不存在的 key → 404
    noexist = _cli(["key", "revoke", "--key-id", "key-000000000000", "--db", db_path])
    assert noexist.get("code") == 404 and noexist["_rc"] == 1


def test_b1_scoped_key_http_auth_cap_and_whitelist(guarded_hub):
    """B1 核心：sk- key 独立 HTTP 认证（修复前永远 401）+ level_cap 内容剥离 +
    endpoints 白名单 403 + 调用画像"""
    db_path = guarded_hub["db_path"]
    admin = _provision(db_path)
    created = _issue_key(db_path)
    key_id, sk = created["key_id"], created["key"]

    # 种子：管理员以 ext-collab 身份存一条 full 级记忆（全权 key 仅管理员持有）
    code, seed = _req("POST", f"/api/v1/memory/store?agent_id={EXT_AGENT}",
                      token=admin["api_key"],
                      body={"memory_key": "b1-cap-mem",
                            "content": MARKER_FULL,
                            "summary": MARKER_SUM,
                            "disclosure_level": "full",
                            "tags": ["b1"]})
    assert code == 200, f"种子写入失败 {code}: {seed}"

    # 外部消费方用 sk- key 自查（规则1 自查 FULL → level_cap=summary 压到 summary）
    code, res = _req("POST", "/api/v1/memory/disclose",
                     token=sk,
                     body={"requester_agent_id": EXT_AGENT,
                           "target_agent_id": EXT_AGENT,
                           "query": "EXT-SECRET",
                           "required_level": "full"})
    assert code == 200, f"sk- key 认证失败(修复前 401) {code}: {res}"
    assert res.get("disclosed_count", 0) >= 1, f"自查应披露: {res}"
    item = res["memories"][0]
    assert item["disclosure_level"] == "summary", \
        f"level_cap=summary 应把 FULL 压到 summary, 实际 {item['disclosure_level']}"
    assert MARKER_FULL not in item["content"], "cap 后不得泄露全文标记"
    assert MARKER_SUM in item["content"], f"cap 到 summary 应返回摘要: {item['content'][:80]}"

    # 调用画像：认证触达 touch → call_count >= 1
    row = _db_row(db_path,
                  "SELECT call_count, last_used_at FROM agent_keys WHERE key_id = ?",
                  (key_id,))
    assert row["call_count"] >= 1, f"调用画像未更新: {dict(row)}"

    # endpoints 白名单：/knowledge 不在白名单 → 中间件 403（非 401/200）
    code, res = _req("POST", "/api/v1/knowledge", token=sk,
                     body={"title": "x", "content": "y", "tags": []})
    assert code == 403, f"白名单外端点应 403, 实际 {code}: {res}"


def test_b1_revoke_kills_http_auth(guarded_hub):
    """吊销即失效：revoke 后同一 sk- key 再调 → 401；二次吊销 → 404"""
    db_path = guarded_hub["db_path"]
    admin = _provision(db_path)
    created = _issue_key(db_path)
    key_id, sk = created["key_id"], created["key"]

    # 吊销前可认证（随便一个带读口的调用即可验证）
    code, _ = _req("GET", "/health", token=sk)  # health 免认证, 仅确认连通
    assert code == 200
    code, res = _req("POST", "/api/v1/memory/disclose", token=sk,
                     body={"requester_agent_id": EXT_AGENT,
                           "target_agent_id": EXT_AGENT, "query": "x"})
    assert code == 200, f"吊销前应可调: {code}: {res}"

    # CLI 吊销 → 立即 401（lookup_by_hash 拒 revoked）
    rv = _cli(["key", "revoke", "--key-id", key_id, "--db", db_path])
    assert rv["status"] == "revoked" and rv["_rc"] == 0
    code, res = _req("POST", "/api/v1/memory/disclose", token=sk,
                     body={"requester_agent_id": EXT_AGENT,
                           "target_agent_id": EXT_AGENT, "query": "x"})
    assert code == 401, f"吊销后应 401, 实际 {code}: {res}"

    # 二次吊销 → 404（幂等收紧）
    rv2 = _cli(["key", "revoke", "--key-id", key_id, "--db", db_path])
    assert rv2.get("code") == 404 and rv2["_rc"] == 1
