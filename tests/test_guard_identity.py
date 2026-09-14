"""
① 守卫行为验证 + ② 路由表扫描
验证 guard_agent_identity 在所有 memory 类端点上生效：
- 行为层：Agent B 的 token + 请求 agent_id "A" → 403
- 结构层：扫描 routes.py AST，断言所有 memory/disclosure 类端点声明了该依赖
"""
import urllib.request, urllib.error
import json, ast, pathlib, pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（tests/ 上一级）
HUB = "http://127.0.0.1:3060"


def _api(method, path, data=None, token=None):
    url = f"{HUB}{path}"
    req = urllib.request.Request(url, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


@pytest.fixture(scope="module")
def agents():
    """Register two test agents and return their tokens."""
    _, a = _api("POST", "/api/v1/agents/register", {
        "agent_id": "guard-test-A", "agent_name": "守卫测试A",
        "role": "worker", "department": "test",
    })
    _, b = _api("POST", "/api/v1/agents/register", {
        "agent_id": "guard-test-B", "agent_name": "守卫测试B",
        "role": "worker", "department": "test",
    })
    return {"A": a["api_key"], "B": b["api_key"]}


@pytest.fixture(scope="module")
def memory_setup(agents):
    """Write a test memory for Agent A."""
    _api("POST", "/api/v1/memory/store?agent_id=guard-test-A", {
        "memory_key": "test-fact-1",
        "content": "Agent A 的测试事实",
        "kind": "fact",
        "tags": ["test"],
        "disclosure_level": "summary",
    }, token=agents["A"])


# ── Layer 1: Behavior — cross-agent 403 tests ──

@pytest.mark.parametrize("endpoint,payload", [
    ("/api/v1/memory/search", {"agent_id": "guard-test-A", "query": "test", "limit": 5}),
    ("/api/v1/memory/disclose", {"requester_agent_id": "guard-test-A", "target_agent_id": "guard-test-B",
                                  "query": "test", "required_level": "summary"}),
    ("/api/v1/memory/semantic_search", {"requester_agent_id": "guard-test-A", "query": "test", "limit": 5}),
])
def test_cross_agent_403(agents, memory_setup, endpoint, payload):
    """Agent B 的 token 请求 agent_id 'A' → 必须 403"""
    code, body = _api("POST", endpoint, payload, token=agents["B"])
    detail = body.get("detail", str(body))[:100] if isinstance(body, dict) else str(body)[:100]
    assert code == 403, f"期望 403，实际 {code}: {detail}"
    assert "Forbidden" in str(detail) or "不能" in str(detail), f"403 详情不含禁止语义: {detail}"


def test_memory_search_self_access(agents, memory_setup):
    """Agent A 访问自己的记忆 → 200"""
    code, body = _api("POST", "/api/v1/memory/search", {
        "agent_id": "guard-test-A", "query": "test", "limit": 5,
    }, token=agents["A"])
    assert code == 200, f"合法自访问应 200，实际 {code}: {body}"


def test_memory_list_self_access(agents, memory_setup):
    """Agent A 列举自己的记忆 → 200"""
    code, body = _api("GET", "/api/v1/memory?agent_id=guard-test-A&kind=fact", token=agents["A"])
    assert code == 200, f"合法自访问应 200，实际 {code}: {body}"


# ── Layer 2: Route table scan ──

def test_all_memory_routes_declare_guard():
    """扫描 routes.py AST：所有 memory/disclosure 类端点必须调用 guard_agent_identity
    
    扫描 AsyncFunctionDef（FastAPI 异步路由）和 FunctionDef（同步路由），
    匹配路径含 memory/disclosure/disclose 的端点。
    registration/register 端点不强制要求守卫（公开端点）。
    """
    import pathlib
    # Phase 2 拆分后：memory/disclosure 端点分布在 routes.py 与各 routes_*.py 子模块，全部扫描
    scan_files = [_ROOT / "routes.py"]
    scan_files += sorted(_ROOT.glob("routes_*.py"))

    memory_keywords = ("memory", "disclosure", "disclose")
    skip_prefixes = ("register",)
    # hub-agent 配置类端点（非 per-agent 数据访问，不强制 current_agent 依赖）
    # 注：api_hub_agent_set_rules 等缺认证是已知债，登记在 pitfall #149
    skip_names = ("api_hub_agent_set_rules", "api_hub_agent_get_config",
                  "api_hub_agent_test", "api_hub_agent_audit",
                  # Team proxy endpoints use remote_api_key auth instead of guard_agent_identity
                  "api_team_proxy_disclose",)
    FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)

    unguarded = []
    route_count = 0

    for routes_path in scan_files:
        source = routes_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, FUNC_TYPES):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                # Extract method name from decorator: @app.post(...) → 'post'
                method_name = None
                if hasattr(dec.func, 'attr'):
                    method_name = dec.func.attr
                elif isinstance(dec.func, ast.Attribute):
                    method_name = dec.func.attr

                if method_name not in ('get', 'post', 'put', 'delete', 'patch'):
                    continue

                if not dec.args:
                    continue
                path_arg = dec.args[0]
                if not isinstance(path_arg, ast.Constant):
                    continue
                path_str = path_arg.value
                if not any(kw in path_str.lower() for kw in memory_keywords):
                    continue

                route_count += 1
                func_source = ast.get_source_segment(source, node) or ""
                has_guard = ("guard_agent_identity" in func_source or
                             "get_current_agent" in func_source)

                if not has_guard:
                    if node.name in skip_names:
                        continue
                    if not any(sk in path_str.lower() for sk in skip_prefixes):
                        unguarded.append(f"{node.name} ({path_str}) {routes_path.name} L{node.lineno}")

    print(f"\n  memory 类路由: {route_count} 个")
    if unguarded:
        print(f"  ❌ 缺少守卫: {len(unguarded)} 个")
        for u in unguarded:
            print(f"     - {u}")
    else:
        print(f"  ✅ 所有 memory 类端点均声明 guard_agent_identity")

    assert not unguarded, f"以下端点缺少 guard_agent_identity: {unguarded}"
    assert route_count >= 3, f"期望至少 3 个 memory 路由，实际 {route_count}"

# ════════════════════════════════════════════════════════
#  R3-FIX: 路由存在性断言 — 防"静默消失"
#  扫描器只能发现已注册的路由有没有守卫，
#  发现不了该有的路由没注册。反向断言补齐。
# ════════════════════════════════════════════════════════

EXPECTED_ROUTES = [
    "/api/v1/agents/register",
    "/api/v1/memory/store",
    "/api/v1/memory/disclose",
    "/api/v1/memory/semantic_search",
    "/api/v1/memory/search",
    "/api/v1/tasks/create",
    "/api/v1/tasks/{task_id}/schedule",
    "/api/v1/notifications",
    "/api/v1/agent/workspace",
    "/api/v1/hub-agent/config",
    "/api/v1/dashboard",
    "/api/v1/report/daily",
    "/api/v1/team/members",
    "/api/v1/team/discover",
    "/api/v1/team/ping",
    "/api/v1/team/pair/request",
    "/api/v1/team/pair/accept",
    "/api/v1/automation/jobs",
    "/api/v1/automation/runs",
    "/api/v1/automation/missed",
    "/api/v1/sessions/archive",
    "/api/v1/sessions/recent",
    "/api/v1/sessions/handoff",
]

EXPECTED_ROUTE_METHODS = {
    "/api/v1/memory/disclose": {"POST"},
    "/api/v1/team/pair/request": {"POST"},
    "/api/v1/team/pair/accept": {"POST"},
    "/api/v1/team/members/{member_id}": {"DELETE"},
    "/api/v1/automation/jobs/{job_id}": {"DELETE"},
    "/api/v1/sessions/handoff": {"POST"},
}


def test_route_existence():
    """R3-FIX: 所有预期路由必须在路由表中存在。防幽灵交付。"""
    from routes import app
    registered = set()
    for route in app.routes:
        if hasattr(route, "path"):
            registered.add(route.path)
    missing = [r for r in EXPECTED_ROUTES if r not in registered]
    if missing:
        pytest.fail(f"Missing routes (幽灵交付): {missing}")


def test_team_routes_exist():
    """R3-FIX: team 路由特定断言 — 历史重灾区"""
    from routes import app
    registered = set()
    for route in app.routes:
        if hasattr(route, "path"):
            registered.add(route.path)
    team_routes = [r for r in EXPECTED_ROUTES if "/team/" in r]
    missing = [r for r in team_routes if r not in registered]
    if missing:
        pytest.fail(f"Team routes missing: {missing}")
