# -*- coding: utf-8 -*-
"""Hub 可选 TLS（CD-068）：配置解析 / 启动 fail-closed / 真 https+wss 端到端

背景（2026-09-20，用户拍板「放出 API 后本地做后鉴权的限制收口」，全做三件之①）：
Hub 此前只有明文 HTTP（uvicorn.run 无 ssl 参数），对外放出 API 后 Bearer 凭据可被
嗅探——这也是「本地后鉴权」在传输层的最大短板。本测试锁定：
  1. 默认不开（缺 server.tls 段 = 旧行为，零迁移）；
  2. 开了但证书文件不存在 → 启动 fail-closed 拒绝（不许静默降级明文）；
  3. 开启后 REST 走 https（无 token 仍 401 / 有 token 200）、WS 走 wss（首帧鉴权通过）；
  4. 明文 http 打 TLS 端口必须失败（确认真的在加密，而不是两套并存）。

端口 3073（避开 3062/3063/3071/3072）。
"""
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB_PORT = 3073
HUB_TOKEN = "tls-e2e-hub-token"
BASE = f"https://127.0.0.1:{HUB_PORT}"

sys.path.insert(0, REPO_ROOT)


def _write_yaml(path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml = __import__("yaml")
        yaml.dump(cfg, f, allow_unicode=True)


def _env(cfg_dir: str, tmpdir: str) -> dict:
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = cfg_dir
    env["SYNC_HUB_CHROMA_PATH"] = os.path.join(tmpdir, "chroma_db")
    env.pop("SYNC_HUB_NO_AUTH", None)
    return env


def _kill_tree(proc) -> None:
    """进程树收尾：Windows 上单 kill 偶发留下监听（实测踩过孤儿占 3073），"
    用 taskkill /F /T 兜底，失败再退回 proc.kill()。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _spawn(cfg_dir: str, tmpdir: str, log_path: str) -> subprocess.Popen:
    """stdout/stderr 落文件（不用 PIPE：管道满会卡死，见项目铁律）"""
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    return subprocess.Popen([sys.executable, "main.py"], cwd=REPO_ROOT,
                            env=_env(cfg_dir, tmpdir), stdout=log, stderr=subprocess.STDOUT)


# ── 用例 1：配置解析（纯函数） ──


def test_tls_config_parse_defaults_and_errors(tmp_path):
    from tls_util import load_tls_config, validate_tls_files, gen_self_signed

    # ① 无 tls 段 = 不开（零迁移）
    cfg_a = tmp_path / "a" / "config.yaml"
    _write_yaml(str(cfg_a), {"server": {"host": "127.0.0.1", "port": 3060}})
    tls_a = load_tls_config(str(cfg_a))
    assert tls_a["enabled"] is False, f"缺 server.tls 段应视为不开启: {tls_a}"
    assert validate_tls_files(tls_a) == "", "未开启时不应报错"

    # ② 开启但证书缺失 → 报错（fail-closed）
    cfg_b = tmp_path / "b" / "config.yaml"
    _write_yaml(str(cfg_b), {"server": {"host": "0.0.0.0", "port": 3060,
                                        "tls": {"enabled": True,
                                                "certfile": "nope.crt",
                                                "keyfile": "nope.key"}}})
    tls_b = load_tls_config(str(cfg_b))
    assert tls_b["enabled"] is True
    assert validate_tls_files(tls_b) != "", "开启但证书缺失必须报错（不许静默降级明文）"

    # ③ 相对路径按 config 目录解析
    (tmp_path / "c").mkdir(parents=True, exist_ok=True)
    cert = tmp_path / "c" / "server.crt"
    key = tmp_path / "c" / "server.key"
    # 用真证书（不是假文本）：validate 会真的 load_cert_chain，假 PEM 必然报错
    gen_self_signed(str(cert), str(key), hosts=["127.0.0.1", "localhost"])
    cfg_c = tmp_path / "c" / "config.yaml"
    _write_yaml(str(cfg_c), {"server": {"tls": {"enabled": True, "certfile": "server.crt",
                                                "keyfile": "server.key"}}})
    tls_c = load_tls_config(str(cfg_c))
    assert os.path.normcase(os.path.abspath(tls_c["certfile"])) == \
        os.path.normcase(os.path.abspath(str(cert))), \
        f"相对路径应基于 config 目录解析: {tls_c}"
    assert validate_tls_files(tls_c) == "", f"文件都在，不应报错: {tls_c}"


def test_self_signed_generator(tmp_path):
    """自签证书生成：文件落地 + SAN 含 127.0.0.1（TLS 校验要用）"""
    from tls_util import gen_self_signed
    cert = tmp_path / "s.crt"
    key = tmp_path / "s.key"
    res = gen_self_signed(str(cert), str(key), hosts=["127.0.0.1", "localhost"])
    assert cert.exists() and key.exists(), f"证书未落盘: {res}"
    assert "BEGIN CERTIFICATE" in cert.read_text(encoding="utf-8")
    assert "PRIVATE KEY" in key.read_text(encoding="utf-8")
    assert res.get("sans"), f"应返回 SAN 清单: {res}"


# ── 用例 2：启动 fail-closed ──


def test_startup_refuses_when_cert_missing(tmp_path):
    tmpdir = str(tmp_path)
    cfg_dir = os.path.join(tmpdir, "config")
    log_path = os.path.join(tmpdir, "boot.log")
    _write_yaml(os.path.join(cfg_dir, "config.yaml"), {
        "server": {"host": "127.0.0.1", "port": HUB_PORT,
                   "tls": {"enabled": True, "certfile": "missing.crt",
                           "keyfile": "missing.key"}},
        "auth": {"enabled": True, "hub_token": HUB_TOKEN},
        "database": {"path": os.path.join(tmpdir, "tls.db"), "backup_enabled": False},
        "logging": {"level": "warning"},
    })
    proc = _spawn(cfg_dir, tmpdir, log_path)
    code = None
    for _ in range(40):
        time.sleep(0.5)
        code = proc.poll()
        if code is not None:
            break
    assert code is not None, "证书缺失时进程应退出（fail-closed），实际仍在运行"
    assert code != 0, f"退出码应非 0，实际 {code}"
    log = open(log_path, "r", encoding="utf-8", errors="replace").read()
    assert "TLS" in log.upper(), f"启动失败信息应提到 TLS: {log[:400]}"
    # 不应留下监听
    assert not _port_open(HUB_PORT), "fail-closed 后不应有监听"


# ── 用例 3：真 https + wss 端到端 ──


def _port_open(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _req(path: str, token: str = "", ctx=None):
    url = f"{BASE}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return r.status, r.read()[:400].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400].decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)[:200]


@pytest.fixture(scope="module")
def tls_hub():
    tmpdir = tempfile.mkdtemp(prefix="tls-e2e-")
    cfg_dir = os.path.join(tmpdir, "config")
    cert = os.path.join(tmpdir, "server.crt")
    key = os.path.join(tmpdir, "server.key")
    from tls_util import gen_self_signed
    gen_self_signed(cert, key, hosts=["127.0.0.1", "localhost"])
    _write_yaml(os.path.join(cfg_dir, "config.yaml"), {
        "server": {"host": "127.0.0.1", "port": HUB_PORT,
                   "tls": {"enabled": True, "certfile": cert, "keyfile": key}},
        "auth": {"enabled": True, "hub_token": HUB_TOKEN},
        "database": {"path": os.path.join(tmpdir, "tls.db"), "backup_enabled": False},
        "logging": {"level": "warning"},
    })
    log_path = os.path.join(tmpdir, "hub.log")
    proc = _spawn(cfg_dir, tmpdir, log_path)
    ok = False
    ctx = ssl.create_default_context(cafile=cert)
    for _ in range(80):
        time.sleep(0.5)
        status, _body = _req("/health", ctx=ctx)
        if status == 200:
            ok = True
            break
    if not ok:
        _kill_tree(proc)
        log = open(log_path, "r", encoding="utf-8", errors="replace").read()[-800:]
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"TLS Hub 未在 40s 内就绪：{log}")
    yield {"cert": cert, "ctx": ctx, "tmpdir": tmpdir}
    _kill_tree(proc)
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_https_rest_auth_matrix(tls_hub):
    ctx = tls_hub["ctx"]
    status, body = _req("/health", ctx=ctx)
    assert status == 200, f"https /health 应 200（免认证探针）: {status}"

    status, body = _req("/api/v1/tasks", ctx=ctx)
    assert status == 401, f"https 无凭据应 401, 实际 {status}: {body}"

    status, body = _req("/api/v1/tasks", token=HUB_TOKEN, ctx=ctx)
    assert status == 200, f"https 带 hub_token 应 200, 实际 {status}: {body}"

    # 页面壳（T0-3 无 token 可打开）：证明确实是同一台 Hub 在 TLS 上服务
    status, body = _req("/", ctx=ctx)
    assert status == 200, f"https 页面壳应 200, 实际 {status}"


def test_plain_http_to_tls_port_fails(tls_hub):
    """TLS 端口不接受明文 HTTP（确认在加密，不是两套并存）"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", HUB_PORT, timeout=5)
    failed = False
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
    except Exception:
        failed = True
    finally:
        conn.close()
    assert failed, "明文 HTTP 打 TLS 端口应失败（握手不成立）"


def test_wss_first_frame_auth(tls_hub):
    websocket = pytest.importorskip("websocket")
    cert = tls_hub["cert"]
    sslopt = {"ca_certs": cert}

    # ① 正确 hub_token：首帧鉴权通过 → ping/pong 可用
    ws = websocket.create_connection(
        f"wss://127.0.0.1:{HUB_PORT}/ws/dashboard", timeout=5, sslopt=sslopt)
    try:
        ws.send(json.dumps({"type": "auth", "token": HUB_TOKEN}))
        time.sleep(0.5)
        ws.send(json.dumps({"msg_type": "ping"}))
        got = ""
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                got = ws.recv()
            except Exception:
                break
            if "pong" in str(got):
                break
        assert "pong" in str(got), f"wss 首帧鉴权后应收到 pong, 实际: {got!r}"
    finally:
        try:
            ws.close()
        except Exception:
            pass

    # ② 错 token：4401
    ws2 = websocket.create_connection(
        f"wss://127.0.0.1:{HUB_PORT}/ws/dashboard", timeout=5, sslopt=sslopt)
    try:
        ws2.send(json.dumps({"type": "auth", "token": "wrong-token"}))
        time.sleep(0.6)
        code = _read_close_code(ws2)
        assert code == 4401, f"错 token 应 4401, 实际 {code}"
    finally:
        try:
            ws2.close()
        except Exception:
            pass


def _read_close_code(ws, timeout=1.5):
    import struct
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
    raise SystemExit(pytest.main([__file__, "-q"]))
