#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CD-034 R4 运维工具：TSA 令牌离线验签 + 远端锚取回比对。

用法（仓库根目录下执行）：
  python scripts/verify_tsa_anchor.py token <file.tsr> [--head <hex>] [--db <path>]
      离线验签单个 .tsr 令牌：解析 → 签名 → 证书链 → 指纹钉扎 → imprint 比对。
      --head 缺省时取 --db（默认 models.CONFIG.DB_PATH）的当前链头。
  python scripts/verify_tsa_anchor.py anchors [--db <path>] [--url <u> ...]
      从 AUDIT_ANCHOR_URLS（或显式 --url）取回远端锚，比对本地主链。
  python scripts/verify_tsa_anchor.py tsa-dir [--db <path>] [--tsa-dir <dir>]
      对 audit/tsa/index.jsonl 全部 ok 记录逐个离线验签 + 链中比对。
  python scripts/verify_tsa_anchor.py fingerprint <cert.pem|cert.der>
      打印证书 SHA-256 指纹（配置 audit/tsa_trust.json 用）。

退出码：0 = 全部 verified/valid；1 = 任一 failed/valid=False；2 = 存在 unverified。
"""
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from audit_chain import (  # noqa: E402
    _load_crypto, _load_trusted_fingerprints, current_chain_head,
    fetch_and_verify_anchors, tsa_out_dir, verify_tsa, verify_tsa_token,
)


def _default_db() -> str:
    try:
        from models import CONFIG
        return CONFIG.DB_PATH
    except Exception:
        return os.path.join(REPO_ROOT, "sync_hub.db")


def cmd_token(args) -> int:
    head = args.head
    if not head:
        if not args.db:
            print("ERROR: 未给 --head 时必须给 --db", file=sys.stderr)
            return 2
        head = current_chain_head(args.db)
        if not head:
            print("ERROR: 链头为空（无主链/空链）", file=sys.stderr)
            return 2
    r = verify_tsa_token(args.tsr, head, None)
    print(json.dumps({"chain_head": head, **r}, ensure_ascii=False, indent=2))
    return {"verified": 0, "failed": 1}.get(r["status"], 2)


def cmd_anchors(args) -> int:
    r = fetch_and_verify_anchors(args.db, urls=args.url or None)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if not r["valid"]:
        return 1
    return 2 if r.get("unverified") else 0


def cmd_tsa_dir(args) -> int:
    out_dir = tsa_out_dir(args.tsa_dir or "")
    idx = os.path.join(out_dir, "index.jsonl")
    base = verify_tsa(args.db, out_dir=out_dir)
    pins = _load_trusted_fingerprints()
    rows = []
    if os.path.isfile(idx):
        with open(idx, "r", encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    details = []
    worst = 0
    for rec in rows:
        if rec.get("status") != "ok" or not rec.get("tsr"):
            continue
        if not os.path.isfile(rec["tsr"]):
            details.append({"tsr": rec["tsr"], "status": "unverified",
                            "reason": "tsr_file_missing"})
            worst = max(worst, 2)
            continue
        r = verify_tsa_token(rec["tsr"], rec["anchor"], pins)
        details.append({"tsr": rec["tsr"], "anchor": (rec.get("anchor") or "")[:16],
                        **r})
        worst = max(worst, {"verified": 0, "failed": 1}.get(r["status"], 2))
    print(json.dumps({"out_dir": out_dir, "chain_check": base,
                      "tokens": details}, ensure_ascii=False, indent=2))
    if not base.get("valid", True):
        return 1
    return worst


def cmd_fingerprint(args) -> int:
    crypto = _load_crypto()
    if crypto is None:
        print("ERROR: 需要 cryptography（pip install cryptography）", file=sys.stderr)
        return 2
    x509, hashes = crypto[0], crypto[1]
    with open(args.cert, "rb") as f:
        blob = f.read()
    try:
        cert = x509.load_pem_x509_certificate(blob)
    except ValueError:
        cert = x509.load_der_x509_certificate(blob)
    print(json.dumps({"subject": cert.subject.rfc4514_string(),
                      "sha256": cert.fingerprint(hashes.SHA256()).hex()},
                     ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="CD-034 TSA 验签 / 锚比对运维工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _db_arg(p):
        p.add_argument("--db", default=_default_db(), help="sqlite 库路径")

    p = sub.add_parser("token", help="离线验签单个 .tsr")
    p.add_argument("tsr")
    p.add_argument("--head", default="", help="链头 hex（缺省取 --db 当前链头）")
    _db_arg(p)
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("anchors", help="远端锚取回比对")
    p.add_argument("--url", action="append", help="锚 URL（可多次；缺省读配置）")
    _db_arg(p)
    p.set_defaults(func=cmd_anchors)

    p = sub.add_parser("tsa-dir", help="audit/tsa 全部盖章记录离线验签")
    p.add_argument("--tsa-dir", default="", help="缺省 audit/tsa（或 env）")
    _db_arg(p)
    p.set_defaults(func=cmd_tsa_dir)

    p = sub.add_parser("fingerprint", help="打印证书 SHA-256 指纹")
    p.add_argument("cert")
    p.set_defaults(func=cmd_fingerprint)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
