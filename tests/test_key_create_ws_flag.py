# -*- coding: utf-8 -*-
"""CD-095：hub_cli key create 缺 --ws —— 补 flag 并透传 scope.ws=true，
与 REST POST /api/v1/keys 的 ws 口径对齐（routes_ws._ws_auth_accept：
scoped key 需显式 scope.ws 才放行 WS 首帧鉴权，fail-closed）。

测试口径：scope 落库断言（WS 首帧鉴权门的判定字段即 scope.ws，
tests/test_l6_ws_auth_regression.py 已覆盖该门本身的行为矩阵）。
"""
import json
import os
import shutil
import sqlite3
import tempfile

import pytest

import hub_cli
import key_scopes


@pytest.fixture()
def cli_db(monkeypatch):
    # key_scopes.get_store 是模块级单例（首调锁定路径）——每个用例重置，
    # 否则后续用例复用指向上一个已删除临时目录的 store
    monkeypatch.setattr(key_scopes, "_store", None)
    tmp = tempfile.mkdtemp(prefix="cd095-")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY, agent_name TEXT)")
    conn.execute("INSERT INTO agents VALUES ('ext', '外部协作者')")
    conn.execute(
        """CREATE TABLE agent_keys (
            key_id TEXT PRIMARY KEY, agent_id TEXT, key_hash TEXT, scope TEXT,
            status TEXT, created_by TEXT, created_at TEXT, expires_at TEXT,
            last_used_at TEXT, call_count INTEGER)"""
    )
    conn.commit()
    conn.close()
    yield db
    shutil.rmtree(tmp, ignore_errors=True)


def _scope_of(db: str, key_id: str) -> dict:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT scope FROM agent_keys WHERE key_id = ?", (key_id,)).fetchone()
    finally:
        conn.close()
    assert row, f"key {key_id} 未落库"
    return json.loads(row[0])


def test_key_create_ws_flag_sets_scope_ws(cli_db):
    """--ws（ws=True）→ scope.ws=true 返回体与落库一致。"""
    r = hub_cli.cmd_key_create("ext", "/gateway/read", "", "", "", cli_db, ws=True)
    assert r["status"] == "created"
    assert r["scope"].get("ws") is True, "返回体 scope 应含 ws=true"
    assert _scope_of(cli_db, r["key_id"]).get("ws") is True, "落库 scope 应含 ws=true"


def test_key_create_without_ws_keeps_fail_closed(cli_db):
    """不声明 --ws：scope 无 ws 键——routes_ws fail-closed 拒连口径不变。"""
    r = hub_cli.cmd_key_create("ext", "", "", "", "", cli_db)
    assert r["status"] == "created"
    assert "ws" not in r["scope"]
    assert "ws" not in _scope_of(cli_db, r["key_id"])


def test_cli_main_ws_flag_wiring(cli_db, capsys):
    """argparse 接线：CLI `--ws` 透传进 scope（main 成功路径 exit 0）。"""
    with pytest.raises(SystemExit) as ei:
        hub_cli.main(["key", "create", "--agent", "ext", "--ws", "--db", cli_db])
    assert ei.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "created"
    assert out["scope"].get("ws") is True
    assert _scope_of(cli_db, out["key_id"]).get("ws") is True
