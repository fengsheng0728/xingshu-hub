"""db_facade — 星枢统一 db 访问门面（D-10 / 3-1a 门面底座 + 慢查询护栏）

定位
----
**唯一入口**：新代码查库一律走本模块；存量同步调用点（tools/db_call_sites.py 统计）
随 D-11 热点迁移与后续 PG 迁移逐步收敛到本门面。

**to_thread 的理由（CD-017 教训）**：慢的从来不是 SQLite 本身，而是"在事件循环上
同步等 SQL"——`_record_trace` 曾用同步 SQLite 写把事件循环串行化，峰值入队 31s。
因此本门面把连接建立（`sqlite3.connect` 本身也会阻塞）与执行**全部**放进
`asyncio.to_thread`，事件循环零阻塞。

**PG 接缝**：上游决议（docs/architecture-decision-data-backbone.md §四）主干路线为
SQLite → PostgreSQL → 多 Hub 分片。本门面即将来替换 asyncpg 的接缝——调用点只认
本模块签名，驱动更换时业务代码零改动。

**连接语义**（与 hub_core._db() 对齐，不自创变体）：每次调用新建连接 →
`row_factory = sqlite3.Row` → `PRAGMA busy_timeout = 5000` → 用完即关。

**慢查询护栏**：每次调用计时（含连接建立），耗时 >= `CONFIG.DB_SLOW_QUERY_MS`
（默认 200ms，config.yaml database.slow_query_ms 可调）→ WARNING + 计数器累计。
阈值运行时读取（不在 import 时固化），测试可 monkeypatch。
"""
import asyncio
import logging
import sqlite3
import threading
import time

from models import CONFIG

logger = logging.getLogger("xingshu.db_facade")

# ============ 慢查询护栏计数器（to_thread 多线程并发，必须加锁） ============
_lock = threading.Lock()
_stats = {
    "calls": 0,
    "slow_calls": 0,
    "max_ms": 0.0,
    "total_ms": 0.0,
}
_slow_top = []  # 慢查询明细（按耗时降序，最多留 10 条）
_SLOW_TOP_MAX = 10


def _db_path(db_path):
    """db_path 缺省 = CONFIG.DB_PATH；运行时读取（CONFIG 可变，测试会 monkeypatch）。"""
    return db_path if db_path is not None else CONFIG.DB_PATH


def _connect(db_path):
    """新建连接并对齐 hub_core._db() 语义：Row + busy_timeout=5000。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _one_line(sql, limit=120):
    """SQL 压成单行截断，供日志/计数器使用。"""
    return " ".join(str(sql).split())[:limit]


def _record(label, elapsed_ms):
    """计时落账 + 慢查询判定。label = SQL 或 run_sync/run_in_conn 的 fn 名。"""
    threshold = getattr(CONFIG, "DB_SLOW_QUERY_MS", 200)
    with _lock:
        _stats["calls"] += 1
        _stats["total_ms"] += elapsed_ms
        if elapsed_ms > _stats["max_ms"]:
            _stats["max_ms"] = elapsed_ms
        if elapsed_ms >= threshold:
            _stats["slow_calls"] += 1
            _slow_top.append({"sql": _one_line(label), "ms": round(elapsed_ms, 1)})
            _slow_top.sort(key=lambda x: -x["ms"])
            del _slow_top[_SLOW_TOP_MAX:]
    if elapsed_ms >= threshold:
        logger.warning(
            "db_facade 慢查询 %.1fms >= %dms: %s",
            elapsed_ms, threshold, _one_line(label),
        )


def stats_snapshot() -> dict:
    """护栏观测面快照（供 /health 与运维排查）。"""
    with _lock:
        calls = _stats["calls"]
        return {
            "calls": calls,
            "slow_calls": _stats["slow_calls"],
            "max_ms": round(_stats["max_ms"], 1),
            "avg_ms": round(_stats["total_ms"] / calls, 1) if calls else 0.0,
            "threshold_ms": getattr(CONFIG, "DB_SLOW_QUERY_MS", 200),
            "slow_top": list(_slow_top),
        }


def reset_stats() -> None:
    """清零计数器（测试与运维重置用）。"""
    with _lock:
        _stats["calls"] = 0
        _stats["slow_calls"] = 0
        _stats["max_ms"] = 0.0
        _stats["total_ms"] = 0.0
        _slow_top.clear()


# ============ 对外 API ============

async def execute(sql, params=(), *, db_path=None) -> int:
    """单条写操作（单事务 commit），返回 rowcount。异常原样抛出。"""
    def _run():
        conn = _connect(_db_path(db_path))
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    return await _timed(sql, _run)


async def executemany(sql, seq_of_params, *, db_path=None) -> int:
    """批量写（单事务 commit），返回 rowcount。异常原样抛出。"""
    def _run():
        conn = _connect(_db_path(db_path))
        try:
            cur = conn.executemany(sql, seq_of_params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    return await _timed(sql, _run)


async def query(sql, params=(), *, db_path=None) -> list:
    """查询，返回 list[sqlite3.Row]（线程内 fetchall 物化后才关连接）。"""
    def _run():
        conn = _connect(_db_path(db_path))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    return await _timed(sql, _run)


async def query_one(sql, params=(), *, db_path=None):
    """查询首行，返回 sqlite3.Row | None。"""
    def _run():
        conn = _connect(_db_path(db_path))
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()
    return await _timed(sql, _run)


async def run_sync(fn, *args, **kwargs):
    """任意同步 DB 逻辑经 to_thread 执行（连接由 fn 自己建/关）。

    用于包装不便拆成单条 SQL 的存量同步函数。计时与慢查询护栏同样适用，
    label 取 fn.__name__。
    """
    return await _timed(getattr(fn, "__name__", repr(fn)),
                        lambda: fn(*args, **kwargs))


async def run_in_conn(fn, *, db_path=None, write=False):
    """同一连接内跑多语句（D-11 迁移友好原语：现有热点大量是"一连接多语句+commit"）。

    - 连接与 fn 的调用全部在 to_thread 里（sqlite3.connect 本身也会阻塞，也要进线程）。
    - fn 收到的 conn 已设好 row_factory=Row + busy_timeout=5000。
    - write=False：只读语义，不 commit。
    - write=True：成功路径自动 commit；异常路径 rollback 后**原异常原样抛出**（不吞不包装）。
    - 计时与慢查询护栏同样适用（阈值比较用整次 fn 耗时）。

    最小用例（多语句事务 + 异常回滚）::

        def txn(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("UPDATE t SET v = v + 1")

        await run_in_conn(txn, write=True)          # 成功 → commit
        await run_in_conn(bad_txn, write=True)      # fn 抛错 → rollback，异常原样抛出
    """
    def _run():
        conn = _connect(_db_path(db_path))
        try:
            result = fn(conn)
            if write:
                conn.commit()
            return result
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()
    return await _timed(getattr(fn, "__name__", repr(fn)), _run)


async def _timed(label, fn):
    """to_thread 执行 + 计时落账。连接建立时间计入（在 _run 内部）。"""
    t0 = time.perf_counter()
    try:
        return await asyncio.to_thread(fn)
    finally:
        _record(label, (time.perf_counter() - t0) * 1000)
