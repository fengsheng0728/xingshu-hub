# -*- coding: utf-8 -*-
"""ShadowWriter 反哺合并组（3-2c 自 shadow.py 逐字搬运）。"""
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


class MergeMixin:
    def execute_merge(self, kind: str, sources: list, content: str, *,
                      action: str = "auto_merge", actor: str = "system",
                      queue_id=None, cos: float = 1.0, owner_key_fp: str = "",
                      title: str = "") -> dict:
        """合并执行器（§2.3 动作序列，自动/人工共用同一执行器，仅触发源不同）。

        sources: [{branch, id, path, trust, level, tainted_at, locked, owner, title,
                   trunk_commit, branch_commit}, ...]；sources[0] = 主来源（保留原路径，
                   对齐 §3 示例 sources[0]），其余为被吸收来源（归档 vault/_merged/）。
        动作序列：① 主干 customers/<canonical_id>.json 写/更新 + index 追加 merged_into
        修正行（纯追加不改历史行）；② 被吸收分干 vault md 归档（front-matter 追加
        merged_into）；③ 内存 _origins 重指向 canonical 档案路径；④ 批 commit message
        含 canonical_id + audit/chain-head.jsonl 链头互证。
        审计 events/audit_log 双写由调用方（hub_core.backfeed_*）用返回的 audit 载荷落链。
        D4：任何异常静默降级返回 {"status": "error"}，绝不抛。
        """
        if not self._backfeed_enabled():
            return {"status": "disabled", "enabled": False}
        try:
            from data_trunk import canonical_id_for, synthesize_tags, _now_iso
            from chunker import chunk_hash
            dt = self.dt
            sources = [dict(s) for s in sources
                       if s.get("id") and s.get("branch") and s.get("path")]
            if len(sources) < 2:
                return {"status": "error", "detail": "合并至少需要 2 个来源"}
            cid = canonical_id_for(content)
            now = _now_iso()
            # 幂等：全部来源已 merged_into 本 canonical → 零动作
            state = _index_state(_index_rows(dt, kind))
            if all((state.get(s["id"]) or {}).get("merged_into") == cid
                   for s in sources):
                return {"status": "already_merged", "canonical_id": cid}

            # ② 分干归档（被吸收来源；主来源保留原路径，对齐 §3 示例）
            archived = []
            for s in sources[1:]:
                br = dt.branch_repo(s["branch"])
                old_rel = s["path"]
                new_rel = f"vault/_merged/{_today()}/{_safe_name(s['id'])}.md"
                text = self._read_branch_file(s["branch"], old_rel)
                if not text:
                    logger.warning("反哺归档源文件缺失，跳过: %s:%s", s["branch"], old_rel)
                    continue
                if not br.write_file(new_rel,
                                     _with_front_matter_field(text, "merged_into", cid)):
                    continue
                if br.remove_file(old_rel, f"阶段4-B2: 反哺归档 {s['id']} → {cid}"):
                    s["path"] = new_rel  # canonical sources 记归档后路径（§3 示例）
                    archived.append({"branch": s["branch"], "id": s["id"],
                                     "path": new_rel})

            # ① 主干 canonical 档案（§3 逐字段；tags 只降不升：连既有快照重算 min）
            existing = dt.read_canonical(cid)
            snap = synthesize_tags(sources, base=(existing or {}).get("tags_snapshot"))
            if existing:
                record = existing
                before = {"trust": (existing.get("tags_snapshot") or {}).get("trust", ""),
                          "level": (existing.get("tags_snapshot") or {}).get("level", "")}
            else:
                record = {
                    "canonical_id": cid, "kind": kind,
                    "title": title or sources[0].get("title") or sources[0]["id"],
                    "content_digest": "sha256:" + chunk_hash(content or ""),
                    "identity": {"owner_key_fp": owner_key_fp or "",
                                 "owner_agent_ids": []},
                    "sources": [], "tags_snapshot": {}, "merge_history": [],
                    "created_at": now, "updated_at": now,
                }
                before = {"trust": sources[0].get("trust", ""),
                          "level": sources[0].get("level", "")}
            known = {(s.get("branch"), s.get("id")) for s in record["sources"]}
            for s in sources:
                if (s["branch"], s["id"]) not in known:
                    record["sources"].append({
                        "branch": s["branch"], "kind": kind, "id": s["id"],
                        "path": s["path"],
                        "trunk_commit": s.get("trunk_commit", ""),
                        "branch_commit": s.get("branch_commit", "")})
            owners = set(record["identity"].get("owner_agent_ids") or [])
            owners.update(s.get("owner") for s in sources if s.get("owner"))
            record["identity"]["owner_agent_ids"] = sorted(owners)
            if owner_key_fp and not record["identity"].get("owner_key_fp"):
                record["identity"]["owner_key_fp"] = owner_key_fp
            absorbed = [f"{s['branch']}:{s['id']}" for s in sources[1:]]
            record["tags_snapshot"] = snap
            record["merge_history"].append({
                "ts": now, "action": action, "cos": cos, "absorbed": absorbed,
                "actor": actor, "queue_id": queue_id,
                "before": before,
                "after": {"trust": snap["trust"], "level": snap["level"]}})
            record["updated_at"] = now
            dt.write_canonical(record)

            # ①b index 修正行（纯追加；已并入本 canonical 的 id 不重复追加）
            for s in sources:
                if (state.get(s["id"]) or {}).get("merged_into") == cid:
                    continue
                append_index_correction(dt, kind, {
                    "id": s["id"], "merged_into": cid, "ts": now, "actor": actor})
            # ④ 批 commit message 含 canonical_id 列表 + 链头互证
            msg = f"阶段4-B2: 反哺合并 [{cid}] kind={kind} 来源={len(sources)}"
            dt.trunk.commit(msg)
            self._append_chain_head(msg)
            # ③ 内存 _origins 重指向 canonical 档案路径
            head = dt.trunk.head_hash()
            cpath = dt.canonical_path(cid)
            for s in sources:
                self._origins[s["id"]] = {
                    "kind": kind, "branch": "", "path": cpath,
                    "merged": True, "canonical_id": cid,
                    "trunk_commit": head, "branch_commit": "", "ts": now}
            return {"status": "merged", "canonical_id": cid, "kind": kind,
                    "archived": archived, "tags_snapshot": snap,
                    "audit": {"canonical_id": cid, "kind": kind, "action": action,
                              "actor": actor, "queue_id": queue_id, "cos": cos,
                              "absorbed": absorbed,
                              "after": {"trust": snap["trust"], "level": snap["level"]}}}
        except Exception:
            logger.exception("execute_merge 异常（降级）")
            return {"status": "error"}

    def undo_merge(self, canonical_id: str, *, actor: str = "system") -> dict:
        """回滚（§2.3-5）：归档移回原路径 + index 追加 unmerged 修正行。
        git 历史全程留痕（read_at 可读任意历史版本）。D4：异常静默降级。"""
        if not self._backfeed_enabled():
            return {"status": "disabled", "enabled": False}
        try:
            from data_trunk import _now_iso
            dt = self.dt
            record = dt.read_canonical(canonical_id)
            if not record:
                return {"status": "not_found", "canonical_id": canonical_id}
            kind = record.get("kind", "")
            now = _now_iso()
            # 原路径来源：index 首个含 path 的原始行（修正行只追加不改历史）
            orig_paths = {rid: st["row"]["path"]
                          for rid, st in _index_state(_index_rows(dt, kind)).items()
                          if st.get("row") and st["row"].get("path")}
            restored = []
            for s in record.get("sources") or []:
                rid, branch, cur = s.get("id"), s.get("branch", ""), s.get("path", "")
                if not rid or not branch:
                    continue
                orig = orig_paths.get(rid)
                br = dt.branch_repo(branch)
                # 归档文件移回原路径（front-matter 去掉 merged_into）；主来源未动过
                if cur.startswith("vault/_merged/") and orig and orig != cur:
                    text = self._read_branch_file(branch, cur)
                    if text:
                        br.write_file(orig, _drop_front_matter_field(text, "merged_into"))
                        if br.remove_file(cur, f"阶段4-B2: 反哺回滚 {rid} ← {canonical_id}"):
                            s["path"] = orig
                            restored.append({"branch": branch, "id": rid, "path": orig})
                # index 追加 unmerged 修正行（含主来源——读取端据此取消重指向）
                append_index_correction(dt, kind, {
                    "id": rid, "unmerged": True, "merged_into": canonical_id,
                    "ts": now, "actor": actor})
                self._origins[rid] = {
                    "kind": kind, "branch": branch,
                    "path": s.get("path") or orig or cur,
                    "trunk_commit": dt.trunk.head_hash(),
                    "branch_commit": br.head_hash(), "ts": now}
            record.setdefault("merge_history", []).append({
                "ts": now, "action": "unmerge", "cos": None,
                "absorbed": [f"{r['branch']}:{r['id']}" for r in restored],
                "actor": actor, "queue_id": None, "before": None, "after": None})
            record["updated_at"] = now
            dt.write_canonical(record)
            msg = f"阶段4-B2: 反哺回滚 [{canonical_id}]"
            dt.trunk.commit(msg)
            self._append_chain_head(msg)
            return {"status": "unmerged", "canonical_id": canonical_id,
                    "restored": restored,
                    "audit": {"canonical_id": canonical_id, "kind": kind,
                              "action": "unmerge", "actor": actor,
                              "restored": [f"{r['branch']}:{r['id']}" for r in restored]}}
        except Exception:
            logger.exception("undo_merge 异常（降级）")
            return {"status": "error"}

    def _mk_source(self, it: dict, kind: str) -> dict:
        """由 scan 条目构造 execute_merge 的 sources 成员(对齐 §3 sources 字段)。"""
        row = it["row"]
        return {
            "id": it["id"], "branch": it["branch"], "path": it["path"],
            "trust": row.get("trust", ""), "level": row.get("level", ""),
            "tainted_at": row.get("tainted_at", ""),
            "locked": bool(row.get("locked", False)),
            "owner": row.get("owner", ""), "title": row.get("title", ""),
            "trunk_commit": it.get("trunk_commit", ""),
            "branch_commit": it.get("branch_commit", ""),
        }

    def resolve_sources(self, kind: str, ids: list) -> list:
        """按 id 从 index/commits 现查来源(人工队列 approve 后执行用)。
        返回 [{..._mk_source 字段, body}], 缺失 id 静默跳过(D4)。"""
        try:
            if not ids:
                return []
            anchors = {}
            for ln in _read_worktree(self.dt.trunk.root,
                                     "index/.commits.jsonl").splitlines():
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                for rid in rec.get("ids") or []:
                    anchors[rid] = rec
            st_map = _index_state(_index_rows(self.dt, kind))
            out = []
            for rid in ids:
                st = st_map.get(rid)
                row = st.get("row") if st else None
                if not row or st.get("merged_into"):
                    continue
                branch, path = row.get("branch", ""), row.get("path", "")
                body = _strip_front_matter(
                    self._read_branch_file(branch, path))
                if not body.strip():
                    continue
                anchor = anchors.get(rid) or {}
                heads = anchor.get("branches") or {}
                it = {"id": rid, "branch": branch, "path": path,
                      "row": row,
                      "trunk_commit": anchor.get("commit", ""),
                      "branch_commit": heads.get(branch, "")}
                src = self._mk_source(it, kind)
                src["body"] = body
                out.append(src)
            return out
        except Exception:
            logger.exception("resolve_sources 异常(降级空)")
            return []
