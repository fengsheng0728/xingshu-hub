"""
模块⑤ 首页交付台后端验收：通知/产物路径打通
- ⑤-T1 交付卡能读到带 artifact_path 的系统通知
- ⑤-T2 自动化运行结果上报后生成带产物路径的通知
"""
import os
import sys
import sqlite3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient


@pytest.fixture
def tmp_hub(monkeypatch, tmp_path):
    """在临时数据库上初始化 Hub schema，并返回 db_path / app / hub"""
    from models import CONFIG
    from db import init_db
    from routes import app, hub, NO_AUTH

    db_path = str(tmp_path / "module5.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    monkeypatch.setattr("routes.NO_AUTH", True, raising=False)
    monkeypatch.setattr("routes_automation.NO_AUTH", True, raising=False)
    init_db()

    # 注册一个测试 Agent
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO agents (agent_id, agent_name, role, api_key, status) VALUES (?, ?, ?, ?, ?)",
        ("agent-5", "测试员", "worker", "key-5", "online"),
    )
    conn.commit()
    conn.close()

    return {"db_path": db_path, "app": app, "hub": hub}


class TestModule5DeliveryBackend:
    """⑤-T1 / ⑤-T2 后端行为验证"""

    def test_create_notification_with_artifact_path(self, tmp_hub):
        """通知支持 artifact_path，并在工作台可见"""
        hub = tmp_hub["hub"]

        import asyncio
        asyncio.run(hub.create_notification(
            "agent-5", "automation", "晨报已生成", body="今日晨报已保存",
            source="automation", artifact_path="E:/work/reports/晨报_20260729.docx"
        ))

        ws = asyncio.run(hub.get_agent_workspace("agent-5"))
        notifs = ws.get("notifications", [])
        assert any(
            n.get("artifact_path") == "E:/work/reports/晨报_20260729.docx"
            and n.get("event") == "delivery"
            and n.get("title") == "晨报已生成"
            for n in notifs
        ), f"工作台应返回带 artifact_path 的通知，实际: {notifs}"

    def test_automation_result_endpoint_creates_delivery_notification(self, tmp_hub):
        """自动化运行接口上报 artifact_path 后，工作台出现 delivery 通知"""
        app = tmp_hub["app"]
        client = TestClient(app)

        run = {
            "job_id": 99,
            "name": "晨报生成",
            "status": "success",
            "result_summary": "已生成晨报",
            "full_result": "生成完成",
            "artifact_path": "E:/work/reports/晨报_20260729.docx",
            "duration_ms": 1200,
            "iterations": 3,
            "tokens_used": 1200,
        }
        r = client.post("/api/v1/automation/runs?agent_id=agent-5", json=run)
        assert r.status_code == 200, r.text

        ws = client.get("/api/v1/agent/workspace?agent_id=agent-5")
        assert ws.status_code == 200, ws.text
        notifs = ws.json().get("notifications", [])
        assert any(
            n.get("artifact_path") == "E:/work/reports/晨报_20260729.docx"
            and n.get("event") == "delivery"
            for n in notifs
        ), f"自动化结果应生成带产物路径的通知，实际: {notifs}"
