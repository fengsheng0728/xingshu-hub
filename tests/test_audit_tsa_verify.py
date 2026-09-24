# -*- coding: utf-8 -*-
"""CD-034 R4（2026-09-21 拍板）：RFC3161 TSA 令牌离线验签 + 指纹钉扎 + 远端锚取回比对

背景：CD-034 R3 的 tsa_stamp 只做了「盖章 + 廉价自洽检查（imprint 在 token 里）」，
没做真验签——不验签的盖章防不了伪造令牌；验签不钉指纹防不了「谁都能自签一个
合法令牌」。本文件验收拍板①②：
  ① verify_tsa_token：解析 → 签名验证 → 证书链校验 → 指纹钉扎（空配置 = fail-closed
     unverified，不假绿）→ imprint == SHA256(链头) 比对；
  ② cryptography 仅验签路径延迟导入，缺失 → unverified 而非 crash；
  附带 fetch_and_verify_anchors：从 AUDIT_ANCHOR_URLS 取回远端锚比对本地链
  （判据与 verify_tsa 相同：锚节点在链中即可，链正常增长不算不一致）。

离线夹具：现场用 cryptography 生成自签 CA + TSA 证书，手工拼 RFC3161
TimeStampResp DER（复用 audit_chain._der/_der_int 编码器），ECDSA 真签名——
全程不出网、不依赖公网 TSA。

覆盖：有效令牌(钉叶子/钉CA)通过、摘要被篡改拒绝、签名被篡改拒绝、指纹不匹配
拒绝、无指纹配置 unverified、cryptography 缺失 unverified、env/JSON 配置加载、
远端锚 match_head/match_chain/mismatch/不可达/SSRF URL 校验。
"""
import hashlib
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from audit_chain import (  # noqa: E402
    AuditChain, _der, _der_int, _validate_anchor_url,
    fetch_and_verify_anchors, verify_tsa_token,
)

pytest.importorskip("cryptography", reason="造离线夹具需要 cryptography（验签对象本身）")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

CHAIN_HEAD = hashlib.sha256(b"cd034-test-chain-head").hexdigest()


# ── OID / DER 夹具工具 ──

def _oid(s: str) -> bytes:
    """OID 点分串 → DER TLV。"""
    parts = [int(x) for x in s.split(".")]
    out = bytes([40 * parts[0] + parts[1]])
    for v in parts[2:]:
        stack = [v & 0x7F]
        v >>= 7
        while v:
            stack.append(0x80 | (v & 0x7F))
            v >>= 7
        out += bytes(reversed(stack))
    return _der(0x06, out)


_SHA256_ALGID = bytes.fromhex("300d06096086480165030402010500")  # SEQUENCE{OID sha256, NULL}
_ECDSA_SHA256_ALGID = _der(0x30, _oid("1.2.840.10045.4.3.2"))
_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_OID_TSTINFO = "1.2.840.113549.1.9.16.1.4"
_OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
_OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
_TEST_POLICY = "1.3.6.1.4.1.99999.1"  # 测试用私有 policy OID


@pytest.fixture(scope="module")
def pki():
    """自签 CA + CA 签发的 TSA 证书（ECDSA P-256，生成快）。"""
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CD034 Test CA")])
    ca_cert = (x509.CertificateBuilder()
               .subject_name(ca_name).issuer_name(ca_name)
               .public_key(ca_key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=1))
               .not_valid_after(now + timedelta(days=3650))
               .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                              critical=True)
               .sign(ca_key, hashes.SHA256()))
    tsa_key = ec.generate_private_key(ec.SECP256R1())
    tsa_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CD034 Test TSA")])
    tsa_cert = (x509.CertificateBuilder()
                .subject_name(tsa_name).issuer_name(ca_name)
                .public_key(tsa_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=365))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                               critical=True)
                .add_extension(x509.ExtendedKeyUsage(
                    [x509.oid.ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
                .sign(ca_key, hashes.SHA256()))
    return {"tsa_key": tsa_key, "tsa_cert": tsa_cert, "ca_cert": ca_cert,
            "fp_tsa": tsa_cert.fingerprint(hashes.SHA256()).hex(),
            "fp_ca": ca_cert.fingerprint(hashes.SHA256()).hex()}


def _build_token(pki, chain_head: str, gen_time: datetime = None) -> bytes:
    """手工拼 RFC3161 TimeStampResp（status=granted + signedData 包裹 TSTInfo），
    ECDSA 真签名——等价于公网 TSA 返回的 .tsr，但全程离线、确定。"""
    imprint = hashlib.sha256(chain_head.encode("utf-8")).digest()
    gt = (gen_time or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%SZ")
    tstinfo = _der(0x30,
                   _der_int(1)
                   + _oid(_TEST_POLICY)
                   + _der(0x30, _SHA256_ALGID + _der(0x04, imprint))
                   + _der_int(123456)
                   + _der(0x18, gt.encode("ascii")))
    md = hashlib.sha256(tstinfo).digest()
    # signedAttrs（DER SET OF 已按编码排序：9.3 < 9.4）
    attr_ct = _der(0x30, _oid(_OID_CONTENT_TYPE)
                   + _der(0x31, _oid(_OID_TSTINFO)))
    attr_md = _der(0x30, _oid(_OID_MESSAGE_DIGEST)
                   + _der(0x31, _der(0x04, md)))
    attrs = attr_ct + attr_md
    sig = pki["tsa_key"].sign(_der(0x31, attrs), ec.ECDSA(hashes.SHA256()))
    issuer_der = pki["tsa_cert"].issuer.public_bytes()
    sid = _der(0x30, issuer_der + _der_int(pki["tsa_cert"].serial_number))
    signer_info = _der(0x30,
                       _der_int(1) + sid + _SHA256_ALGID
                       + _der(0xA0, attrs)
                       + _ECDSA_SHA256_ALGID + _der(0x04, sig))
    certs = (pki["tsa_cert"].public_bytes(serialization.Encoding.DER)
             + pki["ca_cert"].public_bytes(serialization.Encoding.DER))
    signed_data = _der(0x30,
                       _der_int(1)
                       + _der(0x31, _SHA256_ALGID)
                       + _der(0x30, _oid(_OID_TSTINFO)
                              + _der(0xA0, _der(0x04, tstinfo)))
                       + _der(0xA0, certs)
                       + _der(0x31, signer_info))
    content_info = _der(0x30, _oid(_OID_SIGNED_DATA) + _der(0xA0, signed_data))
    return _der(0x30, _der(0x30, _der_int(0)) + content_info)  # status=granted


# ── verify_tsa_token ──

def test_valid_token_pinned_leaf(pki):
    """有效令牌 + 钉 TSA 签名证书指纹 → verified"""
    r = verify_tsa_token(_build_token(pki, CHAIN_HEAD), CHAIN_HEAD, [pki["fp_tsa"]])
    assert r["status"] == "verified", r
    assert r["pinned"] == "tsa_cert"
    assert r["fingerprint"] == pki["fp_tsa"]
    assert r["imprint"] == hashlib.sha256(CHAIN_HEAD.encode()).hexdigest()


def test_valid_token_pinned_ca(pki):
    """有效令牌 + 钉 CA 指纹（证书链闭合到已钉扎 CA）→ verified"""
    r = verify_tsa_token(_build_token(pki, CHAIN_HEAD), CHAIN_HEAD, [pki["fp_ca"]])
    assert r["status"] == "verified", r
    assert r["pinned"] == "trusted_ca"
    assert r["fingerprint"] == pki["fp_ca"]
    assert len(r["chain_subjects"]) == 2, "链应含 TSA + CA 两级"


def test_imprint_tampered_rejected(pki):
    """摘要被篡改（token 盖的是别的链头）→ failed imprint_mismatch"""
    other_head = hashlib.sha256(b"forged-chain-head").hexdigest()
    token = _build_token(pki, other_head)
    r = verify_tsa_token(token, CHAIN_HEAD, [pki["fp_tsa"]])
    assert r["status"] == "failed", r
    assert r["reason"] == "imprint_mismatch"


def test_signature_tampered_rejected(pki):
    """签名被篡改（末字节翻转）→ failed signature_invalid"""
    token = bytearray(_build_token(pki, CHAIN_HEAD))
    token[-2] ^= 0xFF  # 签名值在 token 末尾
    r = verify_tsa_token(bytes(token), CHAIN_HEAD, [pki["fp_tsa"]])
    assert r["status"] == "failed", r
    assert r["reason"] in ("signature_invalid", "malformed_token"), r


def test_fingerprint_not_pinned_rejected(pki):
    """指纹不匹配（钉了一个不相干指纹）→ failed fingerprint_not_pinned"""
    bogus = hashlib.sha256(b"not-our-tsa").hexdigest()
    r = verify_tsa_token(_build_token(pki, CHAIN_HEAD), CHAIN_HEAD, [bogus])
    assert r["status"] == "failed", r
    assert r["reason"] == "fingerprint_not_pinned"


def test_no_fingerprints_unverified(pki, monkeypatch, tmp_path):
    """无指纹配置（显式空列表 / 配置全缺）→ unverified，不假绿"""
    token = _build_token(pki, CHAIN_HEAD)
    r = verify_tsa_token(token, CHAIN_HEAD, [])
    assert r["status"] == "unverified", r
    assert r["reason"] == "no_trusted_fingerprints"
    monkeypatch.delenv("SYNC_HUB_TSA_TRUSTED_FP", raising=False)
    missing = str(tmp_path / "no_such_trust.json")
    r = verify_tsa_token(token, CHAIN_HEAD, None, trust_file=missing)
    assert r["status"] == "unverified" and r["reason"] == "no_trusted_fingerprints", r


def test_crypto_missing_unverified(pki, monkeypatch):
    """cryptography 缺失（模拟 ImportError）→ unverified 而非 crash"""
    monkeypatch.setitem(sys.modules, "cryptography", None)
    r = verify_tsa_token(_build_token(pki, CHAIN_HEAD), CHAIN_HEAD, [pki["fp_tsa"]])
    assert r["status"] == "unverified", r
    assert r["reason"] == "cryptography_unavailable"


def test_trust_via_env(pki, monkeypatch):
    """env SYNC_HUB_TSA_TRUSTED_FP 配置（逗号分隔、带冒号大写均可归一）"""
    fp = ":".join(pki["fp_tsa"][i:i + 2] for i in range(0, 64, 2)).upper()
    monkeypatch.setenv("SYNC_HUB_TSA_TRUSTED_FP", f"{'ab' * 32}, {fp}")
    r = verify_tsa_token(_build_token(pki, CHAIN_HEAD), CHAIN_HEAD, None)
    assert r["status"] == "verified", r


def test_trust_via_json_file(pki, monkeypatch, tmp_path):
    """JSON 配置文件 audit/tsa_trust.json 同构路径（trust_file 入参）"""
    monkeypatch.delenv("SYNC_HUB_TSA_TRUSTED_FP", raising=False)
    tf = tmp_path / "tsa_trust.json"
    tf.write_text(json.dumps({"trusted_fingerprints": [pki["fp_ca"]]}),
                  encoding="utf-8")
    r = verify_tsa_token(_build_token(pki, CHAIN_HEAD), CHAIN_HEAD, None,
                         trust_file=str(tf))
    assert r["status"] == "verified" and r["pinned"] == "trusted_ca", r


def test_token_from_file_path(pki, tmp_path):
    """tsr 入参支持 .tsr 文件路径（运维直接喂落盘文件）"""
    p = tmp_path / "tok.tsr"
    p.write_bytes(_build_token(pki, CHAIN_HEAD))
    r = verify_tsa_token(str(p), CHAIN_HEAD, [pki["fp_tsa"]])
    assert r["status"] == "verified", r


# ── fetch_and_verify_anchors ──

class _AnchorHandler(BaseHTTPRequestHandler):
    """GET 返回 server.anchor_json（锚接收方需暴露 GET 取回口，见验收文档）"""

    def do_GET(self):
        body = self.server.anchor_json
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def anchor_server():
    srv = HTTPServer(("127.0.0.1", 0), _AnchorHandler)
    srv.anchor_json = b"{}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


@pytest.fixture()
def anchor_db(tmp_path):
    db = str(tmp_path / "fetch_anchor_test.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL DEFAULT '',
            ref_table TEXT DEFAULT '',
            ref_id TEXT DEFAULT '',
            payload TEXT DEFAULT '',
            prev_hash TEXT NOT NULL DEFAULT '',
            entry_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT)""")
    conn.commit()
    conn.close()
    ac = AuditChain(db)
    heads = []
    for i in range(1, 4):
        ac.append("event", "events", f"cd034-{i}", {"k": f"v{i}"})
        heads.append(ac._tail_hash(ac._get_conn()))
    return {"db": db, "heads": heads}


def _url(srv):
    return f"http://127.0.0.1:{srv.server_address[1]}/anchor"


def test_fetch_match_head(anchor_db, anchor_server):
    anchor_server.anchor_json = json.dumps({"anchor": anchor_db["heads"][-1]}).encode()
    r = fetch_and_verify_anchors(anchor_db["db"], urls=[_url(anchor_server)])
    assert r["valid"] is True and r["checked"] == 1, r
    assert r["results"][0]["status"] == "match_head", r


def test_fetch_match_chain_after_growth(anchor_db, anchor_server):
    """锚是历史链头、链已增长 → match_chain（不误报，判据=节点在链中）"""
    anchor_server.anchor_json = json.dumps({"anchor": anchor_db["heads"][0]}).encode()
    r = fetch_and_verify_anchors(anchor_db["db"], urls=[_url(anchor_server)])
    assert r["valid"] is True, r
    assert r["results"][0]["status"] == "match_chain", r
    assert r["results"][0]["log_id"] == 1


def test_fetch_mismatch_rewrite_suspect(anchor_db, anchor_server):
    """远端锚不在本地链中 → valid=False（整段重写/截断嫌疑）"""
    anchor_server.anchor_json = json.dumps({"anchor": "ab" * 32}).encode()
    r = fetch_and_verify_anchors(anchor_db["db"], urls=[_url(anchor_server)])
    assert r["valid"] is False, r
    assert r["results"][0]["status"] == "mismatch", r


def test_fetch_unreachable_unverified_not_invalid(anchor_db):
    """取不回 = unverified 计数，不判 valid=False（取不回不是篡改证据）"""
    r = fetch_and_verify_anchors(anchor_db["db"], urls=["http://127.0.0.1:1/x"],
                                 timeout=2)
    assert r["valid"] is True and r["unverified"] == 1, r
    assert r["results"][0]["status"] == "unverified", r


def test_fetch_plain_text_anchor(anchor_db, anchor_server):
    """兼容 anchor.txt 快照格式（纯文本末词 = hash）"""
    anchor_server.anchor_json = (
        f"2026-09-21T00:00:00+00:00 {anchor_db['heads'][-1]}\n".encode())
    r = fetch_and_verify_anchors(anchor_db["db"], urls=[_url(anchor_server)])
    assert r["valid"] is True
    assert r["results"][0]["status"] == "match_head", r


def test_url_validation_ssrf():
    """SSRF 防护：仅 http/https、禁 userinfo；内网段默认放宽（内网产品，
    收紧档 SYNC_HUB_ANCHOR_ALLOW_PRIVATE=0 / allow_private=False 拒私网）"""
    assert _validate_anchor_url("file:///etc/passwd") is not None
    assert _validate_anchor_url("ftp://x/anchor") is not None
    assert _validate_anchor_url("http://user:pw@192.168.1.1/a") is not None
    assert _validate_anchor_url("http:///nohost") is not None
    # 默认放宽：内网段/环回合法（锚接收方典型是同网段审计服务器）
    assert _validate_anchor_url("http://192.168.1.10/anchor") is None
    assert _validate_anchor_url("http://127.0.0.1:8080/anchor") is None
    # 收紧档：私网/环回拒绝，公网 IP 与域名放行
    assert _validate_anchor_url("http://192.168.1.10/anchor",
                                allow_private=False) is not None
    assert _validate_anchor_url("http://127.0.0.1/anchor",
                                allow_private=False) is not None
    assert _validate_anchor_url("http://8.8.8.8/anchor",
                                allow_private=False) is None
    assert _validate_anchor_url("https://anchor.example.com/a",
                                allow_private=False) is None


def test_fetch_rejected_scheme(anchor_db):
    """file:// 在 fetch 路径被拒（rejected + 计入 unverified，不出请求）"""
    r = fetch_and_verify_anchors(anchor_db["db"], urls=["file:///etc/passwd"])
    assert r["results"][0]["status"] == "rejected", r
    assert r["unverified"] == 1
