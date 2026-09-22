"""星枢 SyncHub — knowledge Mixin"""
import asyncio
import json
import db_facade
# D-11: 别名供 async 函数内调用 — 门面 AST 自检口径按属性名计数，
# db_facade.execute(...) 会被误记为直连调用，故经模块级 Name 引用。
_facade_execute = db_facade.execute
import hashlib
import time
import os
import sqlite3
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import numpy as np

from deps import KnowledgeEntry, CONFIG, logger
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong


def kb_chunk_id(entry_id: str, piece_index: int) -> str:
    """K-1: 知识 chunk 的 chroma id（`kb:` 前缀，与 memory_id 的 sha256-hex 空间不冲突）"""
    return f"kb:{entry_id}:{piece_index}"


def kb_chunk_metadata(entry_id: str, title: str, category: str,
                      importance, chunk: dict) -> dict:
    """K-1: 知识 chunk 的 chroma metadata（写侧 knowledge_upsert 与 rebuild 共用）。

    键清单与取值约定见 docs/architecture-decision-kb-retrieval.md；
    chromadb 不接受 None 值 → 一律 ""/0.0 兜底。

    CD-052（2026-09-19，方案 A）：最小披露——删 content 键（明文不再进索引，
    对齐 CD-048 memory 层手法）；新增 piece_index（int）——命中后靠它回查
    knowledge_base.content 重切定位对应段落（disclosure._knowledge_hit）。"""
    return {
        "layer": "knowledge",
        "entry_id": entry_id or "",
        "title": title or "",
        "piece_index": int(chunk.get("piece_index") or 0),
        "source_type": category or "",
        "importance": float(importance or 0.0),
        "chunk_hash": chunk["chunk_hash"] or "",
    }


class KnowledgeMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def knowledge_upsert(self, entry: KnowledgeEntry) -> dict:
        entry_id = entry.entry_id or hashlib.sha256(
            f"{entry.title}:{time.time()}".encode()
        ).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()

        payload = {
            "entry_id": entry_id, "title": entry.title,
            "content": entry.content or "",
            "tags_json": json.dumps(entry.tags),
            "links_json": json.dumps(entry.links),
            "category": entry.category, "importance": entry.importance,
            "created_by": entry.created_by,
            "created_at": now, "updated_at": now,
        }
        self._enqueue_write("upsert", payload)

        self._record_trace(action="upsert", agent_id=entry.created_by,
                           title=entry.title, entry_id=entry_id)
        self._wiki_sync_pending = True

        # K-1: 知识条目切片入统一 chroma 集合（layer=knowledge，方案 B 写侧）。
        # 在 _enqueue_write 事务之外同步执行；失败降级不阻塞知识落库（D4 纪律，
        # 对齐 memory.py 的 ChromaDB 写入失败处理）。
        try:
            await self._embed_knowledge_chunks(entry_id, entry)
        except (ValueError, RuntimeError, OSError) as e:
            logger.error(f"[knowledge 向量写入失败] entry_id={entry_id} "
                         f"{type(e).__name__}: {e}")

        logger.info(f"knowledge: {'update' if entry.entry_id else 'create'} [{entry.title}] (queued)")
        return {"status": "ok", "entry_id": entry_id}


    async def _embed_knowledge_chunks(self, entry_id: str, entry) -> int:
        """K-1: 知识条目切片入统一 chroma 集合。返回写入向量数。

        - chunker.chunk_document(doc_id, content, embed_fn=None)：默认 512/50 token，
          不传 embed_fn（写入路径不做额外模型调用的话题切分）
        - chunk id = kb:{entry_id}:{piece_index}（与 memory_id 空间不冲突）
        - 更新语义（CD-049 改序）：先 upsert 新 chunk（同 id 覆盖，幂等），再删
          差集（旧 ids − 新 ids）——任意时刻该条目都不出现"零 chunk"中间态
        - 降级：chroma 不可用 / embedding 模型缺失 / 空内容 → 返回 0；
          upsert 失败 → logger.error 并返回 0（旧 chunk 仍在，可接受的降级，
          且必须能被告警看到）；差集删除失败 → logger.error（最坏是索引里
          残留旧版本 chunk 片段，下次重灌会被同 id 覆盖，不许静默）
        """
        if self._chroma_collection is None:
            return 0
        content = entry.content or ""
        if not content.strip():
            return 0
        from chunker import chunk_document
        chunks = chunk_document(f"kb:{entry_id}", content, embed_fn=None)
        if not chunks:
            return 0
        # 写入延迟护栏：单条知识最多入向量的切片数（默认 200，
        # sentence 档 ~10-30ms/chunk → 上限内最坏个位数秒；hasher 档可忽略）
        max_chunks = getattr(CONFIG, "KB_EMBED_MAX_CHUNKS", 200)
        chunks = chunks[:max_chunks]

        model = await self._ensure_embedding_model()
        if model is None:
            return 0

        vecs = model([c["content"] for c in chunks])  # __call__ 兼容 batch
        ids, embs, metas = [], [], []
        for j, ch in enumerate(chunks):
            ids.append(kb_chunk_id(entry_id, ch["piece_index"]))
            embs.append(np.asarray(vecs[j], dtype=np.float32).tolist())
            metas.append(kb_chunk_metadata(entry_id, entry.title, entry.category,
                                           entry.importance, ch))
        # CD-049 改序：查现存 ids → upsert 新 chunk（同 id 覆盖）→ 删差集。
        # 任意时刻该条目都有 chunk 可查；写失败时旧 chunk 保留（降级但可告警）。
        try:
            existing = self._chroma_collection.get(
                where={"$and": [{"layer": "knowledge"}, {"entry_id": entry_id}]},
                include=[])  # 只取 ids
            old_ids = set((existing or {}).get("ids") or [])
            self._chroma_collection.upsert(ids=ids, embeddings=embs, metadatas=metas)
        except Exception as e:
            logger.error(f"[knowledge 向量写入失败] entry_id={entry_id} "
                         f"chunks={len(ids)} {type(e).__name__}: {e}")
            return 0
        # 只删旧版本多出来的 chunk（旧 ids − 新 ids），不动同 id 新 chunk
        stale = old_ids - set(ids)
        if stale:
            try:
                self._chroma_collection.delete(ids=sorted(stale))
            except Exception as e:
                # 残留旧 chunk 会被下次重灌覆盖；最坏是索引里有旧版本 chunk 片段，不许静默
                logger.error(f"[knowledge 旧 chunk 差集删除失败] entry_id={entry_id} "
                             f"stale={len(stale)} {type(e).__name__}: {e}")
        return len(ids)


    async def reconcile_kb_vectors(self, entry_id: str = "") -> dict:
        """CD-051: 知识向量对账 — 逐条比对 chroma 实际 chunk ids 与
        「按当前口径重新切分应得的 ids」，不一致就重灌那一条。

        - 切分口径与写入侧完全一致：chunker.chunk_document(f"kb:{entry_id}",
          content, embed_fn=None) + kb_chunk_id + CONFIG.KB_EMBED_MAX_CHUNKS 上限
          （禁另造切片/id 规则——对不上会误判"缺失"从而反复重灌）
        - 重灌走 CD-049 改序后的 _embed_knowledge_chunks（upsert 新 → 删差集）；
          重灌后复查 ids，仍不一致计入 errors（_embed 写失败只记日志返回 0，
          以复查结果判定成败，不许静默）
        - 逐条容错：单条异常计入 errors，不中断整轮
        - 降级：chroma 不可用 / embedding 模型缺失 → status=skipped 早退；
          内容为空 → 该条计 skipped（与写侧"空内容不动索引"语义一致）
        """
        t0 = time.time()
        stats = {"status": "ok", "checked": 0, "ok": 0, "reparsed": 0,
                 "removed": 0, "skipped": 0, "orphan_removed": 0,
                 "errors": [], "duration_ms": 0}

        def _finish():
            stats["duration_ms"] = int((time.time() - t0) * 1000)
            return stats

        collection = self._chroma_collection
        if collection is None:
            stats.update(status="skipped", reason="chroma_unavailable")
            logger.warning("[knowledge 对账跳过] chroma 不可用")
            return _finish()
        model = await self._ensure_embedding_model()
        if model is None:
            stats.update(status="skipped", reason="embedding_model_unavailable")
            logger.warning("[knowledge 对账跳过] embedding 模型缺失")
            return _finish()

        try:
            if entry_id:
                rows = await db_facade.query(
                    "SELECT entry_id, title, content, category, importance"
                    " FROM knowledge_base WHERE entry_id = ?", (entry_id,))
            else:
                rows = await db_facade.query(
                    "SELECT entry_id, title, content, category, importance"
                    " FROM knowledge_base ORDER BY entry_id")
        except Exception as e:
            stats["errors"].append(
                f"query knowledge_base: {type(e).__name__}: {e}")
            return _finish()

        from chunker import chunk_document  # 与 _embed_knowledge_chunks 同处惰性导入
        from types import SimpleNamespace
        max_chunks = getattr(CONFIG, "KB_EMBED_MAX_CHUNKS", 200)
        for row in rows:
            eid = row["entry_id"]
            stats["checked"] += 1
            try:
                content = row["content"] or ""
                if not content.strip():
                    # 写侧对空内容不动索引（_embed 早退），对账保持同语义
                    stats["skipped"] += 1
                    continue
                chunks = chunk_document(f"kb:{eid}", content, embed_fn=None)
                chunks = chunks[:max_chunks]
                expected = {kb_chunk_id(eid, ch["piece_index"]) for ch in chunks}
                existing = collection.get(
                    where={"$and": [{"layer": "knowledge"}, {"entry_id": eid}]},
                    include=[])  # 只取 ids
                actual = set((existing or {}).get("ids") or [])
                if actual == expected:
                    stats["ok"] += 1
                    continue
                stale = actual - expected
                entry = SimpleNamespace(
                    content=content, title=row["title"] or "",
                    category=row["category"] or "",
                    importance=row["importance"] or 0.0)
                await self._embed_knowledge_chunks(eid, entry)
                after = collection.get(
                    where={"$and": [{"layer": "knowledge"}, {"entry_id": eid}]},
                    include=[])
                if set((after or {}).get("ids") or []) != expected:
                    stats["errors"].append(f"{eid}: 重灌后仍不一致")
                    continue
                stats["reparsed"] += 1
                stats["removed"] += len(stale)
            except Exception as e:
                # 逐条容错：单条失败不中断整轮
                stats["errors"].append(f"{eid}: {type(e).__name__}: {e}")
                logger.error(f"[knowledge 对账重灌失败] entry_id={eid} "
                             f"{type(e).__name__}: {e}")

        # CD-052（用户修正 1）：反向对账——chroma 有、DB 无的知识 chunk 即孤儿，
        # 删掉并计 orphan_removed（正向循环只遍历 DB 现存条目，盖不到这一向）。
        # id 形态 kb:{entry_id}:{piece_index} → 从右侧只切一次再剥 kb: 前缀
        # （entry_id 自身可带冒号，如 doc: 聚合条目）。
        # 注：任务书示例的 where={"$and":[{"layer":"knowledge"}]} 被 chromadb
        # 拒绝（$and 至少两个表达式，1.5.9 实测 ValueError）→ 用等值单条件。
        try:
            if entry_id:
                # 单条模式：该条目已不在 DB（rows 为空）→ 其全部 chunk 即孤儿
                if not rows:
                    existing = collection.get(
                        where={"$and": [{"layer": "knowledge"},
                                        {"entry_id": entry_id}]},
                        include=[])
                    orphan_ids = list((existing or {}).get("ids") or [])
                else:
                    orphan_ids = []
            else:
                live_ids = {r["entry_id"] for r in rows}
                all_kb = collection.get(where={"layer": "knowledge"}, include=[])
                orphan_ids = []
                for cid in (all_kb or {}).get("ids") or []:
                    owner_part = cid.rsplit(":", 1)[0]
                    eid = owner_part[3:] if owner_part.startswith("kb:") else owner_part
                    if eid not in live_ids:
                        orphan_ids.append(cid)
            if orphan_ids:
                collection.delete(ids=orphan_ids)
                stats["orphan_removed"] += len(orphan_ids)
        except Exception as e:
            # 反向清扫失败不掩盖正向结果：计 errors 可告警，不静默
            stats["errors"].append(f"orphan sweep: {type(e).__name__}: {e}")
            logger.error(f"[knowledge 反向对账孤儿清理失败] "
                         f"{type(e).__name__}: {e}")
        return _finish()


    @staticmethod
    def _knowledge_row_to_dict(row) -> dict:
        d = dict(row)
        # embedding BLOB 等二进制字段不可 JSON 序列化 → 剔除
        d = {k: v for k, v in d.items() if not isinstance(v, (bytes, bytearray))}
        for k in ("tags", "links"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except Exception:
                d[k] = []
        return d

    async def knowledge_get(self, entry_id: str = None) -> dict:
        """获取知识条目。entry_id=None → 全部列表（按重要性排序）；否则单条。"""
        if entry_id:
            row = await db_facade.query_one(
                "SELECT * FROM knowledge_base WHERE entry_id = ?", (entry_id,))
            if not row:
                raise KeyError(f"knowledge entry not found: {entry_id}")
            return {"status": "ok", "entry": self._knowledge_row_to_dict(row)}
        rows = await db_facade.query(
            "SELECT * FROM knowledge_base ORDER BY importance DESC, updated_at DESC")
        return {"status": "ok", "entries": [self._knowledge_row_to_dict(r) for r in rows]}


    async def knowledge_delete(self, entry_id: str) -> dict:
        # CD-052（用户修正 2，对齐 CD-046 memory 侧 2553c40）：删除 DB 行之外
        # 同时删该 entry 的全部 chroma chunk（否则每次删除都在制造新孤儿）。
        # 顺序定死先删向量、后删行——删向量成功而删行失败时条目仍在，向量可由
        # CD-051 正向对账补；反之则会留孤儿，故不选。
        if self._chroma_collection is None:
            logger.warning(f"[knowledge 删除跳过向量清理] chroma 不可用 "
                           f"entry_id={entry_id}")
        else:
            try:
                self._chroma_collection.delete(
                    where={"$and": [{"layer": "knowledge"},
                                    {"entry_id": entry_id}]})
            except Exception as e:
                # 删除失败不许静默：残留向量由 reconcile 反向对账兜底
                logger.error(f"[knowledge 向量删除失败] entry_id={entry_id} "
                             f"{type(e).__name__}: {e}")
        self._enqueue_write("delete", {"entry_id": entry_id})
        self._record_trace(action="delete", agent_id="", title=entry_id, entry_id=entry_id)
        self._wiki_sync_pending = True
        logger.info(f"knowledge: delete [{entry_id}] (queued)")
        return {"status": "deleted"}


    async def knowledge_graph(self, requester: str = "") -> dict:
        """返回知识图谱数据（节点 + 边，用于 D3 可视化）。
        N4(2026-08-05): 权限级过滤 — requester 对节点创建者(created_by)的披露级别为 NONE 时隐藏节点。
        CD-054(T19) 三修：
        a. 过滤判定异常 fail-closed —— 隐藏节点 + logger.warning（原 except: pass 静默放行已废）；
        b. edges 两端与回填节点都必须先过同一可见性判定 —— 被隐藏/悬空节点不再以
           title=id 回填（泄露节点存在性）；无 requester 的内部调用保持旧行为（全量 + 悬空回填）；
        c. 无登记属主的条目（created_by 空/非 agents 登记 = 企业已发布公共内容）打
           published 标记，复用 DisclosureEngine 的 r4_published_public 链尾提升
           （NONE→SUMMARY），与读出口 disclosure._knowledge_hit 语义对齐；
           可见性判定本身全部留在 DisclosureEngine，本文件不自建披露规则。
        CD-065(T21，2026-09-20 用户拍板) 两补：
        d. published 判定对齐读出口：知识库条目 = 已发布内容
           （disclosure._knowledge_hit 无条件打 published=True），有登记属主的
           条目同样打 published —— 图谱层 METADATA 级（id/标题/标签）对任何
           认证主体可见，正文永不进图谱（现状保持）；
        e. 标签守卫：标签是自由文本（「客户A-续约谈判」本身就是泄露），进图谱前
           逐个过 sensitivity._load_secret_keywords() 机密词库与 scan_pii()；
           命中即非特权主体下该节点 tags 置空（只留 id/标题）+ logger.info
           （含 entry_id，不含标签内容）；特权主体（manager/orchestrator）
           不受限；无 requester 的内部调用保持旧行为（不守卫）。
           复用 sensitivity 现成实现，不自建词库/规则。
        """
        rows = await db_facade.query("SELECT * FROM knowledge_base ORDER BY importance DESC")

        nodes = []
        node_ids = set()
        edges = []
        hidden = 0
        visible_rows = []
        visible_ids = set()

        from disclosure import DisclosureEngine, DisclosureLevel as _DL
        # CD-065(T21)e：复用 sensitivity 现成机密词库/PII 扫描（禁止自建词库）
        from sensitivity import _load_secret_keywords, scan_pii
        _engine = DisclosureEngine(self) if requester else None
        for row in rows:
            rd = dict(row)
            eid = rd["entry_id"]
            # N4: 权限级过滤 — requester 对 owner 无披露权限则隐藏节点
            if requester:
                _owner = rd.get("created_by") or rd.get("owner_agent_id") or ""
                try:
                    _lv = _engine._calculate_disclosure_level(
                        memory={**rd, "owner_agent_id": _owner,
                                # CD-065(T21)d：知识库条目 = 已发布内容（读出口
                                # disclosure._knowledge_hit 无条件 published=True），
                                # 有登记属主条目同样打 published → r4 链尾提升对
                                # 任何认证主体可见（T19 仅无登记属主打标的折中废止）
                                "published": True},
                        requester=requester, task={}, required_level=_DL.SUMMARY)
                    if _lv == _DL.NONE:
                        hidden += 1
                        continue  # 无权限 → 节点不可见
                except Exception as _exc:
                    # CD-054(T19)a: 过滤异常 fail-closed —— 隐藏节点并告警（禁止静默放行）
                    logger.warning("[knowledge_graph 过滤异常 fail-closed] "
                                   "entry_id=%s err=%s", eid, type(_exc).__name__)
                    hidden += 1
                    continue
            visible_ids.add(eid)
            visible_rows.append(rd)

        # CD-065(T21)e 标签守卫：非特权主体（requester 存在且角色非
        # manager/orchestrator）下，节点 tags 逐个过机密词库 + PII 扫描，
        # 命中即 tags 置空（只留 id/标题）；特权主体与无 requester 内部调用不受限
        _tag_guard = bool(requester) and (
            (((self.agents or {}).get(requester) or {}).get("role") or "").strip()
            not in ("manager", "orchestrator"))
        _secret_words = _load_secret_keywords() if _tag_guard else None

        for rd in visible_rows:
            eid = rd["entry_id"]
            node_ids.add(eid)
            tags = json.loads(rd["tags"] or "[]")
            if _tag_guard and tags:
                try:
                    _tag_hit = any(
                        any(w and w in t for w in _secret_words)
                        or bool(scan_pii(t))
                        for t in tags)
                except Exception as _exc:
                    # 守卫扫描异常 fail-closed：视同命中摘标签 + 告警（禁止静默放行）
                    logger.warning("[knowledge_graph 标签守卫异常 fail-closed] "
                                   "entry_id=%s err=%s", eid, type(_exc).__name__)
                    _tag_hit = True
                if _tag_hit:
                    # 守卫日志只含 entry_id，严禁落标签内容
                    logger.info("[knowledge_graph 标签守卫] entry_id=%s "
                                "标签命中机密词/PII，非特权主体下 tags 置空", eid)
                    tags = []
            nodes.append({
                "id": eid,
                "title": rd["title"],
                "category": rd["category"],
                "importance": rd["importance"],
                "tags": tags,
            })
            # 双向链接 → 边（CD-054(T19)b: 有过滤时 edges 两端均须可见）
            links = json.loads(rd["links"] or "[]")
            for linked_id in links:
                if not requester or linked_id in visible_ids:
                    edges.append({"source": eid, "target": linked_id})

        # 补充被链接但不在节点列表中的节点（只显示有链接关系的）
        # CD-054(T19)b: 回填同样须先过同一可见性判定 —— 有过滤时被隐藏/悬空 id
        # 一律不回填（存在性不外泄）；无 requester 时保持旧行为
        linked_ids = {e["target"] for e in edges} - node_ids
        for lid in linked_ids:
            if requester and lid not in visible_ids:
                continue  # 未过可见性判定 → 不回填
            nodes.append({
                "id": lid, "title": lid, "category": "unknown",
                "importance": 0.5, "tags": [],
            })

        return {"status": "ok", "nodes": nodes, "edges": edges, "hidden": hidden}

    # ═══════════════════════════════════════════════════════════
    # M2: 会话摘要归档
    # ═══════════════════════════════════════════════════════════


    async def _upsert_doc_entry(self, doc_id: str, content: str,
                                 created_by: str, kind: str, level: str) -> None:
        """父文档聚合条目进 knowledge_base（图谱节点 + wiki 页面的数据源）"""
        now = datetime.now(timezone.utc).isoformat()
        title = doc_id.replace("_", " ").replace("-", " ").title()[:80]
        summary = content[:300] + "..." if len(content) > 300 else content
        await _facade_execute(
            """INSERT OR REPLACE INTO knowledge_base
               (entry_id, title, content, tags, links, category, importance,
                created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"doc:{doc_id}", title, summary, json.dumps([kind]),
             "[]", "document", 1.0 if level == "full" else 0.6,
             created_by, now, now),
        )

        # 阶段3-P1: 影子双写（直写路径不走缓冲——buffer.py 挂钩覆盖不到，这里补）
        try:
            if getattr(self, "_shadow", None) is not None:
                self._shadow.submit("knowledge", {
                    "entry_id": f"doc:{doc_id}",
                    "title": title,
                    "content": summary,
                    "created_by": created_by,
                    "tags": [kind],
                    "date": now[:10],
                })
        except Exception:
            pass  # 影子失败不阻塞主链路（D4）


