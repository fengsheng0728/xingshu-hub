# -*- coding: utf-8 -*-
"""CD-110（2026-09-24）：联邦 wiki 配对凭据通道验收测试。

方案①：对齐 snapshot 的「路由层豁免 + 端点函数内双通道自认证」。
判定复用 routes_common.authorize_federated_caller（自 routes_federation
._authorize_snapshot_caller 下沉，逐行等价），不新造第二套权限逻辑。

口径（对齐 test_dead_letters / test_ops_gate_matrix / test_wiki_collection 配方）：
  - tmp_path 独立 sqlite 库：monkeypatch CONFIG.DB_PATH + db.init_db() 全 schema
  - 临时 wiki 根：monkeypatch wiki_engine.WIKI_ROOT 与 wiki_sync.WIKI_ROOT
    （后者是模块级 `from wiki_engine import WIKI_ROOT` 绑定，_update_index 用它）
  - 直调 handler 协程 + _FakeRequest（不起真实 Hub、不绑端口）
  - 门语义需 NO_AUTH=0：monkeypatch routes_common.NO_AUTH=False 才打得到门
"""
import asyncio
import hashlib
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod          # noqa: E402
import routes_common        # noqa: E402
import routes_wiki          # noqa: E402
import wiki_engine          # noqa: E402
import wiki_sync            # noqa: E402
from models import CONFIG   # noqa: E402

PAIR_KEY = "pair-cd110-" + "a" * 32
HASH_KEY = "hash-cd110-" + "b" * 32
REVOKED_KEY = "revoked-cd110-" + "c" * 30
WORKER_KEY = "worker-cd110-" + "d" * 30
MGR_KEY = "mgr-cd110-" + "e" * 32
ORC_KEY = "orc-cd110-" + "f" * 32
HUB_TOKEN = "hub-token-cd110"
UNKNOWN_KEY = "unknown-cd110-" + "x" * 28


class _FakeRequest:
    """最小 Request 替身：双通道自认证读 headers.Authorization + scope；import 读 json()。"""

    def __init__(self, token="", body=None):
        self._token = token
        self.scope = {"client": ("127.0.0.1", 54321)}
        self._body = body if body is not None else {}
        self.query_params = {}
        self.path_params = {}

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def json(self):
        return self._body


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（全 schema）+ 临时 wiki 根 + 关闭 NO_AUTH（门语义生效）。"""
    db_path = str(tmp_path / "cd110.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    monkeypatch.setattr(CONFIG, "HUB_TOKEN", HUB_TOKEN)
    db_mod.init_db()
    wiki_root = str(tmp_path / "wiki")
    for sub in ("entities", "concepts", "comparisons", "queries"):
        os.makedirs(os.path.join(wiki_root, sub), exist_ok=True)
    monkeypatch.setattr(wiki_engine, "WIKI_ROOT", wiki_root)
    monkeypatch.setattr(wiki_sync, "WIKI_ROOT", wiki_root)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    try:
        routes_common._auth_provider().invalidate_auth_cache()
    except Exception:
        routes_common._AUTH_PROVIDER = None
    return SimpleNamespace(db_path=db_path, wiki_root=wiki_root)


# ── 落库 helper ──

def _pair(db_path, key, *, revoked=False, hash_only=False, hub_id="hub-peer"):
    """直写一条 team_members 配对记录（模拟 pair exchange 落库）。"""
    conn = sqlite3.connect(db_path)
    try:
        if hash_only:
            conn.execute(
                "INSERT INTO team_members (local_agent_id, remote_hub_id,"
                " remote_hub_url, remote_agent_id, remote_api_key,"
                " remote_api_key_hash, paired_at, key_expires_at, revoked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("local-a", hub_id, "http://192.168.1.2:3060", "remote-b",
                 "plaintext-not-matching-" + hub_id,
                 hashlib.sha256(key.encode("utf-8")).hexdigest(),
                 "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                 "2026-06-01T00:00:00+00:00" if revoked else None))
        else:
            conn.execute(
                "INSERT INTO team_members (local_agent_id, remote_hub_id,"
                " remote_hub_url, remote_agent_id, remote_api_key,"
                " paired_at, key_expires_at, revoked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("local-a", hub_id, "http://192.168.1.2:3060", "remote-b", key,
                 "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                 "2026-06-01T00:00:00+00:00" if revoked else None))
        conn.commit()
    finally:
        conn.close()


def _agent(db_path, agent_id, role, api_key):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO agents (agent_id, agent_name, role, api_key)"
            " VALUES (?, ?, ?, ?)", (agent_id, agent_id, role, api_key))
        conn.commit()
    finally:
        conn.close()


def _drop_hash_col(db_path):
    """剥掉 remote_api_key_hash 列 —— 覆盖 `_is_paired_member_key` 无 hash 列的明文分支。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE team_members DROP COLUMN remote_api_key_hash")
        conn.commit()
    finally:
        conn.close()


def _deny_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT requester, kind, target, granted_level FROM gateway_read_log")]
    finally:
        conn.close()


def _call_import(env, token, body=None):
    req = _FakeRequest(token, body if body is not None else
                       {"pages": {"concepts/cd110-p.md": "---\ntitle: T\n---\n\n正文"}})
    return asyncio.run(routes_wiki.api_wiki_import(
        request=req, current_agent="", principal=None))


def _call_export(env, token):
    req = _FakeRequest(token)
    return asyncio.run(routes_wiki.api_wiki_export(
        request=req, current_agent="", principal=None))


def _capture(coro):
    try:
        return asyncio.run(coro)
    except HTTPException as e:
        return e


# ═══ 1. 配对凭据（明文列）→ /wiki/import 放行 ═══

def test_pairing_credential_plaintext_column_import_allowed(env):
    _drop_hash_col(env.db_path)          # 无 hash 列 → 只走明文匹配分支
    _pair(env.db_path, PAIR_KEY, revoked=False, hub_id="hub-plain")
    res = _call_import(env, PAIR_KEY)
    assert not isinstance(res, HTTPException), \
        f"未撤销配对凭据必须放行，实际被拒: {getattr(res, 'status_code', res)}"
    assert res.get("status") == "ok" and res.get("imported", 0) >= 1, \
        f"import 应写入页面: {res}"
    assert not _deny_rows(env.db_path), f"放行路径不应留 denied 痕: {_deny_rows(env.db_path)}"


# ═══ 2. 配对凭据（hash 列）→ 放行（覆盖 remote_api_key_hash 分支） ═══

def test_pairing_credential_hash_column_import_allowed(env):
    _pair(env.db_path, HASH_KEY, revoked=False, hash_only=True, hub_id="hub-hash")
    res = _call_import(env, HASH_KEY)
    assert not isinstance(res, HTTPException), \
        f"hash 命中的配对凭据必须放行，实际被拒: {getattr(res, 'status_code', res)}"
    assert res.get("status") == "ok" and res.get("imported", 0) >= 1


# ═══ 3. 已撤销配对凭据 → 拒绝（fail-closed，关键负向） ═══

def test_revoked_pairing_credential_denied(env):
    _pair(env.db_path, REVOKED_KEY, revoked=True, hub_id="hub-revoked")
    r = _capture(routes_wiki.api_wiki_import(
        request=_FakeRequest(REVOKED_KEY, {"pages": {}}),
        current_agent="", principal=None))
    assert isinstance(r, HTTPException), \
        f"已撤销配对凭据绝不能放行: {r}"
    assert r.status_code in (401, 403), f"revoked 应 401/403，实得 {r.status_code}"
    rows = _deny_rows(env.db_path)
    assert any(row["granted_level"] == "denied" and row["kind"] == "wiki"
               and row["target"] == "import" for row in rows), \
        f"拒绝必须留 _log_deny 痕（CD-059 口径）: {rows}"


# ═══ 4. worker 的普通 api_key → 仍 403（不许回归） ═══

def test_worker_api_key_still_denied(env):
    _agent(env.db_path, "wkr-1", "worker", WORKER_KEY)
    r = _capture(routes_wiki.api_wiki_import(
        request=_FakeRequest(WORKER_KEY, {"pages": {}}),
        current_agent="", principal=None))
    assert isinstance(r, HTTPException), f"worker 不许进 wiki import: {r}"
    assert r.status_code == 403, f"worker 必须 403（身份有效但无权限），实得 {r.status_code}"


# ═══ 5. hub_token / manager / orchestrator → 放行（既有行为不变） ═══

def test_standard_credentials_still_allowed(env):
    _agent(env.db_path, "mgr-1", "manager", MGR_KEY)
    _agent(env.db_path, "orc-1", "orchestrator", ORC_KEY)
    for token, tag in ((HUB_TOKEN, "hub_token"), (MGR_KEY, "manager"),
                       (ORC_KEY, "orchestrator")):
        res = _call_export(env, token)
        assert not isinstance(res, HTTPException), \
            f"{tag} 仍须放行（既有行为不回归），实际被拒: {getattr(res, 'status_code', res)}"
        assert res.get("status") == "ok"


# ═══ 6. /wiki/export 与 /wiki/import 同门同行为 ═══

def test_export_and_import_same_gate(env):
    _agent(env.db_path, "wkr-1", "worker", WORKER_KEY)
    _pair(env.db_path, PAIR_KEY, revoked=False, hub_id="hub-same")

    # 两侧 worker 都 403
    r_exp = _capture(routes_wiki.api_wiki_export(
        request=_FakeRequest(WORKER_KEY), current_agent="", principal=None))
    r_imp = _capture(routes_wiki.api_wiki_import(
        request=_FakeRequest(WORKER_KEY, {"pages": {}}), current_agent="", principal=None))
    assert isinstance(r_exp, HTTPException) and r_exp.status_code == 403, \
        f"export 侧 worker 应 403: {getattr(r_exp, 'status_code', r_exp)}"
    assert isinstance(r_imp, HTTPException) and r_imp.status_code == 403, \
        f"import 侧 worker 应 403: {getattr(r_imp, 'status_code', r_imp)}"

    # 两侧配对凭据都放行（与上面的 403 构成对照，两侧不同才算打中判定）
    p_exp = _call_export(env, PAIR_KEY)
    p_imp = _call_import(env, PAIR_KEY)
    assert not isinstance(p_exp, HTTPException), f"export 侧配对凭据应放行: {p_exp}"
    assert not isinstance(p_imp, HTTPException), f"import 侧配对凭据应放行: {p_imp}"


# ═══ 7. 不存在的 key → 拒绝；枚举预言机防护 ═══

def test_unknown_key_rejected_and_no_oracle_on_paired(env):
    """不存在的 key 必拒；「配对凭据存在但已撤销」与「不存在」同响应（不泄露配对存在性）。

    注：worker（标准凭据·身份有效但无权）与未知串的 403/401 分叉是既有
    `_authorize_snapshot_caller` 判定形态（认证失败 401 / 认证成功无权 403），
    本任务按约束 §2.4 逐行等价复用、不改写其语义 —— 如实上报，不硬凑同响应。
    """
    _pair(env.db_path, REVOKED_KEY, revoked=True, hub_id="hub-oracle")

    r_unknown = _capture(routes_wiki.api_wiki_import(
        request=_FakeRequest(UNKNOWN_KEY, {"pages": {}}),
        current_agent="", principal=None))
    r_revoked = _capture(routes_wiki.api_wiki_import(
        request=_FakeRequest(REVOKED_KEY, {"pages": {}}),
        current_agent="", principal=None))
    assert isinstance(r_unknown, HTTPException), f"不存在的 key 必须被拒: {r_unknown}"
    assert isinstance(r_revoked, HTTPException), f"已撤销 key 必须被拒: {r_revoked}"
    assert r_unknown.status_code in (401, 403)
    assert r_revoked.status_code == r_unknown.status_code, \
        (f"「存在但已撤销」与「不存在」必须同响应（枚举预言机），"
         f"实得 revoked={r_revoked.status_code} unknown={r_unknown.status_code}")

    # 无 token 同样拒绝
    r_empty = _capture(routes_wiki.api_wiki_import(
        request=_FakeRequest("", {"pages": {}}), current_agent="", principal=None))
    assert isinstance(r_empty, HTTPException) and r_empty.status_code in (401, 403)
