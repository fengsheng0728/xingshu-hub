# -*- coding: utf-8 -*-
"""tls_util.py — Hub 可选传输层加密（CD-068，2026-09-20）

为什么需要：放出 API 后，Bearer 凭据在明文 HTTP 上过网可被嗅探/中间人改写；
Hub 此前唯一自带加密的信道只有 Hub↔Hub 联邦（AES-GCM 应用层）。对外交付 API
而不给 TLS，等于把「谁能进来」这道锁挂在敞开的门上。

设计（默认关，零迁移）::

    server:
      tls:
        enabled: false               # 默认关；开启后 REST 走 https、WS 走 wss
        certfile: ./config/server.crt
        keyfile:  ./config/server.key

- 相对路径按 config 文件所在目录解析。
- 开启了但证书/私钥缺失或不可读 → 启动 fail-closed 拒绝（绝不静默降级明文）。
- 自签证书仅供内网联调（浏览器/客户端要显式信任）；对外交付用企业 CA 或公网证书，
  此时系统信任链即可，客户端零配置。
"""
import datetime
import ipaddress
import logging
import os
import socket
from typing import Dict, List, Optional

logger = logging.getLogger("xingshu.tls")


def load_tls_config(config_path: str) -> Dict:
    """读 server.tls 段。缺段/缺键 = 不开启（零迁移），路径基于 config 目录解析。"""
    out = {"enabled": False, "certfile": "", "keyfile": "", "config_path": config_path}
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        tls = ((cfg.get("server") or {}).get("tls") or {})
    except Exception as e:
        logger.warning("读取 TLS 配置失败（按未开启处理）: %s", e)
        return out
    out["enabled"] = bool(tls.get("enabled", False))
    base = os.path.dirname(os.path.abspath(config_path))
    for key in ("certfile", "keyfile"):
        raw = str(tls.get(key) or "").strip()
        if raw:
            # MSYS/相对路径都按 config 目录解析；绝对路径原样保留
            out[key] = raw if os.path.isabs(raw) else os.path.normpath(os.path.join(base, raw))
        else:
            out[key] = os.path.normpath(os.path.join(base, "server." + ("crt" if key == "certfile" else "key")))
    return out


def validate_tls_files(tls: Dict) -> str:
    """启动前校验：返回错误文案，空串 = 可用。未开启时恒返回空串（零迁移）。"""
    if not (tls or {}).get("enabled"):
        return ""
    missing = []
    for key, label in (("certfile", "证书"), ("keyfile", "私钥")):
        p = (tls or {}).get(key) or ""
        if not p or not os.path.isfile(p):
            missing.append(f"{label} {key}={p or '(空)'}")
    if missing:
        return ("TLS enabled 但文件不可用: " + "; ".join(missing)
                + "（生成自签证书: python scripts/gen_tls_cert.py --out <config目录>）")
    try:
        # 提前解析一次，把「文件存在但不是 PEM / 私钥加密」这类错误在启动期暴露，而不是首个请求
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=tls["certfile"], keyfile=tls["keyfile"])
    except Exception as e:
        return f"TLS enabled 但证书/私钥无法加载: {e}"
    return ""


def scheme_for(tls_enabled: bool) -> Dict[str, str]:
    """对外展示/日志用的 scheme 推导（客户端同口径：hub_url https → ws 用 wss）。"""
    if tls_enabled:
        return {"http": "https", "ws": "wss"}
    return {"http": "http", "ws": "ws"}


def _default_hosts() -> List[str]:
    hosts = ["127.0.0.1", "::1", "localhost", socket.gethostname()]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip and ip not in hosts and not ip.startswith("169.254."):
                hosts.append(ip)
    except Exception as e:
        logger.debug("枚举本机地址失败（忽略）: %s", e)
    return hosts


def gen_self_signed(cert_path: str, key_path: str, hosts: Optional[List[str]] = None,
                    days: int = 825) -> Dict:
    """生成自签 RSA2048 证书（PEM，SAN 含传入主机 + 本机地址），返回元信息。

    仅供内网联调。对外交付请改用企业 CA / 公网证书（客户端系统信任链零配置）。
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    hosts = [h for h in (hosts or _default_hosts()) if h]
    dns_names, ip_addrs, sans = [], [], []
    for h in hosts:
        try:
            ip_addrs.append(ipaddress.ip_address(h))
            sans.append(f"IP:{h}")
        except ValueError:
            dns_names.append(h)
            sans.append(f"DNS:{h}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0] if dns_names else str(ip_addrs[0])),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sync Hub (self-signed)"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    alt = []
    for d in dns_names:
        alt.append(x509.DNSName(d))
    for i in ip_addrs:
        alt.append(x509.IPAddress(i))
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=int(days)))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    for path, data in ((cert_path, cert.public_bytes(serialization.Encoding.PEM)),
                       (key_path, key.private_bytes(
                           encoding=serialization.Encoding.PEM,
                           format=serialization.PrivateFormat.TraditionalOpenSSL,
                           encryption_algorithm=serialization.NoEncryption()))):
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    try:
        os.chmod(key_path, 0o600)
    except Exception as e:
        logger.debug("设置私钥权限失败（Windows 忽略）: %s", e)
    return {"certfile": os.path.abspath(cert_path), "keyfile": os.path.abspath(key_path),
            "sans": sans, "days": int(days),
            "not_after": (now + datetime.timedelta(days=int(days))).date().isoformat()}
