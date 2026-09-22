# -*- coding: utf-8 -*-
"""T20 · CD-058（MCP 主体身份注入，contextvars）验收测试（2026-09-20）

M-1 特权全文（先红核心）：manager / hub_token 主体 + 页面正文含 200 字之外
    的哨兵串 → level=="full"、哨兵在 content 里、truncated is False
M-2 无主体对照：不注入 contextvar 直调 → level=="summary"、content 不含哨兵、
    truncated is True（CD-054 fail-closed 现状逐字保留）
M-3 审计对照：M-1 → gateway_read_log 记真实 requester/auth_mode；
    M-2 → 仍 mcp-tool / anonymous-tool（逐字不变，供对照实证）
M-4 非特权不放宽：worker 主体 → 仍 summary（审计记真实主体）
M-5 零回归：wiki_list / wiki_graph / buffer_stats 直调返回形态不变，
    且不新增读审计行
M-6 wiki_search 只标注 level：特权=full、无主体=summary，结果集/snippet 口径不变

脚手架（同 test_mcp_failclosed）：临时库 db.init_db() 建全 schema +
补 knowledge_base.embedding 增量列；WIKI_ROOT 两处 patch 指 tmp_path；
agents 表插 mgr-1(manager) / wrk-1(worker) 供 principal_is_privileged 查 role。
主体经 routes_gateway.set_mcp_principal 手工注入（直调工具函数路径）；
HTTP 级验证（中间件真实注入）另由临时 Hub(3068) MCP SSE 真调用覆盖（探针报告）。
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models  # noqa: E402
import wiki_engine  # noqa: E402
import mcp_server  # noqa: E402
from auth_provider import Principal  # noqa: E402
from routes_gateway import set_mcp_principal  # noqa: E402

SENTINEL = "SENTINEL-CD058-TAIL-特权全文哨兵"
PAGE_REL = "entities/t20-probe.md"

FRONTMATTER = (
    "---\n"
    "title: T20探针页\n"
    "created: 2026-09-20\n"
    "updated: 2026-09-20\n"
    "type: entity\n"
    "tags: t20, cd058, 身份注入\n"
    "---\n"
)


def _make_body() -> str:
    """正文：前段填充 >200 字，唯一哨兵串放在 200 字之外的尾部。"""
    head = "这是正文开头。" + "星枢探针正文填充。" * 40  # 9*40=360 字
    return head + "\n\n结尾段。" + SENTINEL + "\n"


@pytest.fixture(autouse=True)
def _clean_ctx():
    """每条用例前后清空主体 contextvar（防同线程跨用例串扰）。"""
    set_mcp_principal(None)
    yield
    set_mcp_principal(None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表 + 补 embedding 增量列 + agents 角色行）
    + 临时 wiki 根目录（埋一页含尾部哨兵串的探针页）。"""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()
    conn = sqlite3.connect(db_path)
    kb_cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_base)")}
    if "embedding" not in kb_cols:
        conn.execute("ALTER TABLE knowledge_base ADD COLUMN embedding BLOB")
    conn.execute(
        "INSERT INTO agents (agent_id, agent_name, role) VALUES ('mgr-1', '经理', 'manager')")
    conn.execute(
        "INSERT INTO agents (agent_id, agent_name, role) VALUES ('wrk-1', '工人', 'worker')")
    conn.commit()
    conn.close()

    wiki_root = str(tmp_path / "wiki")
    monkeypatch.setattr(wiki_engine, "WIKI_ROOT", wiki_root)
    monkeypatch.setattr(mcp_server, "WIKI_ROOT", wiki_root)
    os.makedirs(os.path.join(wiki_root, "entities"), exist_ok=True)
    with open(os.path.join(wiki_root, "entities", "t20-probe.md"),
              "w", encoding="utf-8", newline="") as f:
        f.write(FRONTMATTER + _make_body())
    return {"db": db_path, "wiki": wiki_root, "page": PAGE_REL}


def _log_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT requester, auth_mode, kind, query, target, granted_level,"
        " item_count, stripped_chunks FROM gateway_read_log ORDER BY log_id")]
    conn.close()
    return rows


def _principal(subject_id: str, auth_mode: str = "api_key") -> Principal:
    return Principal(subject_type="service", subject_id=subject_id,
                     auth_mode=auth_mode)


# ═══════════ M-1 特权全文（先红核心：改动前一律 summary，必然失败） ═══════════

def test_m1_privileged_fulltext_manager(env):
    set_mcp_principal(_principal("mgr-1"))
    r = mcp_server.wiki_get(env["page"])
    assert r["status"] == "ok"
    assert r["level"] == "full", f"manager 主体应拿到全文，实际 level={r['level']}"
    assert r["truncated"] is False
    assert SENTINEL in r["content"], "200 字之外的哨兵串应在全文 content 里"
    assert len(r["content"]) > 200
    assert r["content"].startswith("---"), "frontmatter 仍保留"


def test_m1b_privileged_fulltext_hub_token(env):
    set_mcp_principal(_principal("__hub__", auth_mode="hub_token"))
    r = mcp_server.wiki_get(env["page"])
    assert r["status"] == "ok"
    assert r["level"] == "full", f"hub_token 主体应拿到全文，实际 level={r['level']}"
    assert r["truncated"] is False
    assert SENTINEL in r["content"]


# ═══════════ M-2 无主体对照（CD-054 fail-closed 逐字保留） ═══════════

def test_m2_no_principal_failclosed(env):
    r = mcp_server.wiki_get(env["page"])
    assert r["status"] == "ok"
    assert r["level"] == "summary"
    assert r["truncated"] is True
    assert SENTINEL not in json.dumps(r, ensure_ascii=False), \
        "无主体不得泄露 200 字之外的哨兵串"


# ═══════════ M-3 审计对照：真实主体 vs 无主体逐字不变 ═══════════

def test_m3_audit_real_principal(env):
    before = len(_log_rows(env["db"]))
    set_mcp_principal(_principal("mgr-1"))
    r = mcp_server.wiki_get(env["page"])
    assert r["status"] == "ok"
    rows = _log_rows(env["db"])
    assert len(rows) == before + 1, f"wiki_get 应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "mgr-1", "审计 requester 应记真实主体 id"
    assert row["auth_mode"] == "api_key", "审计 auth_mode 应记真实类别"
    assert row["kind"] == "wiki"
    assert row["target"] == env["page"]
    assert row["granted_level"] == "full"


def test_m3b_audit_hub_token_principal(env):
    before = len(_log_rows(env["db"]))
    set_mcp_principal(_principal("__hub__", auth_mode="hub_token"))
    r = mcp_server.wiki_get(env["page"])
    assert r["status"] == "ok"
    rows = _log_rows(env["db"])
    assert len(rows) == before + 1
    row = rows[-1]
    assert row["requester"] in ("__hub__", "hub-token"), \
        "hub_token 主体记其 subject_id 或 hub-token"
    assert row["auth_mode"] == "hub_token"
    assert row["granted_level"] == "full"


def test_m3c_audit_anonymous_unchanged(env):
    before = len(_log_rows(env["db"]))
    r = mcp_server.wiki_get(env["page"])  # 无主体
    assert r["status"] == "ok"
    rows = _log_rows(env["db"])
    assert len(rows) == before + 1
    row = rows[-1]
    assert row["requester"] == "mcp-tool"  # 逐字不变
    assert row["auth_mode"] == "anonymous-tool"  # 逐字不变
    assert row["granted_level"] == "summary"


# ═══════════ M-4 非特权不放宽（worker 仍 summary，审计记真实主体） ═══════════

def test_m4_worker_not_elevated(env):
    before = len(_log_rows(env["db"]))
    set_mcp_principal(_principal("wrk-1"))
    r = mcp_server.wiki_get(env["page"])
    assert r["status"] == "ok"
    assert r["level"] == "summary", f"worker 主体不得放宽，实际 level={r['level']}"
    assert r["truncated"] is True
    assert SENTINEL not in json.dumps(r, ensure_ascii=False)
    rows = _log_rows(env["db"])
    assert len(rows) == before + 1
    row = rows[-1]
    assert row["requester"] == "wrk-1"
    assert row["auth_mode"] == "api_key"
    assert row["granted_level"] == "summary"


# ═══════════ M-5 零回归：结构类工具形态不变、不落读审计 ═══════════

def test_m5_structural_tools_unchanged(env):
    set_mcp_principal(_principal("mgr-1"))  # 特权主体也不影响结构类工具
    before = len(_log_rows(env["db"]))
    r1 = mcp_server.wiki_list()
    assert r1["status"] == "ok"
    assert r1["total"] == 1
    assert "by_type" in r1
    r2 = mcp_server.wiki_graph()
    assert "nodes" in r2 and "links" in r2
    r3 = mcp_server.buffer_stats()
    assert "queue_depth" in r3 and "total_flushed" in r3
    assert len(_log_rows(env["db"])) == before, \
        "wiki_list / wiki_graph / buffer_stats 不得新增读审计行"


# ═══════════ M-6 wiki_search 只标注 level，结果集口径不变 ═══════════

def test_m6_search_level_marking(env):
    # 无主体 → summary（现状逐字保留）
    r = mcp_server.wiki_search("探针", field="title")
    assert r["status"] == "ok"
    assert r["level"] == "summary"
    assert r["count"] >= 1
    # 特权主体 → level 标注 full，结果集/snippet 口径不变（不放大成全文）
    set_mcp_principal(_principal("mgr-1"))
    r2 = mcp_server.wiki_search("探针", field="title")
    assert r2["status"] == "ok"
    assert r2["level"] == "full"
    assert r2["count"] == r["count"]
    assert r2["results"] == r["results"], "snippet 结果集口径不得因主体改变"
