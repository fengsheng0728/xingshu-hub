"""P0: CD-016 semantic_search 降级补齐 — 故障注入测试（先红后绿）

独立测试 Hub（3062 + 临时 config/db/chroma）：
- T0-1 故障注入：占位文件挡 chroma → 连续 10 次 semantic_search 全 200、0 个 500、
  响应含 degraded=true 标记、结果非空（SQLite 关键词降级命中）
- T0-2 恢复回切：chroma 恢复后同一查询无 degraded 标记，向量检索命中语义相关条目
- T0-3 启动韧性：chroma 目录锁死状态启动 Hub → 启动成功、/health 200、semantic 走降级

运行要求：独立进程起 Hub（stdout=DEVNULL），临时目录隔离，不碰生产 chroma_db。
"""
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

HUB_PORT = 3062
HUB_URL = f"http://127.0.0.1:{HUB_PORT}"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = "p0-sem-degrade"

hub_proc = None
tmpdir = None


def setup_module():
    global tmpdir
    tmpdir = tempfile.mkdtemp(prefix="p0-semdeg-")
    os.makedirs(os.path.join(tmpdir, "config"), exist_ok=True)
    with open(os.path.join(tmpdir, "config", "config.yaml"), "w", encoding="utf-8") as f:
        f.write(
            f"server:\n  port: {HUB_PORT}\n  host: 127.0.0.1\n"
            f"auth:\n  enabled: True\n"
            f"database:\n  path: {os.path.join(tmpdir, 'test.db').replace(chr(92), '/')}\n"
            f"  backup_enabled: False\n"
            f"  chroma_path: {os.path.join(tmpdir, 'chroma_db').replace(chr(92), '/')}\n"
        )
    os.environ["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
    os.environ["SYNC_HUB_CHROMA_PATH"] = os.path.join(tmpdir, "chroma_db")


def teardown_module():
    global hub_proc
    if hub_proc and hub_proc.poll() is None:
        hub_proc.kill()
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    os.environ.pop("SYNC_HUB_CONFIG_DIR", None)
    os.environ.pop("SYNC_HUB_CHROMA_PATH", None)


def start_hub():
    global hub_proc
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
    env["SYNC_HUB_CHROMA_PATH"] = os.path.join(tmpdir, "chroma_db")
    hub_proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=PROJECT_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen(HUB_URL + "/health", timeout=3) as resp:
                d = json.loads(resp.read())
                if d.get("database", {}).get("status") == "ok":
                    return True
        except Exception:
            pass
    return False


def stop_hub():
    global hub_proc
    if hub_proc and hub_proc.poll() is None:
        hub_proc.kill()
        hub_proc.wait()
        hub_proc = None
    time.sleep(1)


def register_agent():
    payload = {"agent_id": AGENT, "agent_name": "p0-sem-degrade", "role": "manager"}
    r = urllib.request.Request(HUB_URL + "/api/v1/agents/register",
                               data=json.dumps(payload).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read())["api_key"]


def req(method, path, token, data=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(HUB_URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return 0, {"error": str(e)[:120]}


def write_memories(key):
    """写入两条记忆：一条关键词可命中，一条仅语义相关（关键词不匹配）"""
    memories = [
        {"memory_key": "p0-sem-keyword", "kind": "fact", "content": "量子计算在金融风控中的应用",
         "tags": ["p0"], "source_type": "user"},
        {"memory_key": "p0-sem-semantic", "kind": "fact", "content": "蒙特卡洛模拟用于期权定价风险对冲",
         "tags": ["p0"], "source_type": "user"},
    ]
    for m in memories:
        s, d = req("POST", f"/api/v1/memory/store?agent_id={AGENT}", key, m)
        assert s == 200, f"memory store failed: {s} {d}"
    time.sleep(3)  # 等 embedding + chroma 同步


def disable_chroma():
    """占位文件挡 chroma 目录（chromadb 初始化失败 → collection None）"""
    chroma = os.path.join(tmpdir, "chroma_db")
    backup = chroma + ".bak"
    if os.path.exists(backup):
        shutil.rmtree(backup, ignore_errors=True)
    if os.path.exists(chroma):
        os.rename(chroma, backup)
    with open(chroma, "w") as f:
        f.write("PLACEHOLDER - chroma disabled")


def restore_chroma():
    chroma = os.path.join(tmpdir, "chroma_db")
    backup = chroma + ".bak"
    os.remove(chroma)
    os.rename(backup, chroma)


def test_t01_fault_injection_degraded():
    """T0-1 故障注入：chroma 不可用 → 10 次请求全 200、0 个 500、degraded=true、结果非空"""
    assert start_hub(), "测试 Hub 启动失败"
    key = register_agent()
    write_memories(key)

    # 正常态基线：向量检索命中（含语义相关条目）
    s, d = req("POST", "/api/v1/memory/semantic_search", key,
               {"query": "金融风控", "requester_agent_id": AGENT, "n_results": 5})
    assert s == 200, f"基线请求失败: {s} {d}"
    base_total = d.get("total", 0)
    assert base_total > 0, f"基线应命中记忆: {d}"
    assert not d.get("degraded"), f"正常态不应有 degraded: {d}"

    # 故障注入
    stop_hub()
    disable_chroma()
    assert start_hub(), "chroma 禁用后 Hub 启动失败"

    # 连续 10 次
    for i in range(10):
        s, d = req("POST", "/api/v1/memory/semantic_search", key,
                   {"query": "金融风控", "requester_agent_id": AGENT, "n_results": 5})
        assert s == 200, f"第{i+1}次请求非 200: {s} {d}"
        assert d.get("degraded") is True, f"第{i+1}次应带 degraded=true: {d}"
        assert d.get("total", 0) > 0, f"第{i+1}次降级应返回结果: {d}"
    print("T0-1 PASS: 10/10 全 200, 0 个 500, degraded=true, 结果非空")


def test_t02_recovery_switch_back():
    """T0-2 恢复回切：chroma 恢复 → 无 degraded、向量检索命中语义相关条目"""
    key = None
    conn = sqlite3.connect(os.path.join(tmpdir, "test.db"))
    row = conn.execute("SELECT api_key FROM agents WHERE agent_id=?", (AGENT,)).fetchone()
    conn.close()
    if row:
        key = row[0]
    assert key, "agent key 缺失（依赖 T0-1 注册）"

    # 恢复 chroma 并重启
    stop_hub()
    restore_chroma()
    assert start_hub(), "chroma 恢复后 Hub 启动失败"

    s, d = req("POST", "/api/v1/memory/semantic_search", key,
               {"query": "金融风控", "requester_agent_id": AGENT, "n_results": 5})
    assert s == 200, f"恢复后请求失败: {s} {d}"
    assert not d.get("degraded"), f"恢复后不应有 degraded: {d}"
    assert d.get("total", 0) > 0, f"恢复后应命中: {d}"
    print("T0-2 PASS: 恢复后无 degraded, 向量检索命中")


def test_t03_startup_resilience():
    """T0-3 启动韧性：chroma 目录锁死状态启动 Hub → /health 200、semantic 走降级"""
    key = None
    conn = sqlite3.connect(os.path.join(tmpdir, "test.db"))
    row = conn.execute("SELECT api_key FROM agents WHERE agent_id=?", (AGENT,)).fetchone()
    conn.close()
    if row:
        key = row[0]

    stop_hub()
    disable_chroma()
    ok = start_hub()
    assert ok, "chroma 锁死状态 Hub 启动失败"

    with urllib.request.urlopen(HUB_URL + "/health", timeout=5) as resp:
        hd = json.loads(resp.read())
        assert resp.status == 200, f"/health 非 200: {resp.status}"
        assert hd.get("database", {}).get("status") == "ok", f"db 异常: {hd}"

    s, d = req("POST", "/api/v1/memory/semantic_search", key,
               {"query": "金融风控", "requester_agent_id": AGENT, "n_results": 5})
    assert s == 200, f"降级请求失败: {s} {d}"
    assert d.get("degraded") is True, f"应走降级: {d}"
    print("T0-3 PASS: 锁死启动成功 + /health 200 + semantic 走降级")
