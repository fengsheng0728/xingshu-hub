"""T2 通知多渠道出站测试（独立测试 Hub + 独立接收进程）

架构：
- 测试 Hub：3063 端口 + 临时 config（notify_channels 启钉钉/SMTP 指向本地接收端）
- 本地 HTTP 接收端（独立进程）：收钉钉 POST，断言 markdown + 验签
- 本地 SMTP 接收端（aiosmtpd 独立进程）：收邮件，断言 MIME
- 故障注入：接收端关闭 → 10 条通知主链路 10/10 正常
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

PROJECT = str(pathlib.Path(__file__).resolve().parent.parent)  # 仓库根（tests/ 上一级）
HUB_PORT = 3063
HUB_URL = f"http://127.0.0.1:{HUB_PORT}"
DING_PORT = 19125
SMTP_PORT = 19126
DING_URL = f"http://127.0.0.1:{DING_PORT}/robot/send"
SECRET = "test-secret-123"
AGENT = "p2-stress-agent"

hub_proc = None
ding_proc = None
smtp_proc = None
tmpdir = None

DING_CODE = """
import json, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", errors="replace")
        print("DING|" + self.path + "|" + body, flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"errcode": 0}).encode())
    def log_message(self, *a):
        pass
srv = HTTPServer(("127.0.0.1", %d), H)
srv.serve_forever()
""" % DING_PORT

SMTP_CODE = """
import sys, asyncio, json
from aiosmtpd.controller import Controller
messages = []
class Handler:
    async def handle_DATA(self, server, session, envelope):
        await asyncio.sleep(0)
        messages.append(envelope.content.decode("utf-8", errors="replace"))
        return "250 OK"
ctrl = Controller(Handler(), hostname="127.0.0.1", port=%d)
ctrl.start()
print("SMTP_READY", flush=True)
for line in sys.stdin:
    if line.strip() == "get":
        # JSON 编码输出（消息内含换行，readline 只能读第一行）
        print("MSG:" + json.dumps(messages[-1] if messages else "", ensure_ascii=False), flush=True)
    elif line.strip() == "reset":
        messages.clear(); print("RESET", flush=True)
    elif line.strip() == "quit":
        break
ctrl.stop()
""" % SMTP_PORT


def start_ding():
    global ding_proc
    ding_proc = subprocess.Popen([sys.executable, "-c", DING_CODE],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, bufsize=1)
    time.sleep(1)


def start_smtp():
    global smtp_proc
    smtp_proc = subprocess.Popen([sys.executable, "-c", SMTP_CODE],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, bufsize=1)
    for _ in range(15):
        line = smtp_proc.stdout.readline().strip()
        if "SMTP_READY" in line:
            break
        time.sleep(0.5)


def smtp_cmd(cmd):
    smtp_proc.stdin.write(cmd + "\n")
    smtp_proc.stdin.flush()
    line = smtp_proc.stdout.readline().strip()
    if line.startswith("MSG:"):
        import json as _json
        return _json.loads(line[4:])
    return line


def start_hub():
    global hub_proc, tmpdir
    tmpdir = tempfile.mkdtemp(prefix="p2-notify-")
    os.makedirs(os.path.join(tmpdir, "config"), exist_ok=True)
    with open(os.path.join(tmpdir, "config", "config.yaml"), "w", encoding="utf-8") as f:
        f.write(
            f"server:\n  port: {HUB_PORT}\n  host: 127.0.0.1\n"
            f"auth:\n  enabled: True\n"
            f"database:\n  path: {os.path.join(tmpdir, 'test.db').replace(chr(92), '/')}\n"
            f"  backup_enabled: False\n"
            f"  chroma_path: {os.path.join(tmpdir, 'chroma_db').replace(chr(92), '/')}\n"
            f"notify_channels:\n"
            f"  dingtalk:\n    enabled: True\n    webhook: {DING_URL}\n    secret: {SECRET}\n"
            f"  smtp:\n    enabled: True\n    host: 127.0.0.1\n    port: {SMTP_PORT}\n"
            f"    user: test@local\n    password: x\n    from: test@local\n    to: [recv@local]\n"
        )
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
    env["SYNC_HUB_CHROMA_PATH"] = os.path.join(tmpdir, "chroma_db")
    hub_proc = subprocess.Popen([sys.executable, "main.py"], cwd=PROJECT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen(HUB_URL + "/health", timeout=3) as resp:
                if json.loads(resp.read()).get("database", {}).get("status") == "ok":
                    return True
        except Exception:
            pass
    return False


def req(method, path, token=None, data=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(HUB_URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", errors="replace"))


def verify_sign(query):
    params = {}
    for kv in query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            # timestamp 需要解码；sign 保持原始编码态（与 quote_plus 期望同态比较）
            params[k] = urllib.parse.unquote_plus(v) if k == "timestamp" else v
    ts, sign = params.get("timestamp", ""), params.get("sign", "")
    if not ts or not sign:
        return False
    expect = urllib.parse.quote_plus(base64.b64encode(
        hmac.new(SECRET.encode(), f"{ts}\n{SECRET}".encode(), hashlib.sha256).digest()))
    return sign == expect


def main():
    global hub_proc, ding_proc, smtp_proc, tmpdir
    assert start_hub(), "测试 Hub 启动失败"
    print("测试 Hub 就绪 :%d" % HUB_PORT, flush=True)
    key = None
    # 用 register API 注册（Hub 自动建表 + 返回真实 key）
    s, d = req("POST", "/api/v1/agents/register",
               data={"agent_id": AGENT, "agent_name": "p2-stress", "role": "manager"})
    print("  [debug] register s=%s d=%s" % (s, json.dumps(d, ensure_ascii=False)[:120]), flush=True)
    if s == 200 and d.get("api_key"):
        key = d["api_key"]
    assert key, f"register 失败 {s} {d}"
    print("key:", key[:8], flush=True)
    # 确认 Hub 用的 DB 就是 test.db（agents 应已写入）
    try:
        conn = sqlite3.connect(os.path.join(tmpdir, "test.db"))
        agents = conn.execute("SELECT agent_id FROM agents").fetchall()
        conn.close()
        print("  [debug] test.db agents:", agents, flush=True)
    except Exception as e:
        print("  [debug] test.db agents 查询失败:", e, flush=True)

    start_ding()
    start_smtp()
    print("接收进程就绪", flush=True)

    results = {}

    # T2-1 钉钉
    try:
        title = "P2-T2-1 钉钉-" + uuid.uuid4().hex[:6]
        s, d = req("POST", "/api/v1/notifications/create?agent_id=%s&type=info&title=%s&body=%s" %
                   (urllib.parse.quote(AGENT), urllib.parse.quote(title), urllib.parse.quote("T2-1 body 验证")),
                   key)
        assert s == 200, f"创建失败 {s} {d}"
        time.sleep(3)  # 等异步 fan-out 完成
        line = ding_proc.stdout.readline().strip()
        parts = line.split("|", 2) if line.startswith("DING|") else ["", "", ""]
        path, body = parts[1], parts[2]
        p = json.loads(body)
        ok = (p.get("msgtype") == "markdown" and title in p.get("markdown", {}).get("text", "")
              and "T2-1 body 验证" in p.get("markdown", {}).get("text", "")
              and verify_sign(path.split("?")[1] if "?" in path else ""))
        results["T2-1 钉钉出站"] = "PASS" if ok else "FAIL"
        print("T2-1 钉钉:", results["T2-1 钉钉出站"], "| raw:", line[:120], flush=True)
    except Exception as e:
        results["T2-1 钉钉出站"] = "FAIL: %s" % str(e)[:120]
        print("T2-1 异常:", e, flush=True)

    # T2-2 SMTP
    try:
        title2 = "P2-T2-2 SMTP-" + uuid.uuid4().hex[:6]
        # 健康检查：smtp 接收进程是否还活着
        print("  [debug] smtp 进程 alive=%s" % (smtp_proc.poll() is None), flush=True)
        if smtp_proc.poll() is not None:
            print("  [debug] smtp 进程已退出，code=%s" % smtp_proc.poll(), flush=True)
        smtp_cmd("reset")
        s, d = req("POST", "/api/v1/notifications/create?agent_id=%s&type=info&title=%s&body=%s" %
                   (urllib.parse.quote(AGENT), urllib.parse.quote(title2), urllib.parse.quote("T2-2 邮件正文验证")),
                   key)
        assert s == 200, f"创建失败 {s} {d}"
        time.sleep(3)
        mail = smtp_cmd("get")
        print("  [debug] smtp_cmd get 返回 type=%s len=%s 前缀=%s" %
              (type(mail).__name__, len(mail) if isinstance(mail, str) else "-",
               str(mail)[:60] if mail else "EMPTY"), flush=True)
        ok = False
        if isinstance(mail, str) and mail:
            raw = mail
            # aiosmtpd envelope.content 是 raw MIME —— 用 email.parser 解析
            from email import policy
            from email.parser import BytesParser
            try:
                msg = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8", errors="replace"))
                subj = str(msg["Subject"] or "")
                body = msg.get_body(preferencelist=("plain",))
                body_text = body.get_content() if body else ""
                ok = (title2 in subj and "T2-2 邮件正文验证" in body_text)
                print("  subject=%s body含正文=%s" % (subj[:40], "T2-2 邮件正文验证" in body_text), flush=True)
            except Exception as pe:
                print("  MIME 解析失败:", pe, flush=True)
        results["T2-2 邮件出站"] = "PASS" if ok else "FAIL"
        print("T2-2 SMTP:", results["T2-2 邮件出站"], flush=True)
    except Exception as e:
        results["T2-2 邮件出站"] = "FAIL: %s" % str(e)[:120]
        print("T2-2 异常:", e, flush=True)

    # T2-3 故障隔离：停接收端 → 10 条通知
    try:
        ding_proc.kill()
        smtp_cmd("quit")
        time.sleep(1)
        t0 = time.time()
        created = 0
        for i in range(10):
            s, d = req("POST", "/api/v1/notifications/create?agent_id=%s&type=info&title=%s&body=x" %
                       (urllib.parse.quote(AGENT), urllib.parse.quote("T2-3-故障-%d" % i)), key)
            if s == 200:
                created += 1
        elapsed = time.time() - t0
        ok = created == 10 and elapsed < 20
        results["T2-3 故障隔离"] = "PASS" if ok else "FAIL"
        print("T2-3 故障隔离:", results["T2-3 故障隔离"], "| %d/10 耗时 %.1fs" % (created, elapsed), flush=True)
    except Exception as e:
        results["T2-3 故障隔离"] = "FAIL: %s" % str(e)[:120]
        print("T2-3 异常:", e, flush=True)

    # T2-4 全关闭回归：NOTIFY_CHANNELS 为空时通知链路与基线一致
    try:
        # 重启 Hub（config 无 notify_channels）→ 通知创建应正常
        hub_proc.kill()
        time.sleep(1)
        with open(os.path.join(tmpdir, "config", "config.yaml"), "w", encoding="utf-8") as f:
            f.write(
                f"server:\n  port: {HUB_PORT}\n  host: 127.0.0.1\n"
                f"auth:\n  enabled: True\n"
                f"database:\n  path: {os.path.join(tmpdir, 'test.db').replace(chr(92), '/')}\n"
                f"  backup_enabled: False\n"
            )
        env = dict(os.environ)
        env["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
        hub_proc = subprocess.Popen([sys.executable, "main.py"], cwd=PROJECT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(60):
            time.sleep(1)
            try:
                with urllib.request.urlopen(HUB_URL + "/health", timeout=3) as resp:
                    if json.loads(resp.read()).get("database", {}).get("status") == "ok":
                        break
            except Exception:
                pass
        # 通知创建（无渠道配置 → 应正常落库 + WS 推送，无出站）
        t0 = time.time()
        s, d = req("POST", "/api/v1/notifications/create?agent_id=%s&type=info&title=%s&body=x" %
                   (urllib.parse.quote(AGENT), urllib.parse.quote("T2-4-全关")), key)
        elapsed = time.time() - t0
        ok = s == 200 and elapsed < 3
        results["T2-4 全关闭回归"] = "PASS" if ok else "FAIL"
        print("T2-4 全关闭回归:", results["T2-4 全关闭回归"], "| status=%s 耗时 %.2fs" % (s, elapsed), flush=True)
    except Exception as e:
        results["T2-4 全关闭回归"] = "FAIL: %s" % str(e)[:120]
        print("T2-4 异常:", e, flush=True)

    # channel_status 落库检查
    try:
        dbpath = os.path.join(tmpdir, "test.db")
        print("  [debug] 查询 DB 路径=%s 存在=%s" % (dbpath, os.path.exists(dbpath)), flush=True)
        conn = sqlite3.connect(dbpath)
        total = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, channel_status FROM notifications ORDER BY id DESC LIMIT 5").fetchall()
        conn.close()
        print("  [debug] notifications 总数=%d 最近5条: %s" % (total, rows), flush=True)
        # 找 tmpdir 下所有 db 文件
        for f in os.listdir(tmpdir):
            if f.endswith(".db"):
                p = os.path.join(tmpdir, f)
                print("  [debug] tmpdir db 文件: %s (%d bytes)" % (f, os.path.getsize(p)), flush=True)
    except Exception as e:
        print("  [debug] channel_status 查询失败:", e, flush=True)

    print("\n=== T2 结果 ===", flush=True)
    for k, v in results.items():
        print(" ", k, ":", v, flush=True)

    # 统一清理子进程 + 临时目录
    for p in (ding_proc, smtp_proc, hub_proc):
        if p and p.poll() is None:
            p.kill()
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if all("PASS" in v for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
