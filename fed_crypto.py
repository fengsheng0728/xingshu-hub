"""P2: 联邦 AES-GCM 加密信道 — 加密封装 + 防重放

跨 Hub 流量（proxy/disclose）从明文 HTTP 升级为 AES-GCM 应用层加密。
密钥 = 配对握手时 HKDF 派生的 session_key（落库 team_members.shared_secret）。

传输格式（X-Hub-Crypto: v1 头标识）：
    旧格式（默认）：body = {nonce: <12B hex>, ciphertext: <AESGCM(nonce, plaintext) hex>}
    新格式（联邦调用点 with_ts=True）：body = {nonce, ts, ciphertext}，
        ts = UTC epoch 秒字符串，作为 AES-GCM aad 认证（篡改 ts 即解密失败）。

防重放（T17 升级）：
    - nonce 缓存按 (peer, nonce) 隔离，5 分钟窗口
    - 新格式强制时间戳窗口：abs(now - ts) <= 300s，超窗/非数字 fail-closed 拒绝
    - 可选 SQLite 落库（init_replay_store）：重启后窗口内重放仍被拒；
      库故障降级纯内存并打一次 warning，不阻断解密
"""
import logging
import os
import sqlite3
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 防重放 nonce 缓存：(peer, nonce) -> 时间戳，5 分钟窗口
_REPLAY_WINDOW = 300  # 秒
_replay_cache: dict = {}
_replay_lock = threading.Lock()

# T17: SQLite 落库（独立文件，不写主库 sync_hub.db）。None = 纯内存（测试默认）
_replay_db_path: str = ""
_db_warned = False
_DB_RETENTION_SEC = 3600  # 惰性清理阈值：登记时顺带删 1 小时前的记录

_LOG = logging.getLogger(__name__)


def encrypt_payload(key: bytes, plaintext: bytes, with_ts: bool = False) -> dict:
    """AES-GCM 加密：12B 随机 nonce + 密文。

    with_ts=False（默认）：{nonce, ciphertext}，aad=None——integrations 兼容，零变化。
    with_ts=True（联邦调用点显式开启）：{nonce, ts, ciphertext}，aad=ts.encode()。"""
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    if with_ts:
        ts = str(int(time.time()))
        ciphertext = aesgcm.encrypt(nonce, plaintext, ts.encode())
        return {"nonce": nonce.hex(), "ts": ts, "ciphertext": ciphertext.hex()}
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return {"nonce": nonce.hex(), "ciphertext": ciphertext.hex()}


def decrypt_payload(key: bytes, enc: dict, peer: str = "") -> bytes:
    """AES-GCM 解密 + 防重放。重复 nonce / 错 key / 篡改 / 超窗 → 抛异常。

    enc 含 ts → 新格式：aad=ts 认证 → 超窗校验（fail-closed）→ (peer, nonce) 防重放。
    enc 无 ts → 旧格式：维持原逻辑（nonce 缓存），升级兼容期。"""
    nonce_hex = enc.get("nonce", "")
    ct_hex = enc.get("ciphertext", "")
    if not nonce_hex or not ct_hex:
        raise ValueError("缺少 nonce 或 ciphertext")
    ts = enc.get("ts")
    if ts is not None:
        # 新格式：ts 被 aad 认证，篡改即解密失败
        aesgcm = AESGCM(key)
        plain = aesgcm.decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex),
                               str(ts).encode())
        # 时间戳窗口：超窗/解析失败一律拒绝（fail-closed）
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            raise ValueError("timestamp outside window: ts 非数字")
        if abs(time.time() - ts_int) > _REPLAY_WINDOW:
            raise ValueError("timestamp outside window")
        if not check_replay(nonce_hex, peer):
            raise ValueError("Replay detected: nonce 已使用")
        return plain
    # 旧格式：维持现逻辑
    if not check_replay(nonce_hex, peer):
        raise ValueError("Replay detected: nonce 已使用")
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex), None)


def check_replay(nonce_hex: str, peer: str = "") -> bool:
    """登记 (peer, nonce)；若已见过（5min 窗口内，或库中存在）返回 False（重放），首次返回 True。"""
    now = time.time()
    cache_key = (peer, nonce_hex)
    with _replay_lock:
        # 清理过期
        expired = [k for k, ts in _replay_cache.items() if now - ts > _REPLAY_WINDOW]
        for k in expired:
            _replay_cache.pop(k, None)
        if cache_key in _replay_cache:
            return False
        # 内存 miss → 查库兜底（重启后窗口内重放仍被拒）
        if _db_seen(peer, nonce_hex):
            return False
        _replay_cache[cache_key] = now
        _db_record(peer, nonce_hex, now)
        return True


def clear_replay_cache():
    """测试用：清空 nonce 缓存"""
    with _replay_lock:
        _replay_cache.clear()


# ───────────────────────── T17: SQLite 落库 ─────────────────────────

def init_replay_store(db_path: str = None):
    """启用防重放落库。None/不调用 = 纯内存（现状，测试默认）。

    独立 SQLite 文件（不写主库），建表失败降级纯内存，不抛异常。"""
    global _replay_db_path, _db_warned
    _replay_db_path = db_path or ""
    _db_warned = False
    if not _replay_db_path:
        return
    try:
        conn = _db_conn()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS replay_nonce("
                "peer TEXT NOT NULL, nonce TEXT NOT NULL, seen_ts REAL, "
                "PRIMARY KEY(peer, nonce))"
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _warn_db_once(f"init 失败: {e}")


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_replay_db_path, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _warn_db_once(msg: str):
    """库故障降级纯内存：只打一次 warning，不阻断解密（防 replay 库故障拖垮联邦）。"""
    global _db_warned
    if not _db_warned:
        _db_warned = True
        _LOG.warning("replay store 降级纯内存: %s", msg)


def _db_seen(peer: str, nonce_hex: str) -> bool:
    """库中是否已存在 (peer, nonce)。未启用/故障 → False（由内存缓存兜底）。"""
    if not _replay_db_path:
        return False
    try:
        conn = _db_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM replay_nonce WHERE peer=? AND nonce=?",
                (peer, nonce_hex),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception as e:
        _warn_db_once(f"查询失败: {e}")
        return False


def _db_record(peer: str, nonce_hex: str, now: float):
    """登记入库 + 惰性清理 1 小时前记录（登记路径顺带，不单独开线程）。"""
    if not _replay_db_path:
        return
    try:
        conn = _db_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO replay_nonce(peer, nonce, seen_ts) VALUES(?,?,?)",
                (peer, nonce_hex, now),
            )
            conn.execute("DELETE FROM replay_nonce WHERE seen_ts < ?", (now - _DB_RETENTION_SEC,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _warn_db_once(f"写入失败: {e}")
