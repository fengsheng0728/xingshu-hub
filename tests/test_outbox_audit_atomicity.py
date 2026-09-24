# -*- coding: utf-8 -*-
"""CD-045 审计原子性（outbox 同事务）验收测试 — T1 裂缝4（2026-09-17）

工程约定照抄 tests/test_shadow_pending.py：sys.path.insert + tmp_path + monkeypatch，
临时库（db.init_db 全量建表）+ 临时审计目录，不碰生产 sync_hub.db。

覆盖（任务书 T6 验收）：
T6-1 正常写：store_memory → memory_pool 有行 + event_outbox 1 行 pending；
    drain → done + 审计 jsonl 落对应 action
T6-2 原子性反例（核心）：注入 commit() 失败 → memory_pool 无行、event_outbox 无行、
    审计 jsonl 无该 memory_key（数据与审计同生共死，链上不留假记录）
T6-3 审计失败补偿：audit_fn 抛异常 → 业务已提交 + 事件行 pending/attempts 累加/
    last_error 非空；换正常 audit_fn 再 drain → done 且 jsonl 补上（一条不丢）
T6-4 超限标记：drain 到 attempts >= OUTBOX_MAX_ATTEMPTS → failed，stats failed >= 1
T6-5 顺序保持：3 条不同 memory_key → jsonl 出现顺序 == 写入顺序
T6-6 重启 replay：预置 pending → 新建 OutboxConsumer drain → done
T6-7 回退门禁：hub_mixins/memory.py 内 audit_memory( 只出现 1 次（read 路径）
T6-8 schema 门禁：临时库经 db.init_db 后 event_outbox 存在 + user_version == 7
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import audit.memory_audit as memory_audit  # noqa: E402
import db_facade  # noqa: E402
import models  # noqa: E402
from hub_core import SyncHub  # noqa: E402  （复用 _merge_trust/_trust_from_source 真实现）
from hub_mixins.memory import MemoryMixin  # noqa: E402
from hub_mixins.outbox import (  # noqa: E402
    OUTBOX_MAX_ATTEMPTS,
    OutboxConsumer,
    enqueue,
)
from models import MemoryEntry  # noqa: E402


class _MiniHub(MemoryMixin):
    """最小 MemoryMixin 宿主：关 embedding/影子/Chroma，审计链路全保留。"""

    _merge_trust = SyncHub._merge_trust
    _trust_from_source = SyncHub._trust_from_source

    def __init__(self):
        self.agents = {}
        self._shadow = None
        self._chroma_collection = None
        self._memory_lock = asyncio.Lock()

    async def _ensure_embedding_model(self):
        return None  # 无 embedding → 走"直接新增"分支（审计点 :235）

    async def _log_event(self, *args, **kwargs):
        return None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表）+ 临时审计目录（memory_audit 模块级路径重定向）。"""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()
    monkeypatch.setattr(memory_audit, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(memory_audit, "AUDIT_FILE",
                        str(tmp_path / "audit" / "memory_pool.jsonl"))
    monkeypatch.setattr(memory_audit, "_rolling_chain", None)
    return {"db_path": db_path,
            "audit_file": tmp_path / "audit" / "memory_pool.jsonl"}


def _outbox_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM event_outbox ORDER BY id")]
    conn.close()
    return rows


def _audit_lines(audit_file):
    if not audit_file.exists():
        return []
    with open(audit_file, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _enqueue_committed(db_path, event_type, payload):
    """独立连接 enqueue + commit（等价于一个已提交的业务事务）。"""
    conn = sqlite3.connect(db_path)
    enqueue(conn, event_type, payload)
    conn.commit()
    conn.close()


# T6-1 正常写：事件行随业务事务提交，drain 后落审计链
def test_normal_write_through_outbox(env):
    hub = _MiniHub()
    result = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="偏好-少辣", content="客户不吃辣")))
    assert result["status"] == "stored"

    conn = sqlite3.connect(env["db_path"])
    n_pool = conn.execute(
        "SELECT COUNT(*) FROM memory_pool WHERE memory_key='偏好-少辣'").fetchone()[0]
    conn.close()
    assert n_pool == 1, "memory_pool 应有该行"

    rows = _outbox_rows(env["db_path"])
    assert len(rows) == 1, "event_outbox 应有 1 行"
    assert rows[0]["event_type"] == "memory_audit"
    assert rows[0]["status"] == "pending"
    payload = json.loads(rows[0]["payload"])
    assert payload["action"] == "write"
    assert payload["memory_key"] == "偏好-少辣"
    assert payload["agent_id"] == "agent-1"
    assert payload["memory_id"] == result["memory_id"]

    # 确定性 drain（不依赖 sleep）→ done + jsonl 落链
    consumer = OutboxConsumer(env["db_path"])
    assert consumer._drain_once() is True
    rows = _outbox_rows(env["db_path"])
    assert rows[0]["status"] == "done"
    lines = _audit_lines(env["audit_file"])
    assert any(ln["action"] == "write" and ln["memory_key"] == "偏好-少辣"
               for ln in lines), "审计 jsonl 应出现对应 write 记录"
    assert consumer.stats_snapshot()["done"] == 1


# T6-2 原子性反例（核心断言）：commit 注入失败 → 三处同时无记录
def test_commit_failure_rolls_back_data_event_and_audit(env, monkeypatch):
    """注入点：monkeypatch db_facade.run_in_conn，把 fn 的 conn 换成
    commit() 必抛 sqlite3.OperationalError 的代理（memory.py 经模块属性
    db_facade.run_in_conn 解析调用点，故替换生效）。
    注入前（旧代码）此场景 = 审计 jsonl 有记录但 memory_pool 无行（链上假记录）；
    注入后（outbox）三者必须同时为空。"""
    class _FailCommitConn:
        def __init__(self, real):
            self._real = real

        def cursor(self):
            return self._real.cursor()

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure (disk I/O)")

        def __getattr__(self, name):
            return getattr(self._real, name)

    async def _boom_run_in_conn(fn, *, db_path=None, write=False):
        conn = sqlite3.connect(db_path or models.CONFIG.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        wrapped = _FailCommitConn(conn)
        try:
            result = fn(wrapped)
            if write:
                wrapped.commit()  # 对齐真门面 write=True 的提交点 → 注入失败在此引爆
            return result
        finally:
            conn.close()  # 未 commit → close 即回滚

    monkeypatch.setattr(db_facade, "run_in_conn", _boom_run_in_conn)

    hub = _MiniHub()
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(hub.store_memory(
            "agent-1", MemoryEntry(memory_key="原子性反例", content="commit 必败")))

    conn = sqlite3.connect(env["db_path"])
    n_pool = conn.execute(
        "SELECT COUNT(*) FROM memory_pool WHERE memory_key='原子性反例'").fetchone()[0]
    n_outbox = conn.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
    conn.close()
    assert n_pool == 0, "commit 失败 → memory_pool 不得有该行"
    assert n_outbox == 0, "commit 失败 → event_outbox 不得有事件行（同事务回滚）"
    assert all(ln.get("memory_key") != "原子性反例"
               for ln in _audit_lines(env["audit_file"])), \
        "commit 失败 → 审计 jsonl 不得有该 memory_key 记录"


# T6-3 审计失败补偿：业务已提交，事件行累加 attempts；修复后 drain 补齐
def test_audit_failure_compensation_no_loss(env):
    hub = _MiniHub()
    result = asyncio.run(hub.store_memory(
        "agent-1", MemoryEntry(memory_key="补偿-不丢", content="审计后端故障")))
    assert result["status"] == "stored"  # 业务成功（审计异步，不阻塞主链路）

    def _boom_audit(**kwargs):
        raise RuntimeError("audit backend down")

    consumer = OutboxConsumer(env["db_path"], audit_fn=_boom_audit)
    consumer._drain_once()
    row = _outbox_rows(env["db_path"])[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["last_error"], "last_error 应非空"
    assert "RuntimeError" in row["last_error"]
    assert _audit_lines(env["audit_file"]) == [], "失败时 jsonl 不应有记录"

    # 换成正常 audit_fn（默认懒加载 audit_memory）再 drain → done + jsonl 补上
    consumer2 = OutboxConsumer(env["db_path"])
    assert consumer2._drain_once() is True
    row = _outbox_rows(env["db_path"])[0]
    assert row["status"] == "done"
    lines = _audit_lines(env["audit_file"])
    assert any(ln["memory_key"] == "补偿-不丢" for ln in lines), \
        "恢复后 jsonl 应补上该记录（一条不丢）"


# T6-4 超限标记：attempts 达上限 → failed + stats 计数
def test_attempts_exceed_marks_failed(env):
    _enqueue_committed(env["db_path"], "memory_audit", {
        "action": "write", "agent_id": "a", "memory_key": "超限",
        "memory_id": "m-x"})

    def _boom_audit(**kwargs):
        raise RuntimeError("permanent failure")

    consumer = OutboxConsumer(env["db_path"], audit_fn=_boom_audit)
    for _ in range(OUTBOX_MAX_ATTEMPTS):
        consumer._drain_once()
    row = _outbox_rows(env["db_path"])[0]
    assert row["status"] == "failed"
    assert row["attempts"] >= OUTBOX_MAX_ATTEMPTS
    assert consumer.stats_snapshot()["failed"] >= 1


# T6-5 顺序保持：jsonl 出现顺序 == 写入顺序
def test_fifo_order_preserved(env):
    for key in ("顺序-k1", "顺序-k2", "顺序-k3"):
        _enqueue_committed(env["db_path"], "memory_audit", {
            "action": "write", "agent_id": "a",
            "memory_key": key, "memory_id": key})
    consumer = OutboxConsumer(env["db_path"])
    consumer._drain_once()
    keys = [ln["memory_key"] for ln in _audit_lines(env["audit_file"])]
    assert keys == ["顺序-k1", "顺序-k2", "顺序-k3"], \
        f"jsonl 顺序应等于写入顺序，实际 {keys}"
    assert all(r["status"] == "done" for r in _outbox_rows(env["db_path"]))


# T6-6 重启 replay：预置 pending → 新消费者（等价新进程）drain 成 done
def test_restart_replay(env):
    _enqueue_committed(env["db_path"], "memory_audit", {
        "action": "write", "agent_id": "a", "memory_key": "重启replay",
        "memory_id": "m-replay"})
    assert _outbox_rows(env["db_path"])[0]["status"] == "pending"
    # 新建 OutboxConsumer = 等价于进程重启后 replay
    consumer = OutboxConsumer(env["db_path"])
    assert consumer._drain_once() is True
    assert _outbox_rows(env["db_path"])[0]["status"] == "done"
    assert any(ln["memory_key"] == "重启replay"
               for ln in _audit_lines(env["audit_file"]))


# T6-7 回退门禁：memory.py 内 audit_memory( 只出现 1 次（read 路径）
def test_no_direct_audit_call_in_txn_guard():
    path = os.path.join(ROOT, "hub_mixins", "memory.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    n = src.count("audit_memory(")
    assert n == 1, \
        f"hub_mixins/memory.py 内 audit_memory( 应只剩 read 路径 1 处，实际 {n} 处"


# T6-8 schema 门禁：init_db 后 event_outbox 存在 + user_version == 7
def test_schema_v7_event_outbox(env):
    import db as dbmod
    assert dbmod.SCHEMA_VERSION == 7, "db.py SCHEMA_VERSION 应为 7"
    conn = sqlite3.connect(env["db_path"])
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event_outbox)")}
    conn.close()
    assert ver == 7, f"user_version {ver} 应 == 7"
    assert "event_outbox" in tables
    assert "idx_event_outbox_status" in idx
    for col in ("id", "event_type", "payload", "created_at",
                "status", "attempts", "last_error"):
        assert col in cols, f"event_outbox 缺列 {col}"
    dbmod.init_db()  # 幂等：再跑一次不崩
