"""P0: 会话接力 handoff — Hub 侧 T0-1~T0-4（先红后绿）

- T0-1 未认证 → 401（鉴权开启的独立 Hub）
- T0-2 from != current_agent → 403（不能替别人移交）
- T0-3 to_agent 离线 → 400「目标 Agent 不在线」
- T0-4 to_agent 在线 → 200 + 目标 WS 收到 session.handoff 事件（含 messages/title/summary/key_facts）
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _start_auth_hub(tmpdir, port):
    """鉴权开启的独立 Hub（subprocess，stdout DEVNULL）"""
    import socket
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
    # 轮询 health
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


def _register_agent(port, agent_id, role="worker", hub_token="test-hub-token"):
    body = json.dumps({"agent_id": agent_id, "agent_name": agent_id, "role": role}).encode()
    headers = {"Content-Type": "application/json"}
    if hub_token:
        headers["Authorization"] = f"Bearer {hub_token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/agents/register",
                                 data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _api(port, method, path, body=None, token=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class _WSClient:
    """websocket-client 直连，读首帧（handoff 事件到达验证）"""
    def __init__(self, port, agent_id, token):
        import websocket
        ws = websocket.create_connection(f"ws://127.0.0.1:{port}/ws/{agent_id}", timeout=5)
        ws.send(json.dumps({"type": "auth", "token": token}))
        ws.sock.settimeout(3)
        try:
            opcode, abnf = ws.recv_data_frame()
            if opcode == 0x8:
                raise RuntimeError("WS auth rejected")
        except Exception:
            pass
        ws.sock.settimeout(5)
        self.ws = ws

    def recv_json(self, timeout=3):
        self.ws.sock.settimeout(timeout)
        try:
            data = self.ws.recv()
            if not data:
                return None
            return json.loads(data)
        except Exception:
            return None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


@pytest.fixture
def auth_hub():
    tmpdir = tempfile.mkdtemp(prefix="handoff-auth-")
    proc = _start_auth_hub(tmpdir, 3062)
    time.sleep(0.5)
    yield proc, tmpdir
    proc.kill()
    time.sleep(0.5)
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_t01_unauthorized_401(auth_hub):
    proc, tmpdir = auth_hub
    body = {"from_agent_id": "a", "to_agent_id": "b", "local_session_id": 1,
            "title": "t", "summary": "s", "key_facts": [], "messages": []}
    status, resp = _api(3062, "POST", "/api/v1/sessions/handoff", body=body)
    assert status == 401, f"期望 401, got {status}: {resp}"


def test_t02_wrong_agent_403(auth_hub):
    proc, tmpdir = auth_hub
    r = _register_agent(3062, "alice", role="manager")
    key_a = r["api_key"]
    r = _register_agent(3062, "bob")
    key_b = r["api_key"]
    body = {"from_agent_id": "alice", "to_agent_id": "bob", "local_session_id": 1,
            "title": "t", "summary": "s", "key_facts": [], "messages": []}
    # bob 不能替 alice 移交
    status, resp = _api(3062, "POST", "/api/v1/sessions/handoff", body=body, token=key_b)
    assert status == 403, f"期望 403, got {status}: {resp}"


def test_t03_target_offline_400(auth_hub):
    proc, tmpdir = auth_hub
    r = _register_agent(3062, "alice", role="manager")
    key_a = r["api_key"]
    r = _register_agent(3062, "carol")
    body = {"from_agent_id": "alice", "to_agent_id": "carol", "local_session_id": 1,
            "title": "t", "summary": "s", "key_facts": [], "messages": [{"role": "user", "content": "hi"}]}
    status, resp = _api(3062, "POST", "/api/v1/sessions/handoff", body=body, token=key_a)
    assert status == 400, f"期望 400, got {status}: {resp}"
    assert "不在线" in (resp.get("detail") or resp.get("error") or ""), f"resp: {resp}"


def test_t04_online_delivers_event(auth_hub):
    proc, tmpdir = auth_hub
    r = _register_agent(3062, "alice", role="manager")
    key_a = r["api_key"]
    r = _register_agent(3062, "bob")
    key_b = r["api_key"]
    # bob 上线（WS）
    ws_b = _WSClient(3062, "bob", key_b)
    time.sleep(0.5)
    body = {"from_agent_id": "alice", "to_agent_id": "bob", "local_session_id": 7,
            "title": "客户报价", "summary": "讨论了报价方案",
            "key_facts": ["客户预算 3 万"], "messages": [
                {"role": "user", "content": "客户想报价"},
                {"role": "assistant", "content": "建议 3 万方案"},
            ]}
    status, resp = _api(3062, "POST", "/api/v1/sessions/handoff", body=body, token=key_a)
    assert status == 200, f"期望 200, got {status}: {resp}"
    # bob 的 WS 应收到 session.handoff 事件
    evt = None
    for _ in range(10):
        evt = ws_b.recv_json(timeout=2)
        if evt and evt.get("type") == "session.handoff":
            break
    assert evt, "bob WS 未收到 session.handoff"
    assert evt["from_agent_id"] == "alice"
    assert evt["to_agent_id"] == "bob"
    assert evt["local_session_id"] == 7
    assert evt["title"] == "客户报价"
    assert len(evt["messages"]) == 2
    assert evt["messages"][1]["content"] == "建议 3 万方案"
    ws_b.close()
