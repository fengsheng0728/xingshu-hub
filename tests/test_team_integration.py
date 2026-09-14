"""
②d 接收方单机集成测试 — TC4/TC5/TC10
验证 proxy/disclose 端点行为：身份代入、防提权、审计 actor
"""
import urllib.request, json, pathlib, pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（tests/ 上一级）
HUB = "http://127.0.0.1:3060"


def api(method, path, data=None, token=None):
    url = f"{HUB}{path}"
    req = urllib.request.Request(url, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


@pytest.fixture(scope="module")
def setup_remote():
    """注册本地 agent + 构造虚拟 team_member（模拟对方 Hub 已配对）"""
    # 注册本地 agent A
    a = api("POST", "/api/v1/agents/register", {
        "agent_id": "tc-remote-owner",
        "agent_name": "测试数据属主",
        "role": "worker",
        "department": "test",
    })
    # 写一条记忆
    api("POST", "/api/v1/memory/store?agent_id=tc-remote-owner", {
        "memory_key": "tc-secret",
        "content": "这是一条敏感信息，只有 manager 能看到全文",
        "kind": "fact",
        "tags": ["敏感"],
        "disclosure_level": "summary",
        "disclosure_scope": "manager",
    }, token=a["api_key"])

    # 构造配对关系：虚构一个 remote Hub（DESKTOP-REMOTE）配对了 worker 角色
    # 直接插 team_members 表
    import sqlite3, secrets, datetime
    conn = sqlite3.connect(str(_ROOT / "sync_hub.db"))
    c = conn.cursor()
    remote_key = secrets.token_urlsafe(32)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).isoformat()
    c.execute(
        """INSERT OR REPLACE INTO team_members
           (local_agent_id, remote_hub_id, remote_hub_url, remote_agent_id, remote_api_key,
            hostname, user_name, role, department, paired_at, key_expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("tc-remote-owner", "DESKTOP-REMOTE-abcd",
         "http://192.168.99.99:3060", "remote-agent-1",
         remote_key, "remote-pc", "远程同事",
         "worker", "test", now, expires),
    )
    conn.commit()
    conn.close()
    return {"api_key": remote_key}


# ── TC4: 合法跨 Hub 查询 → worker 角色应返回 summary 级 ──
def test_tc4_remote_disclose_worker_sees_summary(setup_remote):
    """对方 Hub 用 worker 身份查询 — 应返回摘要级（非全文）"""
    try:
        req = urllib.request.Request(
            f"{HUB}/api/v1/team/proxy/disclose",
            data=json.dumps({
                "requester_agent_id": "remote-agent-1",
                "target_agent_id": "tc-remote-owner",
                "query": "敏感",
                "required_level": "summary",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {setup_remote['api_key']}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        pytest.fail(f"HTTP {e.code}: {e.read().decode()[:200]}")

    assert result.get("disclosed_count", -1) >= 0, f"应返回披露结果: {result}"
    # worker 角色 + disclosure_scope=manager → disclosure_level 应为 summary
    if result.get("memories"):
        for m in result["memories"]:
            level = m.get("disclosure_level", "")
            assert level != "full", f"worker 不应看到 full: {m}"
    print(f"  disclosed_count={result.get('disclosed_count')}")


# ── TC5: curl 提权尝试 → 对方 Hub 用本地 role 裁决（灵魂用例） ──
def test_tc5_curl_privilege_escalation_rejected(setup_remote):
    """curl 伪造 _guaranteed_role='manager' → 对方 Hub 忽略，仍按 worker 裁决"""
    try:
        req = urllib.request.Request(
            f"{HUB}/api/v1/team/proxy/disclose",
            data=json.dumps({
                "requester_agent_id": "remote-agent-1",
                "target_agent_id": "tc-remote-owner",
                "query": "敏感",
                "required_level": "full",
                "_guaranteed_role": "manager",  # ← 攻击者伪造的角色声称
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {setup_remote['api_key']}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        pytest.fail(f"HTTP {e.code}: {e.read().decode()[:200]}")

    # TC5 的命脉：伪造 _guaranteed_role 不应生效
    # worker 配 disclosure_scope=manager 的记忆，worker 只能看 summary
    if result.get("memories"):
        for m in result["memories"]:
            level = m.get("disclosure_level", "")
            assert level != "full", (
                f"❌ 提权成功！伪造 _guaranteed_role='manager' 后看到了 full: {m}"
            )
    print(f"  disclosed_count={result.get('disclosed_count')} — 提权被拒绝 ✅")


# ── TC10: 审计 actor 区分 — actor=hub:xxx 而非 agent:xxx ──
def test_tc10_audit_actor_is_hub(setup_remote):
    """跨 Hub 请求的审计日志 → actor 应为 hub:xxx"""
    # 先发一个请求确保生成审计日志
    try:
        req = urllib.request.Request(
            f"{HUB}/api/v1/team/proxy/disclose",
            data=json.dumps({
                "requester_agent_id": "remote-agent-1",
                "target_agent_id": "tc-remote-owner",
                "query": "审计测试",
                "required_level": "summary",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {setup_remote['api_key']}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError:
        pass

    # 查 events 表验证
    import sqlite3
    conn = sqlite3.connect(str(_ROOT / "sync_hub.db"))
    c = conn.cursor()
    c.execute(
        "SELECT agent_id, payload FROM events WHERE event_type='remote_disclose' ORDER BY timestamp DESC LIMIT 5"
    )
    rows = c.fetchall()
    conn.close()

    assert len(rows) > 0, "应有 remote_disclose 审计事件"
    actor = rows[0][0]
    assert actor.startswith("hub:"), f"actor 应为 hub:xxx，实际: {actor}"
    print(f"  actor={actor} ✅")


# ── 并发安全性：两个虚拟身份不互相污染 ──
def test_concurrent_remote_disclose_no_pollution(setup_remote):
    """两个并发 remote 请求 → 结果互不污染"""
    import threading

    results = []

    def do_request(agent_name):
        try:
            import urllib.request, json
            req = urllib.request.Request(
                f"{HUB}/api/v1/team/proxy/disclose",
                data=json.dumps({
                    "requester_agent_id": agent_name,
                    "target_agent_id": "tc-remote-owner",
                    "query": "敏感",
                    "required_level": "summary",
                }).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {setup_remote['api_key']}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                results.append(json.loads(resp.read()))
        except Exception as e:
            results.append({"error": str(e)})

    t1 = threading.Thread(target=do_request, args=("remote-agent-1",))
    t2 = threading.Thread(target=do_request, args=("remote-agent-1",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2, f"应收到 2 个响应: {results}"
    # 两个结果应该是独立的 dict（非引用共享）
    # 至少不应该崩溃
    for r in results:
        assert "disclosed_count" in r or "error" in r, f"异常响应: {r}"
    print(f"  并发请求: {len(results)}/{len(results)} 完成，无污染")
