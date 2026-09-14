# -*- coding: utf-8 -*-
"""ShadowWriter 攒批落库 + 写主干/分干组（3-2c 自 shadow.py 逐字搬运）。"""
import datetime
import json
import logging
import os
import time

from .common import (
    _BATCH_INTERVAL,
    _BATCH_SIZE,
    _front_matter,
    _safe_name,
    _today,
)

logger = logging.getLogger("xingshu.shadow")


class WriteMixin:
    # ── worker ──
    def _worker(self):
        while not self._stop.is_set():
            self._drain_once()
            self._stop.wait(_BATCH_INTERVAL)
        # 停止后兜底清一次
        self._drain_once()

    def _drain_once(self):
        batch = []
        with self._qlock:
            while len(batch) < _BATCH_SIZE and self._q:
                batch.append(self._q.popleft())
        if not batch:
            return
        try:
            failed_pids = self._flush_batch(batch)
            self.stats["flushed"] += len(batch)
            self.stats["last_flush_at"] = time.time()
            # G1 批1：flush 成功 → pending 行软标记 done；逐条失败的 attempts+1
            done_ids = [pid for _, _, pid in batch if pid and pid not in failed_pids]
            self._pending_mark_done(done_ids)
            if failed_pids:
                self._pending_note_failure(sorted(failed_pids))
            # G1 批2：flush 恢复成功 → 清零连续失败计数（告警「连续」语义）
            if self._consec_fail:
                self._consec_fail = 0
                self._alert_reset("flush_failure")
        except Exception as e:
            logger.exception("影子批写入失败（降级，不阻塞主链路）")
            self.stats["failures"] += len(batch)
            self.stats["last_failure_at"] = time.time()
            self.stats["last_failure_reason"] = type(e).__name__
            # G1 批1：失败批不再静默丢弃——attempts+1，超限标 failed，
            # 留表待下次启动 replay（重放幂等：index 同 id 去重 + vault 覆盖写）
            self._pending_note_failure([pid for _, _, pid in batch if pid])
            # G1 批2：连续失败告警（阈值 + 去抖由 notifications 封装，不阻塞）
            self._consec_fail += 1
            self._alert("flush_failure",
                        "consecutive=%d err=%s batch=%d"
                        % (self._consec_fail, type(e).__name__, len(batch)))

    # ── 批写入 ──
    def _flush_batch(self, batch: list) -> set:
        """逐条写文件 + 分干/主干双 commit + 锚点登记。

        batch 条目为 (kind, payload, pending_id)。返回逐条失败条目的
        pending_id 集合；分干/主干 commit 返回 False 时抛 RuntimeError
        （整批计入失败路径，由 _drain_once 做 attempts+1）。
        """
        kinds = []
        written = {}  # branch -> [meta]，按映射分干分组 commit（P2 交付1）
        failed = 0
        failed_pids = set()
        for kind, payload, pending_id in batch:
            try:
                branch, meta = self._write_one(kind, payload)
                self._kind_count[kind] = self._kind_count.get(kind, 0) + 1
                if kind not in kinds:
                    kinds.append(kind)
                written.setdefault(branch, []).append(meta)
            except Exception:
                # 逐条容错：单条失败不 abort 整批（D4 影子是增强不是依赖）
                failed += 1
                if pending_id:
                    failed_pids.add(pending_id)
                self.stats["failures"] += 1
                self.stats["last_failure_at"] = time.time()
                self.stats["last_failure_reason"] = "item_write"
                logger.warning("影子镜像单条失败 kind=%s id=%s", kind,
                               payload.get("memory_id") or payload.get("entry_id")
                               or payload.get("doc_id") or "?")
        if not kinds:
            return failed_pids
        msg = "阶段3-P1: 影子镜像 %d 条 [%s]%s" % (
            len(batch) - failed, ",".join(kinds),
            " (失败 %d)" % failed if failed else "")
        # G1 批1：检查 commit 返回值，False 计入失败路径（不再静默当成功）
        for branch in written:
            if not self.dt.branch_repo(branch).commit(msg):
                raise RuntimeError("影子分干 commit 失败: %s" % branch)
        if not self.dt.trunk.commit(msg):
            raise RuntimeError("影子主干 commit 失败")
        # P2 交付2：真相源定位锚点（index 条目 → git 路径 + commit hash）
        self._record_origins(written, msg)
        return failed_pids

    # ── 真相源定位锚点（P2 交付2）──
    def _record_origins(self, written: dict, msg: str):
        """批 commit 后登记 id → {branch, path, trunk_commit, branch_commit, ts}。

        内存映射（self._origins）供网关读取实时附 origin 字段；
        index/.commits.jsonl 持久化（重启后 collect_origins 可纯文件重建）。
        锚点文件随独立「锚点登记」commit 落 git——定位信息本身也进历史。
        失败静默降级（D4：定位是增强，不阻塞主链路）。
        """
        try:
            trunk_hash = self.dt.trunk.head_hash()
            if not trunk_hash:
                return
            branch_heads = {b: self.dt.branch_repo(b).head_hash() for b in written}
            ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            ids = []
            for branch, metas in written.items():
                for meta in metas:
                    oid = meta.get("id")
                    if not oid:
                        continue
                    ids.append(oid)
                    self._origins[oid] = {
                        "kind": meta.get("kind", ""), "branch": branch,
                        "path": meta.get("path", ""),
                        "trunk_commit": trunk_hash,
                        "branch_commit": branch_heads.get(branch, ""),
                        "ts": ts,
                    }
            if not ids:
                return
            self._append_trunk_jsonl("index/.commits.jsonl", {
                "ts": ts, "commit": trunk_hash, "branches": branch_heads,
                "ids": ids, "batch": msg})
            # P2 交付4：审计绑定 — 当前哈希链链头 ↔ 本批 git commit 互证。
            # 与 export_anchor（外部介质锚定）并存：chain-head.jsonl 是 git 内快照，
            # 随锚点 commit 进 git 历史，仓库历史本身证明链头时序。
            if self._audit_db_path:
                from audit_chain import current_chain_head
                self._append_trunk_jsonl("audit/chain-head.jsonl", {
                    "ts": ts, "git_commit": trunk_hash,
                    "branches": branch_heads,
                    "chain_head": current_chain_head(self._audit_db_path),
                    "batch": msg})
            self.dt.trunk.commit("阶段3-P2: 锚点登记（commits + chain-head）")
        except Exception:
            logger.exception("真相源定位锚点登记失败（降级，不阻塞主链路）")

    def _append_trunk_jsonl(self, rel: str, record: dict):
        """主干 jsonl 纯追加（读工作区累积——同批未 commit 的追加不能读 HEAD，
        与 _append_index 同一教训）。"""
        full = os.path.join(self.dt.trunk.root, rel)
        old = ""
        try:
            if os.path.exists(full):
                with open(full, "r", encoding="utf-8") as f:
                    old = f.read()
        except Exception:
            old = ""
        line = json.dumps(record, ensure_ascii=False)
        text = (old.rstrip("\n") + "\n" + line + "\n") if old else line + "\n"
        self.dt.trunk.write_file(rel, text)


    # 各 kind 的属主 agent 字段（scope ↔ 分干映射的解析输入）
    _OWNER_KEYS = {"memory": "owner", "knowledge": "created_by",
                   "wiki": "source_agent_id", "shared": "created_by"}

    def _resolve_branch(self, kind: str, payload: dict) -> str:
        """scope ↔ 分干映射（P2 交付1）：属主 agent + scope → 可写分干。"""
        agent = payload.get(self._OWNER_KEYS.get(kind, ""), "") or ""
        branch = self.dt.branch_for_agent(agent, payload.get("scope"))
        key = (branch, agent)
        if branch != self.dt.branch_default and key not in self._opened:
            # 惰性开通非默认分干（结构 + branches.jsonl 登记 + mapped_agents）
            self.dt.ensure_branch(branch, agent_id=agent)
            self._opened.add(key)
        return branch

    def _write_one(self, kind: str, payload: dict):
        """写打标 md 到分干 vault/ + 元数据追加主干 index/。

        返回 (branch, meta)；未知 kind 抛 ValueError（由批写入逐条容错捕获）。
        """
        date = payload.get("date") or _today()
        branch = self._resolve_branch(kind, payload)
        br = self.dt.branch_repo(branch)
        if kind == "memory":
            mid = payload["memory_id"]
            rel = f"vault/memory/{date}/{_safe_name(mid)}.md"
            meta = {"kind": "memory", "id": mid, "owner": payload.get("owner", ""),
                    "trust": payload.get("trust", ""), "level": payload.get("level", ""),
                    "date": date, "tags": payload.get("tags", [])}
            body = payload.get("content", "")
            title = payload.get("memory_key", "")
        elif kind == "knowledge":
            eid = payload["entry_id"]
            rel = f"vault/knowledge/{date}/{_safe_name(eid)}.md"
            meta = {"kind": "knowledge", "id": eid,
                    "owner": payload.get("created_by", ""),
                    "trust": payload.get("trust", "internal"),
                    "level": payload.get("level", "summary"), "date": date,
                    "tags": payload.get("tags", [])}
            body = payload.get("content", "")
            title = payload.get("title", "")
        elif kind == "wiki":
            doc_id = payload["doc_id"]
            idx = payload.get("piece_index", 0)
            rel = f"vault/wiki/{_safe_name(doc_id)}/{idx:03d}.md"
            meta = {"kind": "wiki", "id": f"{doc_id}-c{idx}",
                    "owner": payload.get("source_agent_id", ""),
                    "trust": payload.get("trust", "internal"),
                    "level": payload.get("level", "summary"), "date": date,
                    "tags": []}
            body = payload.get("content", "")
            title = doc_id
        elif kind == "shared":
            did = payload["doc_id"]
            rel = f"vault/shared/{date}/{_safe_name(did)}.md"
            meta = {"kind": "shared", "id": did,
                    "owner": payload.get("created_by", ""),
                    "trust": payload.get("trust", "internal"),
                    "level": payload.get("level", "summary"), "date": date,
                    "tags": []}
            body = payload.get("content", "")
            title = payload.get("title", "")
        else:
            raise ValueError(f"未知影子 kind: {kind}")
        meta["title"] = title
        meta["branch"] = branch
        meta["path"] = rel
        content = _front_matter(kind, meta) + "\n\n" + (body or "")
        # G1 批2：write_file 返回 False 计入失败路径（批1 遗留项，
        # 与 commit 返回值检查同模式——不再静默当成功）
        if not br.write_file(rel, content):
            raise IOError(f"影子 vault 写文件失败: {rel}")
        # 主干 index 元数据（不含 content 全文）
        self._append_index(kind, meta)
        return branch, meta

    def _append_index(self, kind: str, meta: dict):
        """主干 index/<kind>.jsonl 追加一行。

        读工作区文件而非 HEAD（read_at 读已 commit 版本，同批内未 commit 的
        追加会互相覆盖——实测 index 只剩最后一条，必须读磁盘累积）。
        """
        rel = f"index/{kind}.jsonl"
        full = os.path.join(self.dt.trunk.root, rel)
        old = ""
        try:
            if os.path.exists(full):
                with open(full, "r", encoding="utf-8") as f:
                    old = f.read()
        except Exception:
            old = ""
        line = json.dumps(meta, ensure_ascii=False)
        if old:
            # 去重：同 id 同 path 已存在则跳过（重放/重复提交防双写）
            for ln in old.splitlines():
                try:
                    if json.loads(ln).get("id") == meta.get("id"):
                        return
                except Exception:
                    continue
            text = old.rstrip("\n") + "\n" + line + "\n"
        else:
            text = line + "\n"
        # G1 批2：write_file 返回 False 抛 IOError 计入逐条失败路径（批1 遗留项）。
        # write_file 自身不抛异常（gitrepo D4 返回 False 语义），无需 try 包裹。
        if not self.dt.trunk.write_file(rel, text):
            raise IOError(f"影子 index 写文件失败: {rel}")

    # ═══════════ 阶段4-B2 反哺精确去重合并（设计 §2.3/§3/§6-B2，chunk_hash 档）═══════════

    def _backfeed_enabled(self) -> bool:
        """fail-closed（§6）：data_trunk.enabled=false 或 backfeed.enabled 缺省 → False。"""
        dt = self.dt
        if not getattr(dt, "enabled", False):
            return False
        return bool((getattr(dt, "backfeed", None) or {}).get("enabled", False))

    def _read_branch_file(self, branch: str, path: str) -> str:
        """读分干工作区文件。失败返回空串（D4）。"""
        try:
            full = os.path.join(self.dt.branch_root(branch), path)
            if os.path.isfile(full):
                with open(full, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception as _exc:
            logger.debug("shadow silent-except @783: %s", _exc)
        return ""

    def _append_chain_head(self, batch_msg: str):
        """主干 audit/chain-head.jsonl 追加链头互证（复用 _record_origins 的
        P2 交付4 模式；需构造时传 audit_db_path，空则不写）。失败静默降级。"""
        if not self._audit_db_path:
            return
        try:
            from audit_chain import current_chain_head
            self._append_trunk_jsonl("audit/chain-head.jsonl", {
                "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "git_commit": self.dt.trunk.head_hash(),
                "chain_head": current_chain_head(self._audit_db_path),
                "batch": batch_msg})
            self.dt.trunk.commit("阶段4-B2: 反哺锚点登记（chain-head）")
        except Exception:
            logger.exception("反哺 chain-head 登记失败（降级）")
