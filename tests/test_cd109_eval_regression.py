# -*- coding: utf-8 -*-
"""CD-109(b)：阈值版本化机制 + 同 config_hash 数字回跌告警。

覆盖：
 1. v1 默认 hash 不变：config_json 不含 threshold_profile，指纹 = 改动前实测值；
 2. 非 v1 档 hash 变化：注入 threshold_profile 字段；
 3. 回跌检出：recall@5 差 0.05 > 0.02 → 事件行 + payload 键齐 + 退出码 3；
 4. 无回跌：本次优于历史最优 → 零事件、退出码 0；
 5. 首次基线：无同 hash 历史 → 不算回跌、退出码 0、零事件；
 6. 不同 config_hash 不互比（负向）；
 7. p95 相对阈值：+30% 判回跌、+10% 不判；
 8. 阈值优先级：显式 --threshold > 档位值 > DEFAULT_THRESHOLD。
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

# tools/ 无 __init__.py 且与 site-packages 的 tools 包重名 → 按文件路径显式加载
# （与 tests/test_kb_eval.py 同配方）。
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "kb_eval", os.path.join(ROOT, "tools", "kb_eval.py"))
kb_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb_eval)

# ── v1 默认路径不变量：改动前 config_json / config_hash 实测值（docs/kb-eval.md §5）──
_V1_CFG = {
    "dataset": "corpus-selfsup-v1", "provider": "hasher", "model_path": "",
    "top_k": 5, "threshold": 0.5,
    "corpus_fingerprint": "51f90923949ef2ae",
    "requester_role": "orchestrator", "layer": "knowledge",
    "query_source": "front-matter title"}
_V1_JSON = json.dumps(_V1_CFG, ensure_ascii=False, sort_keys=True)
_V1_HASH = "18193e7083c9ce4d"  # sha256(_V1_JSON) 前 16 位，与首跑落库值一致

_PAYLOAD_KEYS = {"dataset", "config_hash", "metric", "current", "best",
                 "delta", "detected_at"}


@pytest.fixture()
def eval_db(tmp_path, monkeypatch):
    """tmp 库 + init_db 建全 schema（含 evaluation_tasks / events）。"""
    db_path = str(tmp_path / "eval.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    return db_path


def _hist(db_path, config_hash=_V1_HASH, **metrics):
    row = {"dataset": "corpus-selfsup-v1", "name": "hist",
           "sample_count": 4, "config_hash": config_hash,
           "config_json": _V1_JSON}
    row.update(metrics)
    return kb_eval.insert_evaluation_task(db_path, row)


def _current(**metrics):
    row = {"dataset": "corpus-selfsup-v1", "name": "cur",
           "sample_count": 4, "config_hash": _V1_HASH,
           "config_json": _V1_JSON,
           "recall_at_5": 1.0, "refusal_rate": 0.0, "p95_ms": 10.0}
    row.update(metrics)
    return row


def _events(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT event_type, agent_id, payload FROM events"
            " WHERE event_type='eval_regression' ORDER BY event_id").fetchall()
    finally:
        conn.close()


# ═══════════ 1. v1 默认 hash 不变 ═══════════

def test_v1_default_config_json_and_hash_unchanged():
    cfg = kb_eval.build_eval_config(
        dataset="corpus-selfsup-v1", provider="hasher", model_path="",
        top_k=5, threshold=0.5, corpus_fp="51f90923949ef2ae")
    assert "threshold_profile" not in cfg, "v1 默认档禁止注入 threshold_profile"
    assert set(cfg) == set(_V1_CFG), "v1 默认档 config_json 键集必须逐字不变"
    assert json.dumps(cfg, ensure_ascii=False, sort_keys=True) == _V1_JSON
    assert kb_eval.config_fingerprint(cfg) == _V1_HASH
    # 显式传 v1 同样不注入（hash 不变量覆盖显式默认）
    cfg2 = kb_eval.build_eval_config(
        dataset="corpus-selfsup-v1", provider="hasher", model_path="",
        top_k=5, threshold=0.5, corpus_fp="51f90923949ef2ae",
        threshold_profile="v1")
    assert "threshold_profile" not in cfg2
    assert kb_eval.config_fingerprint(cfg2) == _V1_HASH


# ═══════════ 2. 非 v1 档 hash 变化 ═══════════

def test_non_v1_profile_injects_field_and_changes_hash():
    cfg_v1 = kb_eval.build_eval_config(
        dataset="corpus-selfsup-v1", provider="hasher", model_path="",
        top_k=5, threshold=0.5, corpus_fp="51f90923949ef2ae",
        threshold_profile="v1")
    cfg_x = kb_eval.build_eval_config(
        dataset="corpus-selfsup-v1", provider="hasher", model_path="",
        top_k=5, threshold=0.5, corpus_fp="51f90923949ef2ae",
        threshold_profile="vX-test")
    assert cfg_x.get("threshold_profile") == "vX-test"
    assert kb_eval.config_fingerprint(cfg_x) != kb_eval.config_fingerprint(cfg_v1)
    assert kb_eval.config_fingerprint(cfg_x) != _V1_HASH


# ═══════════ 3. 回跌检出：recall@5 0.90 → 0.85 ═══════════

def test_regression_recall_drop_event_and_exit2(eval_db, monkeypatch, capsys):
    _hist(eval_db, recall_at_5=0.90, refusal_rate=0.10, p95_ms=10.0)
    current = _current(recall_at_5=0.85, refusal_rate=0.10, p95_ms=10.0)
    monkeypatch.setattr(kb_eval, "run_evaluation", lambda **kw: dict(current))
    rc = kb_eval.main(["--db", eval_db, "--regression-check"])
    assert rc == 3, "recall@5 下降 0.05 > 0.02 必须判回跌（退出码 3）"
    evs = _events(eval_db)
    assert len(evs) == 1
    assert evs[0][1] == "__eval__"
    payload = json.loads(evs[0][2])
    assert set(payload) == _PAYLOAD_KEYS
    assert payload["metric"] == "recall_at_5"
    assert payload["dataset"] == "corpus-selfsup-v1"
    assert payload["config_hash"] == _V1_HASH
    assert payload["current"] == pytest.approx(0.85)
    assert payload["best"] == pytest.approx(0.90)
    assert payload["delta"] == pytest.approx(-0.05)
    assert payload["detected_at"]
    err = capsys.readouterr().err
    assert "EVAL REGRESSION" in err and "0.85" in err and "0.90" in err


# ═══════════ 4. 无回跌 ═══════════

def test_no_regression_when_not_worse(eval_db):
    _hist(eval_db, recall_at_5=0.90, refusal_rate=0.10, p95_ms=10.0)
    # 本次优于/等于历史最优
    rc = kb_eval.run_regression_check(
        eval_db, _current(recall_at_5=0.95, refusal_rate=0.05, p95_ms=9.0))
    assert rc == 0
    rc = kb_eval.run_regression_check(
        eval_db, _current(recall_at_5=0.90, refusal_rate=0.10, p95_ms=10.0))
    assert rc == 0
    assert _events(eval_db) == [], "无回跌必须零事件"


# ═══════════ 5. 首次基线 ═══════════

def test_first_baseline_without_history(eval_db):
    rc = kb_eval.run_regression_check(
        eval_db, _current(recall_at_5=0.10, refusal_rate=0.99, p95_ms=999.0))
    assert rc == 0, "无同口径历史 → 首次基线，不算回跌"
    assert _events(eval_db) == []


# ═══════════ 6. 不同 config_hash 不互比 ═══════════

def test_different_config_hash_not_compared(eval_db):
    _hist(eval_db, config_hash="deadbeef00000000",
          recall_at_5=0.99, refusal_rate=0.0, p95_ms=5.0)
    rc = kb_eval.run_regression_check(
        eval_db, _current(recall_at_5=0.10, refusal_rate=0.99, p95_ms=999.0))
    assert rc == 0, "hash 不同 = 口径不同，禁止互比 → 按首次基线处理"
    assert _events(eval_db) == []


# ═══════════ 7. p95 相对阈值 ═══════════

def test_p95_relative_threshold(eval_db):
    _hist(eval_db, recall_at_5=1.0, refusal_rate=0.0, p95_ms=10.0)
    # 13ms / 10ms = +30% > 20% → 判回跌
    assert kb_eval.run_regression_check(
        eval_db, _current(recall_at_5=1.0, refusal_rate=0.0, p95_ms=13.0)) == 3
    evs = _events(eval_db)
    assert len(evs) == 1
    payload = json.loads(evs[0][2])
    assert payload["metric"] == "p95_ms"
    assert payload["delta"] == pytest.approx(0.30)
    # 11ms / 10ms = +10% ≤ 20% → 不判
    assert kb_eval.run_regression_check(
        eval_db, _current(recall_at_5=1.0, refusal_rate=0.0, p95_ms=11.0)) == 0
    assert len(_events(eval_db)) == 1, "+10% 不得追加事件"


# ═══════════ 8. 阈值优先级：显式 > 档位值 > DEFAULT_THRESHOLD ═══════════

def test_threshold_resolution_priority(monkeypatch):
    monkeypatch.setattr(kb_eval, "THRESHOLD_PROFILES", {
        "v1": {"default": 0.50},
        "vloose": {"default": 0.30},
        "vempty": {},
    })
    assert kb_eval.resolve_threshold(None, "v1") == pytest.approx(0.50)
    assert kb_eval.resolve_threshold(None, "vloose") == pytest.approx(0.30)
    assert kb_eval.resolve_threshold(0.7, "vloose") == pytest.approx(0.7)
    assert kb_eval.resolve_threshold(None, "vempty") == pytest.approx(
        kb_eval.DEFAULT_THRESHOLD)
    with pytest.raises(ValueError):
        kb_eval.resolve_threshold(None, "vnope")
