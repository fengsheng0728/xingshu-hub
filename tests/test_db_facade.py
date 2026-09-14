"""tests/test_db_facade.py — D-10 db 门面底座单测

全部用临时 db 文件（tmp_path）+ db_path= 显式传入，不碰生产库。
项目无 pytest-asyncio 配置，沿用现有惯例：sync 测试函数内 anyio.run(coro)。
"""
import asyncio
import logging
import sqlite3
import time

import anyio
import pytest

import db_facade
from models import CONFIG


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "facade_test.db")


@pytest.fixture(autouse=True)
def _reset_stats():
    db_facade.reset_stats()
    yield
    db_facade.reset_stats()


# 1. execute / query / query_one 基本语义
def test_execute_query_query_one(db_path):
    async def main():
        n = await db_facade.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)", db_path=db_path)
        assert isinstance(n, int)
        n = await db_facade.execute(
            "INSERT INTO t (name) VALUES (?)", ("alice",), db_path=db_path)
        assert n == 1  # rowcount

        rows = await db_facade.query("SELECT * FROM t", db_path=db_path)
        assert isinstance(rows, list) and len(rows) == 1
        assert isinstance(rows[0], sqlite3.Row)
        assert rows[0]["name"] == "alice"  # row["col"] 可取

        one = await db_facade.query_one(
            "SELECT * FROM t WHERE name = ?", ("alice",), db_path=db_path)
        assert one is not None and one["name"] == "alice"
        none = await db_facade.query_one(
            "SELECT * FROM t WHERE name = ?", ("nobody",), db_path=db_path)
        assert none is None
    anyio.run(main)


# 2. executemany 批量插入
def test_executemany(db_path):
    async def main():
        await db_facade.execute("CREATE TABLE t (v INTEGER)", db_path=db_path)
        n = await db_facade.executemany(
            "INSERT INTO t (v) VALUES (?)", [(1,), (2,), (3,)], db_path=db_path)
        assert n == 3
        rows = await db_facade.query("SELECT COUNT(*) AS c FROM t", db_path=db_path)
        assert rows[0]["c"] == 3
    anyio.run(main)


# 3. 慢查询护栏：阈值=0 时记 WARNING + slow_calls 增长；恢复后不再增长；reset 归零
def test_slow_query_guard(db_path, monkeypatch, caplog):
    async def main():
        await db_facade.execute("CREATE TABLE t (v INTEGER)", db_path=db_path)

        monkeypatch.setattr(CONFIG, "DB_SLOW_QUERY_MS", 0)  # 0 = 每次调用都记
        with caplog.at_level(logging.WARNING, logger="xingshu.db_facade"):
            await db_facade.query("SELECT 1", db_path=db_path)
        snap = db_facade.stats_snapshot()
        assert snap["slow_calls"] >= 1
        assert any(r.levelno == logging.WARNING and "慢查询" in r.getMessage()
                   for r in caplog.records), "caplog 里应有 db_facade 慢查询 WARNING"

        monkeypatch.setattr(CONFIG, "DB_SLOW_QUERY_MS", 10 ** 9)  # 恢复成不可能触发的阈值
        before = db_facade.stats_snapshot()["slow_calls"]
        await db_facade.query("SELECT 1", db_path=db_path)
        assert db_facade.stats_snapshot()["slow_calls"] == before

        db_facade.reset_stats()
        snap = db_facade.stats_snapshot()
        assert snap["calls"] == 0 and snap["slow_calls"] == 0
    anyio.run(main)


# 4. 不阻塞事件循环（门面存在的理由）：
#    run_sync(time.sleep, 0.4) 期间，0.05s 心跳 tick 数 >= 3（同步阻塞则 0~1）
def test_run_sync_does_not_block_event_loop():
    async def main():
        ticks = []

        async def heartbeat():
            for _ in range(20):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        await asyncio.gather(
            db_facade.run_sync(time.sleep, 0.4),
            heartbeat(),
        )
        return ticks

    ticks = anyio.run(main)
    # 0.4s 窗口 / 0.05s 间隔 ≈ 8 tick；放宽到 >= 3 防 CI 抖动
    assert len(ticks) >= 3, f"心跳 tick={len(ticks)}，事件循环被同步阻塞了"


# 5. run_in_conn 事务语义
def test_run_in_conn_write_commit_visible(db_path):
    async def main():
        await db_facade.execute("CREATE TABLE t (v INTEGER)", db_path=db_path)

        def txn(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("INSERT INTO t VALUES (2)")
            return "ok"

        result = await db_facade.run_in_conn(txn, db_path=db_path, write=True)
        assert result == "ok"
        # 另一连接（query 新建连接）可读 → 真的 commit 了
        rows = await db_facade.query("SELECT COUNT(*) AS c FROM t", db_path=db_path)
        assert rows[0]["c"] == 2
    anyio.run(main)


def test_run_in_conn_exception_rolls_back(db_path):
    async def main():
        await db_facade.execute("CREATE TABLE t (v INTEGER)", db_path=db_path)

        def bad_txn(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise ValueError("boom-txn")

        with pytest.raises(ValueError, match="boom-txn"):  # 异常原样抛出
            await db_facade.run_in_conn(bad_txn, db_path=db_path, write=True)
        # rollback 生效：表里没有该批写入
        rows = await db_facade.query("SELECT COUNT(*) AS c FROM t", db_path=db_path)
        assert rows[0]["c"] == 0
    anyio.run(main)


def test_run_in_conn_readonly_no_commit(db_path):
    async def main():
        await db_facade.execute("CREATE TABLE t (v INTEGER)", db_path=db_path)

        def read_side_effect(conn):
            conn.execute("INSERT INTO t VALUES (9)")  # write=False 下不应生效
            return conn.execute("SELECT 1").fetchone()

        await db_facade.run_in_conn(read_side_effect, db_path=db_path, write=False)
        rows = await db_facade.query("SELECT COUNT(*) AS c FROM t", db_path=db_path)
        assert rows[0]["c"] == 0
    anyio.run(main)


# 6. 连接语义：busy_timeout=5000 + row_factory=Row
def test_connection_semantics(db_path):
    async def main():
        rows = await db_facade.query("PRAGMA busy_timeout", db_path=db_path)
        assert rows[0][0] == 5000
        row = await db_facade.query_one("SELECT 1 AS one", db_path=db_path)
        assert isinstance(row, sqlite3.Row) and row["one"] == 1  # row_factory 生效
    anyio.run(main)


# 7. db_path=None 时用 CONFIG.DB_PATH（运行时读取，monkeypatch 生效）
def test_default_db_path_from_config(tmp_path, monkeypatch):
    cfg_db = str(tmp_path / "cfg_default.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", cfg_db)

    async def main():
        await db_facade.execute("CREATE TABLE t (v INTEGER)")  # 不传 db_path
        await db_facade.execute("INSERT INTO t VALUES (42)")
        row = await db_facade.query_one("SELECT v FROM t")
        assert row["v"] == 42
    anyio.run(main)


# 8. SQLite 异常原样抛出（不吞、不包装）
def test_sqlite_error_raised_as_is(db_path):
    async def main():
        with pytest.raises(sqlite3.OperationalError):
            await db_facade.query("SELECT * FROM nope", db_path=db_path)
    anyio.run(main)
