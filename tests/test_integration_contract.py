# -*- coding: utf-8 -*-
"""CD-079 后半 · 集成方接入契约的**机器校验**（契约文档 = docs/integration-contract.md）

为什么要有这个文件：契约文档是给**外部接入方**的承诺。我们日后一次「顺手重命名端点」
或「响应少回一个字段」，文档不会自己红，接入方会先崩。这里把文档里写死的东西变成断言：

  C-1 信封 wire 形状（§3.1）：键集合/类型/version=2/中文不转义
  C-2 信封拒收规则（§3.1/§8）：version<2、缺 type|id|ts、payload 键与信封键重名 → 一律不收
  C-3 黄金线形状（tests/data/contract/wire_shapes.json，由 scripts/contract_capture.py 从
      **真实调用**生成）：方向是「夹具的键**必须仍在**实际响应里 + 类型一致」——
      **允许新增键（向后兼容），不许删键/改类型（破坏性）**，与 §8 承诺一致
  C-4 契约端点存在性（§2 表）：文档里列出的每条路径都必须在 app 路由表里真的存在
  C-5 fail-closed 边界（§1 路径 B 表）：凭据/配置类端点前缀必须仍在硬拒名单里
  C-6 公开入口（§2）：/docs 与 /openapi.json 必须在认证豁免前缀里（文档承诺「无凭据可打开」）
  C-7 文档承诺的默认值（§3.2/§5/§8）：AGENT_MIN_VERSION / WS 首帧超时 / 限流默认 / 配额字段
  C-8 WS 首帧鉴权契约（§1 WS 段）：非 auth 帧 / 空 token / 错 token → close 4401

口径：本文件**不起端口、不 spawn Hub**（沿用 tests/test_403_policy_matrix.py 的直调约定）；
线上状态码级的行为（401/403/422/4401 真连通）由 `scripts/contract_negative_probe.sh` 承担，
CI 里两条一起跑。改契约文档时**同时**改本文件，否则文档会静默漂移。
"""
import asyncio
import io
import json
import os
import re

import pytest

import db
import envelope
import hub_core
from models import AgentRegistration, MemoryEntry, TaskCreate
from routes_memory import MemorySearchRequest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_DOC = os.path.join(ROOT, "docs", "integration-contract.md")
FIXTURE = os.path.join(ROOT, "tests", "data", "contract", "wire_shapes.json")

_TYPES = {"str": str, "int": int, "float": (int, float), "bool": bool, "null": type(None)}


# ─────────────────────────── 工具 ───────────────────────────

def _load_fixture() -> dict:
    with io.open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _shape(obj):
    """与 scripts/contract_capture.py 保持同一口径（两边必须一致，否则比对无意义）。"""
    if isinstance(obj, dict):
        return {k: _shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return ["<list>", _shape(obj[0]) if obj else None]
    if obj is None:
        return "null"
    return type(obj).__name__


def _assert_compatible(expected, actual, path=""):
    """夹具 ⊆ 实际：键必须都在、类型必须一致、列表元素形状必须一致；多出来的键放行。"""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), "%s：契约承诺对象，实际 %s" % (path, type(actual).__name__)
        for k, v in expected.items():
            assert k in actual, "%s.%s：契约承诺的键在实际响应里消失了（破坏性变更，须走版本登记）" % (path, k)
            _assert_compatible(v, actual[k], "%s.%s" % (path, k))
    elif isinstance(expected, list):
        assert isinstance(actual, list), "%s：契约承诺列表，实际 %s" % (path, type(actual).__name__)
        if expected and expected[1] is not None and actual:
            _assert_compatible(expected[1], actual[0], "%s[0]" % path)
    elif expected == "null":
        return  # 值允许为 null，不约束
    elif expected == "bool":
        assert isinstance(actual, bool), "%s：期望 bool，实际 %s" % (path, type(actual).__name__)
    elif expected == "int":
        assert isinstance(actual, int) and not isinstance(actual, bool), \
            "%s：期望 int，实际 %s" % (path, type(actual).__name__)
    else:
        assert isinstance(actual, _TYPES[expected]), \
            "%s：期望 %s，实际 %s" % (path, expected, type(actual).__name__)


def _contract_paths():
    """从契约文档表格里的端点清单抽出路径（含 `{a|b|c}` 交替展开）。"""
    text = io.open(CONTRACT_DOC, encoding="utf-8").read().replace("\\|", "|")  # 表格里转义的竖线
    # 只取「动作 + /api/v1/...」的形式，避免把散文里的引用也当成承诺
    raw = re.findall(r"\b(?:GET|POST|PUT|PATCH|DELETE|WS)\s+(/api/v1/[^\s`、）)]+)", text)
    out = set()
    for p in raw:
        p = p.rstrip(".,;:\\")
        if "*" in p:            # `/api/v1/audit/*` 这类是范围引用，不是具体端点
            continue
        if "|" in p and not re.search(r"\{[^{}]*\|", p):
            p = p.split("|")[0]     # markdown 表格的分隔竖线泄漏出来，截断
        m = re.search(r"\{([^{}]*\|[^{}]*)\}", p)
        if m:
            # 交替项是**字面路径段**（`{schedule|start|…}` 展开成 `/tasks/{id}/schedule`），
            # 不是路径参数——不能加花括号，否则归一化后与真实路由对不上。
            for alt in m.group(1).split("|"):
                out.add(p[:m.start()] + alt + p[m.end():])
        else:
            out.add(p)
    # 普通引用的路径（`/api/v1/keys` 这类写在正文/表格里的）也纳入
    for p in re.findall(r"`(/api/v1/[a-z0-9_{}/.\-]+)`", text):
        out.add(p)
    # §1 的 fail-closed 边界表列的是**前缀**（覆盖其下所有子路径），不是具体端点——
    # 它们由 C-5 单独断言，这里排除，避免把前缀当端点去路由表里找。
    import routes
    deny = {_normalize(x) for x in routes.SCOPED_PRINCIPAL_DENY_PREFIXES}
    return {p for p in out if not p.endswith("/") and _normalize(p) not in deny}


def _normalize(path: str) -> str:
    """两端统一：路径参数名归一成 {}（FastAPI 的 :path 转换器也归一）。"""
    p = re.sub(r"\{[^}]*\}", "{}", path)
    p = p.replace("{}:path", "{}")
    return p.rstrip("/")


def _app_routes():
    import routes
    paths = set()
    for r in routes.app.routes:
        p = getattr(r, "path", None)
        if p:
            paths.add(_normalize(p))
    return paths


def monkeypatch_no_auth(monkeypatch):
    """关掉 conftest 的全局 NO_AUTH=1。

    本仓 `conftest.py` 默认 `SYNC_HUB_NO_AUTH=1`，而 `routes_ws._ws_auth_accept` 第一句就是
    `if routes_common.NO_AUTH: return ...` → 不显式关掉，所有 WS 鉴权用例都是假绿
    （「鉴权分支根本没跑到」，这是本仓踩过的坑：NO_AUTH 是导入期按值绑定的副本，
    必须 patch 命名空间而不是设 env）。用 monkeypatch 自动还原，不影响其它用例。
    """
    import routes_common
    monkeypatch.setattr(routes_common, "NO_AUTH", False)


# ─────────────────────────── C-1 / C-2 信封 ───────────────────────────

def test_c1_envelope_wire_shape():
    """§3.1 信封硬契约：键集合、version=2、ts 毫秒整数、中文不转义。"""
    env = envelope.envelope_dispatch({"method": "demo", "note": "中文"}, session_id="s-1", via="automation")
    assert set(env) == {"type", "id", "session_id", "via", "ts", "version", "payload"}
    assert env["type"] == "dispatch" and env["session_id"] == "s-1" and env["via"] == "automation"
    assert env["version"] == 2, "信封 version 是硬契约（接入方按 version>=2 判收）"
    assert isinstance(env["ts"], int) and len(str(env["ts"])) == 13, "ts 必须是毫秒整数"
    assert isinstance(env["id"], str) and len(env["id"]) == 32, "id 为 uuid4 hex"
    # §3.1：serialize 用 ensure_ascii=False，中文不转义（接入方按 utf-8 直读）
    wire = envelope.serialize(env)
    assert "中文" in wire and "\\u" not in wire
    # 六类构造函数齐备（§3.2 客户端帧表）
    assert envelope.envelope_hello("a", "ckpt")["type"] == "hello"
    assert envelope.envelope_result({"k": "v"})["type"] == "result"
    assert envelope.envelope_ack("dispatch-1")["type"] == "ack"
    assert envelope.envelope_ping()["type"] == "ping"
    assert envelope.envelope_pong()["type"] == "pong"
    assert envelope.envelope_hello("a", "ckpt")["payload"]["last_checkpoint_id"] == "ckpt"


def test_c2_envelope_rejects_legacy_and_malformed():
    """§3.1/§8：平铺旧格式与畸形帧一律拒收（无兼容分支，CD-032 定稿）。"""
    good = envelope.envelope_dispatch({"method": "m"})
    assert envelope.parse_envelope(good) is good

    legacy = {"type": "heartbeat", "agent_id": "a"}          # version 缺省 = 1
    assert envelope.parse_envelope(legacy) is None, "version<2 必须拒收"
    assert envelope.parse_envelope({"type": "x", "id": "1", "ts": 1, "version": 1}) is None

    for missing in ("type", "id", "ts"):
        bad = dict(good)
        bad.pop(missing)
        assert envelope.parse_envelope(bad) is None, "缺 %s 必须拒收" % missing

    collision = envelope.envelope_dispatch({"method": "m"})
    collision["payload"] = {"type": "撞信封字段名"}
    assert envelope.parse_envelope(collision) is None, "payload 键与信封键重名必须拒收"
    with pytest.raises(ValueError):
        envelope.envelope_dispatch({"id": "撞 id"})
    assert envelope.parse_envelope(["不是字典"]) is None
    assert envelope.deserialize("{不是 json") is None


# ─────────────────────────── C-3 黄金线形状 ───────────────────────────

def test_c3_golden_wire_shapes_still_compatible():
    """§8 向后兼容承诺：夹具里承诺的键与类型必须仍然成立（可新增，不可删改）。"""
    fixture = _load_fixture()
    db.init_db()
    hub = hub_core.hub
    reg = asyncio.run(hub.register(AgentRegistration(agent_id="contract-c3", agent_name="契约C3", role="worker")))
    stored = asyncio.run(hub.store_memory("contract-c3", MemoryEntry(memory_key="contract-c3-mem",
                                                                    content="契约夹具内容", kind="fact")))
    actual = {
        "register": reg,
        "memory_store": stored,
        "memory_list": hub.get_memories("contract-c3", ""),
        "memory_search": asyncio.run(hub.memory_search(
            MemorySearchRequest(agent_id="contract-c3", query="夹具", limit=5))),
        "memory_versions": asyncio.run(hub.get_memory_versions("contract-c3-mem", "contract-c3")),
        "task_create": asyncio.run(hub.create_task(TaskCreate(task_id="contract-c3-task",
                                                             description="契约任务",
                                                             creator_agent_id="contract-c3"))),
        "task_cancel": asyncio.run(hub.cancel_task("contract-c3-task", "contract-c3")),
        "envelope_dispatch": envelope.envelope_dispatch({"method": "demo"}, session_id="s1"),
        "envelope_hello": envelope.envelope_hello("contract-c3", "ckpt-1"),
    }
    for name, exp in fixture.items():
        assert name in actual, "夹具里有 %s，但契约测试没采集它（夹具与测试脱节）" % name
        _assert_compatible(exp, actual[name], name)
        # 顺带证明采集口径没漂：实际形状的键集合必须覆盖夹具的键集合
        got = _shape(actual[name])
        assert set(_flatten_keys(exp)) <= set(_flatten_keys(got)), "%s：键集合回退" % name


def _flatten_keys(shape, prefix=""):
    if isinstance(shape, dict):
        for k, v in shape.items():
            yield prefix + k
            yield from _flatten_keys(v, prefix + k + ".")
    elif isinstance(shape, list) and len(shape) == 2 and isinstance(shape[1], dict):
        yield from _flatten_keys(shape[1], prefix + "[0].")


# ─────────────────────────── C-4 端点存在性 ───────────────────────────

def test_c4_contract_endpoints_all_exist():
    """§2 表：文档承诺给接入方的每条路径都必须真的存在（改端点 = 先改文档与本断言）。"""
    promised = _contract_paths()
    assert len(promised) >= 25, "契约文档解析出的端点数偏少（%d），解析口径可能失灵" % len(promised)
    live = _app_routes()
    missing = sorted(p for p in promised if _normalize(p) not in live)
    assert not missing, "契约文档承诺但实际不存在的端点：%s" % missing


# ─────────────────────────── C-5 / C-6 / C-7 边界与默认值 ───────────────────────────

def test_c5_scoped_principal_deny_prefixes():
    """§1 路径 B：凭据/配置类端点对受限钥匙硬拒（不看白名单）。"""
    import routes
    deny = set(routes.SCOPED_PRINCIPAL_DENY_PREFIXES)
    documented = {"/api/v1/keys", "/api/v1/access", "/api/v1/server", "/api/v1/agents/quota",
                  "/api/v1/agents/full-access", "/api/v1/agents/register", "/api/v1/agents/bootstrap"}
    missing = sorted(documented - deny)
    assert not missing, "契约文档写死的硬拒前缀已不在代码里：%s" % missing


def test_c6_public_entrypoints_stay_public():
    """§2：/docs 与 /openapi.json 承诺「无凭据可打开」（接入方自读端点清单的入口）。"""
    import routes
    prefixes = tuple(routes.AUTH_ALLOWLIST_PREFIXES)
    for p in ("/docs", "/openapi.json"):
        assert p in prefixes, "%s 不在认证豁免前缀里，§2 的承诺失效" % p


def test_c7_documented_defaults():
    """§3.2/§5/§8 文档写给接入方的默认值。"""
    import models
    cfg = models.CONFIG
    assert cfg.AGENT_MIN_VERSION == "1.0.0", "§3.2 版本门默认值改了口径，文档要同步"
    assert float(cfg.WS_AUTH_TIMEOUT_SEC) >= 2.0, "§1 WS 首帧超时不得低于 2s（路由层有 3s 兜底）"
    assert int(cfg.RATE_LIMIT_PER_IP) == 1000, "§5 每 IP 限流默认值改了口径，文档要同步"
    assert cfg.AUTH_REGISTRATION in ("open", "guarded"), "§1 注册模式取值变了"
    # §5 配额字段名（接入方读 GET /agents/quota 的键）
    import sqlite3
    conn = sqlite3.connect(cfg.DB_PATH)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_quotas)")}
    finally:
        conn.close()
    for c in ("agent_id", "qps_limit", "mode", "window_sec", "burst"):
        assert c in cols, "§5 承诺的配额字段 %s 不存在" % c


# ─────────────────────────── C-8 WS 首帧鉴权 ───────────────────────────

class _FakeWS:
    """最小 websocket 替身：够 _ws_auth_accept 走完鉴权分支并记录 close 码。"""

    def __init__(self, frame, raise_timeout=False):
        self._frame = frame
        self._raise = raise_timeout
        self.closed = []
        self.query_params = {}
        self.client = type("C", (), {"host": "127.0.0.1"})()

    async def accept(self):
        return None

    async def receive_json(self):
        if self._raise:
            raise asyncio.TimeoutError()
        return self._frame

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


@pytest.mark.parametrize("frame,label", [
    ({"type": "hello", "payload": {}}, "首帧不是 auth 帧"),
    ({"type": "auth", "token": ""}, "空 token"),
    ({"type": "auth", "token": "definitely-wrong"}, "错 token"),
    (["不是字典"], "首帧不是对象"),
])
def test_c8_ws_auth_first_frame_contract(frame, label, monkeypatch):
    """§1 WS 段：首帧必须是 {"type":"auth","token":...}，否则 close 4401。"""
    import routes_ws
    monkeypatch_no_auth(monkeypatch)      # conftest 全局 NO_AUTH=1 会整体绕过鉴权（坑 53）
    ws = _FakeWS(frame)
    authed = asyncio.run(routes_ws._ws_auth_accept(ws, "contract-c8", strict_agent=True))
    assert authed is None, "%s 不应通过鉴权" % label
    assert ws.closed and ws.closed[0][0] == 4401, "%s 应 close 4401，实际 %s" % (label, ws.closed)


def test_c9_ws_auth_timeout_closes_4401(monkeypatch):
    """§1 WS 段：首帧迟迟不发 → close 4401（不能挂死连接）。"""
    import routes_ws
    monkeypatch_no_auth(monkeypatch)
    ws = _FakeWS(None, raise_timeout=True)
    authed = asyncio.run(routes_ws._ws_auth_accept(ws, "contract-c9"))
    assert authed is None and ws.closed and ws.closed[0][0] == 4401, ws.closed
