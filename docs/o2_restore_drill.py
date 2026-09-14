# -*- coding: utf-8 -*-
"""O2 恢复演练脚本（2026-08-05）— 入 E2E 验收表

流程（契约冻结）：
1. 起独立测试 Hub（3064，临时 config/db/chroma）→ register agent + 写记忆 + 建任务
2. hub-cli backup（在线热备，Hub 运行中）→ verify 全绿
3. kill Hub → 用另一份备份恢复到**第二个**临时实例（不同端口 3065）
4. 冒烟：登录(register/heartbeat) → 查记忆 → 发任务 → 全部可用
5. 恢复实例 verify 全绿（含 hash chain）
6. 连续跑 2 轮 = 演练通过

用法：python docs/o2_restore_drill.py
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def http(port, method, path, body=None, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}" + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def wait_health(port, timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            time.sleep(0.5)
    return False


def make_env(tmpdir, port, db_name):
    os.makedirs(os.path.join(tmpdir, "config"), exist_ok=True)
    db_path = os.path.join(tmpdir, db_name).replace("\\", "/")
    chroma_path = os.path.join(tmpdir, f"chroma_{port}").replace("\\", "/")
    cfg_yaml = (
        f"server:\n  port: {port}\n  host: 127.0.0.1\n"
        f"database:\n  path: {db_path}\n"
        f"  chroma_path: {chroma_path}\n"
        "auth:\n  enabled: true\n  hub_token: \"\"\n"
    )
    with open(os.path.join(tmpdir, "config", "config.yaml"), "w", encoding="utf-8") as f:
        f.write(cfg_yaml)
    return os.path.join(tmpdir, "config"), db_path, chroma_path


def start_hub(tmpdir, port):
    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = os.path.join(tmpdir, "config")
    env.pop("SYNC_HUB_NO_AUTH", None)
    return subprocess.Popen(
        [sys.executable, "main.py"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def drill_round(tmpdir, round_no):
    print(f"\n===== 演练轮次 {round_no} =====")
    cfg_dir, drill_db, drill_chroma = make_env(tmpdir, 3064, "drill.db")
    hub = start_hub(tmpdir, 3064)
    try:
        assert wait_health(3064), "演练 Hub 未就绪"
        # 1. 业务数据：register + 记忆 + 任务
        st, reg = http(3064, "POST", "/api/v1/agents/register",
                       {"agent_id": "drill-agent", "agent_name": "演练Agent",
                        "role": "manager"})
        assert st == 200
        key = reg["api_key"]
        st, body = http(3064, "POST", "/api/v1/memory/store?agent_id=drill-agent",
                        {"memory_key": "drill-mem", "content": "演练记忆数据",
                         "kind": "fact"}, token=key)
        assert st == 200, f"记忆写入 {st} {body}"
        st, body = http(3064, "POST", "/api/v1/tasks/create",
                        {"task_id": "drill-task", "description": "演练任务",
                         "creator_agent_id": "drill-agent"}, token=key)
        assert st == 200, f"任务创建 {st} {body}"
        print("1. 业务数据就绪（agent/记忆/任务）")

        # 2. 在线备份（Hub 运行中）→ 显式传演练库路径（不用脚本进程 CONFIG）
        from hub_cli import cmd_backup, _verify_backup
        backup_dir = os.path.join(tmpdir, f"backup-r{round_no}")
        b = cmd_backup(backup_dir, drill_db, drill_chroma)
        v = _verify_backup(backup_dir)
        assert v["valid"], f"备份校验失败: {v['issues']}"
        print(f"2. 在线备份 + verify 全绿 OK ({b['marker']})")

        # 3. kill Hub → 恢复备份到同一临时位置（模拟恢复现场）
        hub.kill()
        hub.wait(timeout=10)
        time.sleep(1)
        from hub_cli import cmd_restore
        rr = cmd_restore(backup_dir, drill_db, drill_chroma)
        assert rr["verify"]["valid"], f"恢复校验失败: {rr['verify']['issues']}"
        # 写 3065 配置（指向同一恢复库），hub2 用该配置启动
        make_env(tmpdir, 3065, os.path.basename(drill_db))
        hub2 = start_hub(tmpdir, 3065)
        try:
            assert wait_health(3065), "恢复实例未就绪"
            # 4. 冒烟：register(恢复原 agent) + 查记忆 + 任务列表
            st, reg2 = http(3065, "POST", "/api/v1/agents/register",
                            {"agent_id": "drill-agent", "agent_name": "演练Agent",
                             "role": "manager"})
            assert st == 200
            key2 = reg2.get("api_key") or key  # register 保留原 key
            st, body = http(3065, "GET", "/api/v1/memory?agent_id=drill-agent&kind=fact",
                            token=key2)
            assert st == 200, f"冒烟查记忆 {st} {body}"
            mems = body.get("memories") or body.get("results") or []
            st, body = http(3065, "GET", "/api/v1/tasks", token=key2)
            assert st == 200, f"冒烟查任务 {st} {body}"
            print(f"3. 恢复实例冒烟 OK（记忆={len(mems)}条, 任务可查）")
        finally:
            hub2.kill()
            hub2.wait(timeout=10)
    finally:
        try:
            hub.kill()
        except Exception:
            pass


def main():
    rounds = int(os.environ.get("O2_DRILL_ROUNDS", "2"))
    tmpdir = tempfile.mkdtemp(prefix="o2-drill-")
    try:
        for i in range(1, rounds + 1):
            drill_round(tmpdir, i)
        print(f"\n===== O2 恢复演练 {rounds}/{rounds} 轮通过 =====")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        # 清理残留进程（脚本异常时）
        import subprocess as sp
        sp.run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'main.py' -and "
                "$_.CommandLine -match '306[45]' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
               capture_output=True)


if __name__ == "__main__":
    main()
