# -*- coding: utf-8 -*-
"""CD-034 R3：审计链头外部时间戳（RFC3161 TSA）盖章 + 回拉比对 + 不一致告警

背景（2026-09-20，用户拍板「全做」三件之③，接 CD-034 R1/R2）：
known_limitations L2-ANCHOR-001 原文指出：本地 audit/anchor.txt 与链同机同目录，
有本机写权限者可同时改链与锚（循环论证），且全仓没有「把外部锚取回比对链头」的机制。
本项补上：把链头交给**信任域之外**的公共 RFC3161 时间戳服务盖章（.tsq/.tsr 落盘 +
index 记录），校验时回拉比对——被盖章的那个链头节点若不在现链中，即判定「链被整段
重写/截断」并告警。

语义要点（避免误报）：链头随每次写入变化，所以「当前链头 == 盖章值」只在盖章瞬间成立。
正确判据是「**被盖章的链头节点仍存在于链中**」——本文件用 T4/T5 一对用例把这条钉住。

网络约束：T1-T4 只连本文件自起的 127.0.0.1 随机端口（假 TSA，确定性）；
真实公共 TSA 只在 T6 里试，连不上就 skip（明确标注「不可达 ≠ 通过」）。
"""
import hashlib
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import models  # noqa: E402
from audit_chain import AuditChain  # noqa: E402

PUBLIC_TSA = "https://rfc3161.ai.moda/"


# ── 假 TSA（确定性，不出网） ──


class _FakeTSAHandler(BaseHTTPRequestHandler):
    """把请求体（.tsq）记下来，回一段假 token 字节 + 200"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        with self.server.lock:
            self.server.received.append(
                {"body": body, "content_type": self.headers.get("Content-Type")})
        payload = b"FAKE-TSR-" + hashlib.sha256(body).hexdigest().encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/timestamp-reply")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_tsa():
    srv = HTTPServer(("127.0.0.1", 0), _FakeTSAHandler)
    srv.received = []
    srv.lock = threading.Lock()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/tsa"
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


# ── 临时库 + 链 ──


@pytest.fixture()
def chain_db(tmp_path, monkeypatch):
    db = str(tmp_path / "tsa_test.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL DEFAULT '',
            ref_table TEXT DEFAULT '',
            ref_id TEXT DEFAULT '',
            payload TEXT DEFAULT '',
            prev_hash TEXT NOT NULL DEFAULT '',
            entry_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT)""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db)
    ac = AuditChain(db)
    for i in range(1, 4):
        ac.append("event", "events", f"tsa-{i}", {"k": f"v{i}"})
    out = str(tmp_path / "tsa_out")
    return {"db": db, "out": out, "tmp": str(tmp_path)}


def _tail(db):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY log_id DESC LIMIT 1").fetchone()
    conn.close()
    return row[0]


# ── 用例 ──


def test_tsa_unreachable_is_safe(chain_db):
    """TSA 不可达：不抛、不写坏产物、链不动（审计路径永不阻塞主链路）"""
    from audit_chain import tsa_stamp
    head_before = _tail(chain_db["db"])
    res = tsa_stamp(chain_db["db"], "http://127.0.0.1:9/none", out_dir=chain_db["out"],
                    timeout=3)
    assert res.get("status") != "ok", f"不可达不应报 ok: {res}"
    assert res.get("anchor") == head_before, "应仍返回链头（便于排障）"
    assert _tail(chain_db["db"]) == head_before, "链不得被改动"
    idx = os.path.join(chain_db["out"], "index.jsonl")
    if os.path.exists(idx):
        rows = [json.loads(l) for l in open(idx, encoding="utf-8") if l.strip()]
        assert all(r.get("status") != "ok" for r in rows), "失败记录不应标 ok"


def test_stamp_writes_artifacts_and_verifies(chain_db, fake_tsa):
    """盖章闭环：.tsq/.tsr 落盘 + index 记 imprint + 回拉比对通过"""
    from audit_chain import tsa_stamp, verify_tsa
    head = _tail(chain_db["db"])
    res = tsa_stamp(chain_db["db"], fake_tsa, out_dir=chain_db["out"])
    assert res.get("status") == "ok", f"盖章应成功: {res}"
    assert res["anchor"] == head
    assert os.path.isfile(res["tsq"]) and os.path.isfile(res["tsr"]), res
    assert os.path.getsize(res["tsr"]) > 0
    # imprint = SHA256(链头字符串)，用于回拉比对
    assert res["imprint"] == hashlib.sha256(head.encode()).hexdigest(), res

    idx = os.path.join(chain_db["out"], "index.jsonl")
    rows = [json.loads(l) for l in open(idx, encoding="utf-8") if l.strip()]
    assert rows and rows[-1]["anchor"] == head and rows[-1]["status"] == "ok"

    v = verify_tsa(chain_db["db"], out_dir=chain_db["out"])
    assert v["valid"] is True, f"刚盖完章应 valid: {v}"
    assert v["checked"] >= 1


def test_chain_growth_no_false_alarm(chain_db, fake_tsa):
    """链正常增长不误报：盖章后继续追加条目 → 被盖章节点仍在链中 → valid"""
    from audit_chain import tsa_stamp, verify_tsa
    tsa_stamp(chain_db["db"], fake_tsa, out_dir=chain_db["out"])
    ac = AuditChain(chain_db["db"])
    ac.append("event", "events", "tsa-after-stamp", {"k": "later"})
    assert _tail(chain_db["db"]) != json.loads(
        open(os.path.join(chain_db["out"], "index.jsonl"), encoding="utf-8")
        .readline())["anchor"], "前置条件：链头应已变化"
    v = verify_tsa(chain_db["db"], out_dir=chain_db["out"])
    assert v["valid"] is True, f"链正常增长不得告警: {v}"


def test_whole_chain_rewrite_detected(chain_db, fake_tsa):
    """整段重写检测：被盖章的链头节点从链中消失 → valid False + reason"""
    from audit_chain import tsa_stamp, verify_tsa
    tsa_stamp(chain_db["db"], fake_tsa, out_dir=chain_db["out"])
    assert verify_tsa(chain_db["db"], out_dir=chain_db["out"])["valid"] is True

    # 模拟「有写权限的人整段重写」：清空旧链，重建一条新链（旧链头节点不复存在）
    conn = sqlite3.connect(chain_db["db"])
    conn.execute("DELETE FROM audit_log")
    conn.commit()
    conn.close()
    ac = AuditChain(chain_db["db"])
    ac.append("event", "events", "rewritten-1", {"k": "forged"})
    ac.append("event", "events", "rewritten-2", {"k": "forged"})

    v = verify_tsa(chain_db["db"], out_dir=chain_db["out"])
    assert v["valid"] is False, f"整段重写必须被抓出: {v}"
    assert v.get("mismatches"), f"应给出不一致明细: {v}"
    reasons = json.dumps(v["mismatches"], ensure_ascii=False)
    assert "rewrite" in reasons or "missing" in reasons, reasons


def test_tsa_dir_defaults_under_audit(chain_db, fake_tsa):
    """默认输出目录：<repo>/audit/tsa（不传 out_dir 时）"""
    from audit_chain import tsa_stamp, TSA_DIR
    res = tsa_stamp(chain_db["db"], fake_tsa)
    assert os.path.normcase(os.path.abspath(res["tsq"])).startswith(
        os.path.normcase(os.path.abspath(TSA_DIR))), f"默认目录应在 audit/tsa: {res}"
    for p in (res["tsq"], res["tsr"],
              os.path.join(TSA_DIR, "index.jsonl")):
        try:
            os.remove(p)
        except Exception:
            pass


def test_real_public_tsa_stamp(chain_db):
    """真实公共 RFC3161 TSA 盖章（联网用例；不可达则 skip——skip ≠ 通过）"""
    from audit_chain import tsa_stamp
    url = os.environ.get("SYNC_HUB_TSA_URL", PUBLIC_TSA)
    res = tsa_stamp(chain_db["db"], url, out_dir=chain_db["out"], timeout=20)
    if res.get("status") != "ok":
        pytest.skip(f"公共 TSA 不可达（网络窗口），非通过项: {res.get('error')}")
    assert os.path.getsize(res["tsr"]) > 100, f"真 token 不应只有几字节: {res}"
    with open(res["tsr"], "rb") as f:
        blob = f.read()
    assert bytes.fromhex(res["imprint"]) in blob, \
        "TSA token 内应含我们提交的 messageImprint（回拉比对的锚）"


# ═══════════════════════════════════════════════════════════════
# 端点 / 告警接线（真实 Hub 进程 :3074 + 假 TSA，端到端真实路径）
# ═══════════════════════════════════════════════════════════════

import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

HUB_PORT = 3074
HUB_TOKEN = "tsa-e2e-hub-token"
MGR = "tsa-mgr"
WORKER = "tsa-worker"
BASE = f"http://127.0.0.1:{HUB_PORT}"


def _write_yaml(path, cfg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        __import__("yaml").dump(cfg, f, allow_unicode=True)


def _req(method, path, token="", body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"err": str(e)[:150]}


def _cli(args):
    r = subprocess.run([sys.executable, "hub_cli.py"] + args, cwd=REPO_ROOT,
                       capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        out = {"raw": r.stdout[:200]}
    out["_rc"] = r.returncode
    return out


def _kill_tree(proc):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def _db(db, sql, params=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def tsa_hub():
    """真实 Hub（guarded + hub_token + audit.tsa 开启指向本文件假 TSA）"""
    srv = HTTPServer(("127.0.0.1", 0), _FakeTSAHandler)
    srv.received = []
    srv.lock = threading.Lock()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake_url = f"http://127.0.0.1:{srv.server_port}/tsa"

    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="tsa-hub-")
    cfg_dir = os.path.join(tmpdir, "config")
    db = os.path.join(tmpdir, "tsa.db")
    _write_yaml(os.path.join(cfg_dir, "config.yaml"), {
        "server": {"host": "127.0.0.1", "port": HUB_PORT},
        "auth": {"enabled": True, "registration": "guarded", "hub_token": HUB_TOKEN},
        "database": {"path": db, "backup_enabled": False},
        "logging": {"level": "warning"},
        # anchor_interval=2s：启动那次盖章时审计链还可能是空的（Hub 尚未落任何事件），
        # 故用短周期让 loop 重试——这是真实部署（默认 3600s）同一路径的加速版
        "audit": {"anchor_interval": 2,
                  "tsa": {"enabled": True, "url": fake_url, "interval": 0}},
    })
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = cfg_dir
    env["SYNC_HUB_CHROMA_PATH"] = os.path.join(tmpdir, "chroma_db")
    # 盖章目录隔离：默认目录是仓库级 audit/tsa，跨库残留会误判「链被重写」
    env["SYNC_HUB_TSA_DIR"] = os.path.join(tmpdir, "tsa")
    env.pop("SYNC_HUB_NO_AUTH", None)
    log_path = os.path.join(tmpdir, "hub.log")
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=REPO_ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    ok = False
    for _ in range(80):
        time.sleep(0.5)
        status, _b = _req("GET", "/health")
        if status == 200:
            ok = True
            break
    if not ok:
        _kill_tree(proc)
        raise RuntimeError(f"TSA Hub 未就绪: {open(log_path, encoding='utf-8', errors='replace').read()[-500:]}")

    creds = {}
    for aid, role in ((MGR, "manager"), (WORKER, "worker")):
        cli = _cli(["agent", "create", "--id", aid, "--name", aid, "--role", role,
                    "--db", db])
        assert cli.get("status") == "created", cli
        creds[aid] = cli["api_key"]
        code, res = _req("POST", "/api/v1/agents/register", token=HUB_TOKEN,
                         body={"agent_id": aid, "agent_name": aid, "role": role,
                               "department": "audit", "capabilities": []})
        assert code in (200, 201), f"register {aid} 失败 {code}: {res}"
    yield {"db": db, "tmpdir": tmpdir, "creds": creds, "fake_url": fake_url,
           "tsa_dir": os.path.join(tmpdir, "tsa")}
    _kill_tree(proc)
    srv.shutdown()
    srv.server_close()
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_live_loop_stamps_and_status(tsa_hub):
    """loop 接线（真实盖章周期）：链非空后 loop 盖章 → status 回拉比对通过"""
    idx = os.path.join(tsa_hub["tsa_dir"], "index.jsonl")
    rows = []
    deadline = time.time() + 30
    while time.time() < deadline:
        if os.path.isfile(idx):
            rows = [json.loads(l) for l in open(idx, encoding="utf-8") if l.strip()]
            if any(r.get("status") == "ok" and r.get("tsa_url") == tsa_hub["fake_url"]
                   for r in rows):
                break
        time.sleep(1)
    ok_rows = [r for r in rows if r.get("status") == "ok"
               and r.get("tsa_url") == tsa_hub["fake_url"]]
    assert ok_rows, f"loop 应在链非空后盖章（{idx}）: {rows[-3:]}"

    code, res = _req("GET", "/api/audit/anchor/status", token=tsa_hub["creds"][MGR])
    assert code == 200, f"status 应 200: {code} {res}"
    assert res["tsa"]["valid"] is True, f"刚盖完章应 valid: {res['tsa']}"
    assert res["tsa"]["checked"] >= 1
    assert res["tsa_enabled"] is True


def test_live_manual_stamp_endpoint(tsa_hub):
    """手动盖章端点：manager 放行 + 落 ops_trigger 审计"""
    code, res = _req("POST", "/api/audit/anchor/stamp", token=tsa_hub["creds"][MGR])
    assert code == 200, f"manager 盖章应 200: {code} {res}"
    assert res["status"] == "ok" and res["bytes"] > 0
    assert os.path.isfile(res["tsr"]), f"tsr 应落盘: {res}"
    assert res["verify"]["valid"] is True
    rows = _db(tsa_hub["db"],
               "SELECT payload FROM events WHERE event_type = 'ops_trigger' "
               "AND payload LIKE '%anchor/stamp%'")
    assert rows, "成功触发应落 ops_trigger 审计"


def test_live_worker_denied_and_audited(tsa_hub):
    """重运维门：worker 触发盖章 → 403 + ops_gate_denied 审计（不静默）"""
    code, res = _req("POST", "/api/audit/anchor/stamp", token=tsa_hub["creds"][WORKER])
    assert code == 403, f"worker 应 403: {code} {res}"
    rows = _db(tsa_hub["db"],
               "SELECT payload FROM events WHERE event_type = 'ops_gate_denied' "
               "AND payload LIKE '%anchor/stamp%'")
    assert rows, "拒绝必须落 ops_gate_denied 审计"


def test_live_whole_chain_rewrite_triggers_alarm(tsa_hub):
    """整段重写 → status 报不一致 + events anchor_mismatch + dashboard 安全通知（最后跑）"""
    assert _req("GET", "/api/audit/anchor/status",
                token=tsa_hub["creds"][MGR])[1]["tsa"]["valid"] is True, "前置：重写前应 valid"
    conn = sqlite3.connect(tsa_hub["db"])
    conn.execute("DELETE FROM audit_log")
    conn.commit()
    conn.close()
    code, res = _req("GET", "/api/audit/anchor/status", token=tsa_hub["creds"][MGR])
    assert code == 200
    assert res["tsa"]["valid"] is False, f"整段重写必须报不一致: {res['tsa']}"
    msgs = [r["payload"] for r in _db(
        tsa_hub["db"], "SELECT payload FROM events WHERE event_type = 'anchor_mismatch'")]
    assert msgs, "不一致必须落 anchor_mismatch 审计"
    notifs = _db(tsa_hub["db"],
                 "SELECT title FROM notifications WHERE source = 'audit_anchor'")
    assert notifs, "不一致必须通知 dashboard（source=audit_anchor）"
