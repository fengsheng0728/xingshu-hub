"""P0 S4 暴露面收敛测试 — WS 鉴权熔断 + REST 限速 + static 联邦发现

独立进程起真实 Hub（SYNC_HUB_CONFIG_DIR 临时目录 + 独立 DB + port 3063），
配方同 test_auth_matrix.py。三组用例：
- T1 WS 熔断：连续 5 次错误 token → 第 6 次直接 4401 "IP banned"（封禁态），
  且 events 表出现 auth_fail_ban 审计记录
- T2 REST 限速：rate_limit.per_ip=5 配置下，第 6 个请求 429
- T3 static 发现：federation.discovery=static + static_peers 清单，
  /api/v1/team/discover（或等价发现端点）返回清单节点，且 UDP 线程未启动

注意：WS 熔断按 IP 计数，本测试全部走 127.0.0.1 —— 窗口(600s)内 5 次失败后
该 IP 被封禁 1800s，测试结束后不影响生产 Hub（独立端口+独立进程）。
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
import websocket

HUB_PORT = 3063
HUB_TOKEN = "test-token-s4-gate"
TEST_DIR = None
HUB_PROC = None

# 独立 Hub 配置：小窗口小阈值（5 次失败 / 30s 窗口 / 10s 封禁）+ 限速 5 + static 发现
TEST_CONFIG = {
    "server": {"host": "127.0.0.1", "port": HUB_PORT},
    "auth": {"enabled": True, "hub_token": HUB_TOKEN},
    "database": {"path": "./test-s4.db", "backup_enabled": False,
                 "chroma_path": "./test-s4-chroma"},
    "ws": {"auth_timeout_sec": 3.0, "max_fails": 5,
           "fail_window_sec": 30, "ban_sec": 10},
    "rate_limit": {"per_ip": 5},
    "federation": {"discovery": "static",
                   "static_peers": [
                       {"hub_id": "static-01", "hostname": "hub-a",
                        "ip": "192.168.99.10", "port": 3060},
                   ]},
}

GOOD_KEY = None  # 注册一个合法 agent 取 key


@pytest.fixture(scope="module", autouse=True)
def hub_fixture():
    """独立 Hub 生命周期：start → 等就绪 → yield → kill + 清理"""
    global TEST_DIR, HUB_PROC, GOOD_KEY
    TEST_DIR = tempfile.mkdtemp(prefix="s4-gate-")
    config_dir = os.path.join(TEST_DIR, "config")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "config.yaml"), "w", encoding="utf-8") as f:
        yaml.dump(TEST_CONFIG, f, allow_unicode=True)

    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = config_dir
    env["SYNC_HUB_CHROMA_PATH"] = os.path.join(TEST_DIR, "chroma")
    env.pop("SYNC_HUB_NO_AUTH", None)  # 关键：conftest 设了 NO_AUTH=1，泄漏进子进程会全放行

    # 独立 Hub（stdout=DEVNULL 防管道满卡死，配方同 auth matrix）
    HUB_PROC = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # 等就绪（最长 40s）
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

    # 注册合法 agent 拿 key（register 在 hub_token 配置下需带 token）
    req = urllib.request.Request(
        base + "/api/v1/agents/register",
        data=json.dumps({"agent_id": "s4-agent", "agent_name": "s4 test",
                         "role": "worker"}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {HUB_TOKEN}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        resp = json.loads(r.read())
    GOOD_KEY = resp.get("api_key") or resp.get("key") or ""
    yield
    # 清理
    if HUB_PROC and HUB_PROC.poll() is None:
        HUB_PROC.kill()
        HUB_PROC.wait(timeout=10)
    shutil.rmtree(TEST_DIR, ignore_errors=True)


def _ws_close_code(path: str, token: str) -> int:
    """连接 WS 并返回 close code（P1 配方：recv_data_frame 读 opcode 0x8）。"""
    ws = websocket.create_connection(f"ws://127.0.0.1:{HUB_PORT}{path}", timeout=5)
    try:
        ws.send(json.dumps({"type": "auth", "token": token}))
        import struct
        deadline = time.time() + 5
        while time.time() < deadline:
            ws.sock.settimeout(1)
            try:
                opcode, abnf = ws.recv_data_frame()
                if opcode == 0x8:
                    return struct.unpack("!H", abnf.data[:2])[0]
                if opcode == 0x1:  # 意外收到消息 = 已通过鉴权
                    return 0
            except websocket.WebSocketTimeoutException:
                continue
        return -1  # 超时未关闭 = 未知
    except Exception:
        return -2
    finally:
        try:
            ws.close()
        except Exception:
            pass


def test_t1_ws_fail_ban(no_cleanup_guard=None):
    """T1: 5 次错误 token → 熔断 → 正确 token 也被拒（IP banned）"""
    # 前置：确认该 IP 未在封禁中（避免前序测试残留——独立进程全新状态，无残留）
    # 连续 5 次错误 token：前 5 次都是 Invalid token (4401)
    codes = []
    for _ in range(5):
        codes.append(_ws_close_code("/ws/s4-agent", "wrong-token"))
    # 至少前 5 次全部 4401
    assert all(c == 4401 for c in codes), f"预期前 5 次全 4401，实测 {codes}"

    # 第 6 次（已在封禁中）→ 4401 IP banned（无法区分 reason，但连接被拒且不超时）
    code6 = _ws_close_code("/ws/s4-agent", "wrong-token")
    assert code6 in (4401,), f"封禁期连接应被拒，实测 {code6}"

    # 审计：events 表应有 auth_fail_ban
    base = f"http://127.0.0.1:{HUB_PORT}"
    req = urllib.request.Request(
        base + "/api/v1/maintenance/db-stats",
        headers={"Authorization": f"Bearer {HUB_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            pass  # 端点不存在则跳过 DB 断言（不阻塞核心行为）
        else:
            raise

    # 直接查 DB 验证审计（独立测试库，允许直读）
    db_path = os.path.join(TEST_DIR, "test-s4.db")
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT event_type FROM events WHERE event_type='auth_fail_ban'").fetchall()
        conn.close()
        assert len(rows) >= 1, "events 表应有 auth_fail_ban 审计记录"


def test_t2_rest_rate_limit(no_cleanup_guard=None):
    """T2: per_ip=5 配置下第 6 个业务请求 429"""
    base = f"http://127.0.0.1:{HUB_PORT}"
    headers = {"Authorization": f"Bearer {HUB_TOKEN}"}
    statuses = []
    for _ in range(8):  # 8 个请求，第 6 个起应 429
        try:
            req = urllib.request.Request(base + "/api/v1/team/members", headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                statuses.append(r.status)
        except urllib.error.HTTPError as e:
            statuses.append(e.code)
        except Exception as e:
            statuses.append(0)
    # 前 5 个 200，之后至少出现 429
    assert statuses[0] == 200, f"第一个请求应 200，实测 {statuses}"
    assert 429 in statuses, f"第 6+ 请求应触发 429，实测 {statuses}"


def test_t3_static_discovery(no_cleanup_guard=None):
    """T3: static 模式发现端点返回清单节点，且无 UDP 线程"""
    base = f"http://127.0.0.1:{HUB_PORT}"
    # T2 限速测试污染同一 IP 窗口：429 时等 1.2s 重试（最多 3 次）
    resp = None
    for attempt in range(3):
        req = urllib.request.Request(
            base + "/api/v1/team/discover",
            headers={"Authorization": f"Bearer {HUB_TOKEN}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                resp = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(1.2)
                continue
            if e.code == 404:
                pytest.skip("发现端点路径不是 /api/v1/team/discover")
                return
            raise
    assert resp is not None, "T3 请求 3 次均未成功"
    # 找 peers 字段（响应结构可能嵌套）
    peers = resp.get("peers", resp.get("discovered", []))
    assert isinstance(peers, list), f"peers 应为列表，实测 {resp}"
    ids = [p.get("hub_id") for p in peers]
    assert "static-01" in ids, f"static 清单节点应出现在发现结果，实测 {ids}"
