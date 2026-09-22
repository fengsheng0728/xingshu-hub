# -*- coding: utf-8 -*-
"""T28 · CD-063（MCP wiki_search tags 档 snippet 截断收口）验收测试（2026-09-20）

G-1 先红核心：tags 串 >200 字的页面 → field="tags" 命中后 snippet ≤203
    （200 + "..."），且以 "..." 结尾。改动前 snippet 为 tags 原样（>200）必红。
G-2 短 tags（≤200）→ 原样返回、不加省略号（不缩窄既有形态）。
G-3 level 键存在且取值来自 T20 按主体分级：无主体=summary / manager 特权=full。
G-4 零回归：content / title / hybrid 三档返回形态不变（键集合与 snippet 形态）。

脚手架（同 test_mcp_failclosed / test_mcp_identity_injection）：临时库
db.init_db() 建全 schema + 补 knowledge_base.embedding 增量列 + agents 角色行；
WIKI_ROOT 两处 patch 指 tmp_path；主体经 routes_gateway.set_mcp_principal 注入。
"""
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

PAGE_REL = "entities/t28-probe.md"
PAGE_REL_SHORT = "entities/t28-short.md"

# 超长 tags：>200 字，query 词 "cd063" 必命中
LONG_TAGS = "cd063," + "敏感客户标签-续约谈判," * 25  # 6 + 11*25 = 281 字
SHORT_TAGS = "cd063, 短标签, 收口"

BODY = "这是 tags 档收口探针正文。探针二字用于 content/hybrid 档命中。"

FRONTMATTER_TPL = (
    "---\n"
    "title: {title}\n"
    "created: 2026-09-20\n"
    "updated: 2026-09-20\n"
    "type: entity\n"
    "tags: {tags}\n"
    "---\n"
)

RESULT_KEYS = {"path", "title", "type", "tags", "snippet"}


@pytest.fixture(autouse=True)
def _clean_ctx():
    """每条用例前后清空主体 contextvar（防同线程跨用例串扰）。"""
    set_mcp_principal(None)
    yield
    set_mcp_principal(None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库 + 临时 wiki 根目录：一页超长 tags、一页短 tags。"""
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
    conn.commit()
    conn.close()

    wiki_root = str(tmp_path / "wiki")
    monkeypatch.setattr(wiki_engine, "WIKI_ROOT", wiki_root)
    monkeypatch.setattr(mcp_server, "WIKI_ROOT", wiki_root)
    os.makedirs(os.path.join(wiki_root, "entities"), exist_ok=True)
    for rel, title, tags in (
        (PAGE_REL, "T28探针页", LONG_TAGS),
        (PAGE_REL_SHORT, "T28短标签页", SHORT_TAGS),
    ):
        with open(os.path.join(wiki_root, rel.replace("/", os.sep)),
                  "w", encoding="utf-8", newline="") as f:
            f.write(FRONTMATTER_TPL.format(title=title, tags=tags) + BODY + "\n")
    return {"db": db_path, "wiki": wiki_root}


# ═══════════ G-1 先红核心：超长 tags → snippet ≤203 且以 "..." 结尾 ═══════════

def test_g1_long_tags_snippet_truncated(env):
    assert len(LONG_TAGS) > 200, "探针 tags 必须 >200 字"
    r = mcp_server.wiki_search("cd063", field="tags")
    assert r["status"] == "ok"
    hit = next(x for x in r["results"] if x["path"] == PAGE_REL)
    snippet = hit["snippet"]
    assert len(snippet) <= 203, \
        f"tags 档 snippet 应 ≤203（200+省略号），实际 {len(snippet)}"
    assert snippet.endswith("..."), "超长 tags 截断后应以 ... 结尾"
    assert snippet[:-3] == LONG_TAGS[:200], "截断内容应为 tags 前 200 字"


# ═══════════ G-2 短 tags（≤200）→ 原样返回、不加省略号 ═══════════

def test_g2_short_tags_verbatim(env):
    assert len(SHORT_TAGS) <= 200, "短 tags 探针必须 ≤200 字"
    r = mcp_server.wiki_search("cd063", field="tags")
    assert r["status"] == "ok"
    hit = next(x for x in r["results"] if x["path"] == PAGE_REL_SHORT)
    assert hit["snippet"] == SHORT_TAGS, "短 tags 应原样返回"
    assert not hit["snippet"].endswith("...")


# ═══════════ G-3 level 键存在且取值来自 T20 按主体分级 ═══════════

def test_g3_level_from_t20_grading(env):
    r = mcp_server.wiki_search("cd063", field="tags")
    assert r["status"] == "ok"
    assert "level" in r, "tags 档返回体缺 level 键"
    assert r["level"] == "summary", f"无主体应 fail-closed 到 summary，实际 {r['level']}"
    set_mcp_principal(Principal(subject_type="service", subject_id="mgr-1",
                                auth_mode="api_key"))
    r2 = mcp_server.wiki_search("cd063", field="tags")
    assert r2["status"] == "ok"
    assert r2["level"] == "full", f"manager 特权主体应为 full，实际 {r2['level']}"


# ═══════════ G-4 零回归：content / title / hybrid 三档形态不变 ═══════════

def test_g4_other_fields_unchanged(env):
    r_title = mcp_server.wiki_search("探针", field="title")
    assert r_title["status"] == "ok"
    assert set(r_title.keys()) == {"status", "q", "field", "count", "results", "level"}
    hit = next(x for x in r_title["results"] if x["path"] == PAGE_REL)
    assert set(hit.keys()) == RESULT_KEYS
    assert hit["snippet"] == BODY[:200], "title 档 snippet 形态应为 body[:200]"

    r_content = mcp_server.wiki_search("探针", field="content")
    assert r_content["status"] == "ok"
    assert set(r_content.keys()) == {"status", "q", "field", "count", "results", "level"}
    hit = next(x for x in r_content["results"] if x["path"] == PAGE_REL)
    assert set(hit.keys()) == RESULT_KEYS
    assert "探针" in hit["snippet"], "content 档 snippet 应含命中窗口"
    assert len(hit["snippet"]) <= len("探针") + 120 + 6, \
        "content 档 snippet 窗口形态不变（±40/80 + 至多两个省略号）"

    r_hybrid = mcp_server.wiki_search("探针", field="hybrid")
    assert r_hybrid["status"] == "ok"
    assert set(r_hybrid.keys()) == {"status", "q", "method", "count", "results", "level"}
    assert r_hybrid["count"] >= 1
