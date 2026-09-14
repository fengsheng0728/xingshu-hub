"""P0: EXPECTED_PROTECTED_ROUTES — REST token 鉴权矩阵（独立进程真实 Hub）

对 routes.py AST 扫描出的全部 HTTP 路由逐一断言：
- 无 token → 401（allowlist 除外）
- 错 token → 401
- 正确 hub_token → 非 401

独立进程起真实 Hub（SYNC_HUB_CONFIG_DIR 临时目录 + hub_token + 独立 DB + port 3062），
避开 conftest 的 NO_AUTH=1（模块常量 import 时求值，同进程无法切换）。
"""
import os
import re
import sys
import time
import json
import shutil
import socket
import tempfile
import subprocess
import urllib.request
import urllib.error

import pytest

HUB_PORT = 3062
HUB_TOKEN = "test-token-p0-matrix"
ALLOWLIST = {
    "/health", "/healthz", "/readyz", "/", "/showcase", "/knowledge", "/chat", "/report", "/wiki", "/team",
    "/docs", "/openapi.json", "/static",
    # 函数内自校验 remote_api_key（team_members）——自认证端点豁免
    "/api/v1/team/proxy/disclose",
    # 配对握手端点：6 位配对码自认证（跨 Hub 调用）
    "/api/v1/team/pair/exchange",
}
# 引导端点（register/bootstrap）：hub_token 配置时受保护（无 token 401），
# 但允许 api_key 之外只认 hub_token——矩阵对它们按普通端点断言

# 路径参数填充
_PATH_PARAMS = {
    "{agent_id}": "test-agent",
    "{task_id}": "1",
    "{memory_key}": "testkey",
    "{notif_id}": "1",
    "{inbox_id}": "1",
    "{entry_id}": "1",
    "{page_path:path}": "test",
    "{doc_id}": "testdoc",
    "{request_id}": "1",
    "{member_id}": "1",
}


def _port_busy(port: int, timeout: float = 0.5) -> bool:
    """端口是否有人监听（连得上=有人）。"""
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _listener_pids(port: int) -> set:
    """监听该端口的 PID 集合（netstat -ano；解析失败返回空集表示无法判定）。"""
    try:
        r = subprocess.run("netstat -ano", shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        pids = set()
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in line.upper() and (":%d" % port) in parts[1]:
                pids.add(parts[-1])
        return pids
    except Exception:
        return set()


def _wait_port_free(port: int, timeout: float = 30.0) -> bool:
    """等端口彻底释放（无人监听）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not _port_busy(port):
            return True
        time.sleep(0.5)
    return False


def _fill_path(path: str) -> str:
    for k, v in _PATH_PARAMS.items():
        path = path.replace(k, v)
    return path


def _scan_routes() -> list:
    """AST 级扫描：routes.py（@app.）+ routes_*.py 子模块（@router.）全部 HTTP 装饰器

    Phase 2 拆分后：端点分布在 routes.py 与各 routes_*.py 子模块，需合并扫描。
    """
    base = os.path.join(os.path.dirname(__file__), "..")
    import glob
    routes = []
    src = open(os.path.join(base, "routes.py"), encoding="utf-8").read()
    routes += re.findall(r'@app\.(get|post|put|delete|patch)\(\s*"([^"]+)"', src)
    for f in sorted(glob.glob(os.path.join(base, "routes_*.py"))):
        s = open(f, encoding="utf-8").read()
        routes += re.findall(r'@router\.(get|post|put|delete|patch)\(\s*"([^"]+)"', s)
    # 按 (method, path) 对去重保序
    seen = set()
    out = []
    for method, path in routes:
        pair = (method.upper(), path)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _req(method: str, path: str, token: str = "") -> int:
    url = f"http://127.0.0.1:{HUB_PORT}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


@pytest.fixture(scope="module")
def hub_process():
    """起独立测试 Hub（临时 config + hub_token + 独立 DB + 3062）

    竞态防护（2026-09-14 实战）：用例逐个 spawn/kill 同一端口 Hub——
    ① 起之前必须等上一轮的 Hub 彻底退出（端口无人监听）：否则新进程绑定失败，
       而健康探针会打到**旧 Hub** 上误判就绪；旧 Hub 随后退出 → 该用例拿连接失败
       （test_allowlist_health_200 实测拿到 0，独立跑绿、全量跑必红）。
    ② 就绪判定不只看 /health 200，还要求监听该端口的 PID 就是本次 spawn 的进程。
    ③ 收尾等端口真正释放，给下一个用例干净起点。
    """
    if not _wait_port_free(HUB_PORT):
        raise RuntimeError("port %d 仍被占用（上一轮测试 Hub 未退出），拒绝起脏实例" % HUB_PORT)
    tmpdir = tempfile.mkdtemp(prefix="p0-auth-")
    cfg_dir = os.path.join(tmpdir, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = {
        "server": {"host": "127.0.0.1", "port": HUB_PORT},
        "auth": {"enabled": True, "hub_token": HUB_TOKEN},
        "database": {"path": os.path.join(tmpdir, "test.db"),
                     "backup_enabled": False},
        "logging": {"level": "warning"},
        "ui": {"close_to_tray": True, "start_minimized": False},
    }
    with open(os.path.join(cfg_dir, "config.yaml"), "w", encoding="utf-8") as f:
        yaml = __import__("yaml")
        yaml.dump(cfg, f, allow_unicode=True)

    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = cfg_dir
    env.pop("SYNC_HUB_NO_AUTH", None)  # 确保鉴权开启

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # 等健康（最长 40s）
    ok = False
    for _ in range(80):
        time.sleep(0.5)
        if proc.poll() is not None:
            break  # 本次 spawn 的进程已退出（如绑定失败）→ 不把别人的 200 当就绪
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{HUB_PORT}/health", timeout=2) as r:
                if r.status == 200:
                    pids = _listener_pids(HUB_PORT)
                    if not pids or str(proc.pid) in pids:
                        ok = True
                        break
        except Exception:
            continue
    if not ok:
        proc.kill()
        _wait_port_free(HUB_PORT)
        raise RuntimeError("Hub failed to start within 40s")
    yield
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    _wait_port_free(HUB_PORT)  # 等端口释放，避免下一个用例起脏实例
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_matrix_no_token_401(hub_process):
    """T0-1: 无 token → 除 allowlist 外全部 401"""
    routes = _scan_routes()
    assert len(routes) >= 80, f"route scan too small: {len(routes)}"
    fails = []
    for method, path in routes:
        if path in ALLOWLIST:
            continue
        code = _req(method, _fill_path(path), token="")
        if code != 401:
            fails.append(f"{method} {path} -> {code} (expect 401)")
    assert not fails, "\n".join(fails[:20])


def test_matrix_wrong_token_401(hub_process):
    """T0-1: 错 token → 除 allowlist 外全部 401"""
    routes = _scan_routes()
    fails = []
    for method, path in routes:
        if path in ALLOWLIST:
            continue
        code = _req(method, _fill_path(path), token="test-token-wrong")
        if code != 401:
            fails.append(f"{method} {path} -> {code} (expect 401)")
    assert not fails, "\n".join(fails[:20])


def test_matrix_correct_token_non401(hub_process):
    """T0-1: 正确 hub_token → 全部非 401（200/4xx 业务码均可，唯独不能 401）"""
    routes = _scan_routes()
    fails = []
    for method, path in routes:
        code = _req(method, _fill_path(path), token=HUB_TOKEN)
        if code == 401:
            fails.append(f"{method} {path} -> 401 (expect non-401 with valid token)")
    assert not fails, "\n".join(fails[:20])


def test_allowlist_health_200(hub_process):
    """allowlist 仅 /health 等白名单：/health 无 token 200"""
    assert _req("GET", "/health") == 200


# ============ T0-3: /mcp 移出认证豁免（CD-013 匿名访问关闭） ============

def test_mcp_no_token_401(hub_process):
    """T0-3: 无凭据 GET /mcp 及子路径 → 401（mount 子请求必须过外层中间件）"""
    for path in ("/mcp", "/mcp/", "/mcp/sse"):
        code = _req("GET", path, token="")
        assert code == 401, f"GET {path} 无凭据 expect 401, got {code}"


def test_mcp_with_hub_token_non401(hub_process):
    """T0-3: 带 hub_token GET /mcp → 非 401（200/404 皆可；SSE 流仅取首行状态码即断开）"""
    code = _req("GET", "/mcp", token=HUB_TOKEN)
    assert code != 401 and code != 0, f"GET /mcp 带 hub_token expect 非 401, got {code}"


def test_openapi_still_exempt_non401(hub_process):
    """T0-3 回归：无凭据 GET /openapi.json 仍豁免（确认未误删其他 allowlist 条目）"""
    code = _req("GET", "/openapi.json", token="")
    assert code == 200, f"GET /openapi.json 无凭据 expect 200, got {code}"


def test_route_scan_count():
    """矩阵覆盖量：扫描出的唯一路径数应 ≥ 80，装饰器对 ≥ 87（当前基线）"""
    routes = _scan_routes()
    print(f"\nscanned route-pairs: {len(routes)}")
    assert len(routes) >= 87
