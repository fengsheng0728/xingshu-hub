# -*- coding: utf-8 -*-
"""T27 / CD-060（2026-09-20）：内联 DDL 与 alembic 基线全表硬等式（差异 = 0）。

口径（用户 2026-09-20 冻结）：
- 空库 A 走 db.init_db()（内联 DDL）；空库 B 走 `alembic upgrade head`（SYNC_HUB_DB 指向
  tmp_path 临时库，不动仓库内 sync_hub.db）；
- 对两者 sqlite_master 做规范化逐项比对：表名集合、每表 DDL 规范化文本、索引集合，
  断言差异 = 0；
- 排除 sqlite_% / alembic_version / FTS5 影子表
  （memory_pool_fts_data|idx|docsize|config，由虚拟表自动管理，0001 口径）；
- lazy 表白名单：shared_docs 由 shared_workspace.py 惰性创建（见 LAZY_TABLES 注记），
  内联 DDL 刻意不建（冻结口径：不制造双份 DDL 冲突），两侧都可缺省 → 集合差在白名单内
  豁免；白名单仅含真正 lazy 的表，不得掩盖真缺口（test_lazy_whitelist_is_exact 固化）。

规范化（仅消除「同一 schema 的两种文本来源」差异，不改变语义）：
- sqlite 存 sqlite_master.sql 时会剥离 IF NOT EXISTS，但保留 `--` 注释与缩进原文；
  alembic 0001 逐字取自现库（含历史注释），内联 DDL 无注释 → 去 `--` 行注释；
- 内联 CREATE 内联全部列 vs alembic「基线 CREATE + 历次 ALTER 追加」产生的文本，
  仅缩进/换行/逗号间距不同 → 折叠所有空白、去逗号/括号前空白。
不做：引号风格归一、默认值改写、列序重排归一——列序/默认值/约束差异一律算真差异。

先红：本测试在 db.py 补齐前必须失败（内联缺 6 表 / team_members.team_id /
5 表列序漂移 → 差异 > 0）。
不跑真实 Hub、不绑端口；alembic 走子进程（沿用 test_alembic_0002_schema 配方）。
"""
import difflib
import os
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from models import CONFIG  # noqa: E402

# FTS5 影子表（memory_pool_fts 虚拟表自动管理，0001/0002 口径均不显式建）
FTS5_SHADOW = {
    "memory_pool_fts_data", "memory_pool_fts_idx",
    "memory_pool_fts_docsize", "memory_pool_fts_config",
}

# lazy 表白名单（两侧都可缺省；逐条登记实际创建者与时机——不得掩盖真缺口）
LAZY_TABLES = {
    "shared_docs": (
        "lazy 建表：shared_workspace.py:29 SHARED_DOCS_DDL，由 "
        "SharedWorkspace._init_db()（shared_workspace.py:63，start() 首次调用时）"
        "惰性创建；内联 DDL 刻意不建（CD-060 冻结口径：避免双份 DDL 冲突），"
        "db.py init_db 的 S3 taint ALTER 段对该表有「不存在则跳过」守卫。"
    ),
}


def _snapshot(db_path):
    """sqlite_master 快照：{(type, name): sql}，排除 sqlite_%/alembic_version/FTS5 影子表。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
            "ORDER BY type, name").fetchall()
    finally:
        conn.close()
    return {(t, n): s for t, n, s in rows if n not in FTS5_SHADOW}


def _norm(sql):
    """DDL 规范化：去 `--` 注释 → 折叠空白 → 去逗号/括号前空白（语义不变）。"""
    s = re.sub(r"--[^\n]*", "", sql or "")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s


def _build_inline(tmp_path, monkeypatch):
    """空库 A：只走 db.init_db()（内联 DDL），CONFIG.DB_PATH 重定向到 tmp_path。

    注意：先 monkeypatch 再 import db——db 模块级会执行一次 init_db()，
    此时 CONFIG.DB_PATH 已指向 tmp 库，不会触碰仓库内 sync_hub.db；
    若 db 已被本会话其它测试 import 过，则显式再调一次 init_db()（幂等）。
    """
    db_path = str(tmp_path / "inline.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    import db as db_mod
    assert db_mod.CONFIG is CONFIG
    db_mod.init_db()
    return db_path


def _build_alembic(tmp_path):
    """空库 B：`alembic upgrade head`，SYNC_HUB_DB 指向 tmp_path（不动仓库 sync_hub.db）。"""
    db_path = str(tmp_path / "alembic.db")
    env = dict(os.environ, SYNC_HUB_DB=db_path)
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"alembic upgrade head 失败:\n{r.stdout}\n{r.stderr}"
    return db_path


# ═══ CD-060 口径修正（2026-09-20）：**有意差异**白名单 ═══
# agents 的 api_key_hash / api_key_prev_hash **有意不内联**（渐进迁移锚点：代码按 PRAGMA 列存在性
# 自动切换明文/哈希模式）。内联补齐 → 新库直接进 hash 模式、注册回执 api_key 变空串（破坏既有幂等
# 回执行为，全量回归实测 5 例红）。故比对前剥除 alembic 侧这两列声明——这不是「未收口的漂移」，
# 是登记在案的有意差异（理由同步见 db.py 同段注释与台账 CD-060）。
INTENTIONAL_COLUMN_DIFFS = {"agents": ("api_key_hash", "api_key_prev_hash")}


def _strip_intentional(tname, ddl):
    """剥除登记在案的有意差异列。

    注意：sqlite_master.sql 里本仓的 CREATE 多为**单行**文本（alembic 0001 基线即如此），
    故不能按行剥除，需按列（逗号分隔）用正则删除该列定义。
    """
    cols = INTENTIONAL_COLUMN_DIFFS.get(tname)
    if not cols:
        return ddl
    s = ddl
    for c in cols:
        c = re.escape(c)
        s = re.sub(r",\s*" + c + r"\s+[^,)]*", "", s)   # 前置逗号形态
        s = re.sub(c + r"\s+[^,)]*\s*,\s*", "", s)      # 后置逗号形态
    return s


def _diff_report(inline, alembic):
    """逐项比对 → (差异行列表, 比对明细行列表)。"""
    diffs, detail = [], []
    keys = sorted(set(inline) | set(alembic))
    for key in keys:
        t, name = key
        a, b = inline.get(key), alembic.get(key)
        if a is None:
            if (t == "table" or t == "view") and name in LAZY_TABLES:
                detail.append(f"LAZY-OK  {t} {name} 仅 alembic 侧存在（白名单豁免）")
                continue
            diffs.append(f"缺失: 内联 DDL 无 {t} {name}（alembic 侧有）")
            detail.append(f"DIFF     {t} {name} 仅 alembic 侧存在")
            continue
        if b is None:
            if (t == "table" or t == "view") and name in LAZY_TABLES:
                detail.append(f"LAZY-OK  {t} {name} 仅内联侧存在（白名单豁免）")
                continue
            diffs.append(f"多出: 内联 DDL 有 {t} {name}（alembic 侧无）")
            detail.append(f"DIFF     {t} {name} 仅内联侧存在")
            continue
        # CD-060 口径修正：先按原始 DDL 剥除登记在案的有意差异（agents hash 两列），再规范化比较
        # （_norm 会把 DDL 压成单行，故剥除必须发生在规范化之前）
        na, nb = _norm(_strip_intentional(name, a)), _norm(_strip_intentional(name, b))
        if na != nb:
            ud = "\n".join(difflib.unified_diff(
                nb.splitlines(), na.splitlines(),
                fromfile=f"alembic:{name}", tofile=f"inline:{name}", lineterm=""))
            diffs.append(f"DDL 漂移: {t} {name}\n{ud}")
            detail.append(f"DIFF     {t} {name} 规范化 DDL 不一致")
        else:
            detail.append(f"OK       {t} {name}")
    return diffs, detail


def test_schema_hard_equality_zero_diff(tmp_path, monkeypatch, capsys):
    """核心验收：内联 DDL 建库 vs alembic upgrade head，规范化硬等式差异 = 0。"""
    inline = _snapshot(_build_inline(tmp_path, monkeypatch))
    alembic = _snapshot(_build_alembic(tmp_path))

    diffs, detail = _diff_report(inline, alembic)
    # 逐项对照输出（报告取证用；pytest -s 可见）
    print("\n===== 硬等式逐项对照（对象 × 内联 × alembic）=====")
    for line in detail:
        print(line)
    print(f"===== 合计 {len(detail)} 项，差异 {len(diffs)} 项 =====")

    assert not diffs, (
        f"内联 DDL 与 alembic 基线存在 {len(diffs)} 处差异（口径：=0 才算收口）:\n"
        + "\n\n".join(diffs))


def test_lazy_whitelist_is_exact(tmp_path, monkeypatch):
    """白名单防滥用：集合差必须恰好等于 lazy 白名单，且白名单逐条带创建者注记。"""
    inline = _snapshot(_build_inline(tmp_path, monkeypatch))
    alembic = _snapshot(_build_alembic(tmp_path))
    only_alembic = {n for (t, n) in alembic if t == "table"} - \
        {n for (t, n) in inline if t == "table"}
    only_inline = {n for (t, n) in inline if t == "table"} - \
        {n for (t, n) in alembic if t == "table"}
    assert only_alembic <= set(LAZY_TABLES), \
        f"alembic 侧多出的表超出 lazy 白名单（真缺口）: {sorted(only_alembic - set(LAZY_TABLES))}"
    assert not only_inline, \
        f"内联 DDL 存在 alembic 没有的表（未登记的漂移）: {sorted(only_inline)}"
    for name, note in LAZY_TABLES.items():
        assert "shared_workspace.py:" in note and "_init_db" in note, \
            f"lazy 白名单 {name} 缺实际创建者 file:line 注记"
        # 白名单表确实在内联侧缺省、alembic 侧存在（否则白名单失效应清理）
        assert ("table", name) not in inline, f"{name} 已内联建表，应从白名单移除"
        assert ("table", name) in alembic, f"{name} 不在 alembic 基线，白名单注记过时"


def test_init_db_idempotent_schema_stable(tmp_path, monkeypatch):
    """幂等：init_db() 连跑两次，sqlite_master 快照逐字节不变。"""
    db_path = _build_inline(tmp_path, monkeypatch)
    first = _snapshot(db_path)
    import db as db_mod
    db_mod.init_db()
    assert _snapshot(db_path) == first
