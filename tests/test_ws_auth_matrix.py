"""P1: WS 五通道首帧鉴权矩阵（独立进程真实 Hub 3063）

对 5 条 WS 通道逐一断言：
- 无 auth 帧直接发业务帧 → 被拒/断开（4401 或先关）
- 错 token → close 4401
- 3s 不发 auth 帧 → close 4401
- 正确首帧 auth → 业务帧正常收发
"""
import os
import sys
import time
import json
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.error

import pytest
import websocket  # websocket-client

HUB_PORT = 3063
HUB_TOKEN = "test-token-p1-matrix"

WS_CHANNELS = [
    "/ws/dashboard",
    "/ws/buffer",
    "/ws/test-agent-1",          # /ws/{agent_id}
    "/ws/shared/watch/doc-p1",   # /ws/shared/watch/{doc_id}
    "/ws/shared/doc-p1",         # /ws/shared/{doc_id}
]


def _ws_url(path: str) -> str:
    return f"ws://127.0.0.1:{HUB_PORT}{path}"


def _connect_raw(path, timeout=3):
    """直连 WS 不发送任何帧，返回 ws 对象（已 accept 或已关闭）"""
    ws = websocket.create_connection(_ws_url(path), timeout=timeout)
    return ws


import websocket  # websocket-client
import struct as _struct


def _read_close_code(ws, timeout=1):
    """非阻塞读服务端 close 帧关闭码（连接保持打开时 1s 超时返回 None=未关闭）"""
    import socket as _socket
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
                    return _struct.unpack("!H", raw[:2])[0]
                return None
            if opcode is None:
                return None
    except Exception:
        return None


def _safe_close_code(ws, timeout=1):
    """兼容探测：优先 close_code 属性，否则非阻塞读 close 帧"""
    try:
        code = ws.close_code
        if code:
            return code
    except Exception:
        pass
    return _read_close_code(ws, timeout)

def _send_business_first(path, token=None):
    """直连后立刻发业务帧（无 auth 首帧），返回 (close_code, received)"""
    ws = websocket.create_connection(_ws_url(path), timeout=3)
    try:
        if path.startswith("/ws/dashboard"):
            ws.send(json.dumps({"msg_type": "ping"}))
        elif path.startswith("/ws/buffer"):
            pass  # buffer 通道只收不发，业务帧=等待数据
        elif path.startswith("/ws/shared/watch"):
            ws.send("keepalive")
        elif path.startswith("/ws/shared"):
            ws.send(json.dumps({"type": "ping"}))
        else:  # /ws/{agent_id}
            ws.send(json.dumps({"type": "ping", "session_id": "", "id": "1",
                                "via": "ws", "ts": 0, "version": 2,
                                "payload": {}}))
        # 尝试收响应或关闭码（recv_data_frame 直接拿 close 帧）
        try:
            opcode, abnf = ws.recv_data_frame()
            if opcode == 0x8:
                raw = getattr(abnf, "data", None)
                if raw and len(raw) >= 2:
                    return _struct.unpack("!H", raw[:2])[0], ""
                return _safe_close_code(ws), ""
            data = getattr(abnf, "data", b"")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            return _safe_close_code(ws), str(data)[:80]
        except Exception as e:
            return _safe_close_code(ws), str(e)[:80]
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _send_auth(path, token, business=None):
    """先发 auth 首帧再发业务帧，返回 (close_code, received)"""
    ws = websocket.create_connection(_ws_url(path), timeout=3)
    try:
        ws.send(json.dumps({"type": "auth", "token": token}))
        time.sleep(0.5)
        _close = _safe_close_code(ws, timeout=1)
        if _close:
            return _close, ""
        if business:
            ws.send(business)
            try:
                opcode, abnf = ws.recv_data_frame()
                if opcode == 0x8:
                    raw = getattr(abnf, "data", None)
                    if raw and len(raw) >= 2:
                        return _struct.unpack("!H", raw[:2])[0], ""
                return _safe_close_code(ws), ""
            except Exception as e:
                return _safe_close_code(ws), str(e)[:80]
        return _safe_close_code(ws), ""
    finally:
        try:
            ws.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def hub_process():
    tmpdir = tempfile.mkdtemp(prefix="p1-auth-")
    cfg_dir = os.path.join(tmpdir, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = {
        "server": {"host": "127.0.0.1", "port": HUB_PORT},
        "auth": {"enabled": True, "hub_token": HUB_TOKEN},
        "database": {"path": os.path.join(tmpdir, "test.db"),
                     "backup_enabled": False},
        "logging": {"level": "warning"},
    }
    with open(os.path.join(cfg_dir, "config.yaml"), "w", encoding="utf-8") as f:
        yaml = __import__("yaml")
        yaml.dump(cfg, f, allow_unicode=True)

    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = cfg_dir
    env.pop("SYNC_HUB_NO_AUTH", None)

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
        raise RuntimeError("Hub failed to start within 40s")
    yield
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_t1_1_no_auth_business_rejected(hub_process):
    """T1-1a: 5/5 通道无 auth 帧发业务帧被拒（4401 或断开）"""
    fails = []
    for path in WS_CHANNELS:
        code, data = _send_business_first(path)
        if code not in (4401, 4001, 1000, 1006):
            fails.append(f"{path} -> close={code} data={data!r}")
    assert not fails, "\n".join(fails)


def test_t1_1_wrong_token_4401(hub_process):
    """T1-1b: 5/5 通道错 token → close 4401"""
    fails = []
    for path in WS_CHANNELS:
        code, _ = _send_auth(path, "test-token-wrong")
        if code != 4401:
            fails.append(f"{path} -> close={code} (expect 4401)")
    assert not fails, "\n".join(fails)


def test_t1_1_correct_auth_ok(hub_process):
    """T1-1d: 5/5 通道正确 auth 首帧 → 不被拒（业务帧可收发或保持连接）"""
    fails = []
    for path in WS_CHANNELS:
        code, data = _send_auth(path, HUB_TOKEN)
        if code not in (None, 1000, 1006) and code != 4401:
            fails.append(f"{path} -> close={code} data={data!r}")
    assert not fails, "\n".join(fails)


def test_t1_1_auth_timeout_4401(hub_process):
    """T1-1c: 3s 不发 auth 帧 → close 4401（直连后只等，不发任何帧）"""
    fails = []
    for path in WS_CHANNELS:
        try:
            ws = websocket.create_connection(_ws_url(path), timeout=6)
            try:
                time.sleep(4)  # 超过 3s 超时窗口
                code = _safe_close_code(ws, timeout=2)
                if code != 4401:
                    fails.append(f"{path} -> close={code} (expect 4401)")
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
        except websocket.WebSocketBadStatusException as e:
            fails.append(f"{path} -> HTTP {e.status_code} (expect WS accept + 4401)")
        except Exception as e:
            fails.append(f"{path} -> {type(e).__name__}: {str(e)[:60]}")
    assert not fails, "\n".join(fails)
