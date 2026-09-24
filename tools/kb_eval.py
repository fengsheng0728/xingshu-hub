# -*- coding: utf-8 -*-
"""tools/kb_eval.py — KB 检索评估 CLI（CD-081 最小骨架）

指标口径（钉死，完整版见 docs/kb-eval.md）：
- recall@5    ：每条查询的正例 entry_id 是否出现在检索结果前 5 条（去重后按
                返回序），命中率取全体查询的算术平均；
- 拒答率      ：检索结果为空，或全部命中的 similarity < --threshold（默认 0.50）
                的查询占比。similarity = 1 - cosine distance（disclosure 同款口径）；
- p95 延迟    ：单条查询 semantic_search 端到端墙钟毫秒（含 embedding 与回查），
                线性插值分位数（与 numpy 默认 'linear' 一致）。

评估集口径（自监督 v1，corpus 无人工问答标注）：
- 逐篇取 front-matter 的 title 作查询，该篇 entry_id 为正例；
- 检索限定 layer="knowledge"，请求方为编排员角色（orchestrator，规则链 6
  全局可见），scope=None —— 评估的是检索召回，不是权限边界。

用法：
  python tools/kb_eval.py --db eval/kb_eval.db            # 真跑并落库
  python tools/kb_eval.py --dry-run                       # 只构造评估集，不检索不落库
  python tools/kb_eval.py --db x.db --provider sentence --model-path <dir>
  python tools/kb_eval.py --db x.db --threshold-profile v1
  python tools/kb_eval.py --db x.db --regression-check    # 同 hash 回跌巡检（回跌退出码 3）

落库：结果 INSERT 进 --db 指向库的 evaluation_tasks 表（alembic 0012 / db.py
内联 DDL 双侧同步；表缺失时先跑 `alembic upgrade head` 或经 db.init_db() 建库）。
本工具用临时 EphemeralClient chroma 集合 + 真实 KnowledgeMixin 写侧灌库 +
真实 DisclosureEngine.semantic_search 读侧检索，不起 HTTP 服务、不碰路由层。
--db 同时是工作库（灌入 corpus 条目）与结果库；指向仓库根 ./sync_hub.db 以外的
路径时自动建全量 schema（db.init_db 幂等）。
"""
import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 钉死的默认口径（改动 = 新口径版本，须同步 docs/kb-eval.md）──
DEFAULT_DATASET = "corpus-selfsup-v1"
DEFAULT_TOP_K = 5
DEFAULT_THRESHOLD = 0.50
EVAL_REQUESTER = "kb-eval"  # 编排员角色请求方（orchestrator，规则 6 全局可见）

# 口径档位：v1 = 既有钉死口径（默认，行为逐字不变）；新档在标注集到位后追加。
THRESHOLD_PROFILES = {
    "v1": {"default": 0.50},   # 与 DEFAULT_THRESHOLD 同值——v1 是默认档
}

# 回跌判据（阈值定死，详见 docs/kb-eval.md §7）
REGRESSION_RECALL_DROP = 0.02     # recall@5 下降 > 0.02（绝对差）
REGRESSION_P95_RISE = 0.20        # p95_ms 上升 > 20%（相对历史最优）
REGRESSION_REFUSAL_RISE = 0.05    # refusal_rate 上升 > 0.05（绝对差）
EXIT_OK = 0
EXIT_REGRESSION = 3               # 3 = 回跌（0 = 正常）；避开 argparse 用法错误的 2


# ═══════════ 评估集构造（纯函数，可单测） ═══════════

def build_eval_set(entries):
    """自监督口径：每篇 (query=title, positive=entry_id)。跳过空标题条目。"""
    pairs = []
    for e in entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        pairs.append({"query": title, "positive": e["entry_id"],
                      "source_file": e.get("source_file", "")})
    return pairs


# ═══════════ 指标计算（纯函数，可单测） ═══════════

def recall_at_k(results, k=5):
    """results: [(positive_id, [hit_entry_id, ...]), ...] → 正例落在前 k 的比例。"""
    if not results:
        return 0.0
    hits = sum(1 for pos, ranked in results if pos in ranked[:k])
    return hits / len(results)


def refusal_rate(hit_sets, threshold):
    """hit_sets: [[similarity, ...], ...] → 空命中或全部低于阈值的比例。"""
    if not hit_sets:
        return 0.0
    refused = sum(
        1 for sims in hit_sets
        if not sims or max(sims) < threshold)
    return refused / len(hit_sets)


def percentile(values, p):
    """线性插值分位数（与 numpy 默认 method='linear' 一致）。空列表 → 0.0。"""
    vals = sorted(values)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return float(vals[0])
    rank = (len(vals) - 1) * p / 100.0
    lo = int(rank)
    hi = min(lo + 1, len(vals) - 1)
    frac = rank - lo
    return float(vals[lo] + (vals[hi] - vals[lo]) * frac)


def config_fingerprint(cfg: dict) -> str:
    """评估配置 → 稳定 hash（sha256 前 16 位）；同 hash = 同口径可比。"""
    blob = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_eval_config(dataset, provider, model_path, top_k, threshold,
                      corpus_fp, threshold_profile="v1"):
    """评估配置 dict → config_json / config_hash 的唯一来源。

    **不变量**：threshold_profile == "v1"（含默认与显式传 v1）时**不注入**
    threshold_profile 字段——v1 默认路径 config_json 与历史逐字一致，
    保证 config_hash 可比性。非 v1 档注入字段（hash 变化 = 独立口径）。
    """
    cfg = {"dataset": dataset, "provider": provider, "model_path": model_path,
           "top_k": top_k, "threshold": threshold,
           "corpus_fingerprint": corpus_fp,
           "requester_role": "orchestrator", "layer": "knowledge",
           "query_source": "front-matter title"}
    if threshold_profile != "v1":
        cfg["threshold_profile"] = threshold_profile
    return cfg


def resolve_threshold(cli_threshold, profile_name="v1"):
    """阈值优先级：显式 --threshold > 档位值 > 档位 default（DEFAULT_THRESHOLD）。

    cli_threshold=None 表示 CLI 未显式传参。未知档位 → ValueError。
    """
    if profile_name not in THRESHOLD_PROFILES:
        raise ValueError(
            f"未知阈值档位: {profile_name!r}（可选: {sorted(THRESHOLD_PROFILES)}）")
    if cli_threshold is not None:
        return float(cli_threshold)                    # 显式 --threshold 最高
    profile = THRESHOLD_PROFILES[profile_name]
    if "default" in profile:
        return float(profile["default"])               # 档位值
    return float(DEFAULT_THRESHOLD)                    # 档位 default


def corpus_fingerprint(entries) -> str:
    """语料指纹：逐篇 entry_id+内容 sha256，合并取 sha256 前 16 位。"""
    h = hashlib.sha256()
    for e in entries:
        h.update(e["entry_id"].encode("utf-8"))
        h.update(b"\x00")
        h.update((e.get("content") or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# ═══════════ 落库 ═══════════

def insert_evaluation_task(db_path, row: dict) -> int:
    """INSERT 一行 evaluation_tasks，返回行 id。表缺失 → RuntimeError 指引迁移。"""
    conn = sqlite3.connect(db_path)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='evaluation_tasks'").fetchone()
        if not exists:
            raise RuntimeError(
                "evaluation_tasks 表不存在：请先 `alembic upgrade head`"
                " 或经 db.init_db() 建库（alembic 0012）")
        cur = conn.execute(
            """INSERT INTO evaluation_tasks
               (dataset, name, recall_at_5, refusal_rate, p95_ms,
                sample_count, config_hash, config_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["dataset"], row["name"], row.get("recall_at_5"),
             row.get("refusal_rate"), row.get("p95_ms"),
             int(row.get("sample_count") or 0),
             row.get("config_hash", ""), row.get("config_json", "")))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ═══════════ 回跌巡检（同 dataset + 同 config_hash 才可比） ═══════════

def collect_history_best(db_path, dataset, config_hash, before_id=None):
    """历史最优：recall_at_5 最大 / p95_ms 最小 / refusal_rate 最小（NULL 跳过）。

    before_id 非 None 时只取 id < before_id（本次之前）；无任何可用历史 → None。
    """
    sql = ("SELECT recall_at_5, refusal_rate, p95_ms FROM evaluation_tasks"
           " WHERE dataset = ? AND config_hash = ?")
    params = [dataset, config_hash]
    if before_id is not None:
        sql += " AND id < ?"
        params.append(before_id)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    recalls = [r[0] for r in rows if r[0] is not None]
    refusals = [r[1] for r in rows if r[1] is not None]
    p95s = [r[2] for r in rows if r[2] is not None]
    best = {}
    if recalls:
        best["recall_at_5"] = max(recalls)
    if refusals:
        best["refusal_rate"] = min(refusals)
    if p95s:
        best["p95_ms"] = min(p95s)
    return best or None


def detect_regressions(current, best):
    """与历史最优比对；任一判据命中 → 返回回跌清单（delta 与判据同口径）。

    delta 口径：recall_at_5 / refusal_rate 为绝对差（current − best）；
    p95_ms 为相对差（(current − best) / best）。
    """
    found = []
    cur, bst = current.get("recall_at_5"), best.get("recall_at_5")
    if cur is not None and bst is not None and (bst - cur) > REGRESSION_RECALL_DROP:
        found.append({"metric": "recall_at_5", "current": cur, "best": bst,
                      "delta": round(cur - bst, 6)})
    cur, bst = current.get("p95_ms"), best.get("p95_ms")
    if (cur is not None and bst is not None and bst > 0
            and ((cur - bst) / bst) > REGRESSION_P95_RISE):
        found.append({"metric": "p95_ms", "current": cur, "best": bst,
                      "delta": round((cur - bst) / bst, 6)})
    cur, bst = current.get("refusal_rate"), best.get("refusal_rate")
    if cur is not None and bst is not None and (cur - bst) > REGRESSION_REFUSAL_RISE:
        found.append({"metric": "refusal_rate", "current": cur, "best": bst,
                      "delta": round(cur - bst, 6)})
    return found


def insert_eval_regression_event(db_path, payload: dict) -> bool:
    """往同库 events 表插 eval_regression 行。失败只 warning，不吞回跌结论。"""
    from datetime import datetime, timezone
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO events (event_type, agent_id, payload, timestamp)"
                " VALUES (?, ?, ?, ?)",
                ("eval_regression", "__eval__",
                 json.dumps(payload, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        print(f"[kb-eval] WARNING: eval_regression 事件插入失败"
              f"（不吞回跌结论）: {exc}", file=sys.stderr)
        return False


def _regression_alert_line(row, best):
    """一行人类可读告警：各指标 current / best / delta（delta 同 detect_regressions 口径）。"""
    parts = []
    for metric in ("recall_at_5", "p95_ms", "refusal_rate"):
        cur, bst = row.get(metric), best.get(metric)
        if cur is None or bst is None:
            parts.append(f"{metric}=n/a")
            continue
        if metric == "p95_ms" and bst > 0:
            delta = (cur - bst) / bst
            parts.append(f"{metric}={cur:.2f}/{bst:.2f}/Δ{delta:+.2%}")
        else:
            delta = cur - bst
            parts.append(f"{metric}={cur:.4f}/{bst:.4f}/Δ{delta:+.4f}")
    return ("[kb-eval] EVAL REGRESSION"
            f" dataset={row.get('dataset')}"
            f" config_hash={row.get('config_hash')} | " + " | ".join(parts))


def run_regression_check(db_path, row) -> int:
    """同 dataset + 同 config_hash 的数字回跌巡检。返回退出码（0 正常 / 3 回跌）。

    row 为本次评估结果 dict（run_evaluation 返回值）；其 id 之前的记录算历史。
    无历史 → 首次基线，不算回跌。回跌时插 events + 打印告警行。
    """
    dataset = row.get("dataset") or ""
    config_hash = row.get("config_hash") or ""
    best = collect_history_best(db_path, dataset, config_hash,
                                before_id=row.get("id"))
    if not best:
        print(f"[kb-eval] 回跌巡检：dataset={dataset} config_hash={config_hash}"
              " 无同口径历史 → 首次基线，不判回跌")
        return EXIT_OK
    found = detect_regressions(row, best)
    if not found:
        return EXIT_OK
    from datetime import datetime, timezone
    detected_at = datetime.now(timezone.utc).isoformat()
    for f in found:
        insert_eval_regression_event(db_path, {
            "dataset": dataset, "config_hash": config_hash,
            "metric": f["metric"], "current": f["current"],
            "best": f["best"], "delta": f["delta"],
            "detected_at": detected_at})
    print(_regression_alert_line(row, best), file=sys.stderr)
    return EXIT_REGRESSION


# ═══════════ 评估执行（进程内真实检索栈） ═══════════

def _make_eval_hub(db_path, collection, model):
    """装配 KnowledgeMixin/DisclosureEngine 需要的最小 hub 面。

    与 tests/test_kb_unified_retrieval.py FakeHub 同配方：_enqueue_write
    同步直写工作库（替代缓冲队列，评估同步可见）。
    """
    from hub_mixins.knowledge import KnowledgeMixin

    class EvalHub(KnowledgeMixin):
        def __init__(self):
            self._db_path = str(db_path)
            self._chroma_client = None
            self._chroma_collection = collection
            self._embedding_model = model
            self.agents = {}
            self._disclosure_policy = {
                "department_peer_visibility": False,
                "default_manager_level": "summary",
                "orchestrator_max_level": "full",
                "allow_peer_disclosure": True,
            }
            self._wiki_sync_pending = False
            self.traces = []

        def _db(self):
            return sqlite3.connect(self._db_path)

        def _enqueue_write(self, kind, payload):
            if kind == "upsert":
                conn = sqlite3.connect(self._db_path)
                conn.execute(
                    """INSERT OR REPLACE INTO knowledge_base
                       (entry_id, title, content, tags, links, category,
                        importance, created_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (payload["entry_id"], payload["title"], payload["content"],
                     payload["tags_json"], payload["links_json"],
                     payload["category"], payload["importance"],
                     payload["created_by"], payload["created_at"],
                     payload["updated_at"]))
                conn.commit()
                conn.close()
            return "queued"

        def _record_trace(self, action, agent_id, title, entry_id):
            self.traces.append((action, agent_id, title, entry_id))

        async def _ensure_embedding_model(self):
            return self._embedding_model

    return EvalHub()


def run_evaluation(db_path, corpus_dir, provider="hasher", model_path="",
                   top_k=DEFAULT_TOP_K, threshold=DEFAULT_THRESHOLD,
                   dataset=DEFAULT_DATASET, name="kb-eval", limit=0,
                   threshold_profile="v1"):
    """真跑一组评估：灌库 → 逐查询检索 → 算指标 → 落库。返回结果 dict。"""
    # SYNC_HUB_DB 必须先于 models/db import 设置（models 在 import 时解析 DB_PATH）；
    # 用 finally 恢复——pytest 会话内不污染其它用例 spawn 的 Hub 子进程（conftest 刻意
    # 不设此变量的同款理由）。若 models 已被 import，则以 CONFIG.DB_PATH 直改为准。
    _prev_db_env = os.environ.get("SYNC_HUB_DB")
    os.environ["SYNC_HUB_DB"] = os.path.abspath(db_path)
    try:
        return _run_evaluation_inner(
            db_path, corpus_dir, provider, model_path, top_k, threshold,
            dataset, name, limit, threshold_profile)
    finally:
        if _prev_db_env is None:
            os.environ.pop("SYNC_HUB_DB", None)
        else:
            os.environ["SYNC_HUB_DB"] = _prev_db_env


def _run_evaluation_inner(db_path, corpus_dir, provider, model_path,
                          top_k, threshold, dataset, name, limit,
                          threshold_profile="v1"):

    import chromadb

    import db as db_mod
    from db import get_embedding_provider
    from disclosure import DisclosureEngine
    from models import CONFIG, KnowledgeEntry, SemanticSearchRequest
    from scripts.seed_kb import build_entries

    CONFIG.DB_PATH = os.path.abspath(db_path)
    db_mod.init_db()  # 幂等：工作库 schema 建全（含 evaluation_tasks）

    entries = build_entries(corpus_dir)
    pairs = build_eval_set(entries)
    if limit:
        pairs = pairs[:limit]
    if not pairs:
        raise RuntimeError(f"评估集为空：{corpus_dir} 无可用条目")

    model = get_embedding_provider(provider, model_path=model_path)
    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection(
        f"kb_eval_{int(time.time() * 1000)}", metadata={"hnsw:space": "cosine"})
    hub = _make_eval_hub(db_path, collection, model)
    engine = DisclosureEngine(hub)
    # 编排员角色请求方：披露规则链 6 全局可见——评估检索召回，不评估权限边界
    hub.agents[EVAL_REQUESTER] = {"role": "orchestrator", "managed_agents": [],
                                  "disclosure_policy": {}, "department": ""}

    # 灌库：真实写侧（chunker + 统一 collection，layer=knowledge）
    for e in entries:
        entry = KnowledgeEntry(
            entry_id=e["entry_id"], title=e["title"], content=e["content"],
            tags=e["tags"], links=[], category=e["category"], importance=1.0,
            created_by="kb-eval")
        asyncio.run(hub.knowledge_upsert(entry))

    # 逐查询检索：真实 DisclosureEngine.semantic_search（layer=knowledge）
    per_query = []
    latencies = []
    for pair in pairs:
        req = SemanticSearchRequest(query=pair["query"],
                                    requester_agent_id=EVAL_REQUESTER,
                                    n_results=top_k, layer="knowledge")
        t0 = time.perf_counter()
        # CD-105：评估工具是进程内直调（不走 HTTP 路由、无 scope 注入）——需要全量
        # 召回评估检索质量，不设披露封顶，故显式声明 internal=True（进程内全信主体
        # 逃生门）。不加这行则默认 fail-closed metadata 封顶会把正文剥掉，recall 评估失真。
        res = asyncio.run(engine.semantic_search(req, internal=True))
        ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(ms)
        hits = res.get("memories") or []
        # 去重到条目级（同一条目多个 chunk 命中只算一次），保持返回序
        seen, ranked, sims = set(), [], []
        for h in hits:
            sims.append(float(h.get("similarity") or 0.0))
            eid = h.get("entry_id") or ""
            if eid and eid not in seen:
                seen.add(eid)
                ranked.append(eid)
        per_query.append({"query": pair["query"], "positive": pair["positive"],
                          "ranked": ranked, "similarities": sims, "ms": ms})

    r5 = recall_at_k([(q["positive"], q["ranked"]) for q in per_query], k=top_k)
    refusal = refusal_rate([q["similarities"] for q in per_query], threshold)
    p95 = percentile(latencies, 95)

    cfg = build_eval_config(dataset, provider, model_path, top_k, threshold,
                            corpus_fingerprint(entries),
                            threshold_profile=threshold_profile)
    row = {"dataset": dataset, "name": name,
           "recall_at_5": round(r5, 4), "refusal_rate": round(refusal, 4),
           "p95_ms": round(p95, 2), "sample_count": len(pairs),
           "config_hash": config_fingerprint(cfg),
           "config_json": json.dumps(cfg, ensure_ascii=False, sort_keys=True)}
    row_id = insert_evaluation_task(db_path, row)
    row["id"] = row_id
    row["per_query"] = per_query
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description="KB 检索评估（CD-081 最小骨架）")
    ap.add_argument("--db", default=os.environ.get("SYNC_HUB_DB", "./sync_hub.db"),
                    help="工作库 + 结果库（evaluation_tasks 落此库）")
    ap.add_argument("--corpus", default=os.path.join(ROOT, "corpus"))
    ap.add_argument("--provider", default="hasher", choices=["hasher", "sentence"],
                    help="embedding provider（hasher=仓内默认零依赖档）")
    ap.add_argument("--model-path", default="",
                    help="sentence provider 的本地模型目录")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--threshold", type=float, default=None,
                    help="拒答阈值：全部命中 similarity 低于此值 = 拒答"
                         "（默认按 --threshold-profile 档位取值）")
    ap.add_argument("--threshold-profile", default="v1",
                    help="阈值档位（THRESHOLD_PROFILES）；默认 v1 = 既有钉死口径"
                         "，config_json 不加字段（hash 不变）")
    ap.add_argument("--regression-check", action="store_true",
                    help="同 dataset+config_hash 历史回跌巡检：命中回跌判据时"
                         "插 events(eval_regression) 并以退出码 3 结束")
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--name", default="kb-eval")
    ap.add_argument("--limit", type=int, default=0, help="只评前 N 条（调试用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只构造评估集并打印，不检索、不落库")
    args = ap.parse_args(argv)

    if args.dry_run:
        from scripts.seed_kb import build_entries
        entries = build_entries(args.corpus)
        pairs = build_eval_set(entries)
        print(json.dumps({
            "dry_run": True, "corpus": args.corpus,
            "entries": len(entries), "pairs": len(pairs),
            "sample": pairs[:3],
        }, ensure_ascii=False, indent=2))
        return 0

    threshold = resolve_threshold(args.threshold, args.threshold_profile)
    row = run_evaluation(
        db_path=args.db, corpus_dir=args.corpus, provider=args.provider,
        model_path=args.model_path, top_k=args.top_k, threshold=threshold,
        dataset=args.dataset, name=args.name, limit=args.limit,
        threshold_profile=args.threshold_profile)
    print(json.dumps({k: v for k, v in row.items() if k != "per_query"},
                     ensure_ascii=False, indent=2))
    if args.regression_check:
        return run_regression_check(args.db, row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
