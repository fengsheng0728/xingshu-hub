# -*- coding: utf-8 -*-
"""CD-106：POST /api/audit/anchor/verify 验签端点 + failed 自动落 anchor_mismatch 审计。

配方（同 test_dead_letters / test_ops_gate_matrix：不起真实 Hub、不绑端口、不联网）：
  - tmp_path 独立 sqlite 库（monkeypatch CONFIG.DB_PATH + db.init_db()，含 events 表）
  - 直调 handler 协程 + 显式传 current_agent（本仓 conftest 全局 SYNC_HUB_NO_AUTH=1
    会短路 require_ops_privilege 的 NO_AUTH 分支 —— 门禁用例必须 monkeypatch
    routes_common.NO_AUTH=False 才能真正打中角色门，否则恒真；
    同 test_dead_letters.test_retry_requires_ops_privilege 口径）
  - 角色走真实 agents 表（routes_common._agent_role 直查 DB，不打桩角色判定）
  - 验签 / 锚取回一律 monkeypatch 到函数级（audit_chain.verify_tsa_token /
    audit_chain.fetch_and_verify_anchors）——不真造 PKI 证书、不访问公网 TSA
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import audit_chain  # noqa: E402
import db as db_mod  # noqa: E402
import routes_audit  # noqa: E402
import routes_common  # noqa: E402
from models import CONFIG  # noqa: E402

WORKER = {"auth_mode": "api_key", "subject_id": "w1"}
MANAGER = {"auth_mode": "api_key", "subject_id": "m1"}
ORCH = {"auth_mode": "api_key", "subject_id": "o1"}
HUB_TOKEN = {"auth_mode": "hub_token", "subject_id": "__hub__"}


class _FakeRequest:
    """最小 Request 替身：require_ops_privilege 只读 .scope['principal']，
    handler 另读 await request.json()。"""

    def __init__(self, principal=None, body=None):
        self.scope = {}
        if principal is not None:
            self.scope["principal"] = principal
        self._body = body if body is not None else {}
        self.query_params = {}
        self.path_params = {}

    async def json(self):
        return self._body


def _fetch(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _anchor_rows(db_path):
    return [json.loads(r["payload"]) for r in _fetch(
        db_path, "SELECT payload FROM events WHERE event_type = 'anchor_mismatch'")]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立库 + 真实 events 落库 + 角色可查 + NO_AUTH 关闭（打中角色门）。"""
    db_path = str(tmp_path / "cd106.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    conn = sqlite3.connect(db_path)
    for aid, role in (("w1", "worker"), ("m1", "manager"), ("o1", "orchestrator")):
        conn.execute(
            "INSERT INTO agents (agent_id, agent_name, role, status)"
            " VALUES (?, ?, ?, 'offline')", (aid, aid, role))
    conn.commit()
    conn.close()
    # conftest 的 SYNC_HUB_NO_AUTH=1 让 require_ops_privilege 恒放行 → 门禁恒真；
    # 必须关掉才测得到 403（require_ops_privilege 体内读的是 routes_common.NO_AUTH）
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    # 验收加固（CD-106，2026-09-24）：本文件只测「端点自身」的行为，但 verify_tsa /
    # verify_anchor 默认读 SYNC_HUB_TSA_DIR（conftest.py 的 SYNC_HUB_TSA_DIR 指向**共享**的
    # test-artifacts/audit/tsa）——全量跑时该目录已被 test_audit_tsa 写入真实 .tsr，
    # 临时空库比对必然 mismatch → 凭空多出 anchor_mismatch 行，把「零告警」类断言打红
    # （单跑无残留故绿、全量红）。此处把链头/本地锚验签与 tsa 目录钉成默认全绿/空目录，
    # 各用例只覆盖自己要测的变量；专测 verify_tsa 的用例在函数内再 monkeypatch 覆盖。
    monkeypatch.setattr(audit_chain, "verify_tsa",
                        lambda *a, **k: {"valid": True, "checked": 0, "mismatches": []})
    monkeypatch.setattr(audit_chain, "verify_anchor",
                        lambda *a, **k: {"valid": True, "checked": 0})
    monkeypatch.setattr(audit_chain, "tsa_out_dir",
                        lambda *a, **k: str(tmp_path / "tsa_empty"))
    return db_path


def _call(body=None, principal=HUB_TOKEN, agent="ops-bot"):
    return asyncio.run(routes_audit.api_audit_anchor_verify(
        _FakeRequest(principal, body=body or {}), current_agent=agent))


# ═══════════ 1. 门禁：worker 403（与同文件 stamp 同门同行为）／特权放行 ═══════════

def test_gate_worker_denied_403_same_as_stamp(env):
    """worker → 403 + ops_gate_denied（不静默）；stamp 同门同样 403。"""
    with pytest.raises(HTTPException) as ei:
        _call(body={}, principal=WORKER, agent="w1")
    assert ei.value.status_code == 403, f"worker 应 403，实际 {ei.value.status_code}"
    with pytest.raises(HTTPException) as ei2:
        asyncio.run(routes_audit.api_audit_anchor_stamp(
            _FakeRequest(WORKER, body={}), current_agent="w1"))
    assert ei2.value.status_code == 403, (
        f"stamp 同门应同样 403，实际 {ei2.value.status_code}")
    denied = _fetch(
        env,
        "SELECT payload FROM events WHERE event_type = 'ops_gate_denied'"
        " AND payload LIKE '%anchor/verify%'")
    assert denied, "verify 拒绝必须落 ops_gate_denied 审计（同 stamp 语义）"


@pytest.mark.parametrize("principal,agent", [
    (MANAGER, "m1"),
    (ORCH, "o1"),
    (HUB_TOKEN, "ops-bot"),
])
def test_gate_privileged_allowed(env, principal, agent):
    """manager / orchestrator / hub_token → 不被门拦，返回 status=ok。"""
    resp = _call(body={}, principal=principal, agent=agent)
    assert resp["status"] == "ok", resp
    assert not _anchor_rows(env), "空链自检不应误报 anchor_mismatch"


# ═══════════ 2. failed → 落 anchor_mismatch 审计 ═══════════

def test_failed_token_writes_anchor_mismatch(env, monkeypatch):
    """verify_tsa_token status=failed → events.anchor_mismatch 且 source=http_verify。"""
    monkeypatch.setattr(
        audit_chain, "verify_tsa_token",
        lambda *a, **k: {"status": "failed", "reason": "imprint_mismatch",
                         "detail": "token 内的 messageImprint 与链头摘要不符"})
    resp = _call(body={"tsr": "fake.tsr"}, principal=MANAGER, agent="m1")
    assert resp["tokens"] and resp["tokens"][0]["status"] == "failed", resp
    rows = _anchor_rows(env)
    assert rows, "failed 必须落 anchor_mismatch 审计"
    detail = rows[-1]
    assert detail.get("source") == "http_verify", detail
    toks = detail.get("tokens") or []
    assert toks and toks[0].get("status") == "failed", detail
    assert toks[0].get("reason") == "imprint_mismatch", detail
    assert toks[0].get("tsr") == "fake.tsr", detail


# ═══════════ 3. unverified 不触发告警（但返回体如实报出） ═══════════

def test_unverified_token_no_alarm_but_reported(env, monkeypatch):
    """unverified ≠ failed：审计表零新增 anchor_mismatch，但返回体必须有 unverified 项。"""
    monkeypatch.setattr(
        audit_chain, "verify_tsa_token",
        lambda *a, **k: {"status": "unverified", "reason": "no_trusted_fingerprints",
                         "detail": "未配置钉扎指纹——不钉则验签等于白验"})
    resp = _call(body={"tsr": "fake.tsr"}, principal=MANAGER, agent="m1")
    # 两侧断言（与用例 2 区分）：返回体有 unverified + 审计表零新增
    assert resp["tokens"] and resp["tokens"][0]["status"] == "unverified", resp
    assert not _anchor_rows(env), (
        "unverified 不算 failed，不得落 anchor_mismatch（取不回不是篡改证据）")


# ═══════════ 4. anchors.valid=False → 落告警 ═══════════

def test_anchors_invalid_writes_anchor_mismatch(env, monkeypatch):
    """fetch_and_verify_anchors valid=False（重写嫌疑）→ anchor_mismatch + mismatches 键。"""
    monkeypatch.setattr(
        audit_chain, "fetch_and_verify_anchors",
        lambda *a, **k: {"valid": False, "checked": 1, "unverified": 0,
                         "mismatches": [{"url": "http://anchor.example/a",
                                         "reason": "远端锚不在本地链中"}]})
    resp = _call(body={"urls": ["http://anchor.example/a"]},
                 principal=MANAGER, agent="m1")
    assert resp["anchors"]["valid"] is False, resp
    rows = _anchor_rows(env)
    assert rows, "anchors.valid=False 必须落 anchor_mismatch 审计"
    detail = rows[-1]
    assert detail.get("source") == "http_verify", detail
    mm = detail.get("mismatches") or []
    assert mm and mm[0].get("url") == "http://anchor.example/a", detail


def test_anchors_unverified_not_mixed_into_mismatches(env, monkeypatch):
    """取不回记 unverified、不判 valid=False、不混进 mismatches（口径对齐
    fetch_and_verify_anchors docstring）。"""
    monkeypatch.setattr(
        audit_chain, "fetch_and_verify_anchors",
        lambda *a, **k: {"valid": True, "checked": 0, "unverified": 1,
                         "results": [{"url": "http://down.example/a", "ok": False,
                                      "status": "unverified",
                                      "reason": "fetch_failed: URLError"}]})
    resp = _call(body={"urls": ["http://down.example/a"]},
                 principal=MANAGER, agent="m1")
    assert resp["anchors"]["valid"] is True, resp
    assert resp["anchors"]["unverified"] == 1, resp
    assert not _anchor_rows(env), "unverified 不得触发 anchor_mismatch"


# ═══════════ 5. 全绿路径：不落告警 + 返回体结构完整 ═══════════

def test_all_green_no_alarm_full_shape(env, monkeypatch):
    """全部 verified/valid → 零 anchor_mismatch，返回体键集合完整。"""
    monkeypatch.setattr(
        audit_chain, "verify_tsa_token",
        lambda *a, **k: {"status": "verified", "pinned": "tsa_cert",
                         "fingerprint": "ab" * 32, "imprint": "cd" * 32})
    monkeypatch.setattr(
        audit_chain, "fetch_and_verify_anchors",
        lambda *a, **k: {"valid": True, "checked": 1, "unverified": 0,
                         "results": [{"url": "http://anchor.example/a", "ok": True,
                                      "status": "match_head"}]})
    resp = _call(body={"tsr": "fake.tsr"}, principal=MANAGER, agent="m1")
    assert not _anchor_rows(env), "全绿不得落 anchor_mismatch"
    assert resp["status"] == "ok", resp
    assert set(resp.keys()) == {"status", "chain", "anchors", "tokens",
                                "trusted_fingerprints_configured"}, resp.keys()
    assert set(resp["chain"].keys()) == {"local_anchor", "tsa_stamped_head"}, resp["chain"]
    assert resp["chain"]["local_anchor"]["valid"] is True, resp["chain"]
    assert resp["chain"]["tsa_stamped_head"]["valid"] is True, resp["chain"]
    assert resp["anchors"]["valid"] is True, resp["anchors"]
    assert resp["tokens"] and resp["tokens"][0]["status"] == "verified", resp["tokens"]
    assert isinstance(resp["trusted_fingerprints_configured"], bool), resp


# ═══════════ 6. verify_tsa（链头回拉）valid=False → 也落告警（任务 3.2b 条件3） ═══════════

def test_tsa_head_replay_invalid_writes_anchor_mismatch(env, monkeypatch):
    """verify_tsa valid=False（被盖章链头不在链中）→ anchor_mismatch。"""
    monkeypatch.setattr(
        audit_chain, "verify_tsa",
        lambda *a, **k: {"valid": False, "checked": 1,
                         "mismatches": [{"anchor": "deadbeef" * 4,
                                         "reason": "anchored_head_missing_from_chain"}]})
    resp = _call(body={}, principal=MANAGER, agent="m1")
    assert resp["chain"]["tsa_stamped_head"]["valid"] is False, resp
    rows = _anchor_rows(env)
    assert rows, "verify_tsa valid=False 必须落 anchor_mismatch 审计"
    detail = rows[-1]
    assert detail.get("source") == "http_verify", detail
    mm = detail.get("mismatches") or []
    assert mm and "anchored_head_missing_from_chain" in str(mm[0].get("reason", "")), detail


# ═══════════ 7. db_unavailable 不算 mismatch → 不落告警（CD-106 验收裁决） ═══════════

def test_db_unavailable_does_not_alarm(env, monkeypatch):
    """fetch_and_verify_anchors 在 db_unavailable 时也返回 valid=False，但 results 为空
    ——它不是篡改证据，落 anchor_mismatch 会把「库打不开」误报成「链被整段重写」。

    验收裁决（2026-09-24，Hermes）：只有**真 mismatch**（成功取回但锚不在链中）才告警；
    db_unavailable / unverified / rejected 不落 anchor_mismatch，但返回体必须如实带 error。
    """
    monkeypatch.setattr(
        audit_chain, "fetch_and_verify_anchors",
        lambda *a, **k: {"valid": False, "checked": 0, "unverified": 1,
                         "results": [],
                         "error": "db_unavailable: unable to open database file"})
    resp = _call(body={"urls": ["http://anchor.example/a"]},
                 principal=MANAGER, agent="m1")
    assert resp["anchors"]["valid"] is False, resp
    assert resp["anchors"].get("error"), "返回体必须如实报出 error（可见性不能丢）"
    assert not _anchor_rows(env), (
        "db_unavailable 不是 mismatch，不得落 anchor_mismatch（避免误报链被重写）")
