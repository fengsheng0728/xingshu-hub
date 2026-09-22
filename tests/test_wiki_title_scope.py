# -*- coding: utf-8 -*-
"""T29 / CD-067（2026-09-20）：wiki 派生页 title/tags 按源记忆级别过滤。

口径（用户 2026-09-20 选 C）：
- 派生页（frontmatter 含 `memory_id`）的 title/tags 按 `min(页面级别, 主体上限)` 过滤 ——
  级别不足 `full` 时 title 脱敏为 `[记忆] memory-<memory_id 前 8 位>`、tags 置空；
- 手写页 / 知识派生页（无 memory_id）不受影响；
- 只改读出口返回体，**磁盘 frontmatter 不动**；
- 读不到 frontmatter 时按 fail-closed（路径命中 `concepts/memory-*.md` 即脱敏）。

先红：本文件在 wiki_engine/routes_wiki 改动前必须失败（派生页 title 原样含人名）。
不 spawn Hub、不绑端口。
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import wiki_engine  # noqa: E402
from models import CONFIG  # noqa: E402

DERIVED_STEM = "memory-conv-张三-20260725"
DERIVED_REL = f"concepts/{DERIVED_STEM}.md"
MEM_ID = "39b623c99423a11fa86e"
RAW_TITLE = "[记忆] conv-张三-20260725"
RAW_TAGS = "[客服对话, 张三, memory, fact]"


def _mini_wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    (root / "concepts").mkdir(parents=True)
    (root / "entities").mkdir(parents=True)
    (root / "concepts" / f"{DERIVED_STEM}.md").write_text(
        "---\n"
        f"title: {RAW_TITLE}\n"
        "type: concept\n"
        f"tags: {RAW_TAGS}\n"
        f"memory_id: {MEM_ID}\n"
        "---\n\n派生页正文（客户张三的对话事实）\n", encoding="utf-8")
    (root / "entities" / "产品a.md").write_text(
        "---\ntitle: 产品A\ntype: entity\ntags: [产品]\n---\n\n手写页正文\n", encoding="utf-8")
    monkeypatch.setattr(wiki_engine, "WIKI_ROOT", str(root))
    return root


def _mini_db(tmp_path, monkeypatch, level="summary"):
    db = tmp_path / "t.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE memory_pool (memory_id TEXT PRIMARY KEY, disclosure_level TEXT)")
    con.execute("INSERT INTO memory_pool VALUES (?, ?)", (MEM_ID, level))
    con.commit()
    con.close()
    monkeypatch.setattr(CONFIG, "DB_PATH", str(db))
    return db


def _by_path(pages, rel):
    for p in pages:
        if p.get("path") == rel:
            return p
    return None


# ═══════════ W-1 非特权：派生页 title/tags 脱敏（先红核心） ═══════════

def test_w1_derived_title_sanitized_for_nonpriv(tmp_path, monkeypatch):
    _mini_wiki(tmp_path, monkeypatch)
    _mini_db(tmp_path, monkeypatch, level="summary")
    pages = wiki_engine.list_pages_scoped(privileged=False)
    d = _by_path(pages, DERIVED_REL)
    assert d is not None, "派生页应出现在列表中"
    assert d["title"] == f"[记忆] memory-{MEM_ID[:8]}", f"非特权下 title 应脱敏，实际: {d['title']}"
    assert RAW_TITLE not in str(d), "原 title（含人名）不得出现在返回体"
    assert d["tags"] == "", f"非特权下 tags 应置空，实际: {d['tags']}"
    assert d.get("level") == "summary"


# ═══════════ W-2 特权：原样 ═══════════

def test_w2_derived_title_intact_for_privileged(tmp_path, monkeypatch):
    """特权主体 + 源记忆级别 full → 原样（min(页面自身, 源) = full）。

    注意：源记忆是 summary 时**特权也脱敏**（min 只降不升）——该情形由 W-5 覆盖，
    本用例专测「级别允许时特权能拿到原始 title/tags」。
    """
    _mini_wiki(tmp_path, monkeypatch)
    _mini_db(tmp_path, monkeypatch, level="full")
    pages = wiki_engine.list_pages_scoped(privileged=True)
    d = _by_path(pages, DERIVED_REL)
    assert d["title"] == RAW_TITLE, f"特权下 title 应原样，实际: {d['title']}"
    assert d["tags"] == RAW_TAGS


# ═══════════ W-3 手写页：两主体下均不变 ═══════════

def test_w3_handwritten_page_unaffected(tmp_path, monkeypatch):
    _mini_wiki(tmp_path, monkeypatch)
    _mini_db(tmp_path, monkeypatch, level="summary")
    for priv in (False, True):
        pages = wiki_engine.list_pages_scoped(privileged=priv)
        h = _by_path(pages, "entities/产品a.md")
        assert h is not None
        assert h["title"] == "产品A", f"手写页 title 不应被改（privileged={priv}）"
        assert h["tags"] == "[产品]"


# ═══════════ W-4 graph label 同口径 ═══════════

def test_w4_graph_label_scoped(tmp_path, monkeypatch):
    """graph label 同口径：非特权脱敏；特权在源级别允许（full）时原样。"""
    _mini_wiki(tmp_path, monkeypatch)
    _mini_db(tmp_path, monkeypatch, level="full")
    nonpriv = wiki_engine.get_graph(privileged=False)
    labels = [n["label"] for n in nonpriv["nodes"]]
    assert f"[记忆] memory-{MEM_ID[:8]}" in labels, f"非特权 graph label 应脱敏，实际: {labels}"
    assert RAW_TITLE not in labels
    priv = wiki_engine.get_graph(privileged=True)
    plabels = [n["label"] for n in priv["nodes"]]
    assert RAW_TITLE in plabels, f"特权 graph label 应原样，实际: {plabels}"


# ═══════════ W-5 源记忆级别不足：特权也脱敏（min 语义） ═══════════

def test_w5_source_level_none_caps_even_privileged(tmp_path, monkeypatch):
    _mini_wiki(tmp_path, monkeypatch)
    _mini_db(tmp_path, monkeypatch, level="none")
    pages = wiki_engine.list_pages_scoped(privileged=True)
    d = _by_path(pages, DERIVED_REL)
    assert d["title"] == f"[记忆] memory-{MEM_ID[:8]}", "源级别 NONE 时即使特权也应脱敏（min 只降不升）"
    assert d["tags"] == ""


# ═══════════ W-6 磁盘 frontmatter 未被改动 ═══════════

def test_w6_disk_frontmatter_untouched(tmp_path, monkeypatch):
    root = _mini_wiki(tmp_path, monkeypatch)
    _mini_db(tmp_path, monkeypatch, level="summary")
    before = (root / DERIVED_REL).read_text(encoding="utf-8")
    wiki_engine.list_pages_scoped(privileged=False)
    wiki_engine.get_graph(privileged=False)
    after = (root / DERIVED_REL).read_text(encoding="utf-8")
    assert before == after, "脱敏只作用于返回体，磁盘 frontmatter 不得被改写"
    assert RAW_TITLE in after


# ═══════════ W-7 读不到 frontmatter：fail-closed 脱敏 ═══════════

def test_w7_unreadable_frontmatter_fail_closed(tmp_path, monkeypatch):
    """造一个以 .md 结尾的**目录**（list_pages 会收它，open() 必失败）
    → 路径命中派生页命名约定时按 fail-closed 脱敏（特权也不例外：读不到即无法判定级别）。"""
    root = _mini_wiki(tmp_path, monkeypatch)
    _mini_db(tmp_path, monkeypatch, level="summary")
    ghost_rel = "concepts/memory-ghost.md"
    (root / "concepts" / "memory-ghost.md").mkdir()  # 同名目录 → open() 抛 IsADirectoryError

    import hashlib
    expected = "[记忆] memory-" + hashlib.sha1(ghost_rel.encode("utf-8")).hexdigest()[:8]

    for priv in (False, True):
        pages = wiki_engine.list_pages_scoped(privileged=priv)
        d = _by_path(pages, ghost_rel)
        assert d is not None, "目录条目应出现在 list_pages（.md 后缀）"
        assert d["title"] == expected, \
            f"读不到 frontmatter 应 fail-closed 脱敏（privileged={priv}），实际: {d['title']}"
        assert d["tags"] == "" and d.get("level") == "summary"

    # 手写页不受该分支影响
    assert _by_path(wiki_engine.list_pages_scoped(False), "entities/产品a.md")["title"] == "产品A"
