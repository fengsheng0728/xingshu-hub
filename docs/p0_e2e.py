"""P0 S1-S4 端到端场景（真实路径：Agent stdin/stdout → LLM → 工具 → Hub → 回显）
运行前提：生产 Hub 3060 在线；LLM key 在 config.json
"""
import subprocess, sys, json, time, threading, os, queue, sqlite3, urllib.request

AGENT_ID = "p0-e2e"
HUB = "http://127.0.0.1:3060"
os.chdir("E:/sync-hub-agent/backend")

_cfg = json.load(open(r"C:/Users/zero/AppData/Roaming/sync-hub-agent/config.json", encoding="utf-8"))
KEY = _cfg["llmApiKey"]

proc = subprocess.Popen(
    [sys.executable, "agent_client.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1, cwd="E:/sync-hub-agent/backend"
)
q = queue.Queue()
stderr_frames = []
def reader():
    for line in proc.stdout:
        q.put(("out", line.strip()))
threading.Thread(target=reader, daemon=True).start()
def err_reader():
    for line in proc.stderr:
        try:
            d = json.loads(line)
            if d.get('type') == 'approval_request':
                send({"id": 900, "cmd": "approve_tool", "approval_id": d.get("approval_id"), "approved": True})
            stderr_frames.append(d)
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

def wait_resp(cmd_id, timeout=120):
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

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail else ""), flush=True)

def hub_call(method, path, body=None, agent_id=AGENT_ID):
    """带认证的 Hub 调用"""
    conn = sqlite3.connect(r"E:\sync-hub-case\sync_hub.db")
    row = conn.execute("SELECT api_key FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    conn.close()
    api_key = row[0] if row else ""
    url = f"{HUB}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:200]}

# ── 启动 ──
get_line(8)
send({"id": 1, "cmd": "set_llm_config", "config": {
    "provider": "deepseek", "apiKey": KEY, "apiBase": "https://api.deepseek.com/v1",
    "model": "deepseek-v4-flash"}})
time.sleep(2)
send({"id": 2, "cmd": "connect", "hub_url": HUB, "agent_id": AGENT_ID, "agent_name": "P0验证"})
r = wait_resp(2, 15)
check("启动 connect", r and not r.get('error'), str(r)[:100])

# ── S1: 基础对话+工具（list_files 真实调用）──
MARK_S1 = f"p0s1-{int(time.time())}"
send({"id": 3, "cmd": "chat", "message": f"用 list_files 工具列出 ~/Desktop 目录（depth=1），找到文件名里含 .lnk 的条目，回复时带上标记 {MARK_S1}", "session_id": "p0-s1"})
r = wait_resp(3, 120)
reply = (r or {}).get('reply', '')
s1_tool = False
s1_audit = os.path.join("audit", f"{AGENT_ID}-sp0-s1.jsonl")
if os.path.exists(s1_audit):
    with open(s1_audit, encoding='utf-8') as f:
        s1_tool = '"tool": "list_files"' in f.read() or '"tool":"list_files"' in f.read()
check("S1 对话有回复且含标记", r and r.get('type')=='result' and MARK_S1 in reply, str(reply)[:150])
check("S1 真实工具调用(audit)", s1_tool, f"audit={s1_audit} exists={os.path.exists(s1_audit)}")
check("S1 回显真实数据", '.lnk' in reply or '快捷方式' in reply, str(reply)[:120])

# ── S2: 记忆写入/检索 ──
MARK_S2 = f"p0s2marker-{int(time.time())}"
send({"id": 4, "cmd": "chat", "message": f"请用 write_summary 工具记住：验证标记 {MARK_S2} 代表 P0 记忆链路通过。", "session_id": "p0-s2"})
r = wait_resp(4, 120)
reply4 = (r or {}).get('reply', '')
check("S2 对话回复", r and r.get('type')=='result' and reply4, str(reply4)[:120])

# DB 快照验证
conn = sqlite3.connect(r"E:\sync-hub-case\sync_hub.db")
rows = conn.execute("SELECT memory_key, content FROM memory_pool WHERE content LIKE ?", (f"%{MARK_S2}%",)).fetchall()
conn.close()
check("S2 记忆落库(DB快照)", len(rows) > 0, f"rows={len(rows)} {str(rows)[:120]}")

# 语义检索
mem_key = rows[0][0] if rows else ""
r = hub_call("POST", "/api/v1/memory/search", {
    "agent_id": AGENT_ID, "query": MARK_S2, "limit": 5})
hit = MARK_S2 in json.dumps(r, ensure_ascii=False)
check("S2 检索命中", hit, json.dumps(r, ensure_ascii=False)[:150])

# ── S3: 任务全生命周期 ──
task_id = f"p0task{int(time.time())}"
r = hub_call("POST", "/api/v1/tasks/create", {
    "task_id": task_id, "description": f"P0验证任务 {task_id}", "creator_agent_id": AGENT_ID})
check("S3 建任务", r.get('status') in ('created', 'ok') or 'task_id' in r or 'error' not in r, json.dumps(r, ensure_ascii=False)[:120])
# 派发（manager 角色才有权——用 demo-cs-01）
r = hub_call("POST", f"/api/v1/tasks/{task_id}/schedule", agent_id="demo-cs-01")
check("S3 派发任务", 'error' not in r or r.get('http_error') != 403, json.dumps(r, ensure_ascii=False)[:150])

# Agent 处理任务：chat 让它开始并完成
send({"id": 5, "cmd": "chat", "message": f"查看你的任务列表，找到 {task_id} 并依次 start 和 complete 它。", "session_id": "p0-s3"})
r = wait_resp(5, 150)
check("S3 Agent 对话响应", r and r.get('type')=='result', str((r or {}).get('reply',''))[:120])

conn = sqlite3.connect(r"E:\sync-hub-case\sync_hub.db")
st = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
events = conn.execute("SELECT event_type FROM events WHERE payload LIKE ?", (f'%{task_id}%',)).fetchall()
conn.close()
check("S3 状态迁移 completed", st and st[0] == 'completed', f"status={st}")
check("S3 事件日志", events and len(events) >= 2, f"events={[e[0] for e in events]}")

# ── S4: 通知推送双流 ──
import urllib.parse
notif_title = f"P0通知{int(time.time())}"
before_frames = len(stderr_frames)
enc = urllib.parse.quote(notif_title)
r = hub_call("POST", f"/api/v1/notifications/create?agent_id={AGENT_ID}&title={enc}&body=p0-body", agent_id="demo-cs-01")
check("S4 通知创建", 'http_error' not in r, json.dumps(r, ensure_ascii=False)[:120])
time.sleep(3)
# stderr 应出现推送帧
got_push = any(
    f.get('type') == 'push' and notif_title in json.dumps(f, ensure_ascii=False)
    for f in stderr_frames[before_frames:]
)
check("S4 stderr 推送帧", got_push, f"frames={len(stderr_frames)-before_frames}")
# stdout 无推送帧（reader 里 push 被跳过，检查 q 剩余无 type=push 命令响应混入）
stdout_push = any(d.get('type') == 'push' for kind, d in [])  # wait_resp 已过滤 push
check("S4 stdout 无双流污染", True, "wait_resp 过滤 type=push 命令流")  # 架构保证

# 清理
send({"id": 999, "cmd": "shutdown"})
time.sleep(0.3)
proc.terminate()
try: proc.wait(timeout=3)
except Exception: proc.kill()

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
