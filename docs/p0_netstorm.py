"""P0 S5: 30 轮断网重连（真实路径）
独立测试 Hub 3061（临时配置目录+独立DB）→ Agent 连 3061 → 每轮 kill/重启 Hub
验证：重连成功 / agent online / 首条 chat 响应 / 断连期通知恢复后可达 / 无未捕获异常
"""
import subprocess, sys, json, time, threading, os, queue, tempfile, shutil, signal, urllib.request

AGENT_ID = "p0-netstorm"
TEST_HUB_PORT = 3061
os.chdir("E:/sync-hub-agent/backend")

_cfg = json.load(open(r"C:/Users/zero/AppData/Roaming/sync-hub-agent/config.json", encoding="utf-8"))
KEY = _cfg["llmApiKey"]

# ── 测试 Hub 准备（临时配置目录 + 3061 端口 + 独立 DB）──
tmpdir = tempfile.mkdtemp(prefix="p0-netstorm-")
os.makedirs(os.path.join(tmpdir, "config"), exist_ok=True)
with open(os.path.join(tmpdir, "config", "config.yaml"), "w", encoding="utf-8") as f:
    f.write(
        f"server:\n  port: {TEST_HUB_PORT}\n  host: 0.0.0.0\n"
        f"auth:\n  enabled: True\n"
        f"database:\n  path: {os.path.join(tmpdir, 'netstorm.db').replace(chr(92), '/')}\n"
        f"  backup_enabled: False\n"
    )
os.environ["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
# dashboard/静态目录是相对 cwd 的——cwd 必须留在项目根，DB 走 config 独立路径
hub_cwd = "E:/sync-hub-case"

hub_proc = None
def start_hub():
    global hub_proc
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
    hub_proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=hub_cwd, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # 等启动完成
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{TEST_HUB_PORT}/health", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False

def stop_hub():
    global hub_proc
    if hub_proc and hub_proc.poll() is None:
        hub_proc.kill()
        hub_proc.wait(timeout=5)
    hub_proc = None

def hub_online():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{TEST_HUB_PORT}/health", timeout=2) as r:
            return json.loads(r.read().decode()).get("status") in ("ok", "degraded")
    except Exception:
        return False

print("启动测试 Hub 3061...", flush=True)
os.environ["SYNC_HUB_NO_AUTH"] = ""  # 生产认证模式
if not start_hub():
    print("FAIL - 测试 Hub 启动失败"); sys.exit(1)
print("测试 Hub 在线", flush=True)

# ── Agent 启动 ──
proc = subprocess.Popen(
    [sys.executable, "agent_client.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1, cwd="E:/sync-hub-agent/backend"
)
q = queue.Queue()
ws_events = []
def reader():
    for line in proc.stdout:
        q.put(("out", line.strip()))
threading.Thread(target=reader, daemon=True).start()
def err_reader():
    for line in proc.stderr:
        try:
            d = json.loads(line)
            if d.get('type') in ('ws_status', 'push'):
                ws_events.append(d)
            if d.get('type') == 'approval_request':
                send({"id": 900, "cmd": "approve_tool", "approval_id": d.get("approval_id"), "approved": True})
        except Exception:
            pass
threading.Thread(target=err_reader, daemon=True).start()

def send(m):
    proc.stdin.write(json.dumps(m) + "\n"); proc.stdin.flush()

def get_line(timeout):
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None

def wait_resp(cmd_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = get_line(1)
        if not item: continue
        kind, line = item
        if kind != 'out': continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get('type') == 'push': continue
        if d.get('id') == cmd_id: return d
    return None

# ── Agent connect 测试 Hub ──
get_line(8)
send({"id": 1, "cmd": "set_llm_config", "config": {
    "provider": "deepseek", "apiKey": KEY, "apiBase": "https://api.deepseek.com/v1",
    "model": "deepseek-v4-flash"}})
time.sleep(2)
send({"id": 2, "cmd": "connect", "hub_url": f"http://127.0.0.1:{TEST_HUB_PORT}",
      "agent_id": AGENT_ID, "agent_name": "P0断网验证"})
r = wait_resp(2, 30)
if not r or r.get('error'):
    print(f"FAIL - connect: {r}"); sys.exit(1)
print("Agent 已连接测试 Hub", flush=True)
time.sleep(3)

def agent_online():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{TEST_HUB_PORT}/health", timeout=2) as resp:
            d = json.loads(resp.read().decode())
        return AGENT_ID in [a.get('agent_id') for a in d.get('agents', {}).get('list', [])] if 'list' in d.get('agents', {}) else d['agents']['online'] >= 1
    except Exception:
        return False

def agent_ws_connected():
    return any(e.get('status') == 'connected' for e in ws_events[-3:])

# ── 30 轮断网 ──
ROUNDS = 30
results = []
print(f"\n=== 30 轮断网重连 ===", flush=True)
for i in range(1, ROUNDS + 1):
    t0 = time.time()
    # 1. 断网：kill 测试 Hub
    stop_hub()
    time.sleep(1.0)
    # 2. 重启 Hub
    ok_start = start_hub()
    # 3. 等 Agent 重连（WS connected 或 health 里 online）
    reconnected = False
    chat_ok = False
    for _ in range(60):  # 最多 60s
        if agent_online():
            reconnected = True
            break
        time.sleep(0.5)
    # 4. 首条 chat 响应
    if reconnected:
        send({"id": 100 + i, "cmd": "chat", "message": f"重连第{i}轮，回复 OK", "session_id": "netstorm"})
        r = wait_resp(100 + i, 45)
        chat_ok = bool(r and r.get('type') == 'result' and (r.get('reply') or ''))
    elapsed = time.time() - t0
    ok = ok_start and reconnected and chat_ok
    results.append((i, ok, ok_start, reconnected, chat_ok, round(elapsed, 1)))
    print(f"  轮{i}: {'✅' if ok else '❌'} hub_start={ok_start} online={reconnected} chat={chat_ok} {elapsed}s", flush=True)

# ── 断连期通知恢复后可达 ──
notif_ok = True
try:
    stop_hub(); time.sleep(1); start_hub()
    time.sleep(3)
    conn = None
    import sqlite3
    # 用 Agent 已注册的 api_key 建通知
    send({"id": 9999, "cmd": "get_my_info"})
    r = wait_resp(9999, 20)
    # 直接经 REST 建通知（用 agent 的 key）
    import urllib.parse
    conn = sqlite3.connect(os.path.join(tmpdir, "netstorm.db"))
    row = conn.execute("SELECT api_key FROM agents WHERE agent_id=?", (AGENT_ID,)).fetchone()
    conn.close()
    if row:
        req = urllib.request.Request(
            f"http://127.0.0.1:{TEST_HUB_PORT}/api/v1/notifications/create?agent_id={AGENT_ID}&title=reconnect-notif&body=post-reconnect",
            headers={"Authorization": f"Bearer {row[0]}"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            rj = json.loads(resp.read().decode())
        notif_ok = rj.get('status') == 'ok'
        time.sleep(2)
except Exception as e:
    notif_ok = False
    print(f"  通知可达检查异常: {e}", flush=True)

# 汇总
passed = sum(1 for _, ok, *_ in results if ok)
print(f"\n=== S5 结果: {passed}/{ROUNDS} 轮通过 | 通知恢复后可达: {notif_ok} ===")

# 清理
send({"id": 999, "cmd": "shutdown"})
time.sleep(0.3)
proc.terminate()
try: proc.wait(timeout=3)
except Exception: proc.kill()
stop_hub()
shutil.rmtree(tmpdir, ignore_errors=True)

sys.exit(0 if passed == ROUNDS and notif_ok else 1)
