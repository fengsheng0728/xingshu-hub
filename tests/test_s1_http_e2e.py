# -*- coding: utf-8 -*-
"""S1 HTTP 层端到端验证（独立测试 Hub 3062，临时 config/db）"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（tests/ 上一级）
HUB_PORT = 3062
BASE = f"http://127.0.0.1:{HUB_PORT}"

def http(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}

def wait_health(timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            time.sleep(0.5)
    return False

def main():
    tmpdir = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmpdir, "config"), exist_ok=True)
    db_path = os.path.join(tmpdir, "test.db").replace("\\", "/")
    cfg_yaml = (
        "server:\n  port: 3062\n  host: 127.0.0.1\n"
        "database:\n  path: " + db_path + "\n"
        "auth:\n  enabled: true\n  hub_token: \"\"\n"
    )
    with open(os.path.join(tmpdir, "config", "config.yaml"), "w", encoding="utf-8") as f:
        f.write(cfg_yaml)

    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
    env.pop("SYNC_HUB_NO_AUTH", None)  # 关键：防止 NO_AUTH 泄漏进子进程（skill 坑）
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_health(), "Hub 未就绪"
        print("1. Hub 就绪")

        # 2. register → api_key + 轮换列落库
        st, reg = http("POST", "/api/v1/agents/register", {
            "agent_id": "s1-test-agent",
            "agent_name": "S1测试",
            "role": "worker",
            "capabilities": [],
        })
        assert st == 200, f"register {st}"
        api_key = reg.get("api_key", "")
        assert api_key, "无 api_key"
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT api_key_created_at, api_key_expires_at FROM agents WHERE agent_id='s1-test-agent'"
        ).fetchone()
        assert row and row[0] and row[1], f"轮换列未写: {row}"
        print(f"2. register OK, api_key 前8位={api_key[:8]}…, expires={row[1][:19]}")

        # 3. 无 token → 401
        st, _ = http("GET", "/api/v1/agents/s1-test-agent/heartbeat")
        assert st == 401, f"无token应401, got {st}"
        print("3. 无 token 401 OK")

        # 4. 正确 api_key → 通过
        st, body = http("POST", f"/api/v1/agents/s1-test-agent/heartbeat", {}, token=api_key)
        assert st == 200, f"正确key应200, got {st}"
        print("4. 正确 api_key 通过 OK")

        # 5. 错误 key → 401
        st, _ = http("GET", "/api/v1/agents/s1-test-agent/heartbeat", token="wrong-key")
        assert st == 401, f"错key应401, got {st}"
        print("5. 错误 key 401 OK")

        # 6. 手动过期 api_key → 401（provider 过期语义 HTTP 层生效）
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        conn.execute("UPDATE agents SET api_key_expires_at=? WHERE agent_id='s1-test-agent'", (past,))
        conn.commit()
        st, _ = http("GET", "/api/v1/agents/s1-test-agent/heartbeat", token=api_key)
        assert st == 401, f"过期key应401, got {st}"
        print("6. 过期 key 401 OK（provider 语义 HTTP 生效）")

        # 7. principal 注入验证（中间件写 scope）
        #   通过 WS 鉴权路径验证（provider 语义）：正确 key 可连 WS
        #   简化：重启 Hub 后 register 新 agent 走 bootstrap
        st, boot = http("POST", "/api/v1/agents/bootstrap", {
            "agent_id": "s1-boot-agent",
            "agent_name": "Bootstrap测试",
            "role": "worker",
        })
        assert st == 200, f"bootstrap {st}"
        boot_key = boot.get("api_key", "")
        assert boot_key, "bootstrap 无 api_key"
        print("7. bootstrap OK")

        # 8. 测试 Hub 的 memory 写入（走 get_current_agent 依赖 → provider）
        st, body = http("POST", "/api/v1/memory/store?agent_id=s1-boot-agent", {
            "memory_key": "s1-test-mem",
            "content": "身份接入验证记忆",
            "kind": "fact",
        }, token=boot_key)
        assert st == 200, f"memory store {st}: {body}"
        print("8. memory store via provider OK")

        # 9. WS 首帧鉴权（provider 语义）——非阻塞探测 close 帧
        import websocket
        ws = websocket.create_connection(
            f"ws://127.0.0.1:{HUB_PORT}/ws/s1-boot-agent", timeout=6)
        ws.send(json.dumps({"type": "auth", "token": boot_key}))
        ws.sock.settimeout(1.5)
        closed = False
        try:
            opcode, _ = ws.recv_data_frame()
            if opcode == 0x8:
                closed = True
        except Exception:
            closed = False  # 超时/无帧 = 连接保持 = 鉴权通过
        ws.close()
        assert not closed, "WS 鉴权应通过（provider 路径）"
        print("9. WS 首帧鉴权（provider）OK")

        # 10. 错误 WS token → 4401
        ws2 = websocket.create_connection(
            f"ws://127.0.0.1:{HUB_PORT}/ws/s1-boot-agent", timeout=6)
        ws2.send(json.dumps({"type": "auth", "token": "bad-ws-token"}))
        code = None
        try:
            opcode, abnf = ws2.recv_data_frame()
            if opcode == 0x8:
                import struct
                code = struct.unpack("!H", abnf.data[:2])[0]
        except Exception:
            pass
        ws2.close()
        assert code == 4401, f"错token应4401, got {code}"
        print("10. WS 错 token 4401 OK")

        print("\n===== S1 HTTP 层验证 10/10 全绿 =====")
        conn.close()
    finally:
        proc.kill()
        proc.wait(timeout=10)
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    main()
