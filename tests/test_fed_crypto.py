"""P2: 联邦 AES-GCM 加密信道 — 单测（加密/解密/防重放）

- 加密封装 roundtrip（cryptography AESGCM，12B nonce + 密文）
- 错密钥解密失败
- nonce 防重放：同 nonce 密文重复提交被拒
"""
import os
import sys
import json
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fed_crypto import encrypt_payload, decrypt_payload, check_replay, clear_replay_cache


def test_roundtrip():
    """加密 → 解密还原原文"""
    key = os.urandom(32)
    body = json.dumps({"requester_agent_id": "a", "query": "机密内容"}).encode()
    enc = encrypt_payload(key, body)
    assert isinstance(enc, dict)
    assert "nonce" in enc and "ciphertext" in enc
    # nonce 12B hex
    assert len(bytes.fromhex(enc["nonce"])) == 12
    dec = decrypt_payload(key, enc)
    assert json.loads(dec) == json.loads(body)


def test_wrong_key_fails():
    """错密钥解密必须失败"""
    key1 = os.urandom(32)
    key2 = os.urandom(32)
    enc = encrypt_payload(key1, b"secret")
    with pytest.raises(Exception):
        decrypt_payload(key2, enc)


def test_replay_detection():
    """同一 nonce 的密文重复提交 → 被拒（5min 窗口）"""
    clear_replay_cache()
    key = os.urandom(32)
    enc = encrypt_payload(key, b"payload")
    # 首次解密成功（内部登记 nonce）
    assert decrypt_payload(key, enc)
    # 重放：同 nonce 再次解密 → 拒绝
    with pytest.raises(Exception):
        decrypt_payload(key, enc)  # 重放被拒


def test_replay_unique_nonce():
    """不同 nonce 不误伤"""
    clear_replay_cache()
    key = os.urandom(32)
    e1 = encrypt_payload(key, b"a")
    e2 = encrypt_payload(key, b"b")
    assert e1["nonce"] != e2["nonce"]
    assert decrypt_payload(key, e1)  # 独立 nonce 可解
    assert decrypt_payload(key, e2)


def test_tamper_detection():
    """密文篡改 → 解密失败（AES-GCM tag 校验）"""
    key = os.urandom(32)
    enc = encrypt_payload(key, b"important")
    # 篡改密文最后一个字节
    ct = bytearray(bytes.fromhex(enc["ciphertext"]))
    ct[-1] ^= 0x01
    tampered = {"nonce": enc["nonce"], "ciphertext": ct.hex()}
    with pytest.raises(Exception):
        decrypt_payload(key, tampered)


# ───────────────────────── T17: 联邦防重放升级 ─────────────────────────

from fed_crypto import init_replay_store
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _enc_with_ts(key: bytes, plaintext: bytes, ts) -> dict:
    """构造指定 ts 的新格式 envelope（aad=ts），用于超窗/篡改场景"""
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext, str(ts).encode())
    return {"nonce": nonce.hex(), "ts": str(ts), "ciphertext": ct.hex()}


def test_ts_roundtrip():
    """T17-3.1: with_ts=True → envelope 含 ts 字段，解密还原"""
    clear_replay_cache()
    key = os.urandom(32)
    body = json.dumps({"query": "带时间戳"}).encode()
    enc = encrypt_payload(key, body, with_ts=True)
    assert "ts" in enc and "nonce" in enc and "ciphertext" in enc
    assert json.loads(decrypt_payload(key, enc, peer="hub-a")) == json.loads(body)


def test_default_format_unchanged():
    """T17-3.1: 默认 with_ts=False 零变化——无 ts 字段（integrations 兼容）"""
    clear_replay_cache()
    key = os.urandom(32)
    enc = encrypt_payload(key, b"cfg")
    assert "ts" not in enc
    assert set(enc.keys()) == {"nonce", "ciphertext"}


def test_ts_tamper_fails():
    """T17-3.1: 篡改 ts 值 → aad 认证失败 → 抛异常"""
    clear_replay_cache()
    key = os.urandom(32)
    enc = encrypt_payload(key, b"payload", with_ts=True)
    enc["ts"] = str(int(enc["ts"]) + 1)  # 改 ts → aad 不匹配
    with pytest.raises(Exception):
        decrypt_payload(key, enc, peer="hub-a")


def test_ts_window_enforced():
    """T17-3.1: 超窗（now-301s）拒绝；窗口内（now-299s）通过"""
    clear_replay_cache()
    key = os.urandom(32)
    now = int(time.time())
    stale = _enc_with_ts(key, b"old", now - 301)
    with pytest.raises(Exception, match="timestamp"):
        decrypt_payload(key, stale, peer="hub-a")
    fresh = _enc_with_ts(key, b"ok", now - 299)
    assert decrypt_payload(key, fresh, peer="hub-a") == b"ok"


def test_ts_non_numeric_rejected():
    """T17-3.1: ts 非数字 → fail-closed 按超窗拒绝"""
    clear_replay_cache()
    key = os.urandom(32)
    enc = _enc_with_ts(key, b"x", "not-a-number")
    with pytest.raises(Exception, match="timestamp"):
        decrypt_payload(key, enc, peer="hub-a")


def test_peer_isolation():
    """T17-3.2: 同 nonce 不同 peer 都放行；同 (peer, nonce) 二次拒绝"""
    clear_replay_cache()
    key = os.urandom(32)
    enc = encrypt_payload(key, b"payload", with_ts=True)
    assert decrypt_payload(key, enc, peer="hub-a")
    assert decrypt_payload(key, enc, peer="hub-b")  # 不同 peer 不误伤
    with pytest.raises(Exception, match="Replay"):
        decrypt_payload(key, enc, peer="hub-a")  # 同 (peer,nonce) 重放被拒


def test_check_replay_peer_param():
    """T17-3.2: check_replay(nonce, peer) 签名；默认 peer="" 兼容既有语义"""
    clear_replay_cache()
    assert check_replay("aa" * 12) is True
    assert check_replay("aa" * 12) is False          # 默认 peer 重放拒绝
    assert check_replay("aa" * 12, peer="p1") is True  # 换 peer 放行
    assert check_replay("aa" * 12, peer="p1") is False


def test_sqlite_replay_fallback(tmp_path):
    """T17-3.3: 落库后清空内存（模拟重启）→ 同密文再解密仍被拒（查库兜底）"""
    clear_replay_cache()
    db = str(tmp_path / "replay_nonce.db")
    init_replay_store(db)
    try:
        key = os.urandom(32)
        enc = encrypt_payload(key, b"payload", with_ts=True)
        assert decrypt_payload(key, enc, peer="hub-a")
        clear_replay_cache()  # 模拟重启：内存缓存丢失
        with pytest.raises(Exception, match="Replay"):
            decrypt_payload(key, enc, peer="hub-a")  # 库兜底拒绝
    finally:
        init_replay_store(None)  # 复位纯内存，避免污染其他用例
        clear_replay_cache()


def test_legacy_format_compat():
    """T17-3.1: 旧格式（无 ts）走旧逻辑：一次放行、二次拒绝、清缓存后放行"""
    clear_replay_cache()
    key = os.urandom(32)
    enc = encrypt_payload(key, b"legacy")  # with_ts=False 默认
    assert decrypt_payload(key, enc)
    with pytest.raises(Exception, match="Replay"):
        decrypt_payload(key, enc)
    clear_replay_cache()
    assert decrypt_payload(key, enc)


def test_main_wires_replay_store():
    """T17-3.4: 轻量断言 main.py 已接线 init_replay_store（不启动服务）"""
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py"),
                    encoding="utf-8").read()
    assert "init_replay_store" in main_src
    assert "replay_nonce.db" in main_src
