# -*- coding: utf-8 -*-
"""CD-091：WS 通道 memory_search 死路径修复 验收测试（独立进程真实 Hub 3064）

背景（2026-09-22 实测）：WS `request` 的 `method=memory_search` 走
routes_ws._handle_ws_request 的 fallback 分支，调了不存在的
`hub.search_memory(agent_id, query, limit)`——真实方法名是
`hub.memory_search(req)`（hub_mixins/memory.py，入参模型与 REST
/memory/search 同款）→ 恒返回 {"error":"'SyncHub' object has no
attribute 'search_memory'"}。

先红口径：不经 REST /memory/search 绕道，直接走 WS request 通道断言
真实返回行（results 含刚写入的记忆），修前必红、修后必绿。

W-1 写入 → WS memory_search → response.result.results 含该行（先红核心）
W-2 无命中查询 → result.results 为空列表而非 error 帧（区分「死路径」与「真空」）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

import pytest
import websocket  # websocket-client

HUB_PORT = 3064
HUB_TOKEN = "test-token-cd091"
AGENT_ID = "cd091-agent"
AGENT_ID_EMPTY = "cd091-agent-empty"
# unicode61 把连续中文 run 当单 token——查询词取整段 run 才能 FTS MATCH；
# LIKE 兜底同样子串命中（同 tests/test_memory_fts_sync.py 口径）
TOKEN_RUN = "星枢检索验收专用标记词甲"


def _http(method, path, token, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{HUB_PORT}{path}", method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _env(type_, payload):
    return {"type": type_, "id": uuid.uuid4().hex, "session_id": "",
            "via": "ws", "ts": int(time.time() * 1000), "version": 2,
            "payload": payload}


def _ws_memory_search(api_key, query, limit=10, agent_id=AGENT_ID):
    """WS request 通道跑一次 memory_search，返回 response 帧的 payload。"""
    ws = websocket.create_connection(
        f"ws://127.0.0.1:{HUB_PORT}/ws/{agent_id}", timeout=5)
    try:
        ws.send(json.dumps({"type": "auth", "token": api_key}))
        time.sleep(0.3)  # 等首帧鉴权通过（无 ack 帧，失败会被 4401 关掉）
        rid = uuid.uuid4().hex
        req = _env("request", {"method": "memory_search",
                               "params": {"query": query, "limit": limit}})
        req["id"] = rid
        ws.send(json.dumps(req))
        deadline = time.time() + 10
        while time.time() < deadline:
            ws.sock.settimeout(max(0.1, deadline - time.time()))
            frame = json.loads(ws.recv())
            pl = frame.get("payload", {})
            if frame.get("type") == "response" and pl.get("id") == rid:
                return pl
        raise AssertionError("10s 内未收到 memory_search 的 response 帧")
    finally:
        try:
            ws.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def hub_process():
    tmpdir = tempfile.mkdtemp(prefix="cd091-")
    cfg_dir = os.path.join(tmpdir, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = {
        "server": {"host": "127.0.0.1", "port": HUB_PORT},
        "auth": {"enabled": True, "hub_token": HUB_TOKEN},
        "database": {"path": os.path.join(tmpdir, "test.db"),
                     "backup_enabled": False,
                     "chroma_path": os.path.join(tmpdir, "chroma_db")},
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
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{HUB_PORT}/health", timeout=2) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            continue
    if not ok:
        proc.kill()
        raise RuntimeError("Hub failed to start within 40s")

    # 注册 agent 拿 api_key（配了 hub_token 的部署 register 必须带它，契约 §1）
    status, reg = _http("POST", "/api/v1/agents/register", HUB_TOKEN,
                        {"agent_id": AGENT_ID, "agent_name": "CD-091 验收",
                         "role": "worker"})
    assert status == 200 and reg.get("api_key"), f"注册失败: {status} {reg}"
    api_key = reg["api_key"]

    # REST 写入一条记忆（写读闭环的「写」走 REST，「读」走被测的 WS 通道）
    status, _ = _http("POST", f"/api/v1/memory/store?agent_id={AGENT_ID}",
                      api_key,
                      {"memory_key": "cd091-mem",
                       "content": f"CD-091 验收内容 {TOKEN_RUN} 写入于测试",
                       "kind": "fact", "tags": ["cd091"]})
    assert status == 200, f"memory/store 失败: {status}"

    # 第二个 agent（零记忆）——W-2 用：语义 top-k 对唯一记忆也会返回（无相关度
    # 阈值），「无命中」口径只能在零记忆主体上成立
    status, reg2 = _http("POST", "/api/v1/agents/register", HUB_TOKEN,
                         {"agent_id": AGENT_ID_EMPTY,
                          "agent_name": "CD-091 验收·空", "role": "worker"})
    assert status == 200 and reg2.get("api_key"), f"注册失败: {status} {reg2}"

    yield api_key, reg2["api_key"]
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_w1_ws_memory_search_returns_stored_row(hub_process):
    """W-1（先红核心）：WS memory_search 真返回刚写入的行，而非 error 帧。"""
    api_key, _ = hub_process
    pl = _ws_memory_search(api_key, TOKEN_RUN)
    assert "error" not in pl, f"WS memory_search 返回 error：{pl.get('error')}"
    result = pl.get("result") or {}
    rows = result.get("results") or []
    assert any(r.get("memory_key") == "cd091-mem" for r in rows), \
        f"结果里找不到刚写入的记忆：{json.dumps(result, ensure_ascii=False)[:300]}"


def test_w2_ws_memory_search_no_hit_is_empty_not_error(hub_process):
    """W-2：零记忆主体查询 → result.results == []，不得伪装成 error。"""
    _, api_key_empty = hub_process
    pl = _ws_memory_search(api_key_empty, "绝不存在的查询词zzqq",
                           agent_id=AGENT_ID_EMPTY)
    assert "error" not in pl, f"空命中被报成 error：{pl.get('error')}"
    result = pl.get("result") or {}
    assert result.get("results") == [], \
        f"空命中应返回空列表：{json.dumps(result, ensure_ascii=False)[:300]}"
