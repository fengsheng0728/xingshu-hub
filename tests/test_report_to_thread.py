"""batchD：B 类 sqlite 热点 to_thread 化回归测试

覆盖两个热点：
- hub_mixins/dashboard.py: DashboardMixin.get_dashboard_data → _get_dashboard_data_sync + asyncio.to_thread
- routes_report.py: api_daily_report → _daily_report_stats_sync + asyncio.to_thread（LLM 调用保持在 async 壳内）

验证点：
1. 行为零变化：返回结构 / 角色过滤语义与改造前一致
2. async 壳确实经 asyncio.to_thread 执行同步查询段
"""
import asyncio
import ast
import contextlib
import inspect
import json
import sqlite3
from datetime import datetime, timezone

import pytest


# ─────────────────────────────────────────────
# Dashboard: get_dashboard_data
# ─────────────────────────────────────────────

def _init_dashboard_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE agents (
            agent_id TEXT PRIMARY KEY,
            agent_name TEXT,
            department TEXT,
            role TEXT,
            status TEXT,
            capabilities TEXT,
            managed_agents TEXT,
            last_heartbeat TEXT
        );
        CREATE TABLE memory_pool (
            memory_key TEXT PRIMARY KEY,
            owner_agent_id TEXT,
            created_at TEXT
        );
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT,
            depends_on TEXT,
            creator_agent_id TEXT,
            assigned_agent_id TEXT,
            updated_at TEXT
        );
        CREATE TABLE disclosure_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_agent_id TEXT,
            to_agent_id TEXT,
            disclosed_level TEXT,
            disclosed_at TEXT,
            reason TEXT
        );
        CREATE TABLE disclosure_requests (
            request_id TEXT PRIMARY KEY,
            task_id TEXT,
            agent_id TEXT,
            reason TEXT,
            new_phase TEXT,
            status TEXT,
            created_at TEXT,
            audit_decision TEXT,
            audit_reason TEXT,
            audit_risk_level TEXT
        );
    """)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO agents VALUES (?,?,?,?,?,?,?,?)",
        [
            ("orch", "总管", "mgmt", "orchestrator", "online", "[]", '["mgr"]', now),
            ("mgr", "经理", "sales", "manager", "online", "[]", '["w1"]', now),
            ("w1", "工一号", "sales", "worker", "offline", "[]", "[]", now),
        ],
    )
    conn.execute("INSERT INTO memory_pool VALUES ('m1', 'w1', ?)", (now,))
    conn.execute(
        "INSERT INTO tasks VALUES ('t1', 'pending', '[]', 'mgr', 'w1', ?)", (now,))
    conn.execute(
        "INSERT INTO disclosure_log (from_agent_id, to_agent_id, disclosed_level, disclosed_at, reason)"
        " VALUES ('mgr', 'w1', 'summary', ?, '测试')", (now,))
    conn.execute(
        "INSERT INTO disclosure_requests VALUES ('r1', 't1', 'w1', '升级', 'p2', 'pending', ?, '', '', '')",
        (now,))
    conn.commit()
    conn.close()


from hub_mixins.dashboard import DashboardMixin
from hub_mixins.tasks import TasksMixin


class _FakeHub:
    """最小 hub 替身：只提供 get_dashboard_data 依赖的 _db / agents / _blocked_by"""
    get_dashboard_data = DashboardMixin.get_dashboard_data
    _get_dashboard_data_sync = DashboardMixin._get_dashboard_data_sync
    _blocked_by = TasksMixin._blocked_by

    def __init__(self, db_path: str, agents: dict):
        self._db_path = db_path
        self.agents = agents

    @contextlib.contextmanager
    def _db(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


@pytest.fixture()
def dash_env(tmp_path):
    db_path = str(tmp_path / "dash.db")
    _init_dashboard_db(db_path)
    return db_path


def test_dashboard_shell_uses_to_thread():
    from hub_mixins.dashboard import DashboardMixin
    src = inspect.getsource(DashboardMixin.get_dashboard_data)
    assert "asyncio.to_thread" in src
    assert "execute" not in src  # async 壳内不得残留同步查询


def test_dashboard_orchestrator_sees_all(dash_env):
    hub = _FakeHub(dash_env, agents={})
    data = asyncio.run(hub.get_dashboard_data(""))
    assert data["viewer_role"] == "orchestrator"
    assert data["agents"]["total"] == 3
    assert data["agents"]["online"] == 2
    assert len(data["agents"]["list"]) == 3
    assert data["memories"]["total"] == 1
    assert data["tasks"]["total"] == 1
    assert data["tasks"]["pending"] == 1
    assert data["tasks"]["by_status"] == {"pending": 1}
    assert data["tasks"]["list"][0]["blocked_by"] == []
    assert data["disclosures"]["total"] == 1
    assert data["disclosures"]["recent"][0]["from"] == "mgr"
    assert len(data["pending_disclosures"]) == 1
    assert data["pending_disclosures"][0]["request_id"] == "r1"


def test_dashboard_worker_scope(dash_env):
    agents = {
        "mgr": {"role": "manager", "managed_agents": ["w1"]},
        "w1": {"role": "worker", "managed_agents": []},
    }
    hub = _FakeHub(dash_env, agents=agents)
    data = asyncio.run(hub.get_dashboard_data("w1"))
    assert data["viewer_role"] == "worker"
    # worker 可见：自己 + 上级 mgr（mgr.managed_agents 含 w1）
    visible = {a["agent_id"] for a in data["agents"]["list"]}
    assert visible == {"w1", "mgr"}
    assert data["agents"]["total"] == 2


def test_dashboard_unknown_requester_defaults_worker(dash_env):
    hub = _FakeHub(dash_env, agents={})
    data = asyncio.run(hub.get_dashboard_data("ghost"))
    assert data["viewer_role"] == "worker"
    # ghost 不在 agents 表 → 可见集仅 {ghost}，IN 查询命中 0 行
    assert data["agents"]["total"] == 0
    assert data["agents"]["list"] == []


# ─────────────────────────────────────────────
# Report: api_daily_report
# ─────────────────────────────────────────────

def _init_report_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT,
            updated_at TEXT
        );
        CREATE TABLE memory_pool (
            memory_key TEXT PRIMARY KEY,
            tags TEXT,
            created_at TEXT
        );
        CREATE TABLE disclosure_requests (
            request_id TEXT PRIMARY KEY,
            status TEXT,
            created_at TEXT
        );
        CREATE TABLE knowledge_base (
            entry_id TEXT PRIMARY KEY
        );
    """)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.executemany(
        "INSERT INTO tasks VALUES (?,?,?)",
        [
            ("t1", "completed", f"{today}T01:00:00"),
            ("t2", "completed", f"{today}T02:00:00"),
            ("t3", "failed", f"{today}T03:00:00"),
            ("t4", "in_progress", f"{today}T04:00:00"),
            ("t5", "assigned", f"{today}T05:00:00"),
            ("t6", "completed", "2020-01-01T00:00:00"),  # 非今日，不计入
        ],
    )
    conn.executemany(
        "INSERT INTO memory_pool VALUES (?,?,?)",
        [
            ("m1", '["售后","退款"]', f"{today}T01:00:00"),
            ("m2", '["售后"]', f"{today}T02:00:00"),
            ("m3", '', f"{today}T03:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO disclosure_requests VALUES (?,?,?)",
        [("r1", "pending", f"{today}T01:00:00"), ("r2", "approved", f"{today}T02:00:00")],
    )
    conn.execute("INSERT INTO knowledge_base VALUES ('kb1')")
    conn.commit()
    conn.close()


@pytest.fixture()
def report_env(tmp_path, monkeypatch):
    import routes_report
    from models import CONFIG

    db_path = str(tmp_path / "report.db")
    _init_report_db(db_path)
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    # Agent 状态来自内存 hub.agents；隔离为固定两实例
    monkeypatch.setattr(routes_report.hub, "agents", {
        "a1": {"status": "online"},
        "a2": {"status": "offline"},
    })
    return routes_report


def test_report_shell_uses_to_thread_and_llm_stays_async():
    import routes_report
    src = inspect.getsource(routes_report.api_daily_report)
    assert "asyncio.to_thread" in src
    assert "await client.post" in src          # LLM 调用必须留在 async 壳内
    assert "sqlite3.connect" not in src        # async 壳内不得残留同步连接
    sync_src = inspect.getsource(routes_report._daily_report_stats_sync)
    # 同步函数内不得有 await（AST 级断言，避开 docstring 文字干扰）
    tree = ast.parse(sync_src)
    assert not any(isinstance(n, ast.Await) for n in ast.walk(tree))


def test_daily_report_no_llm(report_env, monkeypatch):
    routes_report = report_env
    monkeypatch.setattr(routes_report.hub_agent, "is_configured", lambda: False)

    result = asyncio.run(routes_report.api_daily_report())

    assert result["status"] == "ok"
    assert result["summary"] is None
    stats = result["stats"]
    assert stats["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert stats["tasks"]["done"] == 2
    assert stats["tasks"]["failed"] == 1
    assert stats["tasks"]["active"] == 2  # in_progress + assigned
    assert stats["tasks"]["by_status"]["completed"] == 2
    assert stats["memories"] == 3
    assert stats["disclosures"] == {"total": 2, "pending": 1}
    assert stats["agents"] == {"online": 1, "total": 2}
    assert stats["top_tags"][0] == {"tag": "售后", "count": 2}
    assert {t["tag"] for t in stats["top_tags"]} == {"售后", "退款"}
    assert stats["knowledge_base"] == 1


def test_daily_report_sync_function_directly(report_env):
    """同步函数可独立执行并返回完整 stats dict（to_thread 的目标形态）"""
    stats = report_env._daily_report_stats_sync()
    assert stats["tasks"]["done"] == 2
    assert stats["knowledge_base"] == 1
