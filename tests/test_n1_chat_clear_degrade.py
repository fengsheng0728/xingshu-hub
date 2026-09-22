# -*- coding: utf-8 -*-
"""T8 CD-035 残留：routes_n1 chat_clear 分支 langchain 缺失降级（2026-09-19）

背景：routes_n1.py 的 `_execute_n1_delete` chat_clear 分支裸
`from hub_agent_lc import SQLiteChatHistory`，而 hub_agent_lc 模块级无保护
import langchain_openai/langchain_core —— 未装向量栈的部署上该分支直接
ImportError → 500 堆栈。同仓 routes_hubagent.py 四处同类导入均已
try/except ImportError → 明确降级响应，本文件把 n1 这处漏网点固化为回归：
- N1-1 langchain 缺失 → 不抛异常、返回明确降级标记
- N1-2 正常路径（本机已装 langchain）→ 行为与改动前一致（真清库、返回 executed）

仓里惯例直调 handler 协程（不走 TestClient），DB 路径 monkeypatch 到 tmp_path。
"""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import CONFIG
from routes_n1 import _execute_n1_delete


# N1-1 降级：强制 import hub_agent_lc 抛 ImportError → 明确降级响应，不抛异常
def test_chat_clear_degrades_without_langchain(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hub_agent_lc", None)  # import 即 ImportError
    monkeypatch.setattr(CONFIG, "DB_PATH", str(tmp_path / "chat.db"))
    detail = {"endpoint": "chat_clear", "params": {"session_id": "sess-t8"}}
    result = asyncio.run(_execute_n1_delete(detail))  # 修复前此处直接 ImportError
    assert isinstance(result, dict)
    assert result.get("status") == "degraded"
    assert result.get("endpoint") == "chat_clear"
    assert "langchain" in result.get("error", "")
    # 降级路径不得真建/真碰对话库
    assert not os.path.exists(str(tmp_path / "chat.db"))


# N1-2 正常路径不变：本机已装 langchain，真拿到 SQLiteChatHistory 并清库
def test_chat_clear_normal_path_unchanged(monkeypatch, tmp_path):
    db = str(tmp_path / "chat.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE hub_agent_conversations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,"
        " role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO hub_agent_conversations (session_id, role, content, created_at)"
        " VALUES ('sess-t8', 'user', 'hi', '2026-09-19T00:00:00')")
    conn.execute(
        "INSERT INTO hub_agent_conversations (session_id, role, content, created_at)"
        " VALUES ('sess-other', 'user', 'keep', '2026-09-19T00:00:00')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(CONFIG, "DB_PATH", db)
    detail = {"endpoint": "chat_clear", "params": {"session_id": "sess-t8"}}
    result = asyncio.run(_execute_n1_delete(detail))
    assert result == {"status": "executed", "endpoint": "chat_clear"}
    conn = sqlite3.connect(db)
    n_target = conn.execute(
        "SELECT COUNT(*) FROM hub_agent_conversations WHERE session_id = 'sess-t8'"
    ).fetchone()[0]
    n_other = conn.execute(
        "SELECT COUNT(*) FROM hub_agent_conversations WHERE session_id = 'sess-other'"
    ).fetchone()[0]
    conn.close()
    assert n_target == 0   # 目标会话已清
    assert n_other == 1    # 其它会话不受影响
