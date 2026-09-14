# -*- coding: utf-8 -*-
"""ShadowWriter 反哺扫描组（3-2c 自 shadow.py 逐字搬运，验收修正自 merge.py 再拆）。"""
import json
import logging
import os
import time

from .common import (
    _INDEX_KINDS,
    _MERGE_MIN_AGE_SEC,
    _drop_front_matter_field,
    _index_rows,
    _index_state,
    _read_worktree,
    _safe_name,
    _strip_front_matter,
    _today,
    _ts_epoch,
    _with_front_matter_field,
    append_index_correction,
)

logger = logging.getLogger("xingshu.shadow")


class ScanMixin:
    """反哺扫描与阈值判定（scan_and_merge / scan_cos_merge / _backfeed_threshold）。"""

    def scan_and_merge(self, kinds=None, dry_run: bool = False,
                       min_age_sec: float = None, actor: str = "system") -> dict:
        """自动合并扫描入口（§6-B2 判据，chunk_hash 精确去重档）。

        判定单元：主干 index/<kind>.jsonl 条目对（kind ∈ memory/knowledge/wiki/shared）；
        内容取分干 vault md（剥 front-matter 后的正文）；chunk_hash(content) 相等
        → 调 execute_merge 自动合并（首个来源为主来源，保留原路径）。
        不可合并窗口 _MERGE_MIN_AGE_SEC=60s（§2.3-3）：条目年龄取 index/.commits.jsonl
        锚点 ts，缺失回落 vault 文件 mtime，两者皆无 → 保守跳过。
        dry_run 只报告不写。fail-closed：backfeed 未开 → {"enabled": False}。
        """
        if not self._backfeed_enabled():
            return {"enabled": False}
        try:
            from chunker import chunk_hash
            dt = self.dt
            kinds = list(kinds) if kinds else list(_INDEX_KINDS)
            min_age = _MERGE_MIN_AGE_SEC if min_age_sec is None else float(min_age_sec)
            now_ts = time.time()
            # id → 最新锚点（ts + commit；后批覆盖前批）
            anchors = {}
            for ln in _read_worktree(dt.trunk.root, "index/.commits.jsonl").splitlines():
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                for rid in rec.get("ids") or []:
                    anchors[rid] = rec
            result = {"enabled": True, "dry_run": bool(dry_run), "scanned": 0,
                      "skipped_young": 0, "groups": [], "merges": [], "merged": 0}
            for kind in kinds:
                groups = {}
                for rid, st in _index_state(_index_rows(dt, kind)).items():
                    row = st.get("row")
                    if not row or st.get("merged_into"):
                        continue  # 已合并不再参与（幂等：二次扫描零动作）
                    branch, path = row.get("branch", ""), row.get("path", "")
                    if not branch or not path:
                        continue
                    anchor = anchors.get(rid) or {}
                    epoch = _ts_epoch(anchor.get("ts", ""))
                    if epoch is None:
                        try:
                            epoch = os.path.getmtime(
                                os.path.join(dt.branch_root(branch), path))
                        except Exception:
                            epoch = None
                    if epoch is None or (now_ts - epoch) < min_age:
                        result["skipped_young"] += 1
                        continue
                    body = _strip_front_matter(self._read_branch_file(branch, path))
                    if not body.strip():
                        continue
                    result["scanned"] += 1
                    heads = anchor.get("branches") or {}
                    groups.setdefault(chunk_hash(body), []).append({
                        "id": rid, "branch": branch, "path": path,
                        "trust": row.get("trust", ""), "level": row.get("level", ""),
                        "tainted_at": row.get("tainted_at", ""),
                        "locked": bool(row.get("locked", False)),
                        "owner": row.get("owner", ""), "title": row.get("title", ""),
                        "trunk_commit": anchor.get("commit", ""),
                        "branch_commit": heads.get(branch, "")})
                for h in sorted(groups):
                    members = groups[h]
                    if len(members) < 2:
                        continue
                    if dry_run:
                        result["groups"].append({
                            "kind": kind, "content_digest": "sha256:" + h,
                            "ids": [m["id"] for m in members],
                            "branches": sorted({m["branch"] for m in members})})
                        continue
                    content = _strip_front_matter(self._read_branch_file(
                        members[0]["branch"], members[0]["path"]))
                    r = self.execute_merge(kind, members, content,
                                           action="auto_merge", actor=actor)
                    result["merges"].append(r)
                    if r.get("status") == "merged":
                        result["merged"] += 1
            return result
        except Exception:
            logger.exception("scan_and_merge 异常（降级）")
            return {"enabled": True, "status": "error"}

    # ── 阶段4-B3：cos 相似度三档分流（docs/phase4-backfeed-design.md §2.1/§6-B3）──
    # 三档：≥auto 自动合并 / review_floor≤cos<auto 进人工队列(review_candidates 输出,
    #       hub 层 queue_backfeed_merge) / <review_floor 各自保留。
    # 阈值 provider 版本化(§2.1「阈值绑定 provider」): backfeed 配置键
    #   cos_auto_merge@<provider> 优先 → cos_auto_merge → 默认
    #   (hasher 档 0.99≈精确去重/禁语义自动, sentence 档 0.9——K1 修正 3 教训)。
    # hasher 词袋无语义区分度 → hasher 档自动合并近关, 0.6-0.99 进人工(设计 §1.6)。

    def _backfeed_threshold(self, key: str, provider: str,
                            fallback: float) -> float:
        """阈值读取: data_trunk.backfeed 配置键 `{key}@{provider}` 优先(版本化),
        → `{key}` → fallback。仅接受 int/float(防 yaml 类型注入)。"""
        bf = getattr(self.dt, "backfeed", None) or {}
        if provider:
            v = bf.get(f"{key}@{provider}")
            if isinstance(v, (int, float)):
                return float(v)
        v = bf.get(key)
        return float(v) if isinstance(v, (int, float)) else fallback

    def scan_cos_merge(self, kinds=None, dry_run: bool = False,
                       min_age_sec: float = None, cos_auto_merge: float = None,
                       cos_review_floor: float = None,
                       embed_fn=None, provider_name: str = "",
                       cos_pair_limit: int = 400) -> dict:
        """cos 相似度三档分流扫描（§6-B3，B2 chunk_hash 档的语义扩展）。

        判定：同 kind 未合并条目两两算 embedding cos；长度剪枝(比例 0.5-2.0,
        语义相似文本长度通常接近,低成本排除)后：
          cos ≥ auto          → 自动合并(execute_merge, action=auto_merge, cos=实测)
          review_floor ≤ cos < auto → review_candidates(供 hub 层入人工队列)
          cos < review_floor  → 各自保留
        阈值/版本化/默认见 _backfeed_threshold 与 docstring。embed_fn 注入
        (测试用假向量)；provider_name 用于阈值版本化键。幂等:同批内已合并 id
        跳过；dry_run 只报告。fail-closed 同 B2。复杂度 N² 剪枝 + 上限 cos_pair_limit。
        """
        if not self._backfeed_enabled():
            return {"enabled": False}
        try:
            import numpy as _np
            from models import CONFIG
            dt = self.dt
            provider = provider_name or getattr(CONFIG, "EMBEDDING_PROVIDER", "hasher")
            if embed_fn is None:
                try:
                    from db import get_embedding_provider
                    prov = get_embedding_provider(
                        provider, getattr(CONFIG, "EMBEDDING_MODEL_PATH", ""))
                    embed_fn = prov.encode
                except Exception:
                    embed_fn = None
            if embed_fn is None:
                return {"enabled": True, "status": "error",
                        "detail": "embedding provider 不可用"}
            auto = cos_auto_merge if cos_auto_merge is not None \
                else self._backfeed_threshold(
                    "cos_auto_merge", provider,
                    0.99 if provider == "hasher" else 0.9)
            floor = cos_review_floor if cos_review_floor is not None \
                else self._backfeed_threshold("cos_review_floor", provider, 0.6)
            kinds = list(kinds) if kinds else list(_INDEX_KINDS)
            min_age = _MERGE_MIN_AGE_SEC if min_age_sec is None else float(min_age_sec)
            now_ts = time.time()
            anchors = {}
            for ln in _read_worktree(dt.trunk.root, "index/.commits.jsonl").splitlines():
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                for rid in rec.get("ids") or []:
                    anchors[rid] = rec
            result = {"enabled": True, "provider": provider,
                      "cos_auto_merge": auto, "cos_review_floor": floor,
                      "dry_run": bool(dry_run), "scanned": 0, "pairs": 0,
                      "skipped_young": 0, "auto_merged": 0,
                      "review_candidates": [], "merges": [], "status": "ok"}
            for kind in kinds:
                items = []
                for rid, st in _index_state(_index_rows(dt, kind)).items():
                    row = st.get("row")
                    if not row or st.get("merged_into"):
                        continue
                    branch, path = row.get("branch", ""), row.get("path", "")
                    if not branch or not path:
                        continue
                    anchor = anchors.get(rid) or {}
                    epoch = _ts_epoch(anchor.get("ts", ""))
                    if epoch is None:
                        try:
                            epoch = os.path.getmtime(
                                os.path.join(dt.branch_root(branch), path))
                        except Exception:
                            epoch = None
                    if epoch is None or (now_ts - epoch) < min_age:
                        result["skipped_young"] += 1
                        continue
                    body = _strip_front_matter(self._read_branch_file(branch, path))
                    if not body.strip():
                        continue
                    heads = anchor.get("branches") or {}
                    items.append({
                        "id": rid, "branch": branch, "path": path, "body": body,
                        "row": row,
                        "trunk_commit": anchor.get("commit", ""),
                        "branch_commit": heads.get(branch, "")})
                result["scanned"] += len(items)
                if len(items) < 2:
                    continue
                # 逐条编码 → 两两 cos(长度剪枝)
                try:
                    vecs = embed_fn([it["body"] for it in items])
                except Exception:
                    logger.exception("B3 embed 失败(kind=%s), 跳过该 kind", kind)
                    continue
                merged_ids = set()
                pair_count = 0
                for i in range(len(items)):
                    if items[i]["id"] in merged_ids:
                        continue
                    for j in range(i + 1, len(items)):
                        if items[j]["id"] in merged_ids:
                            continue
                        pair_count += 1
                        if pair_count > cos_pair_limit:
                            return {**result, "status": "pair_limit",
                                    "detail": f"pair 超上限 {cos_pair_limit}, 中止"}
                        la, lb = len(items[i]["body"]), len(items[j]["body"])
                        if la <= 0 or lb <= 0 or \
                                max(la, lb) / min(la, lb) > 2.0:
                            continue  # 长度剪枝(语义词长度接近)
                        a, b = vecs[i], vecs[j]
                        if getattr(a, "ndim", 0) and getattr(b, "ndim", 0):
                            na = float(_np.linalg.norm(a)) or 1.0
                            nb = float(_np.linalg.norm(b)) or 1.0
                            cos = float(_np.dot(a, b) / (na * nb))
                        else:
                            cos = 0.0
                        result["pairs"] += 1
                        if cos >= auto:
                            members = [
                                self._mk_source(it, kind) for it in (items[i], items[j])]
                            if dry_run:
                                result.setdefault("auto_candidates", []).append(
                                    {"kind": kind,
                                     "ids": [items[i]["id"], items[j]["id"]],
                                     "cos": round(cos, 4), "decision": "auto"})
                                merged_ids.update(
                                    [items[i]["id"], items[j]["id"]])
                                continue
                            r = self.execute_merge(
                                kind, members, items[i]["body"],
                                action="auto_merge", actor="system", cos=round(cos, 4))
                            if r.get("status") == "merged":
                                result["auto_merged"] += 1
                                result["merges"].append(r)
                                merged_ids.update([items[i]["id"], items[j]["id"]])
                        elif cos >= floor:
                            result["review_candidates"].append({
                                "kind": kind,
                                "pair": [
                                    {"id": items[i]["id"], "branch": items[i]["branch"],
                                     "path": items[i]["path"]},
                                    {"id": items[j]["id"], "branch": items[j]["branch"],
                                     "path": items[j]["path"]}],
                                "cos": round(cos, 4),
                                "decision": "review",
                                "digest": "cos"})
            return result
        except Exception:
            logger.exception("scan_cos_merge 异常（降级）")
            return {"enabled": True, "status": "error"}
