"""3-7 SharedWorkspace 容量护栏测试（任务书 D-1, 2026-09-10）

覆盖：TTL 空闲卸载 / 活跃连接保护 / 关闭语义(ttl=0) / 水位告警只打一次 /
优雅退出无残留任务 / can_access private+allowed_agents 的 NameError 修复(T5)。
全程 tmp_path 独立 store 目录与 sqlite 库，不起真实端口、不依赖 3060。
"""
import asyncio
import json as _json
import logging
import sqlite3
import time

import anyio

from models import CONFIG
from shared_workspace import SharedWorkspace


def _mk_ws(tmp_path):
    db_path = str(tmp_path / "shared_test.db")
    store_dir = str(tmp_path / "store")
    return SharedWorkspace(db_path, store_dir=store_dir), db_path


def _insert_doc(db_path, doc_id, title, created_by, visibility="team", allowed=None):
    """直插 shared_docs 行（表由 _init_db 建出）"""
    conn = sqlite3.connect(db_path)
    now = time.time()
    conn.execute(
        "INSERT INTO shared_docs (doc_id, title, created_by, created_at, updated_at, visibility, allowed_agents) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc_id, title, created_by, now, now, visibility, _json.dumps(allowed or [])),
    )
    conn.commit()
    conn.close()


# 1. TTL 过期卸载
def test_ttl_expired_room_unloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "WORKSPACE_ROOM_IDLE_TTL_SEC", 60)
    ws, db = _mk_ws(tmp_path)

    async def main():
        await ws._init_db()
        _insert_doc(db, "doc-a", "A", "agent-a")
        _insert_doc(db, "doc-b", "B", "agent-a")
        await ws.start()
        try:
            assert len(ws._rooms) == 2
            # 把 doc-a 的最后活动时间改写成已过期
            ws._last_active["doc-a"] = time.monotonic() - 3600
            unloaded = await ws.sweep_once()
            assert unloaded == 1
            assert "doc-a" not in ws._rooms
            assert "doc-b" in ws._rooms
        finally:
            await ws.stop()

    anyio.run(main)


# 2. 活跃连接保护：过期但有活跃连接的 room 不卸载
def test_active_connection_protects_room(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "WORKSPACE_ROOM_IDLE_TTL_SEC", 60)
    ws, db = _mk_ws(tmp_path)

    async def main():
        await ws._init_db()
        _insert_doc(db, "doc-a", "A", "agent-a")
        await ws.start()
        try:
            ws._last_active["doc-a"] = time.monotonic() - 3600  # 已过期
            ws._active_conns["doc-a"] = 1                        # 但有活跃连接
            unloaded = await ws.sweep_once()
            assert unloaded == 0
            assert "doc-a" in ws._rooms
        finally:
            await ws.stop()

    anyio.run(main)


# 3. 关闭语义：ttl=0 时即使有超时 room 也不卸载
def test_ttl_zero_disables_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "WORKSPACE_ROOM_IDLE_TTL_SEC", 0)
    ws, db = _mk_ws(tmp_path)

    async def main():
        await ws._init_db()
        _insert_doc(db, "doc-a", "A", "agent-a")
        await ws.start()
        try:
            ws._last_active["doc-a"] = time.monotonic() - 3600
            unloaded = await ws.sweep_once()
            assert unloaded == 0
            assert "doc-a" in ws._rooms
        finally:
            await ws.stop()

    anyio.run(main)


# 4. 水位告警：跨阈值只打一次，继续超线不重复刷屏
def test_capacity_warning_fires_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(CONFIG, "WORKSPACE_MAX_ROOMS", 1)
    ws, db = _mk_ws(tmp_path)

    async def main():
        await ws._init_db()
        for i in range(3):
            _insert_doc(db, f"doc-{i}", f"D{i}", "agent-a")
        await ws.start()
        try:
            assert len(ws._rooms) == 3
        finally:
            await ws.stop()

    with caplog.at_level(logging.WARNING):
        anyio.run(main)
    cap_records = [r for r in caplog.records
                   if r.levelno == logging.WARNING and "水位" in r.getMessage()]
    assert len(cap_records) == 1, [r.getMessage() for r in cap_records]
    msg = cap_records[0].getMessage()
    assert "2" in msg  # 触发时的 rooms 数
    assert "1" in msg  # 阈值
    assert "3" in msg  # 库内未归档文档总数


# 5. 向后兼容 / 优雅退出：默认配置 start→stop 正常完成，无残留清扫任务
def test_start_stop_no_leftover_sweeper_task(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "WORKSPACE_ROOM_IDLE_TTL_SEC", 1800)
    monkeypatch.setattr(CONFIG, "WORKSPACE_SWEEP_INTERVAL_SEC", 60)
    ws, db = _mk_ws(tmp_path)

    def _sweeper_tasks():
        out = []
        for t in asyncio.all_tasks():
            coro = t.get_coro()
            name = getattr(coro, "__qualname__", "") or getattr(coro, "__name__", "")
            if "_sweeper_loop" in name:
                out.append(t)
        return out

    async def main():
        await ws._init_db()
        _insert_doc(db, "doc-a", "A", "agent-a")
        await ws.start()
        assert _sweeper_tasks(), "ttl>0 时清扫循环应已挂到 task group"
        await ws.stop()
        assert not _sweeper_tasks(), "stop() 后不得残留清扫任务"

    anyio.run(main)


# 6. T5 附带修复：private 文档 + 非创建者 + allowed_agents 命中 → True
#    （修复前该路径 json.loads 处 NameError）
def test_can_access_private_allowed_agent(tmp_path):
    ws, db = _mk_ws(tmp_path)

    async def main():
        await ws._init_db()
        _insert_doc(db, "doc-p", "私密文档", "agent-a", "private", ["agent-c"])
        assert await ws.can_access("doc-p", "agent-c") is True   # 白名单命中
        assert await ws.can_access("doc-p", "agent-b") is False  # 非白名单
        assert await ws.can_access("doc-p", "agent-a") is True   # 创建者

    anyio.run(main)
