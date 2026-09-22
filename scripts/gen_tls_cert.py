# -*- coding: utf-8 -*-
"""生成自签 TLS 证书（内网联调用）— CD-068

用法::

    # 默认写入 ./config/server.crt + ./config/server.key，SAN 含 localhost/127.0.0.1/本机名/本机地址
    python scripts/gen_tls_cert.py

    # 指定输出目录与额外主机（外部方要用的域名/IP 必须写进 SAN）
    python scripts/gen_tls_cert.py --out ./config --hosts hub.corp.local,192.168.1.10

生成后在 config.yaml 打开::

    server:
      tls:
        enabled: true
        certfile: ./config/server.crt
        keyfile:  ./config/server.key

注意：自签证书需要调用方显式信任（浏览器会告警、curl 要 --cacert）。
对外交付请用企业 CA 或公网证书——那样客户端走系统信任链，零配置。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gen_tls_cert", description="生成自签 TLS 证书（联调用）")
    ap.add_argument("--out", default="./config", help="输出目录（默认 ./config）")
    ap.add_argument("--hosts", default="", help="额外 SAN 主机（逗号分隔域名或 IP）")
    ap.add_argument("--name", default="server", help="文件名前缀（默认 server → server.crt/server.key）")
    ap.add_argument("--days", type=int, default=825, help="有效期天数（默认 825）")
    args = ap.parse_args(argv)

    from tls_util import gen_self_signed
    extra = [h.strip() for h in (args.hosts or "").split(",") if h.strip()]
    hosts = ["127.0.0.1", "::1", "localhost"] + extra
    out = os.path.abspath(args.out)
    res = gen_self_signed(os.path.join(out, args.name + ".crt"),
                          os.path.join(out, args.name + ".key"),
                          hosts=hosts, days=args.days)
    res["status"] = "ok"
    res["config_snippet"] = {
        "server": {"tls": {"enabled": True,
                           "certfile": f"./{os.path.basename(out)}/{args.name}.crt",
                           "keyfile": f"./{os.path.basename(out)}/{args.name}.key"}},
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("[提示] 自签证书需调用方显式信任；对外交付建议改用企业 CA/公网证书。",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
