# -*- coding: utf-8 -*-
"""CD-081：KB 评估框架（tools/kb_eval.py + evaluation_tasks 表）测试。

覆盖：
 1. 表 CRUD：init_db 建库后 insert_evaluation_task 落行、读回字段一致、
    created_at 由 DB 默认填充；
 2. 指标纯函数手算对照：recall@k / 拒答率 / p95 线性插值；
 3. 评估集构造：自监督口径（query=title, positive=entry_id），空标题跳过；
 4. CLI dry-run：只构造评估集打印，不检索、不落库；
 5. 端到端 smoke：tmp 库 + EphemeralClient 真跑 limit=2，指标在 [0,1] 且落库。
"""
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as db_mod  # noqa: E402
from models import CONFIG  # noqa: E402

# tools/ 无 __init__.py 且与 site-packages 的 tools 包重名（常规包优先于命名空间
# 包，`from tools import ...` 会落到错误的包）→ 按文件路径显式加载。
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "kb_eval", os.path.join(ROOT, "tools", "kb_eval.py"))
kb_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb_eval)


@pytest.fixture()
def eval_db(tmp_path, monkeypatch):
    """tmp 库 + init_db 建全 schema（含 evaluation_tasks，alembic 0012 内联侧）"""
    db_path = str(tmp_path / "eval.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


# ═══════════ 1. 表 CRUD ═══════════

def test_evaluation_tasks_insert_and_readback(eval_db):
    row_id = kb_eval.insert_evaluation_task(eval_db, {
        "dataset": "corpus-selfsup-v1", "name": "kb-eval",
        "recall_at_5": 0.5, "refusal_rate": 0.25, "p95_ms": 12.34,
        "sample_count": 4, "config_hash": "abc123", "config_json": "{}"})
    assert row_id >= 1
    conn = sqlite3.connect(eval_db)
    try:
        row = conn.execute(
            "SELECT dataset, name, recall_at_5, refusal_rate, p95_ms,"
            " sample_count, config_hash, created_at"
            " FROM evaluation_tasks WHERE id = ?", (row_id,)).fetchone()
    finally:
        conn.close()
    assert row[:7] == ("corpus-selfsup-v1", "kb-eval", 0.5, 0.25, 12.34,
                       4, "abc123")
    assert row[7], "created_at 应由 DB 默认 datetime('now') 填充"


def test_evaluation_tasks_metrics_nullable(eval_db):
    """dry-run 等只登记场景：指标列允许 NULL（表口径钉死为可空）。"""
    row_id = kb_eval.insert_evaluation_task(eval_db, {
        "dataset": "d", "name": "n", "sample_count": 0})
    conn = sqlite3.connect(eval_db)
    try:
        row = conn.execute(
            "SELECT recall_at_5, refusal_rate, p95_ms FROM evaluation_tasks"
            " WHERE id = ?", (row_id,)).fetchone()
    finally:
        conn.close()
    assert row == (None, None, None)


def test_insert_fails_closed_when_table_missing(tmp_path):
    """未迁移的老库：不静默建表，报错指引 alembic upgrade head。"""
    db_path = str(tmp_path / "old.db")
    sqlite3.connect(db_path).close()
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        kb_eval.insert_evaluation_task(db_path, {"dataset": "d", "name": "n"})


# ═══════════ 2. 指标纯函数（手算对照） ═══════════

def test_recall_at_k_hand_computed():
    results = [("a", ["a", "b", "c"]),   # 正例在 rank 1
               ("b", ["x", "b", "y"]),   # 正例在 rank 2
               ("c", ["x", "y", "z"])]   # 未命中
    assert kb_eval.recall_at_k(results, k=5) == pytest.approx(2 / 3)
    assert kb_eval.recall_at_k(results, k=1) == pytest.approx(1 / 3)
    assert kb_eval.recall_at_k(results, k=2) == pytest.approx(2 / 3)
    assert kb_eval.recall_at_k([], k=5) == 0.0


def test_refusal_rate_hand_computed():
    hit_sets = [[],               # 空命中 → 拒答
                [0.6, 0.2],       # max=0.6 ≥ 0.5 → 不拒答
                [0.3, 0.4]]       # max=0.4 < 0.5 → 拒答
    assert kb_eval.refusal_rate(hit_sets, 0.5) == pytest.approx(2 / 3)
    # 阈值下调到 0.3：第三组 max=0.4 ≥ 0.3 不再拒答，空命中仍拒答
    assert kb_eval.refusal_rate(hit_sets, 0.3) == pytest.approx(1 / 3)
    assert kb_eval.refusal_rate([], 0.5) == 0.0


def test_percentile_linear_interpolation():
    vals = [float(i) for i in range(1, 11)]  # 1..10
    # 线性插值（numpy 'linear' 同款）：rank = (10-1)*0.95 = 8.55 → v8 + 0.55*(v9-v8)
    assert kb_eval.percentile(vals, 95) == pytest.approx(1 + 8.55)
    assert kb_eval.percentile(vals, 50) == pytest.approx(5.5)
    assert kb_eval.percentile([3.0], 95) == 3.0
    assert kb_eval.percentile([], 95) == 0.0


def test_config_fingerprint_order_independent():
    a = {"provider": "hasher", "top_k": 5, "threshold": 0.5}
    b = {"threshold": 0.5, "provider": "hasher", "top_k": 5}
    assert kb_eval.config_fingerprint(a) == kb_eval.config_fingerprint(b)
    assert kb_eval.config_fingerprint(a) != kb_eval.config_fingerprint(
        {**a, "top_k": 10})


# ═══════════ 3. 评估集构造 ═══════════

def test_build_eval_set_self_supervised():
    entries = [
        {"entry_id": "kb-a", "title": "标题A", "content": "x"},
        {"entry_id": "kb-b", "title": "  ", "content": "y"},   # 空标题跳过
        {"entry_id": "kb-c", "title": "标题C", "content": "z"},
    ]
    pairs = kb_eval.build_eval_set(entries)
    assert [(p["query"], p["positive"]) for p in pairs] == [
        ("标题A", "kb-a"), ("标题C", "kb-c")]


# ═══════════ 4. CLI dry-run（不检索、不落库） ═══════════

def test_cli_dry_run(capsys, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "kb-demo-one.md").write_text(
        "---\ntitle: 演示条目一\ncategory: faq\ntags: [a]\nsource: test\n---\n\n正文一",
        encoding="utf-8")
    (corpus / "kb-demo-two.md").write_text(
        "---\ntitle: 演示条目二\ncategory: faq\ntags: [b]\nsource: test\n---\n\n正文二",
        encoding="utf-8")
    rc = kb_eval.main(["--dry-run", "--corpus", str(corpus),
                       "--db", str(tmp_path / "untouched.db")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["entries"] == 2 and out["pairs"] == 2
    assert out["sample"][0]["query"] == "演示条目一"
    # dry-run 不得建库/落库
    assert not (tmp_path / "untouched.db").exists()


# ═══════════ 5. 端到端 smoke（真实写侧 + 真实 DisclosureEngine） ═══════════

def test_run_evaluation_smoke(eval_db, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i, (slug, title) in enumerate(
            [("kb-smoke-alpha", "退换货政策"), ("kb-smoke-beta", "橱柜色差处理流程")]):
        (corpus / f"{slug}.md").write_text(
            f"---\ntitle: {title}\ncategory: process\ntags: [t{i}]\n"
            f"source: test\n---\n\n# {title}\n\n{title}的正文内容，第{i}篇。",
            encoding="utf-8")
    row = kb_eval.run_evaluation(
        db_path=eval_db, corpus_dir=str(corpus), name="smoke", limit=0)
    assert row["sample_count"] == 2
    for m in ("recall_at_5", "refusal_rate"):
        assert 0.0 <= row[m] <= 1.0
    assert row["p95_ms"] >= 0.0
    # 标题查询的正例应召回（标题同时是正文首行，hasher 档词袋必然重叠）
    assert row["recall_at_5"] == 1.0
    # 落库可读回
    conn = sqlite3.connect(eval_db)
    try:
        got = conn.execute(
            "SELECT name, sample_count, config_hash FROM evaluation_tasks"
            " WHERE id = ?", (row["id"],)).fetchone()
    finally:
        conn.close()
    assert got[0] == "smoke" and got[1] == 2 and len(got[2]) == 16
