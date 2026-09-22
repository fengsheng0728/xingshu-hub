# -*- coding: utf-8 -*-
"""employee_keys.py — 员工凭据账本（CD-072，2026-09-20）

背景：阶段 1e 的员工凭据是 `employee_accounts.key_hash` 单列——每人只有一把、补签即覆盖、
没有 key_id/过期/调用画像，UI 无法回答「这把钥匙谁在用、什么时候用过」。

本模块把员工凭据升级成**账本**（形态对齐 `key_scopes.py` / `agent_keys`）：
- 每把一个 `key_id`，同一员工可多把并存；
- 各自 `expires_at`（空=不过期）、`status`（active/revoked）、`last_used_at` + `call_count`（调用画像）；
- 可按 `key_id` 单把吊销，也可整人吊销（`revoke_all`）。

兼容口径：
- 明文不落库（只存 SHA256），明文仅签发时返回一次（同 S1K）；
- `employee_accounts.key_hash` 保留为**最近一把的镜像**（双写）→ 老代码/回滚路径仍可用；
- 认证侧先查账本，账本未命中再回落 legacy `employee_accounts.key_hash`
  （覆盖回填未跑的库/测试直插行）。
"""
import hashlib
import logging
import secrets
import sqlite3
import threading
from typing import Dict, List, Optional

logger = logging.getLogger("employee_keys")


def key_hash(raw: str) -> str:
    """SHA256 哈希（不存明文）"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def gen_key() -> str:
    """生成员工凭据明文（emp_ 前缀，与阶段 1e 既有形态一致）"""
    return "emp_" + secrets.token_hex(24)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _expired(expires_at: str) -> bool:
    """expires_at 已过期？空=永久（False）；非空但解析失败 → fail-closed（True）。

    口径同 key_scopes.ScopedKeyStore.lookup_by_hash（2026-09-09 T13）。
    """
    if not expires_at:
        return False
    from datetime import datetime
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return datetime.now().astimezone() > exp
    except Exception:
        logger.warning("employee key expires_at unparsable, denied: %r", expires_at)
        return True


class EmployeeKeyStore:
    """employee_keys 表读写（线程安全）"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()

    # ── 签发 ──

    def create(self, employee_id: str, label: str = "", created_by: str = "",
               expires_at: str = "") -> Dict:
        """签一把新凭据，返回 {key_id, key(明文仅此一次)}。不覆盖同员工已有凭据。"""
        raw = gen_key()
        with self._lock:
            conn = _connect(self._db_path)
            key_id = "key-" + secrets.token_hex(6)
            conn.execute(
                "INSERT INTO employee_keys (key_id, employee_id, key_hash, label, status,"
                " created_by, created_at, expires_at, last_used_at, call_count)"
                " VALUES (?, ?, ?, ?, 'active', ?, datetime('now'), ?, '', 0)",
                (key_id, employee_id, key_hash(raw), label or "", created_by, expires_at or ""),
            )
            conn.commit()
            conn.close()
        return {"key_id": key_id, "key": raw, "employee_id": employee_id}

    # ── 认证查询 ──

    def lookup_by_hash(self, raw_key: str) -> Optional[Dict]:
        """按明文查账本（含 status/过期判定）。未命中/已吊销/已过期 → None。"""
        h = key_hash(raw_key)
        with self._lock:
            conn = _connect(self._db_path)
            try:
                conn.execute("SELECT 1 FROM employee_keys LIMIT 1")
            except Exception:
                conn.close()
                return None  # 表未迁移
            row = conn.execute("SELECT * FROM employee_keys WHERE key_hash = ?", (h,)).fetchone()
            conn.close()
        if not row:
            return None
        d = dict(row)
        if (d.get("status") or "") != "active":
            return None
        if _expired(d.get("expires_at") or ""):
            return None
        return d

    def touch(self, key_id: str) -> None:
        """调用画像：last_used_at + call_count"""
        with self._lock:
            conn = _connect(self._db_path)
            conn.execute(
                "UPDATE employee_keys SET last_used_at = datetime('now'),"
                " call_count = call_count + 1 WHERE key_id = ?", (key_id,))
            conn.commit()
            conn.close()

    # ── 吊销 ──

    def revoke(self, key_id: str) -> bool:
        """单把吊销（幂等：已吊销/不存在 → False）"""
        with self._lock:
            conn = _connect(self._db_path)
            cur = conn.execute(
                "UPDATE employee_keys SET status = 'revoked'"
                " WHERE key_id = ? AND status != 'revoked'", (key_id,))
            conn.commit()
            n = cur.rowcount
            conn.close()
        return n > 0

    def revoke_all(self, employee_id: str) -> int:
        """整人吊销（所有 active → revoked），返回吊销把数"""
        with self._lock:
            conn = _connect(self._db_path)
            cur = conn.execute(
                "UPDATE employee_keys SET status = 'revoked'"
                " WHERE employee_id = ? AND status != 'revoked'", (employee_id,))
            conn.commit()
            n = cur.rowcount
            conn.close()
        return n

    def list_keys(self, employee_id: str) -> List[Dict]:
        """列某员工凭据（调用画像），不含 key_hash"""
        with self._lock:
            conn = _connect(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT key_id, employee_id, label, status, created_by, created_at,"
                    " expires_at, last_used_at, call_count FROM employee_keys"
                    " WHERE employee_id = ? ORDER BY created_at DESC, key_id", (employee_id,)
                ).fetchall()
            except Exception:
                rows = []
            conn.close()
        return [dict(r) for r in rows]

    def count_active(self, employee_id: str) -> int:
        """该员工当前有效（active 且未过期）凭据数"""
        with self._lock:
            conn = _connect(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT expires_at FROM employee_keys"
                    " WHERE employee_id = ? AND status = 'active'", (employee_id,)).fetchall()
            except Exception:
                rows = []
            conn.close()
        return sum(0 if _expired(r["expires_at"] or "") else 1 for r in rows)

    def latest_active_hash(self, employee_id: str) -> str:
        """最新一把有效凭据的 hash（仅供 legacy `employee_accounts.key_hash` 镜像，不经 API 暴露）"""
        with self._lock:
            conn = _connect(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT key_hash, expires_at FROM employee_keys"
                    " WHERE employee_id = ? AND status = 'active'"
                    " ORDER BY created_at DESC, key_id DESC", (employee_id,)).fetchall()
            except Exception:
                rows = []
            conn.close()
        for r in rows:
            if not _expired(r["expires_at"] or ""):
                return r["key_hash"]
        return ""

    def key_of_employee(self, key_id: str) -> Optional[str]:
        """key_id → employee_id（吊销时的归属校验）"""
        with self._lock:
            conn = _connect(self._db_path)
            try:
                row = conn.execute(
                    "SELECT employee_id FROM employee_keys WHERE key_id = ?", (key_id,)).fetchone()
            except Exception:
                row = None
            conn.close()
        return row["employee_id"] if row else None


# ── 模块级 store（惰性，跟随 CONFIG.DB_PATH）──
_store = None
_store_lock = threading.Lock()


def get_store(db_path: str = "") -> EmployeeKeyStore:
    global _store
    if _store is None:
        if not db_path:
            from models import CONFIG
            db_path = CONFIG.DB_PATH
        with _store_lock:
            if _store is None:
                _store = EmployeeKeyStore(db_path)
    return _store
