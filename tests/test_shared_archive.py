"""讨论室归档语义测试（2026-09-22）

覆盖：
- 归档后：列表消失、REST 读 404、REST 写 404
- 归档后：WS 进房被拒（一帧 shared_archived + close 4404）
- 取消归档后：列表恢复、可读、可写、可再进房
- 取消归档边界：未归档 → 409；不存在 → 404
- 冷文档（已被 sweep 卸载、不在内存）同样可归档（原实现在此 404）

注：「归档时踢掉房里已有连接」需要并发两条连接 + 一条归档请求，
TestClient 的 websocket_connect 上下文内无法安全重入 REST 调用，
故该项由真 Hub 的端到端脚本验证（_sync/archive_probe/）。
"""
import os

import pytest

os.environ["SYNC_HUB_NO_AUTH"] = "1"


@pytest.fixture(scope="module")
def client():
    """共享工作区由 app lifespan 统一初始化（with 触发 lifespan）"""
    from routes import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def _mk_doc(client, title="归档用例", agent="arch-owner"):
    r = client.post("/api/v1/shared/docs", params={"agent_id": agent},
                    json={"title": title, "visibility": "team"})
    assert r.status_code == 200, r.text
    return r.json()["doc_id"]


def _ws_archived_room_probe(client, doc_id):
    """归档房：收第一帧（应为 shared_archived）+ 再收一次取 close code"""
    from starlette.websockets import WebSocketDisconnect
    code = None
    with client.websocket_connect("/ws/shared/watch/%s" % doc_id) as ws:
        first = ws.receive_text()
        try:
            ws.receive_text()
        except WebSocketDisconnect as e:
            code = e.code
    return first, code


def _ws_live_room_probe(client, doc_id):
    """正常房：只取第一帧（presence）就退出——活房不会主动关闭，不可等 close"""
    with client.websocket_connect("/ws/shared/watch/%s" % doc_id) as ws:
        return ws.receive_text()


class TestSharedArchive:
    doc_id = None

    def test_01_archive_blocks_rest_and_hides(self, client):
        """归档后：列表消失 + 读 404 + 写 404"""
        doc = _mk_doc(client)
        TestSharedArchive.doc_id = doc
        r = client.post("/api/v1/shared/docs/%s/blocks" % doc,
                        params={"agent_id": "arch-owner"}, json={"text": "归档前的发言"})
        assert r.status_code == 200, r.text

        r = client.delete("/api/v1/shared/docs/%s" % doc, params={"agent_id": "arch-owner"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "deleted"

        docs = client.get("/api/v1/shared/docs", params={"agent_id": "arch-owner"}).json()["docs"]
        assert doc not in [d["doc_id"] for d in docs], "归档后不应出现在列表"

        r = client.get("/api/v1/shared/docs/%s" % doc, params={"agent_id": "arch-owner"})
        assert r.status_code == 404, r.text

        r = client.post("/api/v1/shared/docs/%s/blocks" % doc,
                        params={"agent_id": "arch-owner"}, json={"text": "归档后还想说"})
        assert r.status_code == 404, r.text

    def test_02_archived_room_rejects_ws(self, client):
        """归档后进房：先收 shared_archived 帧，随后 close 4404"""
        doc = TestSharedArchive.doc_id
        first, code = _ws_archived_room_probe(client, doc)
        assert first is not None and "shared_archived" in first, \
            "归档房应回一帧 shared_archived，实际: %r" % (first,)
        assert code == 4404, "归档房应以 4404 关闭，实际: %r" % (code,)

    def test_03_unarchive_restores(self, client):
        """取消归档后：列表恢复 + 可读 + 可写"""
        doc = TestSharedArchive.doc_id
        r = client.post("/api/v1/shared/docs/%s/unarchive" % doc, params={"agent_id": "arch-owner"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "restored"

        docs = client.get("/api/v1/shared/docs", params={"agent_id": "arch-owner"}).json()["docs"]
        assert doc in [d["doc_id"] for d in docs], "恢复后应重新出现在列表"

        r = client.get("/api/v1/shared/docs/%s" % doc, params={"agent_id": "arch-owner"})
        assert r.status_code == 200, r.text
        assert "归档前的发言" in r.json()["content"], "归档不删内容，恢复后原文仍在"

        r = client.post("/api/v1/shared/docs/%s/blocks" % doc,
                        params={"agent_id": "arch-owner"}, json={"text": "恢复后的发言"})
        assert r.status_code == 200, r.text

    def test_04_restored_room_accepts_ws(self, client):
        """恢复后进房：正常收到 presence（不再被 4404 拒）"""
        doc = TestSharedArchive.doc_id
        first = _ws_live_room_probe(client, doc)
        assert first is not None and "shared_presence" in first, \
            "恢复后应正常进房（presence 帧），实际: %r" % (first,)

    def test_05_unarchive_edge_cases(self, client):
        """未归档 → 409；不存在 → 404"""
        doc = TestSharedArchive.doc_id
        r = client.post("/api/v1/shared/docs/%s/unarchive" % doc, params={"agent_id": "arch-owner"})
        assert r.status_code == 409, "重复取消归档应 409，实际 %s" % r.status_code

        r = client.post("/api/v1/shared/docs/doc-not-exist-xyz/unarchive",
                        params={"agent_id": "arch-owner"})
        assert r.status_code == 404, "不存在的 doc 应 404，实际 %s" % r.status_code

    def test_06_cold_doc_archivable(self, tmp_path):
        """冷文档（不在内存）也可归档 —— 原实现 `doc_id not in self._docs → False` 会 404

        用独立 SharedWorkspace 实例 + 独立库，不依赖 TestClient 的 lifespan；
        单次 asyncio.run 内完成全部调用（避免跨 loop 复用 asyncio.Lock）。
        """
        import asyncio
        import sqlite3
        from shared_workspace import SharedWorkspace, SHARED_DOCS_DDL

        db = str(tmp_path / "cold.db")
        conn = sqlite3.connect(db)
        conn.executescript(SHARED_DOCS_DDL)
        conn.execute(
            "INSERT INTO shared_docs (doc_id, title, created_by, created_at, updated_at) "
            "VALUES ('d-cold', '冷文档', 'o', 0, 0)")
        conn.commit()
        conn.close()

        ws = SharedWorkspace(db_path=db, store_dir=str(tmp_path / "store"))

        async def _flow():
            assert await ws.is_archived("d-cold") is False
            assert await ws.delete_doc("d-cold") is True, "冷文档归档应成功"
            assert await ws.is_archived("d-cold") is True
            assert await ws.delete_doc("d-cold") is False, "重复归档应为 False（幂等）"
            assert await ws.restore_doc("d-cold") is True, "冷文档恢复应成功"
            assert await ws.is_archived("d-cold") is False
            assert await ws.restore_doc("d-cold") is False, "未归档再恢复应为 False"
            assert await ws.is_archived("no-such-doc") is False, "不存在的 doc 不算归档态"

        asyncio.run(_flow())
