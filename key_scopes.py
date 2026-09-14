# -*- coding: utf-8 -*-
"""
key_scopes.py — scoped API key（S1K，2026-08-07）

三层 scope 模型（prompt 1e）：
  endpoints   → 该 key 能调哪些端点（前缀匹配；空 = 全部）
  data_domain → 数据域（部门/项目标签）；disclosure 判定时叠加
  level_cap   → 最高披露级别（走 disclosure 判定叠加 min，零平行逻辑）

存储：agent_keys 表，key_hash=SHA256(明文)，不存明文。
认证：LocalProvider.authenticate 双模式（2026-09-06 B1 定案）：
      A 换发模式——token 命中 agents.api_key 且 agent_keys 有同串登记 → scope 附加
        （key 即 agent 的 api_key，guard_liveness 探针 5 语义）；
      B 独立模式——sk- key 不依赖 agents.api_key 相等，顶层按 key_hash 命中
        agent_keys → scope 附加，subject_id = key 绑定的 agent_id（B1 外部协作者，
        修复前只有模式 A，而签发从不把 sk- 写回 agents.api_key → 纯 scoped key 永远 401）。
"""
import hashlib
import json
import logging
import os
import sqlite3
import secrets
import threading
from typing import Dict, List, Optional

logger = logging.getLogger("key_scopes")

_DEFAULT_SCOPE = {"endpoints": [], "data_domain": [], "level_cap": ""}


def key_hash(raw: str) -> str:
    """SHA256 哈希（不存明文）"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def gen_key() -> str:
    """生成 32 字节随机 key（前缀 sk- 区分 scoped key）"""
    return "sk-" + secrets.token_urlsafe(32)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class ScopedKeyStore:
    """agent_keys 表读写（线程安全）"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()

    # ── 创建 ──

    def create(self, agent_id: str, scope: dict, created_by: str = "",
               expires_at: str = "") -> Dict:
        """创建 scoped key，返回 {key_id, key(明文仅此一次), scope}"""
        raw_key = gen_key()
        scope_norm = {**_DEFAULT_SCOPE, **{k: v for k, v in (scope or {}).items() if v}}
        with self._lock:
            conn = _connect(self._db_path)
            key_id = "key-" + secrets.token_hex(6)
            conn.execute(
                """INSERT INTO agent_keys
                   (key_id, agent_id, key_hash, scope, status, created_by,
                    created_at, expires_at, last_used_at, call_count)
                   VALUES (?, ?, ?, ?, 'active', ?, datetime('now'), ?, '', 0)""",
                (key_id, agent_id, key_hash(raw_key),
                 json.dumps(scope_norm, ensure_ascii=False),
                 created_by, expires_at or ""),
            )
            conn.commit()
            conn.close()
        return {"key_id": key_id, "key": raw_key, "agent_id": agent_id,
                "scope": scope_norm}

    # ── 认证查询 ──

    def lookup_by_hash(self, raw_key: str) -> Optional[Dict]:
        """按明文 key 查 scoped key（含 scope/status/过期）。未命中返回 None。"""
        h = key_hash(raw_key)
        with self._lock:
            conn = _connect(self._db_path)
            try:
                conn.execute("SELECT 1 FROM agent_keys LIMIT 1")
            except Exception:
                conn.close()
                return None
            row = conn.execute(
                "SELECT * FROM agent_keys WHERE key_hash = ?", (h,)).fetchone()
            conn.close()
        if not row:
            return None
        d = dict(row)
        if d["status"] != "active":
            return None
        if d.get("expires_at"):
            # expires_at 格式: 'YYYY-MM-DD HH:MM:SS' 或 ISO
            from datetime import datetime
            try:
                exp = datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00"))
                if exp.tzinfo is not None:
                    exp = exp.astimezone()
                else:
                    exp = exp.replace(tzinfo=datetime.now().astimezone().tzinfo)
                if datetime.now().astimezone() > exp:
                    return None
            except Exception:
                # fail-closed（2026-09-09 T13）：expires_at 非空但解析失败 → 视为
                # 无效/过期，拒绝（原 pass 继续放行，当永不过期）。空值（永久 key）不受影响。
                logger.warning("agent_key %s expires_at unparsable, denied: %r",
                               d.get("key_id"), d["expires_at"])
                return None
        try:
            d["scope"] = json.loads(d["scope"] or "{}")
        except Exception:
            d["scope"] = dict(_DEFAULT_SCOPE)
        return d

    def touch(self, key_id: str) -> None:
        """调用画像：更新 last_used_at + call_count"""
        with self._lock:
            conn = _connect(self._db_path)
            conn.execute(
                "UPDATE agent_keys SET last_used_at = datetime('now'), "
                "call_count = call_count + 1 WHERE key_id = ?", (key_id,))
            conn.commit()
            conn.close()

    # ── 吊销/轮换 ──

    def revoke(self, key_id: str) -> bool:
        """吊销：状态置 revoked（认证立即失效，60s 内全端点）。
        幂等收紧（2026-09-06 B1）：仅当该 key 仍 active 时置 revoked——
        已吊销/不存在均返回 False（调用方 404「不存在或已吊销」），
        原实现 UPDATE 不排除 revoked 行 → 二次吊销误报成功。"""
        with self._lock:
            conn = _connect(self._db_path)
            cur = conn.execute(
                "UPDATE agent_keys SET status='revoked' "
                "WHERE key_id=? AND status != 'revoked'", (key_id,))
            conn.commit()
            changed = cur.rowcount > 0
            conn.close()
        return changed

    def list_keys(self, agent_id: str = "") -> List[Dict]:
        """列 key（调用画像），不含 key_hash 明文"""
        with self._lock:
            conn = _connect(self._db_path)
            if agent_id:
                rows = conn.execute(
                    "SELECT key_id, agent_id, scope, status, created_by, created_at, "
                    "expires_at, last_used_at, call_count FROM agent_keys "
                    "WHERE agent_id = ? ORDER BY created_at DESC", (agent_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key_id, agent_id, scope, status, created_by, created_at, "
                    "expires_at, last_used_at, call_count FROM agent_keys "
                    "ORDER BY created_at DESC").fetchall()
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["scope"] = json.loads(d["scope"] or "{}")
            except Exception:
                d["scope"] = dict(_DEFAULT_SCOPE)
            out.append(d)
        return out


# ── 模块级 store（惰性，跟随 CONFIG.DB_PATH） ──
_store = None
_store_lock = threading.Lock()


def get_store(db_path: str = "") -> ScopedKeyStore:
    global _store
    if _store is None:
        if not db_path:
            from models import CONFIG
            db_path = CONFIG.DB_PATH
        with _store_lock:
            if _store is None:
                _store = ScopedKeyStore(db_path)
    return _store
