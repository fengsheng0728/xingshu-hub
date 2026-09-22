# -*- coding: utf-8 -*-
"""CD-061 / T22: 重运维端点统一门表 + 机器断言 + 触发审计链

配方（确定性优先，不起真实 Hub、不绑端口、不碰生产 sync_hub.db / chroma_db）：
  - tmp_path 独立 sqlite 库（monkeypatch CONFIG.DB_PATH + db_mod.init_db）
  - 端点直调路由函数（tests/test_kb_reconcile.py T7-6 同款），_FakeRequest 注入
    scope["principal"]；NO_AUTH=False + monkeypatch routes_common._agent_role
    控制角色；SyncHub 业务方法与 _log_event 全部打桩（审计行落内存列表）

覆盖（任务书 T3）：
  a. 结构断言：OPS_GATED_ENDPOINTS 清单 + AST 扫描
     - 每个清单端点的 handler 判定路径上存在门调用（require_ops_privilege /
       principal_is_privileged）
     - 求差机制：扫描 routes_*.py 中路径命中运维语义关键词
       （reindex|rebuild|calibrate|reclassify|cleanup|wiki/sync|wiki/inbox，行尾锚定，
       sync/status 与 inbox/{id}/approve|reject 不命中）
       的路由，与清单双向求差 —— 新增运维端点不登记即红
  b. 行为矩阵：清单内每个端点 × worker → 403 + ops_gate_denied 审计；
     × manager/orchestrator/hub_token → 不被门拦 + ops_trigger 审计
  c. 审计落行断言：ops_trigger payload 含 endpoint/requester/at/counts，
     计数取自打桩的真实返回值（rebuilt_mem=5 等），非占位
  d. NO_AUTH 测试态不拦（与既有惯例一致）
"""
import ast
import asyncio
import glob
import inspect
import os
import re
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod
from models import CONFIG
from hub_core import SyncHub
import routes_common
import routes_knowledge
import routes_maintenance
import routes_pipeline
import routes_wiki

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 运维语义关键词（行尾锚定：/api/v1/wiki/sync 命中、/api/v1/wiki/sync/status 不命中）
OPS_KEYWORD_RE = re.compile(r"(?:reindex|rebuild|calibrate|reclassify|cleanup|wiki/sync|wiki/inbox)$")
# 门调用判定名：统一 helper 或同门基准的内联判定（reindex 为 CD-051 既有基准，未改）
GATE_FUNCS = ("require_ops_privilege", "principal_is_privileged")


# ═══════════ 结构断言：扫描与求差 ═══════════

def _route_functions():
    """AST 扫 routes_*.py：{(METHOD, path): handler 体内是否含门调用}"""
    out = {}
    for fp in sorted(glob.glob(os.path.join(ROOT, "routes_*.py"))):
        with open(fp, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id == "router"
                        and dec.args and isinstance(dec.args[0], ast.Constant)
                        and isinstance(dec.args[0].value, str)):
                    continue
                method = dec.func.attr.upper()
                path = dec.args[0].value
                has_gate = any(
                    isinstance(n, ast.Call) and (
                        (isinstance(n.func, ast.Name) and n.func.id in GATE_FUNCS)
                        or (isinstance(n.func, ast.Attribute)
                            and n.func.attr in GATE_FUNCS))
                    for n in ast.walk(node))
                out[(method, path)] = has_gate
    return out


def _scan_ops_routes():
    """命中运维语义关键词的路由集合（新增运维端点不登记 → 与清单求差即红）"""
    return {key for key in _route_functions() if OPS_KEYWORD_RE.search(key[1])}


def _registry():
    return set(getattr(routes_common, "OPS_GATED_ENDPOINTS", ()))


def test_keyword_scanned_ops_routes_all_registered():
    """结构断言 a-1：关键词扫描结果与 OPS_GATED_ENDPOINTS 双向求差为空"""
    scanned = _scan_ops_routes()
    registry = _registry()
    missing = scanned - registry
    assert not missing, \
        f"运维语义端点未登记进 OPS_GATED_ENDPOINTS: {sorted(missing)}"
    stale = registry - scanned
    assert not stale, \
        f"OPS_GATED_ENDPOINTS 含扫描不到的条目（路径漂移？）: {sorted(stale)}"


def test_every_gated_handler_calls_gate():
    """结构断言 a-2：清单内每个端点的 handler 判定路径上存在门调用"""
    registry = _registry()
    assert registry, "OPS_GATED_ENDPOINTS 为空（routes_common 未登记门表）"
    handlers = _route_functions()
    for method, path in sorted(registry):
        assert (method, path) in handlers, \
            f"{method} {path} 在 routes_*.py 中找不到对应 handler"
        assert handlers[(method, path)], \
            f"{method} {path} handler 判定路径上无门调用（{GATE_FUNCS}）"


# ═══════════ 行为矩阵配方 ═══════════

class _FakeRequest:
    """最小 Request stub：路由读 request.scope + await request.json()"""

    def __init__(self, principal=None, body=None):
        self.scope = {}
        if principal is not None:
            self.scope["principal"] = principal
        self._body = body or {}
        self.query_params = {}
        self.path_params = {}

    async def json(self):
        return self._body


# 清单内 8 端点 → 路由函数（reindex 为同门基准：只测门，不断言 ops_trigger——
# 它走 CD-059(T18) 既有 denied 读审计，不在本任务触发审计范围）
_ENDPOINTS = {
    ("POST", "/api/v1/knowledge/reindex"): routes_knowledge.api_knowledge_reindex,
    ("POST", "/api/v1/embeddings/rebuild"): routes_pipeline.api_embeddings_rebuild,
    ("POST", "/api/v1/embeddings/calibrate"): routes_pipeline.api_embeddings_calibrate,
    ("POST", "/api/v1/chunks/reclassify"): routes_pipeline.api_chunks_reclassify,
    ("POST", "/api/v1/maintenance/cleanup"): routes_maintenance.api_force_cleanup,
    ("GET", "/api/v1/wiki/sync"): routes_wiki.api_wiki_sync,
    ("GET", "/api/v1/wiki/inbox"): routes_wiki.api_wiki_inbox,
    ("POST", "/api/v1/wiki/inbox/cleanup"): routes_wiki.api_wiki_inbox_cleanup,
}
_REINDEX = ("POST", "/api/v1/knowledge/reindex")


def _invoke(method, path, req, agent, principal):
    """按签名自适应直调（request/current_agent/principal 有则传，其余走默认）"""
    fn = _ENDPOINTS[(method, path)]
    params = inspect.signature(fn).parameters
    kwargs = {}
    if "request" in params:
        kwargs["request"] = req
    if "current_agent" in params:
        kwargs["current_agent"] = agent
    if "principal" in params:
        kwargs["principal"] = principal
    return fn(**kwargs)


@pytest.fixture()
def ops_env(tmp_path, monkeypatch):
    """独立 sqlite 库 + 真实门卫语义 + 业务/审计打桩"""
    db_path = str(tmp_path / "ops_gate.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    for mod in (routes_common, routes_pipeline, routes_maintenance,
                routes_wiki, routes_knowledge):
        monkeypatch.setattr(mod, "NO_AUTH", False, raising=False)
    roles = {"mgr-1": "manager", "wkr-1": "worker", "orc-1": "orchestrator"}
    monkeypatch.setattr(routes_common, "_agent_role",
                        lambda agent_id: roles.get(agent_id, ""))

    events = []

    async def _fake_log(self, event_type, agent_id, payload):
        events.append({"event_type": event_type, "agent_id": agent_id,
                       "payload": payload})

    monkeypatch.setattr(SyncHub, "_log_event", _fake_log)

    async def _fake_reclassify(self, doc_id="", requester=""):
        return {"status": "ok", "changed": 2, "details": []}

    async def _fake_rebuild(self, requester="", batch_size=100):
        return {"status": "ok", "provider": "hasher", "target_dim": 384,
                "rebuilt_mem": 5, "stale_total": 7}

    async def _fake_cleanup(self):
        return {"memory_pool": 9, "events": 18, "tasks": 5, "db_size_mb": 1.0}

    async def _fake_db_stats(self):
        return {"memory_pool": 10, "events": 20, "tasks": 5, "db_size_mb": 1.0}

    async def _fake_reconcile(self, entry_id=""):
        return {"status": "ok", "checked": 3, "ok": 2, "reparsed": 1,
                "removed": 0, "skipped": 0, "errors": [], "duration_ms": 5}

    monkeypatch.setattr(SyncHub, "reclassify_chunks", _fake_reclassify)
    monkeypatch.setattr(SyncHub, "rebuild_embeddings", _fake_rebuild)
    monkeypatch.setattr(SyncHub, "force_cleanup", _fake_cleanup)
    monkeypatch.setattr(SyncHub, "_db_stats", _fake_db_stats)
    monkeypatch.setattr(SyncHub, "reconcile_kb_vectors", _fake_reconcile)
    monkeypatch.setattr(
        "wiki_sync.sync",
        lambda *a: {"created": 1, "updated": 2, "skipped": 3, "errors": []})
    monkeypatch.setattr("db.get_embedding_provider", lambda *a, **k: object())
    import chunker
    monkeypatch.setattr(
        chunker, "calibrate_cos_threshold",
        lambda model, samples: {"p10": 0.1, "p25": 0.2, "p50": 0.3,
                                "suggested": 0.2})

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO wiki_inbox (page_path, title, status, source)"
                 " VALUES ('entities/t.md', 't', 'pending', 'test')")
    conn.commit()
    conn.close()
    return SimpleNamespace(events=events, db_path=db_path)


_WORKER = {"auth_mode": "api_key", "subject_id": "wkr-1"}
_PRIV = [
    ({"auth_mode": "api_key", "subject_id": "mgr-1"}, "mgr-1"),
    ({"auth_mode": "api_key", "subject_id": "orc-1"}, "orc-1"),
    ({"auth_mode": "hub_token", "subject_id": "__hub__"}, "ops-bot"),
]
_BODY = {"samples": ["标定语料甲", "标定语料乙"]}


def _trigger_rows(env, path):
    return [e for e in env.events
            if e["event_type"] == "ops_trigger"
            and e["payload"].get("endpoint") == path]


# ═══════════ 行为矩阵：非特权 403 ═══════════

@pytest.mark.parametrize("method,path", sorted(_ENDPOINTS))
def test_worker_denied_403(ops_env, method, path):
    """清单内每个端点 × worker → 403，且不执行业务、拒绝落审计（不静默）"""
    req = _FakeRequest(_WORKER, body=_BODY)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invoke(method, path, req, "wkr-1", _WORKER))
    assert exc.value.status_code == 403, \
        f"{method} {path} worker 应 403，实际 {exc.value.status_code}"
    assert not _trigger_rows(ops_env, path), "被拒路径不应落 ops_trigger"
    if (method, path) != _REINDEX:
        # reindex 走既有 T18 denied 读审计（gateway_read_log），不在 ops_gate_denied 范围
        denied = [e for e in ops_env.events
                  if e["event_type"] == "ops_gate_denied"
                  and e["payload"].get("endpoint") == path]
        assert denied, f"{method} {path} 拒绝未落 ops_gate_denied 审计"
        assert denied[-1]["payload"].get("requester") == "wkr-1"


# ═══════════ 行为矩阵：特权不拦 + 触发审计 ═══════════

@pytest.mark.parametrize("method,path", sorted(_ENDPOINTS))
@pytest.mark.parametrize("principal,agent", _PRIV)
def test_privileged_not_blocked(ops_env, method, path, principal, agent):
    """清单内每个端点 × manager/orchestrator/hub_token → 不被门拦；
    本任务 7 端点成功触发落 ops_trigger（endpoint/requester/at/counts 齐全）"""
    req = _FakeRequest(principal, body=_BODY)
    result = asyncio.run(_invoke(method, path, req, agent, principal))
    assert result is not None, f"{method} {path} 特权主体不应被门拦"
    if (method, path) == _REINDEX:
        return  # 基准端点不断言 ops_trigger（见 _ENDPOINTS 注释）
    rows = _trigger_rows(ops_env, path)
    assert rows, f"{method} {path} 成功触发未落 ops_trigger 审计"
    p = rows[-1]["payload"]
    assert p.get("requester") == agent
    assert p.get("at"), "ops_trigger payload 缺 at（ISO 时间）"
    assert "counts" in p, "ops_trigger payload 缺 counts"


# ═══════════ 审计计数真实性（来自打桩返回值，非占位） ═══════════

def test_ops_trigger_counts_from_real_returns(ops_env):
    hub_tok = {"auth_mode": "hub_token", "subject_id": "__hub__"}

    def run(method, path, body=None):
        req = _FakeRequest(hub_tok, body=body or {})
        asyncio.run(_invoke(method, path, req, "ops-bot", hub_tok))
        rows = _trigger_rows(ops_env, path)
        assert rows, f"{path} 缺 ops_trigger 行"
        return rows[-1]["payload"]["counts"]

    c = run("POST", "/api/v1/embeddings/rebuild")
    assert c == {"rebuilt_mem": 5, "stale_total": 7}, f"rebuild 计数应来自返回值: {c}"
    c = run("POST", "/api/v1/chunks/reclassify")
    assert c == {"changed": 2}, f"reclassify 计数应来自返回值: {c}"
    c = run("POST", "/api/v1/embeddings/calibrate", body=_BODY)
    assert c == {"checked": 2}, f"calibrate 计数应为真实样本次数: {c}"
    c = run("POST", "/api/v1/maintenance/cleanup")
    assert c["events"] == {"before": 20, "after": 18}, \
        f"cleanup 计数应来自 _db_stats/force_cleanup 真实返回: {c}"
    c = run("GET", "/api/v1/wiki/sync")
    assert c == {"created": 1, "updated": 2, "skipped": 3}, \
        f"wiki/sync 计数应来自 sync() 返回: {c}"
    c = run("GET", "/api/v1/wiki/inbox")
    assert c == {"total": 1, "returned": 1}, f"wiki/inbox 计数应来自真实查询: {c}"
    c = run("POST", "/api/v1/wiki/inbox/cleanup")
    assert c["scanned"] == 1 and "removed" in c, \
        f"inbox/cleanup 计数应来自真实清理统计: {c}"


# ═══════════ NO_AUTH 测试态不拦 ═══════════

def test_no_auth_mode_gate_open(ops_env, monkeypatch):
    """NO_AUTH（测试态）时门不拦，与既有惯例一致"""
    for mod in (routes_common, routes_pipeline, routes_maintenance, routes_wiki):
        monkeypatch.setattr(mod, "NO_AUTH", True, raising=False)
    for method, path in [("POST", "/api/v1/chunks/reclassify"),
                         ("POST", "/api/v1/maintenance/cleanup"),
                         ("POST", "/api/v1/wiki/inbox/cleanup")]:
        req = _FakeRequest(_WORKER, body=_BODY)
        result = asyncio.run(_invoke(method, path, req, "wkr-1", _WORKER))
        assert result is not None, f"NO_AUTH 下 {method} {path} 不应被门拦"


# ═══════════ 兼容：进程内直调无 Request 不过门 ═══════════

def test_direct_call_without_request_legacy_compat(ops_env):
    """request=None（进程内直调，HTTP 不可达此分支）→ 门跳过，兼容
    CD-042/CD-043 既有直调测试；HTTP 路径 FastAPI 必注入 Request，门必生效
    （由上方行为矩阵锚定）"""
    out = asyncio.run(routes_wiki.api_wiki_inbox(limit=10))
    assert out["total"] == 1
    out = asyncio.run(routes_wiki.api_wiki_sync())
    assert out["status"] == "ok" and out["created"] == 1
