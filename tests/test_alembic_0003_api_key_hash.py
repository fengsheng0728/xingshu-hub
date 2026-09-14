"""T1-2：alembic 0003 agents.api_key 哈希化迁移验收。

配方同 test_alembic_0002_schema.py（subprocess alembic + tmp 库，不连真实 Hub）：
- 升到 0002 → 植入存量明文行（api_key / api_key_prev）→ upgrade head(0003)
- 断言：hash 列存在、明文列清空、api_key_hash == sha256(原明文) 逐行核对
- 幂等：二次 upgrade head schema 与数据不变
"""
import hashlib
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _upgrade(db_path, target="head"):
    env = dict(os.environ, SYNC_HUB_DB=str(db_path))
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"alembic upgrade {target} 失败:\n{r.stdout}\n{r.stderr}"


def _cols(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _agents_rows(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT agent_id, COALESCE(api_key,''), COALESCE(api_key_prev,''),"
        " COALESCE(api_key_hash,''), COALESCE(api_key_prev_hash,'') FROM agents"
    ).fetchall()
    conn.close()
    return rows


def test_0003_migrates_plaintext_to_hash(tmp_path):
    """存量明文 → hash 落列 + 明文清空，sha256 逐行核对。"""
    db = str(tmp_path / "mig.db")
    _upgrade(db, "0002_freeze_incremental_alters")
    # 植入存量明文行（模拟 0003 前的生产形态）
    k1, k1_prev = "plain-key-alpha", "plain-prev-alpha"
    k2 = "plain-key-beta"
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO agents (agent_id, api_key, api_key_prev) VALUES (?,?,?)",
                 ("ag-alpha", k1, k1_prev))
    conn.execute("INSERT INTO agents (agent_id, api_key) VALUES (?,?)", ("ag-beta", k2))
    conn.execute("INSERT INTO agents (agent_id, api_key) VALUES (?,?)", ("ag-empty", ""))
    conn.commit()
    conn.close()

    _upgrade(db)  # → 0003

    conn = sqlite3.connect(db)
    assert "api_key_hash" in _cols(conn, "agents")
    assert "api_key_prev_hash" in _cols(conn, "agents")
    conn.close()

    rows = {r[0]: r[1:] for r in _agents_rows(db)}
    # 明文清零
    for aid, (mk, mkp, mh, mhp) in rows.items():
        assert mk == "" and mkp == "", f"{aid} 明文未清空"
    # hash 正确（真实 sha256 输出核对）
    assert rows["ag-alpha"][2] == hashlib.sha256(k1.encode()).hexdigest()
    assert rows["ag-alpha"][3] == hashlib.sha256(k1_prev.encode()).hexdigest()
    assert rows["ag-beta"][2] == hashlib.sha256(k2.encode()).hexdigest()
    assert rows["ag-beta"][3] == ""  # 无 prev → 不填
    # 空明文行不生成 hash
    assert rows["ag-empty"][2] == ""


def test_0003_upgrade_idempotent(tmp_path):
    """二次 upgrade head：schema 与数据均不变（不重算 hash、不出错）。"""
    db = str(tmp_path / "idem.db")
    _upgrade(db, "0002_freeze_incremental_alters")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO agents (agent_id, api_key) VALUES (?,?)",
                 ("ag-1", "plain-key-1"))
    conn.commit()
    conn.close()
    _upgrade(db)
    before = _agents_rows(db)
    _upgrade(db)
    assert _agents_rows(db) == before
    h = hashlib.sha256(b"plain-key-1").hexdigest()
    assert before[0][3] == h and before[0][1] == ""
