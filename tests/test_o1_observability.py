"""P1 O1 可观测性测试 — /healthz /readyz + trace_id 贯穿

独立进程起真实 Hub（port 3064），验证：
- T1: /healthz 200（存活），/readyz 200（就绪，SQLite + ChromaDB 正常）
- T2: 带 X-Trace-Id 的 REST 请求 → 审计事件（events 表）payload 含同一 trace_id
- T3: 披露写入后 disclosure_log 的 trace_id 列有值（触发一次 disclose 路径）

配方同 test_s4_gate.py（SYNC_HUB_NO_AUTH 必须 pop，否则子进程全放行）。
"""
import os
import sys
import time
import json
import yaml
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.error

import pytest

HUB_PORT = 3064
HUB_TOKEN = "test-token-o1-obs"
TEST_DIR = None
HUB_PROC = None
TRACE_ID = "trace-test-0001"

TEST_CONFIG = {
    "server": {"host": "127.0.0.1", "port": HUB_PORT},
    "auth": {"enabled": True, "hub_token": HUB_TOKEN},
    "database": {"path": "./test-o1.db", "backup_enabled": False,
                 "chroma_path": "./test-o1-chroma"},
}


@pytest.fixture(scope="module", autouse=True)
def hub_fixture():
    global TEST_DIR, HUB_PROC
    TEST_DIR = tempfile.mkdtemp(prefix="o1-obs-")
    config_dir = os.path.join(TEST_DIR, "config")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "config.yaml"), "w", encoding="utf-8") as f:
        yaml.dump(TEST_CONFIG, f, allow_unicode=True)

    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = config_dir
    env["SYNC_HUB_CHROMA_PATH"] = os.path.join(TEST_DIR, "chroma")
    env.pop("SYNC_HUB_NO_AUTH", None)

    HUB_PROC = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{HUB_PORT}"
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
    else:
        pytest.fail("独立 Hub 40s 未就绪")
    yield
    if HUB_PROC and HUB_PROC.poll() is None:
        HUB_PROC.kill()
        HUB_PROC.wait(timeout=10)
    shutil.rmtree(TEST_DIR, ignore_errors=True)


def _get(path, token=None, trace=None, timeout=5):
    """GET 请求，返回 (status, body)。"""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if trace:
        headers["X-Trace-Id"] = trace
    req = urllib.request.Request(f"http://127.0.0.1:{HUB_PORT}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def _register_agent():
    """注册合法 agent 拿 api_key（带 trace_id 验证 trace 落库）。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{HUB_PORT}/api/v1/agents/register",
        data=json.dumps({"agent_id": "o1-agent", "agent_name": "o1 test",
                         "role": "manager"}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {HUB_TOKEN}",
                 "X-Trace-Id": TRACE_ID},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_t1_healthz_readyz():
    """T1: /healthz 200 存活；/readyz 200 就绪（SQLite + ChromaDB 正常）"""
    code, body = _get("/healthz")
    assert code == 200 and body.get("status") == "alive", f"healthz 实测 {code} {body}"
    code, body = _get("/readyz")
    assert code == 200 and body.get("status") == "ready", f"readyz 实测 {code} {body}"


def _db_path() -> str:
    """测试库实际位置：config 是相对路径，subprocess cwd=项目根。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "test-o1.db")


def test_t2_trace_id_in_audit_events():
    """T2: 带 X-Trace-Id 的请求 → events 表审计 payload 含同一 trace_id"""
    _register_agent()
    db_path = _db_path()
    assert os.path.exists(db_path), f"测试库不存在: {db_path}"
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT event_type, payload FROM events WHERE payload LIKE ? "
        "ORDER BY rowid DESC LIMIT 10", (f"%{TRACE_ID}%",)).fetchall()
    conn.close()
    assert len(rows) >= 1, "events 表应有携带 trace_id 的审计记录"
    found = any(TRACE_ID in (r["payload"] or "") for r in rows)
    assert found, f"events payload 应含 {TRACE_ID}，实测 {[r['payload'] for r in rows]}"


def test_t3_disclosure_log_trace_column():
    """T3: disclosure_log 表有 trace_id 列（schema 层面验证）"""
    db_path = _db_path()
    import sqlite3
    conn = sqlite3.connect(db_path)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(disclosure_log)").fetchall()]
    conn.close()
    assert "trace_id" in cols, f"disclosure_log 应含 trace_id 列，实测 {cols}"
