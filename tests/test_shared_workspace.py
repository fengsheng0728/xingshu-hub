"""Shared Workspace 测试"""
import pytest
import asyncio
import os
import sys

os.environ["SYNC_HUB_NO_AUTH"] = "1"


@pytest.fixture(scope="module")
def client():
    """共享工作区由 app lifespan 统一初始化（with 块触发 lifespan；双实例跨 loop 会锁死 _start_lock）"""
    from routes import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture
def agent():
    return {"agent_id": "test-agent"}


class TestSharedWorkspace:
    doc_id = None

    def test_01_create(self, client, agent):
        r = client.post("/api/v1/shared/docs", params=agent, json={"title": "Pytest Report"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "doc_id" in data
        assert data["title"] == "Pytest Report"
        TestSharedWorkspace.doc_id = data["doc_id"]

    def test_02_list(self, client, agent):
        r = client.get("/api/v1/shared/docs", params=agent)
        assert r.status_code == 200
        docs = r.json()["docs"]
        assert len(docs) >= 1
        assert TestSharedWorkspace.doc_id in [d["doc_id"] for d in docs]

    def test_03_append(self, client):
        r = client.post(
            f"/api/v1/shared/docs/{TestSharedWorkspace.doc_id}/blocks",
            params={"agent_id": "agent-a"},
            json={"text": "## Analysis\nRevenue +15%"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

    def test_04_read(self, client):
        r = client.get(f"/api/v1/shared/docs/{TestSharedWorkspace.doc_id}", params={"agent_id": "r"})
        assert r.status_code == 200
        content = r.json()["content"]
        assert "Revenue +15%" in content

    def test_05_concurrent(self, client):
        for agent_id, text in [("alice", "Alice: Market up 5%"), ("bob", "Bob: Risk done")]:
            r = client.post(
                f"/api/v1/shared/docs/{TestSharedWorkspace.doc_id}/blocks",
                params={"agent_id": agent_id}, json={"text": text},
            )
            assert r.status_code == 200

        r = client.get(f"/api/v1/shared/docs/{TestSharedWorkspace.doc_id}", params={"agent_id": "v"})
        content = r.json()["content"]
        assert "Alice" in content, content[:200]
        assert "Bob" in content, content[:200]

    def test_06_delete(self, client):
        r = client.delete(f"/api/v1/shared/docs/{TestSharedWorkspace.doc_id}", params={"agent_id": "admin"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "deleted"

    def test_07_deleted_404(self, client):
        r = client.get(f"/api/v1/shared/docs/{TestSharedWorkspace.doc_id}", params={"agent_id": "r"})
        assert r.status_code == 404

    def test_08_wiki_ok(self, client):
        r = client.get("/api/v1/wiki/pages", params={"agent_id": "t"})
        assert r.status_code == 200
        assert len(r.json()["pages"]) >= 17

    def test_09_export_ok(self, client):
        r = client.get("/api/v1/wiki/export", params={"agent_id": "t"})
        assert r.status_code == 200
        assert r.json()["count"] >= 17
