# -*- coding: utf-8 -*-
"""T15 · CD-054（MCP 出口，用户拍「甲」）：wiki_get / wiki_search fail-closed
到已发布摘要级 + 读审计 验收测试（2026-09-19）

M-1 wiki_get 摘要级（先红）：正文唯一尾部哨兵串不得出现在返回 JSON；
    正文部分 ≤200 字；level="summary"；truncated is True
M-2 frontmatter 保留：meta 仍取到 frontmatter 字段，不与截断正文混淆
M-3 html 档同样受约束：format="html" 返回不含尾部哨兵串
M-4 读审计：wiki_get / wiki_search 成功路径各落 1 行 gateway_read_log
    （requester="mcp-tool" / auth_mode="anonymous-tool" / granted_level="summary"）
M-5 结构性例外未被波及：wiki_list / wiki_graph 调用后 gateway_read_log 行数不变
M-6 wiki_search 四档（hybrid/title/tags/content）返回体均带 level="summary"

脚手架（防假绿）：临时库走 db.init_db() 建全 schema（先 monkeypatch
CONFIG.DB_PATH 再 init_db），gateway_read_log 真实存在；wiki_engine.WIKI_ROOT
是模块级常量，monkeypatch 指到 tmp_path（mcp_server 顶部 from-import 了
WIKI_ROOT，需两处一起 patch）；直调工具函数（MCP 工具就是普通函数）。
hybrid 检索走 knowledge_base.embedding 列（alembic 增量列，init_db 基表无）
→ 夹具里对临时库补 ALTER（仅测试库，不动生产 schema）。
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

SENTINEL = "SENTINEL-TAIL-XJ9Q7Z-绝密尾部哨兵"
PAGE_REL = "entities/t15-probe.md"

FRONTMATTER = (
    "---\n"
    "title: T15探针页\n"
    "created: 2026-09-19\n"
    "updated: 2026-09-19\n"
    "type: entity\n"
    "tags: t15, failclosed, 探针\n"
    "---\n"
)


def _make_body() -> str:
    """正文：前段填充 >200 字，唯一哨兵串放在 200 字之外的尾部。"""
    head = "这是正文开头。" + "星枢探针正文填充。" * 40  # 9*40=360 字
    return head + "\n\n结尾段。" + SENTINEL + "\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（db.init_db 建全表 + 补 knowledge_base.embedding 增量列）
    + 临时 wiki 根目录（埋一页含尾部哨兵串的探针页）。"""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db_path)
    import db as dbmod
    dbmod.init_db()
    conn = sqlite3.connect(db_path)
    kb_cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_base)")}
    if "embedding" not in kb_cols:
        conn.execute("ALTER TABLE knowledge_base ADD COLUMN embedding BLOB")
        conn.commit()
    conn.close()

    wiki_root = str(tmp_path / "wiki")
    monkeypatch.setattr(wiki_engine, "WIKI_ROOT", wiki_root)
    monkeypatch.setattr(mcp_server, "WIKI_ROOT", wiki_root)
    os.makedirs(os.path.join(wiki_root, "entities"), exist_ok=True)
    with open(os.path.join(wiki_root, "entities", "t15-probe.md"),
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


def _body_part(content: str) -> str:
    """取返回 content 中 frontmatter 之后的正文部分（无 frontmatter 则整体）。"""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            return content[end + 3:].strip()
    return content.strip()


# ═══════════ M-1 wiki_get 摘要级（先红：改动前哨兵串直出，必然失败） ═══════════

def test_m1_wiki_get_summary_failclosed(env):
    r = mcp_server.wiki_get(env["page"])
    assert r["status"] == "ok"
    payload = json.dumps(r, ensure_ascii=False)
    assert SENTINEL not in payload, "尾部哨兵串（200 字之外）不得出现在返回体"
    assert r["level"] == "summary"
    assert r["truncated"] is True
    body = _body_part(r["content"])
    assert len(body) <= 200, f"正文部分应 ≤200 字，实际 {len(body)}"


# ═══════════ M-2 frontmatter 保留（YAML 元数据不是正文） ═══════════

def test_m2_frontmatter_preserved(env):
    r = mcp_server.wiki_get(env["page"])
    assert r["status"] == "ok"
    assert r["meta"]["title"] == "T15探针页"
    assert r["meta"]["tags"] == "t15, failclosed, 探针"
    assert r["content"].startswith("---"), "frontmatter 应保留在 content 头部"
    body = _body_part(r["content"])
    assert "title:" not in body, "frontmatter 字段不得混入正文部分"


# ═══════════ M-3 html 档同样受约束（先截断再 md_to_html） ═══════════

def test_m3_html_also_summary(env):
    r = mcp_server.wiki_get(env["page"], format="html")
    assert r["status"] == "ok"
    assert SENTINEL not in json.dumps(r, ensure_ascii=False), \
        "html 档同样不得泄露尾部哨兵串"
    assert r["level"] == "summary"
    assert r["truncated"] is True
    assert r["meta"]["title"] == "T15探针页"


# ═══════════ M-4 读审计：wiki_get / wiki_search 各落 1 行 ═══════════

def test_m4_read_audit(env):
    before = len(_log_rows(env["db"]))

    r1 = mcp_server.wiki_get(env["page"])
    assert r1["status"] == "ok"
    rows = _log_rows(env["db"])
    assert len(rows) == before + 1, f"wiki_get 应恰好多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "mcp-tool"
    assert row["auth_mode"] == "anonymous-tool"
    assert row["kind"] == "wiki"
    assert row["query"] == ""
    assert row["target"] == env["page"]
    assert row["granted_level"] == "summary"
    assert row["item_count"] == 1

    r2 = mcp_server.wiki_search("探针", field="title")
    assert r2["status"] == "ok"
    rows = _log_rows(env["db"])
    assert len(rows) == before + 2, f"wiki_search 应再多 1 行，实际 {len(rows) - before}"
    row = rows[-1]
    assert row["requester"] == "mcp-tool"
    assert row["auth_mode"] == "anonymous-tool"
    assert row["kind"] == "wiki"
    assert row["query"] == "探针"
    assert row["target"] == ""
    assert row["granted_level"] == "summary"
    assert row["item_count"] == r2["count"]


# ═══════════ M-5 结构性例外未被波及（wiki_list / wiki_graph 不落审计） ═══════════

def test_m5_structural_exceptions_untouched(env):
    before = len(_log_rows(env["db"]))
    r1 = mcp_server.wiki_list()
    assert r1["status"] == "ok"
    assert r1["total"] == 1
    r2 = mcp_server.wiki_graph()
    assert "nodes" in r2 and "links" in r2
    assert len(_log_rows(env["db"])) == before, \
        "wiki_list / wiki_graph 无正文，不得新增读审计行"


# ═══════════ M-6 wiki_search 四档返回体均带 level="summary" ═══════════

def test_m6_search_all_fields_level(env):
    for field in ("hybrid", "title", "tags", "content"):
        r = mcp_server.wiki_search("探针", field=field)
        assert r["status"] == "ok", f"{field} 档调用失败: {r}"
        assert r["level"] == "summary", f"{field} 档缺 level=summary"
        assert r["count"] >= 1, f"{field} 档应命中探针页"
