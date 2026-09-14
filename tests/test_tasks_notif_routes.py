"""P2: tasks + notifications 端点 EXPECTED_ROUTES 反向断言"""
import pytest
from routes import app


EXPECTED_TASKS_ENDPOINTS = [
    ("GET", "/api/v1/tasks"),  # P1: 任务列表（含 blocked_by）
    ("POST", "/api/v1/tasks/create"),
    ("POST", "/api/v1/tasks/{task_id}/schedule"),
    ("POST", "/api/v1/tasks/{task_id}/advance"),
    ("POST", "/api/v1/tasks/{task_id}/start"),
    ("POST", "/api/v1/tasks/{task_id}/complete"),
    ("POST", "/api/v1/tasks/{task_id}/fail"),
    ("POST", "/api/v1/tasks/{task_id}/cancel"),
    ("POST", "/api/v1/tasks/{task_id}/update"),
]

EXPECTED_NOTIF_ENDPOINTS = [
    ("GET", "/api/v1/notifications"),
    ("POST", "/api/v1/notifications/{notif_id}/read"),
    ("POST", "/api/v1/notifications/read-all"),
    ("POST", "/api/v1/notifications/create"),
]


def _registered():
    out = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()):
            if m in ("GET", "POST", "PUT", "DELETE", "WEBSOCKET"):
                out.add((m, r.path))
    return out


def test_tasks_endpoints_all_registered():
    registered = _registered()
    missing = [f"{m} {p}" for m, p in EXPECTED_TASKS_ENDPOINTS if (m, p) not in registered]
    assert not missing, f"tasks 端点未注册: {missing}"


def test_notif_endpoints_all_registered():
    registered = _registered()
    missing = [f"{m} {p}" for m, p in EXPECTED_NOTIF_ENDPOINTS if (m, p) not in registered]
    assert not missing, f"notifications 端点未注册: {missing}"
