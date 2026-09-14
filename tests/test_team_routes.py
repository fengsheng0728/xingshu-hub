"""P3: team×9 EXPECTED_ROUTES 断言"""
import pytest
from routes import app


EXPECTED_TEAM_ENDPOINTS = [
    ("GET", "/api/v1/team/discover"),
    ("GET", "/api/v1/team/ping"),
    ("GET", "/api/v1/team/members"),
    ("POST", "/api/v1/team/pair/request"),
    ("POST", "/api/v1/team/pair/accept"),
    ("POST", "/api/v1/team/pair/exchange"),
    ("POST", "/api/v1/team/proxy/disclose"),
    ("POST", "/api/v1/team/disclose/remote"),
    ("DELETE", "/api/v1/team/members/{member_id}"),
    ("POST", "/api/v1/team/members/{member_id}/revoke"),
]


def _registered():
    out = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in ("GET", "POST", "PUT", "DELETE", "WEBSOCKET"):
                out.add((m, r.path))
    return out


def test_team_endpoints_all_registered():
    registered = _registered()
    missing = [f"{m} {p}" for m, p in EXPECTED_TEAM_ENDPOINTS if (m, p) not in registered]
    assert not missing, f"team 端点未注册: {missing}"
