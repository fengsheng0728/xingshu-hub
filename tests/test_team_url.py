"""
②d: URL 安全校验测试 — 仅允许 RFC1918 私网 + 3060 端口
"""
import pytest
import ipaddress
from urllib.parse import urlparse


def validate_remote_url(url: str) -> bool:
    """与 hub_core._validate_remote_url 实现一致"""
    parsed = urlparse(url)
    if parsed.scheme != "http":
        return False
    port = parsed.port or 80
    if port != 3060:
        return False
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    if not ip.is_private:
        return False
    return True


# ── 合法 URL ──
@pytest.mark.parametrize("url", [
    "http://192.168.1.100:3060",
    "http://10.0.0.5:3060",
    "http://172.16.0.1:3060",
])
def test_valid_private_urls(url):
    assert validate_remote_url(url), f"应通过: {url}"


# ── 非法 URL ──
@pytest.mark.parametrize("url,reason", [
    ("http://169.254.169.254:3060", "link-local"),
    ("http://127.0.0.1:3060", "loopback"),
    ("http://8.8.8.8:3060", "public IP"),
    ("http://192.168.1.1", "no port → default 80"),
    ("http://192.168.1.1:8080", "wrong port"),
    ("https://192.168.1.1:3060", "HTTPS not allowed"),
    ("http://evil.com:3060", "hostname not IP"),
])
def test_invalid_urls(url, reason):
    assert not validate_remote_url(url), f"应拒绝 ({reason}): {url}"
