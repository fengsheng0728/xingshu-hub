# -*- coding: utf-8 -*-
"""CD-111：team_members.remote_api_key 哈希化验收（迁移 0014 + 写侧 hash + 读侧命中）。

配方同 test_alembic_0002_schema.py（subprocess alembic + tmp 库，不连真实 Hub、不绑端口）：
1. 迁移幂等：alembic upgrade head 两次不报错；remote_api_key_hash 列存在。
2. 回填正确：存量明文行 → hash == sha256(明文)，且明文列未被清空（与 0003 差异）。
3. 写入路径写 hash：直调 TeamMixin 写入逻辑（accept_pairing INSERT /
   _handle_pair_exchange INSERT + UPDATE），断言 remote_api_key_hash == sha256(明文)。
4. 读侧 hash 命中：routes_federation._is_paired_member_key 明文命中 / 不存在 → False /
   revoked → False（fail-closed 三态），monkeypatch CONFIG.DB_PATH 指到临时库。
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from models import CONFIG  # noqa: E402
from hub_mixins.team import TeamMixin  # noqa: E402


def _alembic(db_path, *args):
    env = dict(os.environ, SYNC_HUB_DB=str(db_path))
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"alembic {' '.join(args)} 失败:\n{r.stdout}\n{r.stderr}"


def _cols(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


class FakeHub(TeamMixin):
    """TeamMixin + 临时 DB + 事件收集（沿用 test_pairing_hardening 配方）。"""

    def __init__(self, db_path):
        self._db_path = db_path
        self.hub_id = "hub-test"
        self.hostname = "test-host"
        self.events = []

    def _db(self):
        return sqlite3.connect(self._db_path)

    async def _log_event(self, event_type, agent_id, payload):
        self.events.append((event_type, agent_id, payload))


def _make_team_members_sql(with_hash=True):
    hash_col = "            remote_api_key_hash TEXT,\n" if with_hash else ""
    return f"""CREATE TABLE IF NOT EXISTS team_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_agent_id TEXT NOT NULL,
            remote_hub_id TEXT NOT NULL,
            remote_hub_url TEXT NOT NULL,
            remote_agent_id TEXT NOT NULL,
            remote_api_key TEXT NOT NULL,
            hostname TEXT,
            user_name TEXT,
            role TEXT DEFAULT 'worker',
            department TEXT,
            paired_at TEXT NOT NULL,
            key_expires_at TEXT NOT NULL,
            last_heartbeat TEXT,
            revoked_at TEXT,
            team_id INTEGER,
            shared_secret TEXT,
{hash_col}            UNIQUE(local_agent_id, remote_hub_id)
        )"""


def _setup_write_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(_make_team_members_sql(with_hash=True))
    conn.execute("""CREATE TABLE IF NOT EXISTS pairing_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            hub_id_a TEXT NOT NULL,
            hub_id_b TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0,
            agent_id_a TEXT)""")
    conn.commit()
    conn.close()


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# ═══════════ 1. 迁移幂等 ═══════════

def test_0014_migration_idempotent(tmp_path):
    """alembic upgrade head 两次不报错；remote_api_key_hash 列存在。"""
    db = str(tmp_path / "idem.db")
    _alembic(db, "upgrade", "head")
    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    try:
        assert "remote_api_key_hash" in _cols(conn, "team_members"), \
            "remote_api_key_hash 列应存在"
    finally:
        conn.close()


# ═══════════ 2. 回填正确 ═══════════

def test_0014_backfill_preserves_plaintext(tmp_path):
    """存量明文 → hash 落列，sha256 核对；明文列未被清空（与 0003 差异）。"""
    db = str(tmp_path / "backfill.db")
    _alembic(db, "upgrade", "0013_agents_api_key_hash_backstop")
    plain_key = "stock-plain-key-abc"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO team_members (local_agent_id, remote_hub_id, remote_hub_url,"
        " remote_agent_id, remote_api_key, paired_at, key_expires_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("ag-1", "hub-1", "http://10.0.0.1:3060", "ra-1", plain_key,
         "2026-01-01T00:00:00", "2026-12-31T00:00:00"))
    conn.commit()
    conn.close()

    _alembic(db, "upgrade", "head")

    conn = sqlite3.connect(db)
    try:
        assert "remote_api_key_hash" in _cols(conn, "team_members")
        row = conn.execute(
            "SELECT remote_api_key, remote_api_key_hash FROM team_members"
            " WHERE local_agent_id = 'ag-1'").fetchone()
    finally:
        conn.close()
    plain, h = row
    expected_hash = _sha(plain_key)
    assert h == expected_hash, f"hash 应为 sha256(明文): {h!r} != {expected_hash!r}"
    assert plain == plain_key, f"明文列未被清空（与 0003 差异）: {plain!r}"


# ═══════════ 3. 写入路径写 hash ═══════════

def test_write_paths_write_hash(tmp_path, monkeypatch):
    """三条写入路径（INSERT ×2 + UPDATE ×1）都写 remote_api_key_hash == sha256(明文)。"""
    db_path = str(tmp_path / "write.db")
    _setup_write_db(db_path)
    hub = FakeHub(db_path)

    # ── 路径 A：accept_pairing INSERT（配对成功后写入对方） ──
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    test_key_a = "accept-pairing-key-xyz"

    def _fake_fetch(request, timeout):
        body = json.loads(request.data)
        initiator_pub = bytes.fromhex(body["dh_public"])
        my_priv = X25519PrivateKey.generate()
        my_pub = my_priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        shared = my_priv.exchange(X25519PublicKey.from_public_bytes(initiator_pub))
        sk = HKDF(algorithm=hashes.SHA256(), length=32,
                  salt=body["code"].encode(), info=b"xingshu-team-pairing-v1"
                  ).derive(shared)
        nonce = os.urandom(12)
        enc = AESGCM(sk).encrypt(nonce, test_key_a.encode(), None)
        return json.dumps({
            "dh_public": my_pub.hex(),
            "encrypted_key": enc.hex(),
            "nonce": nonce.hex(),
            "agent_id_a": "ra-accept",
        }).encode()

    monkeypatch.setattr("hub_mixins.team._fetch_url_sync", _fake_fetch)
    result_a = asyncio.run(hub.accept_pairing("agent-local", {
        "code": "111111",
        "remote_hub_url": "http://10.0.0.1:3060",
        "remote_hub_id": "hub-remote-a",
        "remote_agent_id": "ra-accept",
        "hostname": "host-a",
        "user_name": "user-a",
        "role": "worker",
        "department": "eng",
    }))
    assert result_a.get("status") == "paired", f"accept_pairing 失败: {result_a}"

    conn = sqlite3.connect(db_path)
    row_a = conn.execute(
        "SELECT remote_api_key, remote_api_key_hash FROM team_members"
        " WHERE local_agent_id = 'agent-local' AND remote_hub_id = 'hub-remote-a'"
    ).fetchone()
    conn.close()
    assert row_a is not None, "accept_pairing 应写入 team_members"
    plain_a, hash_a = row_a
    assert plain_a == test_key_a, f"明文列应为解密后 key: {plain_a!r}"
    assert hash_a == _sha(test_key_a), \
        f"accept_pairing INSERT 应写 hash: {hash_a!r} != {_sha(test_key_a)!r}"

    # ── 路径 B：_handle_pair_exchange INSERT（对方配对落库） ──
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO pairing_codes (code, hub_id_a, agent_id_a, expires_at)"
        " VALUES (?,?,?,datetime('now','+5 minutes'))",
        ("222222", "hub-local", "agent-init"))
    conn.commit()
    conn.close()

    initiator_priv = X25519PrivateKey.generate()
    initiator_pub = initiator_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    result_b = asyncio.run(hub._handle_pair_exchange("222222", {
        "dh_public": initiator_pub.hex(),
        "agent_id": "ra-exchange",
        "hostname": "host-b",
        "user_name": "user-b",
        "department": "eng",
        "remote_hub_url": "http://10.0.0.2:3060",
    }))
    assert "encrypted_key" in result_b, f"_handle_pair_exchange 失败: {result_b}"

    conn = sqlite3.connect(db_path)
    row_b = conn.execute(
        "SELECT remote_api_key, remote_api_key_hash FROM team_members"
        " WHERE local_agent_id = 'agent-init' AND remote_hub_id = 'hub-local'"
    ).fetchone()
    conn.close()
    assert row_b is not None, "_handle_pair_exchange 应写入 team_members"
    plain_b, hash_b = row_b
    assert plain_b, "remote_api_key 明文应非空"
    assert hash_b == _sha(plain_b), \
        f"_handle_pair_exchange INSERT 应写 hash: {hash_b!r} != {_sha(plain_b)!r}"

    # ── 路径 C：_handle_pair_exchange UPDATE（重配对刷新 key） ──
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO pairing_codes (code, hub_id_a, agent_id_a, expires_at)"
        " VALUES (?,?,?,datetime('now','+5 minutes'))",
        ("333333", "hub-local", "agent-init"))
    conn.commit()
    conn.close()

    initiator_priv2 = X25519PrivateKey.generate()
    initiator_pub2 = initiator_priv2.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    result_c = asyncio.run(hub._handle_pair_exchange("333333", {
        "dh_public": initiator_pub2.hex(),
        "agent_id": "ra-exchange",
        "hostname": "host-c",
        "user_name": "user-c",
        "department": "eng",
        "remote_hub_url": "http://10.0.0.2:3060",
    }))
    assert "encrypted_key" in result_c

    conn = sqlite3.connect(db_path)
    row_c = conn.execute(
        "SELECT remote_api_key, remote_api_key_hash FROM team_members"
        " WHERE local_agent_id = 'agent-init' AND remote_hub_id = 'hub-local'"
    ).fetchone()
    conn.close()
    plain_c, hash_c = row_c
    assert plain_c and plain_c != plain_b, "重配对应刷新 remote_api_key"
    assert hash_c == _sha(plain_c), \
        f"_handle_pair_exchange UPDATE 应写 hash: {hash_c!r} != {_sha(plain_c)!r}"


# ═══════════ 4. 读侧 hash 命中（fail-closed 三态） ═══════════

def test_read_side_hash_hit(tmp_path, monkeypatch):
    """_is_paired_member_key：明文命中 True / 不存在 False / revoked False。"""
    db_path = str(tmp_path / "read.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    import db as db_mod
    db_mod.init_db()

    from routes_federation import _is_paired_member_key

    plain_ok = "valid-paired-key-001"
    plain_revoked = "revoked-paired-key-002"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO team_members (local_agent_id, remote_hub_id, remote_hub_url,"
        " remote_agent_id, remote_api_key, remote_api_key_hash,"
        " paired_at, key_expires_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("a1", "h1", "http://10.0.0.1:3060", "r1", plain_ok, _sha(plain_ok),
         "2026-01-01T00:00:00", "2026-12-31T00:00:00"))
    conn.execute(
        "INSERT INTO team_members (local_agent_id, remote_hub_id, remote_hub_url,"
        " remote_agent_id, remote_api_key, remote_api_key_hash,"
        " paired_at, key_expires_at, revoked_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("a2", "h2", "http://10.0.0.1:3060", "r2", plain_revoked,
         _sha(plain_revoked),
         "2026-01-01T00:00:00", "2026-12-31T00:00:00", "2026-06-01T00:00:00"))
    conn.commit()
    conn.close()

    # fail-closed 三态
    assert _is_paired_member_key(plain_ok) is True, "明文能命中（未撤销）"
    assert _is_paired_member_key("nonexistent-key-zzz") is False, "不存在 → False"
    assert _is_paired_member_key(plain_revoked) is False, "revoked → False"
