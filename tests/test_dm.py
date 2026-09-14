"""P3: Agent 私聊 — 消息通道 + WS direct_message 推送(先红后绿)

- T3-1 反向断言: ("POST","/api/v1/messages/send") + ("GET","/api/v1/messages") 必须注册
- T3-2 在线直达: B 连 WS → A send → B 的 WS 收到 direct_message 事件 + 入库
- T3-3 身份校验: 不能替别人发送(from==调用者)
- T3-4 离线落通知: to 未连 WS → 入库 + 通知落库(上线可见)
- T3-5 列表: 双向消息按时间返回 + 未读标记
- T3-6 鉴权: 独立鉴权 Hub 无 token → 401
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import app

EXPECTED_DM_ENDPOINTS = [
    ("POST", "/api/v1/messages/send"),
    ("GET", "/api/v1/messages"),
]

TEST_AGENTS = ("dm-a", "dm-b", "dm-c")


def _registered_routes():
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods:
            if m in ("GET", "POST", "PUT", "DELETE", "WEBSOCKET"):
                out.add((m, r.path))
    return out


def test_t3_1_dm_endpoints_registered():
    registered = _registered_routes()
    missing = [f"{m} {p}" for m, p in EXPECTED_DM_ENDPOINTS if (m, p) not in registered]
    assert not missing, f"以下端点未注册: {missing}"


def _cleanup():
    from hub_core import hub as _hub
    for aid in TEST_AGENTS:
        _hub.agents.pop(aid, None)
    conn = _hub._db()
    conn.execute("DELETE FROM agents WHERE agent_id IN ('dm-a','dm-b','dm-c')")
    conn.execute("DELETE FROM messages WHERE from_agent_id IN ('dm-a','dm-b','dm-c') OR to_agent_id IN ('dm-a','dm-b','dm-c')")
    conn.execute("DELETE FROM notifications WHERE agent_id IN ('dm-a','dm-b','dm-c')")
    conn.commit()
    conn.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        _cleanup()
        yield c
    _cleanup()


def _reg(client, aid, role="worker"):
    r = client.post("/api/v1/agents/register",
                    json={"agent_id": aid, "agent_name": aid, "role": role})
    assert r.status_code == 200
    return r.json()


def test_t3_2_online_delivers_via_ws(client):
    """B 在线(WS) → A send → B 的 WS 收到 direct_message + 消息入库"""
    _reg(client, "dm-a", role="manager")
    _reg(client, "dm-b")
    with client.websocket_connect("/ws/dm-b") as ws:
        time.sleep(0.3)
        r = client.post("/api/v1/messages/send?agent_id=dm-a",
                        json={"to_agent_id": "dm-b", "content": "你好 B,把报价方案发我"})
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "sent"
        evt = ws.receive_json()
        assert evt.get("type") == "direct_message", evt
        assert evt.get("from_agent_id") == "dm-a"
        assert "报价方案" in evt.get("content", "")
    # 入库
    r = client.get("/api/v1/messages?agent_id=dm-b")
    msgs = r.json().get("messages", [])
    assert len(msgs) == 1 and msgs[0]["from_agent_id"] == "dm-a"


def test_t3_3_body_forged_from_ignored(client):
    """body 伪造 from_agent_id 不生效 — 身份以调用者为准(防伪造)"""
    _reg(client, "dm-a")
    _reg(client, "dm-b")
    r = client.post("/api/v1/messages/send?agent_id=dm-a",
                    json={"to_agent_id": "dm-b", "content": "伪造尝试", "from_agent_id": "dm-c"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("from_agent_id") == "dm-a", f"from 必须是调用者, got {d}"
    # 入库的 from 也是 dm-a
    r = client.get("/api/v1/messages?agent_id=dm-b")
    msgs = r.json().get("messages", [])
    assert msgs and msgs[0]["from_agent_id"] == "dm-a"


def test_t3_4_offline_target_notified(client):
    """to 未连 WS → 入库 + 通知落库(上线可见, 不丢消息)"""
    _reg(client, "dm-a", role="manager")
    _reg(client, "dm-c")  # 不连 WS
    r = client.post("/api/v1/messages/send?agent_id=dm-a",
                    json={"to_agent_id": "dm-c", "content": "离线消息"})
    assert r.status_code == 200
    assert r.json().get("status") == "queued", r.json()
    # 消息入库
    r = client.get("/api/v1/messages?agent_id=dm-c")
    assert len(r.json().get("messages", [])) == 1
    # 通知落库(上线可见)
    r = client.get("/api/v1/notifications?agent_id=dm-c")
    assert r.status_code == 200
    notifs = r.json().get("notifications", [])
    assert any("私聊" in (n.get("title") or "") or "dm-a" in (n.get("body") or "") for n in notifs), notifs


def test_t3_5_thread_listing(client):
    """双向消息: A→B 两条 + B→A 一条 → 按时间返回, 会话分组"""
    _reg(client, "dm-a", role="manager")
    _reg(client, "dm-b")
    for content in ("第一句", "第二句"):
        r = client.post("/api/v1/messages/send?agent_id=dm-a",
                        json={"to_agent_id": "dm-b", "content": content})
        assert r.status_code == 200
    r = client.post("/api/v1/messages/send?agent_id=dm-b",
                    json={"to_agent_id": "dm-a", "content": "B 的回复"})
    assert r.status_code == 200
    # A 视角: 2 条发出 + 1 条收到
    r = client.get("/api/v1/messages?agent_id=dm-a")
    msgs = r.json().get("messages", [])
    assert len(msgs) == 3
    # 时间升序
    times = [m["created_at"] for m in msgs]
    assert times == sorted(times)
    # B 视角: 2 收到 + 1 发出
    r = client.get("/api/v1/messages?agent_id=dm-b")
    assert len(r.json().get("messages", [])) == 3


# ── T3-6 鉴权: 独立 Hub 401 ──

def _start_auth_hub(tmpdir, port):
    cfg = os.path.join(tmpdir, "config.yaml")
    db = os.path.join(tmpdir, "hub.db")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(f"""server:
  host: 127.0.0.1
  port: {port}
database:
  path: {db}
backup_enabled: False
auth:
  hub_token: "test-hub-token"
""")
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = tmpdir
    env.pop("SYNC_HUB_NO_AUTH", None)
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("auth hub did not start")


def test_t3_6_send_requires_auth():
    tmpdir = tempfile.mkdtemp(prefix="dm-auth-")
    proc = _start_auth_hub(tmpdir, 3068)
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:3068/api/v1/messages/send",
            data=b'{"to_agent_id":"x","content":"y"}', method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "期望 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"期望 401, got {e.code}"
    finally:
        proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
