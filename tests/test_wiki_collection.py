# -*- coding: utf-8 -*-
"""T23 · CD-054 wiki 组收编 验收测试（2026-09-20）

冻结口径（用户 2026-09-20）：
1. 记忆派生页跟源记忆链披露级别，min 语义、只降不升；下调走 reclassify 跟随、
   上调不自动跟随 —— 本任务选路线甲：级别不落盘，读出口实时按
   memory_pool.disclosure_level 计算，天然跟随下调、无同步窗。
2. /wiki/export 关门：普通 agent 禁入（403 + denied 审计行），
   hub_token / manager 以上放行且返回体不变。

覆盖（编号对齐任务书 §3.7 / T4）：
W-1  非特权 worker 调 /wiki/export → 403 + gateway_read_log denied 行（先红）
W-1b 特权 hub_token 调 /wiki/export → 放行且返回体不变（对照）
W-2  源记忆 NONE 级（PII locked）派生页 → 非特权读 /wiki/page 只得 summary（先红）
W-2b 派生页查不到源记忆 → 特权读同样 fail-closed 摘要级 + logger.warning
W-3  源记忆级别下调 full→summary → 派生页读出口实时跟随下调（先红，路线甲）
W-4  特权读 full 级源的派生页 → 全文 + level=full（对照）
W-5  手写页 / 知识派生页「已发布」语义回归：特权 full、非特权 summary
W-6  /wiki/search 非特权命中 → snippet 剥离 + level/truncated 键，返回体零删键

配方（对齐 test_knowledge_read_collection / test_403_policy_matrix 既有惯例）：
  - tmp_path 独立 sqlite 库：monkeypatch CONFIG.DB_PATH + db.init_db() 全 schema
  - 临时 wiki 根：monkeypatch wiki_engine.WIKI_ROOT（routes_wiki 函数内
    `from wiki_engine import WIKI_ROOT` 是调用期取值，monkeypatch 生效）
  - 直调 handler 协程、显式传 current_agent / principal（不起真实 Hub、不绑端口）
  - 门语义需 NO_AUTH=0：monkeypatch routes_common.NO_AUTH=False
    （conftest 全局 NO_AUTH=1 只绕过 Depends 认证，门须显式关才测得到）
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod  # noqa: E402
import routes_common  # noqa: E402
import routes_wiki  # noqa: E402
import wiki_engine  # noqa: E402
from models import CONFIG  # noqa: E402

# 特权主体：auth_mode == "hub_token" 时 principal_is_privileged 短路 True（不触库）
PRIV_PRINCIPAL = SimpleNamespace(auth_mode="hub_token", subject_id="op-admin",
                                 scope=None)
# 非特权 worker：api_key 主体，按 agents 表 role 判定（alice=worker）
WORKER_PRINCIPAL = SimpleNamespace(auth_mode="api_key", subject_id="alice",
                                   scope=None)

# 唯一哨兵：落在正文 200 字之后，summary 剥离后绝不可见
SECRET = "T23机密哨兵-绝不可出摘要-9f4c2bX"
FILLER = "派生页正文填充段。" * 40  # 8 字 × 40 = 320 字符 > 200
LONG_BODY = f"# 密钥轮换记录\n\n{FILLER}{SECRET}\n"

PAGE_KEYS = {"status", "path", "meta", "content", "format"}       # 改动前既有键
SEARCH_HIT_KEYS = {"path", "title", "type", "tags", "snippet"}    # 改动前既有键


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库（全 schema）+ 临时 wiki 根 + 关闭 NO_AUTH（门语义生效）"""
    db_path = str(tmp_path / "wiki_coll.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    wiki_root = str(tmp_path / "wiki")
    for sub in ("entities", "concepts", "comparisons", "queries"):
        os.makedirs(os.path.join(wiki_root, sub), exist_ok=True)
    monkeypatch.setattr(wiki_engine, "WIKI_ROOT", wiki_root)
    monkeypatch.setattr(routes_common, "NO_AUTH", False)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO agents (agent_id, role) VALUES ('alice', 'worker')")
    conn.commit()
    conn.close()
    return SimpleNamespace(db_path=db_path, wiki_root=wiki_root)


def _insert_memory(db_path, memory_id, owner, key, content, level):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memory_pool (memory_id, owner_agent_id, memory_key, content,"
        " summary, embedding, importance, tags, kind, confidence, source_type,"
        " disclosure_level, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, '', NULL, 1.0, '[]', 'fact', 1.0, 'user', ?,"
        " datetime('now'), datetime('now'))",
        (memory_id, owner, key, content, level),
    )
    conn.commit()
    conn.close()


def _set_memory_level(db_path, memory_id, level):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memory_pool SET disclosure_level = ? WHERE memory_id = ?",
                 (level, memory_id))
    conn.commit()
    conn.close()


def _write_page(wiki_root, rel_path, fm_lines, body):
    abs_path = os.path.join(wiki_root, rel_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write("\n".join(["---"] + fm_lines + ["---"]) + "\n\n" + body)
    return rel_path


def _derived_page(wiki_root, memory_id, slug, body=LONG_BODY):
    """记忆派生页：frontmatter 含 memory_id（与 wiki_sync._build_frontmatter 同构）"""
    return _write_page(
        wiki_root, f"concepts/memory-{slug}.md",
        [f"title: [记忆] {slug}", "created: 2026-09-20", "updated: 2026-09-20",
         "type: concept", "tags: [memory, fact]", "kind: fact", "owner: alice",
         f"memory_id: {memory_id}"],
        body)


def _readlog_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT requester, kind, query, target, granted_level, item_count,"
        " stripped_chunks FROM gateway_read_log"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _capture(coro):
    try:
        return await coro
    except HTTPException as e:
        return e


# ═══ W-1 /wiki/export 关门：非特权 worker → 403 + denied 审计行（先红） ═══

def test_w1_export_worker_403_denied_log(env):
    _write_page(env.wiki_root, "concepts/any.md",
                ["title: 任意页", "type: concept"], f"正文含哨兵 {SECRET}")
    r = asyncio.run(_capture(routes_wiki.api_wiki_export(
        current_agent="alice", principal=WORKER_PRINCIPAL)))
    assert isinstance(r, HTTPException), \
        f"非特权 worker 必须 403，实际拿到全量导出: {str(r)[:120]}"
    assert r.status_code == 403, f"必须 403，实际 {r.status_code}"
    rows = _readlog_rows(env.db_path)
    assert any(row["granted_level"] == "denied" and row["kind"] == "wiki"
               and row["target"] == "export" and row["requester"] == "alice"
               and row["item_count"] == 0
               for row in rows), f"缺 wiki/export denied 读审计行: {rows}"


# ═══ W-1b 特权对照：hub_token 放行且返回体不变（全量正文原样） ═══

def test_w1b_export_privileged_passthrough(env):
    _write_page(env.wiki_root, "concepts/any.md",
                ["title: 任意页", "type: concept"], f"正文含哨兵 {SECRET}")
    res = asyncio.run(routes_wiki.api_wiki_export(
        current_agent="op-admin", principal=PRIV_PRINCIPAL))
    assert res["status"] == "ok" and res["count"] == 1
    assert SECRET in res["pages"]["concepts/any.md"], \
        "特权放行返回体必须不变（全量正文原样）"
    assert set(res.keys()) == {"status", "count", "pages"}, \
        f"export 返回体键集合不得漂移: {sorted(res.keys())}"


# ═══ W-2 源记忆 NONE 级（PII locked）派生页 → 非特权只得 summary（先红） ═══

def test_w2_locked_source_page_nonpriv_summary(env):
    _insert_memory(env.db_path, "m-w2", "alice", "密钥轮换",
                   f"含 PII 的记忆正文 {SECRET}", "none")  # locked → none
    rel = _derived_page(env.wiki_root, "m-w2", "w2-key")
    res = asyncio.run(routes_wiki.api_wiki_page(
        page_path=rel, format="md", current_agent="alice",
        principal=WORKER_PRINCIPAL))
    assert PAGE_KEYS <= set(res.keys()), \
        f"返回体只加键不删键，缺键: {PAGE_KEYS - set(res.keys())}"
    assert res.get("level") == "summary", \
        f"非特权读 NONE 级源派生页必须 summary 级: {res.get('level')}"
    assert res.get("truncated") is True, "summary 必须带 truncated 标记"
    assert SECRET not in res["content"], \
        "哨兵在 200 字之后，summary 剥离后绝不可见"
    assert len(res["content"]) <= 203, \
        f"summary 为正文前 200 字（+省略号），实际 {len(res['content'])}"


# ═══ W-2b 派生页查不到源记忆 → 特权也 fail-closed 摘要级 + 告警 ═══

def test_w2b_orphan_derived_page_fail_closed(env, caplog):
    rel = _derived_page(env.wiki_root, "m-ghost-不存在", "w2b-ghost")
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(routes_wiki.api_wiki_page(
            page_path=rel, format="md", current_agent="op-admin",
            principal=PRIV_PRINCIPAL))
    assert res.get("level") == "summary", \
        f"查不到源记忆必须 fail-closed 摘要级（不得回退全文）: {res.get('level')}"
    assert SECRET not in res["content"]
    assert any(rel in r.getMessage() and r.levelno >= logging.WARNING
               for r in caplog.records), \
        "fail-closed 降级必须 logger.warning 且含 page_path（不许静默）"


# ═══ W-3 源记忆级别下调 → 派生页读出口跟随下调（先红；路线甲实时计算） ═══

def test_w3_source_downgrade_follows(env):
    _insert_memory(env.db_path, "m-w3", "alice", "降级跟随", "正文", "full")
    rel = _derived_page(env.wiki_root, "m-w3", "w3-key")
    before = asyncio.run(routes_wiki.api_wiki_page(
        page_path=rel, format="md", current_agent="op-admin",
        principal=PRIV_PRINCIPAL))
    assert before.get("level") == "full" and SECRET in before["content"], \
        f"下调前特权应拿全文: level={before.get('level')}"
    # 源级别下调（reclassify 语义）——不重新同步 wiki，路线甲实时跟随
    _set_memory_level(env.db_path, "m-w3", "summary")
    after = asyncio.run(routes_wiki.api_wiki_page(
        page_path=rel, format="md", current_agent="op-admin",
        principal=PRIV_PRINCIPAL))
    assert after.get("level") == "summary", \
        f"源下调后派生页读出口必须跟随下调（min 语义只降不升）: {after.get('level')}"
    assert after.get("truncated") is True
    assert SECRET not in after["content"], "跟随下调后不得再出全文"
    assert PAGE_KEYS <= set(after.keys())


# ═══ W-4 特权对照：full 级源派生页 → 全文 + level=full ═══

def test_w4_privileged_full_level_source_gets_full(env):
    _insert_memory(env.db_path, "m-w4", "alice", "全文对照", "正文", "full")
    rel = _derived_page(env.wiki_root, "m-w4", "w4-key")
    res = asyncio.run(routes_wiki.api_wiki_page(
        page_path=rel, format="md", current_agent="op-admin",
        principal=PRIV_PRINCIPAL))
    assert res.get("level") == "full", f"特权 + full 级源必须放行全文: {res.get('level')}"
    assert res.get("truncated") is False
    assert SECRET in res["content"], "full 级必须含完整正文"
    assert PAGE_KEYS <= set(res.keys())


# ═══ W-5 手写页 / 知识派生页「已发布」语义回归 ═══

def test_w5_handwritten_and_kb_pages_published_semantics(env):
    hand = _write_page(env.wiki_root, "concepts/hand-written.md",
                       ["title: 手写页", "type: concept", "tags: []"], LONG_BODY)
    kb = _write_page(env.wiki_root, "entities/kb-derived.md",
                     ["title: 知识派生页", "type: entity", "entry_id: kb-1"],
                     LONG_BODY)
    for rel in (hand, kb):
        priv = asyncio.run(routes_wiki.api_wiki_page(
            page_path=rel, format="md", current_agent="op-admin",
            principal=PRIV_PRINCIPAL))
        assert priv.get("level") == "full" and SECRET in priv["content"], \
            f"已发布页特权必须 full: {rel} level={priv.get('level')}"
        non = asyncio.run(routes_wiki.api_wiki_page(
            page_path=rel, format="md", current_agent="alice",
            principal=WORKER_PRINCIPAL))
        assert non.get("level") == "summary" and SECRET not in non["content"], \
            f"已发布页非特权按 CD-052 读出口同口径降 summary: {rel}"


# ═══ W-6 /wiki/search 非特权命中剥离 + 返回体零删键 ═══

def test_w6_search_nonpriv_snippet_stripped(env):
    _insert_memory(env.db_path, "m-w6", "alice", "检索剥离", "正文", "full")
    rel = _derived_page(env.wiki_root, "m-w6", "w6-key")
    res = asyncio.run(routes_wiki.api_wiki_search(
        q=SECRET, field="content", current_agent="alice",
        principal=WORKER_PRINCIPAL))
    hits = [r for r in res["results"] if r["path"] == rel]
    assert hits, f"搜索应命中派生页: {res}"
    hit = hits[0]
    assert SEARCH_HIT_KEYS <= set(hit.keys()), \
        f"搜索命中只加键不删键，缺键: {SEARCH_HIT_KEYS - set(hit.keys())}"
    assert hit.get("level") == "summary" and hit.get("truncated") is True, \
        f"非特权命中必须标 summary+truncated: {hit}"
    assert SECRET not in hit["snippet"], "非特权 snippet 不得含哨兵"
    # 特权对照：full 级源命中 snippet 原样（含哨兵上下文）
    res_p = asyncio.run(routes_wiki.api_wiki_search(
        q=SECRET, field="content", current_agent="op-admin",
        principal=PRIV_PRINCIPAL))
    hit_p = [r for r in res_p["results"] if r["path"] == rel][0]
    assert hit_p.get("level") == "full" and hit_p.get("truncated") is False
    assert SECRET in hit_p["snippet"], "特权 + full 级源 snippet 必须原样"


# ═══ W-7 派生时刻级别快照落盘 + 上调不自动跟随（min 只降不升） ═══

def test_w7_sync_snapshots_level_and_upgrade_not_followed(env, monkeypatch):
    """wiki_sync 派生页 frontmatter 落 disclosure_level 快照；源上调后
    读出口被快照压住（min 只降不升，上调不自动跟随）。"""
    import wiki_sync
    monkeypatch.setattr(wiki_sync, "DB_PATH", env.db_path)
    monkeypatch.setattr(wiki_sync, "WIKI_ROOT", env.wiki_root)
    _insert_memory(env.db_path, "m-w7", "alice", "快照锚点", "记忆正文", "summary")
    stats = wiki_sync.sync()
    assert stats["created"] >= 1, f"同步应产出派生页: {stats}"
    pages = [p for p in os.listdir(os.path.join(env.wiki_root, "concepts"))
             if p.startswith("memory-")]
    assert pages, "派生页未生成"
    rel = f"concepts/{pages[0]}"
    with open(os.path.join(env.wiki_root, "concepts", pages[0]),
              encoding="utf-8") as f:
        text = f.read()
    assert "disclosure_level: summary" in text, \
        f"派生时刻必须快照源级别进 frontmatter: {text[:300]}"
    # 源上调 summary → full：读出口仍被页面自身快照压住（不自动跟随）
    _set_memory_level(env.db_path, "m-w7", "full")
    res = asyncio.run(routes_wiki.api_wiki_page(
        page_path=rel, format="md", current_agent="op-admin",
        principal=PRIV_PRINCIPAL))
    assert res.get("level") == "summary", \
        f"上调不自动跟随（min 只降不升）: {res.get('level')}"
