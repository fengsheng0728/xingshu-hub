# -*- coding: utf-8 -*-
"""S2 HTTP 层端到端验证（独立测试 Hub 3063，临时 config/db）

链路：真实 API 触发事件/披露 → audit_log/disclosure_log 链写入 →
      /api/audit/verify 全绿 → 篡改库 → verify 检出 first_bad_id。
"""
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
HUB_PORT = 3063
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
        "server:\n  port: 3063\n  host: 127.0.0.1\n"
        "database:\n  path: " + db_path + "\n"
        "auth:\n  enabled: true\n  hub_token: \"\"\n"
    )
    with open(os.path.join(tmpdir, "config", "config.yaml"), "w", encoding="utf-8") as f:
        f.write(cfg_yaml)

    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
    env.pop("SYNC_HUB_NO_AUTH", None)
    proc = subprocess.Popen(
        [sys.executable, "main.py"], cwd=str(_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_health(), "Hub 未就绪"
        print("1. Hub 就绪")

        # 2. register 两个 agent（触发 agent_register 事件入链）
        keys = {}
        st, reg = http("POST", "/api/v1/agents/register",
                       {"agent_id": "s2-b", "agent_name": "s2-b",
                        "role": "manager", "managed_agents": ["s2-a"]})
        assert st == 200, f"register s2-b: {st}"
        keys["s2-b"] = reg["api_key"]
        st, reg = http("POST", "/api/v1/agents/register",
                       {"agent_id": "s2-a", "agent_name": "s2-a", "role": "worker"})
        assert st == 200, f"register s2-a: {st}"
        keys["s2-a"] = reg["api_key"]
        print("2. 双 agent register OK (s2-b=manager)")

        # 3. 写记忆 + 披露查询（触发 store_memory + disclosure 审计）
        st, body = http("POST", "/api/v1/memory/store?agent_id=s2-a",
                        {"memory_key": "s2-secret", "content": "S2机密数据",
                         "kind": "fact", "disclosure_level": "summary"},
                        token=keys["s2-a"])
        assert st == 200, f"memory store: {st} {body}"
        print("3. 记忆写入 OK")

        # 4. 触发一次披露请求（s2-b 查 s2-a 的记忆 → disclosure_log 记录）
        st, body = http("POST", "/api/v1/memory/disclose",
                        {"requester_agent_id": "s2-b", "target_agent_id": "s2-a",
                         "query": "机密", "required_level": "summary",
                         "task_id": "s2-task-1"},
                        token=keys["s2-b"])
        assert st == 200, f"disclose: {st} {body}"
        print("4. 披露请求 OK")

        # 5. 验证链已写入（audit_log + disclosure_log hash 列）
        conn = sqlite3.connect(db_path)
        n_audit = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        n_disc = conn.execute(
            "SELECT count(*) FROM disclosure_log WHERE entry_hash != ''").fetchone()[0]
        conn.close()
        assert n_audit > 0, "audit_log 空"
        assert n_disc > 0, "disclosure_log 无 hash"
        print(f"5. 链写入 OK (audit_log={n_audit}, disclosure_hash={n_disc})")

        # 6. /api/audit/verify 全绿
        st, res = http("POST", "/api/audit/verify", {"end_id": 0}, token=keys["s2-a"])
        assert st == 200, f"verify: {st}"
        assert res["valid"] is True, f"verify 应全绿: {res}"
        print(f"6. verify 全绿 OK (checked_total={res.get('checked_total')})")

        # 7. 篡改 disclosure_log → verify 检出
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE disclosure_log SET disclosed_content='被篡改' WHERE entry_hash != ''")
        conn.commit()
        conn.close()
        st, res2 = http("POST", "/api/audit/verify", {"end_id": 0}, token=keys["s2-a"])
        assert st == 200
        assert res2["valid"] is False, "篡改后应 invalid"
        assert res2["chains"]["disclosure_log"]["first_bad_id"] is not None, "应定位 first_bad_id"
        print(f"7. 篡改检出 OK (first_bad_id={res2['chains']['disclosure_log']['first_bad_id']})")

        # 8. 篡改 audit_log 主链 → verify 检出
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE audit_log SET payload='{\"evil\":true}' WHERE log_id=1")
        conn.commit()
        conn.close()
        st, res3 = http("POST", "/api/audit/verify", {"end_id": 0}, token=keys["s2-a"])
        assert res3["valid"] is False
        assert res3["chains"]["audit_log"]["first_bad_id"] is not None
        print(f"8. 主链篡改检出 OK (first_bad_id={res3['chains']['audit_log']['first_bad_id']})")

        print("\n===== S2 HTTP 层验证 8/8 全绿 =====")
    finally:
        proc.kill()
        proc.wait(timeout=10)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
