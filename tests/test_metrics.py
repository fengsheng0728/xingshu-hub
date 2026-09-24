# -*- coding: utf-8 -*-
"""CD-084（2026-09-23）：GET /metrics Prometheus 文本端点测试。

直调 routes_server.metrics()（不起 TestClient）；CONFIG.DB_PATH monkeypatch
到 tmp_path 空库（init_db 建全 schema）。
"""
import asyncio
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as db_mod  # noqa: E402
import routes_server  # noqa: E402
from models import CONFIG  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "m.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


REQUIRED_METRICS = [
    "synchub_memories_total",
    "synchub_tasks_total",
    "synchub_agents_total",
    "synchub_agents_online",
    "synchub_db_size_bytes",
    "synchub_db_calls_total",
    "synchub_db_slow_queries_total",
    "synchub_db_slow_query_max_ms",
    "synchub_db_slow_query_threshold_ms",
    "synchub_chromadb_degraded",
    "synchub_chromadb_max_vectors",
    "synchub_backup_files",
    "synchub_backup_last_success_timestamp_seconds",
    "synchub_dead_letters_total",
    "synchub_dead_letters_pending",
    "synchub_uptime_seconds",
]

_LINE_RE = re.compile(
    r"^[a-z_][a-z0-9_]*(\{[a-z_]+=\"([^\"\\\\]|\\\\.)*\"\})? -?\d+(\.\d+)?$", re.I)


def test_metrics_text_format_and_presence(fresh_db):
    # tasks 空表时不出数值行（label 序列无样本）——插一条保证序列存在
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO tasks (task_id, status, creator_agent_id, assigned_agent_id)"
        " VALUES ('t0', 'pending', 'a', 'b')")
    conn.commit()
    conn.close()
    resp = asyncio.run(routes_server.metrics())
    assert resp.media_type.startswith("text/plain")
    body = resp.body.decode("utf-8")
    assert body.endswith("\n")
    for m in REQUIRED_METRICS:
        assert re.search(rf"^# TYPE {m} ", body, re.M), f"缺 TYPE 行: {m}"
        assert re.search(rf"^{m}({{|\s)", body, re.M), f"缺数值行: {m}"
    # 每个非注释行必须是合法 Prometheus 文本行
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        assert _LINE_RE.match(line), f"非法指标行: {line!r}"


def test_metrics_values_match_db(fresh_db):
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO tasks (task_id, status, creator_agent_id, assigned_agent_id)"
        " VALUES ('t1', 'done', 'a', 'b')")
    conn.commit()
    conn.close()
    db_mod.record_dead_letter("s1", "k", {}, "e1")
    db_mod.record_dead_letter("s2", "k", {}, "e2")
    body = asyncio.run(routes_server.metrics()).body.decode("utf-8")
    m = re.search(r"^synchub_dead_letters_total (\d+)$", body, re.M)
    assert m and int(m.group(1)) == 2
    m = re.search(r"^synchub_dead_letters_pending (\d+)$", body, re.M)
    assert m and int(m.group(1)) == 2
    m = re.search(r'^synchub_tasks_total\{status="done"\} (\d+)$', body, re.M)
    assert m and int(m.group(1)) == 1
    m = re.search(r"^synchub_db_size_bytes (\d+)$", body, re.M)
    assert m and int(m.group(1)) > 0


def test_metrics_survives_missing_dead_letters_table(fresh_db):
    """观测面不许 500：老库未迁移没有 dead_letters 表时其余指标照常输出。"""
    conn = sqlite3.connect(fresh_db)
    conn.execute("DROP TABLE dead_letters")
    conn.commit()
    conn.close()
    body = asyncio.run(routes_server.metrics()).body.decode("utf-8")
    assert "synchub_memories_total" in body
    assert "synchub_agents_online" in body
    assert "synchub_dead_letters_total" not in body  # 缺失即跳过，不出错值
