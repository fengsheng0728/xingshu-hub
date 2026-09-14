"""星枢 SyncHub — ingest Mixin"""
import asyncio
import json
import hashlib
import time
import os
import sqlite3
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import numpy as np

import db_facade
from deps import CONFIG
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong

class IngestMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def ingest_chunks(self, doc_id: str, content: str,
                            source_agent_id: str = "", kind: str = "fact",
                            trust_level: str = "trusted",
                            owner_role: str = "worker") -> dict:
        """H4a 数据汇入管道：切割 → 敏感度打标 → 幂等去重 → 落库 → 三路分流。

        三路分流（附录 E）：
          NONE    → 只进审计，不进图谱/wiki（E.6：也不建向量）
          SUMMARY → 图谱节点可见(title+summary) + wiki 页面
          FULL    → 图谱 + wiki 全文
        幂等：chunk_hash 命中已存在 → 跳过（E.4 重入防护）。
        """
        from chunker import chunk_document
        from sensitivity import classify

        # 0. E.1: PII 预扫在切割前的全文跑（防跨 chunk 边界漏检）
        _full_class = classify(content, kind=kind, trust_level=trust_level,
                               owner_role=owner_role)
        now = datetime.now(timezone.utc).isoformat()

        # 1. 切割
        chunks = chunk_document(doc_id, content)

        # 2. 幂等查重 + 3. 逐 chunk 打标 + 落库（db_facade 事务，commit 由门面接管）
        def _txn(conn):
            return self._ingest_chunks_txn(conn, doc_id, chunks, _full_class,
                                           source_agent_id, trust_level, kind,
                                           owner_role, now)

        inserted, skipped, summary_levels, shadow_rows = \
            await db_facade.run_in_conn(_txn, write=True)

        # 阶段3-P1: 影子双写（chunk 落库后镜像 git 仓库群，零阻塞入队）
        try:
            if getattr(self, "_shadow", None) is not None:
                for _sr in shadow_rows:
                    _ch = _sr["ch"]
                    self._shadow.submit("wiki", {
                        "doc_id": doc_id,
                        "piece_index": _ch["piece_index"],
                        "content": _ch["content"],
                        "source_agent_id": source_agent_id or "",
                        "trust": trust_level,
                        "level": _sr["level"],
                        "date": (now or "")[:10],
                    })
        except Exception:
            pass  # 影子失败不阻塞主链路（D4）

        # 4. 分流：父文档聚合条目进 knowledge_base（图谱/wiki 消费）
        # NONE 全部 → 只审计，不建条目；有 SUMMARY/FULL chunk → 建/更父文档条目
        if summary_levels:
            await self._upsert_doc_entry(doc_id, content, source_agent_id or "", kind,
                                         max(l["level"] for l in summary_levels))
        # K2 实体抽取（附录 F v1.7）：NONE 级不送 LLM（extract_and_queue 内部铁律 1）
        # 抽取结果入 entity_review 审查队列，review 放行后才进图谱（铁律 2/3）
        if summary_levels:
            try:
                await self.extract_and_queue(
                    doc_id, content,
                    level=max(l["level"] for l in summary_levels),
                    source="llm" if (await self._llm_config()).get("api_key") else "heuristic",
                )
            except Exception as e:
                print(f"[K2] 实体抽取失败（不阻塞汇入）: {type(e).__name__}: {e}")

        # 5. 审计
        await self._log_event("chunks_ingested", source_agent_id or doc_id, {
            "doc_id": doc_id, "chunks": len(chunks), "inserted": inserted,
            "skipped_hash": skipped,
            "locked_none": len(chunks) - len(summary_levels),
            "pii_locked": bool(_full_class.get("pii_hits")),
        })

        return {
            "status": "ingested",
            "doc_id": doc_id,
            "chunks": len(chunks),
            "inserted": inserted,
            "skipped_hash": skipped,
            "locked_none": len(chunks) - len(summary_levels),
            "locked": _full_class.get("locked", False),
            "disclosure_level": _full_class["level"],
        }


    def _ingest_chunks_txn(self, conn, doc_id, chunks, _full_class,
                           source_agent_id, trust_level, kind, owner_role, now):
        """ingest_chunks 的落库事务体（db_facade.run_in_conn 回调，同步）。

        返回 (inserted, skipped, summary_levels, shadow_rows)；commit 由门面 write=True 接管。
        """
        from chunker import dedupe_by_hash
        from sensitivity import chunk_level

        # 2. 幂等：查已存在的 chunk_hash
        c = conn.cursor()
        c.execute("SELECT chunk_hash FROM document_chunks WHERE parent_doc_id = ?", (doc_id,))
        existing = {r[0] for r in c.fetchall()}
        fresh = dedupe_by_hash(chunks, existing)

        # 3. 逐 chunk 打标 + 落库
        inserted = 0
        skipped = len(chunks) - len(fresh)
        summary_levels = []
        shadow_rows = []  # 阶段3-P1: 影子双写收集（commit 后统一提交）
        for ch in fresh:
            _lv = chunk_level(ch["content"], ch["chunk_hash"],
                              parent_level=_full_class["level"],
                              trust_level=trust_level, kind=kind, owner_role=owner_role)
            level = _lv["level"]
            shadow_rows.append({"ch": ch, "level": level})
            summary = ch["content"][:200] + "..." if len(ch["content"]) > 200 else ch["content"]
            c.execute(
                """INSERT OR REPLACE INTO document_chunks
                   (chunk_id, parent_doc_id, piece_index, content, summary,
                    source_agent_id, trust_level, tainted_at, disclosure_level,
                    sensitivity_score, chunk_hash, kind, pii_hits, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"{doc_id}-c{ch['piece_index']}", doc_id, ch["piece_index"],
                 ch["content"], summary,
                 source_agent_id or self.agents.get("_ingest", {}).get("agent_id", ""),
                 trust_level,
                 now if trust_level == "untrusted" else "",
                 level, _lv["sensitivity_score"], ch["chunk_hash"], kind,
                 json.dumps(_lv.get("pii_hits", [])), now, now),
            )
            inserted += 1
            if level != "none":
                summary_levels.append({"piece_index": ch["piece_index"], "level": level})
        return inserted, skipped, summary_levels, shadow_rows


    async def reclassify_chunks(self, doc_id: str = "", requester: str = "") -> dict:
        """H4b E.7 级别变更重判定：重跑分类器，级别变化入审计 + 图谱/wiki 同步。

        触发：机密词库更新 / trust_level 升级 / 父文档级别调整后手动或定时调用。
        规则/词库变更 → 受影响 chunk 重跑 sensitivity → 级别变化：
          - 降级(NONE): 从 knowledge_base 摘除父文档条目（图谱/wiki 同步消失）
          - 升级(summary/full): 补发父文档条目
        """
        def _txn(conn):
            return self._reclassify_chunks_txn(conn, doc_id)

        changed = await db_facade.run_in_conn(_txn, write=True)

        # 图谱/wiki 同步（摘除/补发父文档条目）——覆盖本次重判的全部 doc，
        # 不只级别变化的（E.7：历史残留条目也要清理，如早期无 PII 检测时建的）
        affected_docs = {chg["doc_id"] for chg in changed}
        if doc_id:
            affected_docs.add(doc_id)
        else:
            _doc_rows = await db_facade.query("SELECT DISTINCT parent_doc_id FROM document_chunks")
            affected_docs.update(r[0] for r in _doc_rows)
        for d in affected_docs:
            _lv_rows = await db_facade.query("SELECT disclosure_level FROM document_chunks WHERE parent_doc_id = ?", (d,))
            levels = [r[0] for r in _lv_rows]
            has_visible = any(lv != "none" for lv in levels)
            if not has_visible:
                # 全部 NONE → 摘除图谱条目
                await db_facade.execute("DELETE FROM knowledge_base WHERE entry_id = ?", (f"doc:{d}",))
            else:
                # 有可见 chunk → 确保条目存在（补发）
                row = await db_facade.query_one("SELECT content FROM document_chunks WHERE parent_doc_id = ? AND disclosure_level != 'none' LIMIT 1", (d,))
                if row:
                    await self._upsert_doc_entry(d, row["content"] or "", "", "fact",
                                                 "full" if "full" in levels else "summary")

        # 审计
        await self._log_event("chunks_reclassified", requester or "", {
            "doc_id": doc_id or "*", "changed": len(changed),
            "details": changed[:50],
        })
        return {"status": "ok", "changed": len(changed), "details": changed[:50]}


    def _reclassify_chunks_txn(self, conn, doc_id):
        """reclassify_chunks 的重判定事务体（db_facade.run_in_conn 回调，同步）。

        返回 changed 列表；commit 由门面 write=True 接管（原第一处提交点位置不变）。
        """
        from sensitivity import chunk_level
        c = conn.cursor()

        if doc_id:
            c.execute("SELECT * FROM document_chunks WHERE parent_doc_id = ?", (doc_id,))
        else:
            c.execute("SELECT * FROM document_chunks")
        rows = c.fetchall()

        changed = []
        for row in rows:
            ch = dict(row)
            _lv = chunk_level(ch["content"], ch["chunk_hash"],
                              parent_level="summary",
                              trust_level=ch.get("trust_level") or "trusted",
                              kind=ch.get("kind") or "fact",
                              owner_role="worker")
            new_level = _lv["level"]
            old_level = ch.get("disclosure_level") or "summary"
            if new_level != old_level:
                c.execute(
                    "UPDATE document_chunks SET disclosure_level = ?, sensitivity_score = ?, updated_at = ? WHERE chunk_id = ?",
                    (new_level, _lv["sensitivity_score"],
                     datetime.now(timezone.utc).isoformat(), ch["chunk_id"]),
                )
                changed.append({
                    "chunk_id": ch["chunk_id"], "doc_id": ch["parent_doc_id"],
                    "old": old_level, "new": new_level,
                })
        return changed


    def _rebuild_embeddings_txn(self, conn, model, target_dim, batch_size):
        """rebuild_embeddings 的落库事务体（db_facade.run_in_conn 回调，同步）。

        D-11（2026-09-14）: 原为 rebuild_embeddings 内联 `with self._db() as conn:` 块，逐字搬出；
        fn 内保留原 rollback/commit 语义，故外层用 run_in_conn(write=False)。
        返回 (stale, rebuilt)；中途失败返回 error dict。
        """
        import numpy as _np
        c = conn.cursor()

        # 找出 stale 行（embedding 为空 或 维度不匹配）
        c.execute("SELECT memory_id, content FROM memory_pool")
        mem_rows = c.fetchall()
        stale = []
        for row in mem_rows:
            r2 = conn.execute(
                "SELECT embedding FROM memory_pool WHERE memory_id = ?", (row["memory_id"],)).fetchone()
            blob = r2[0] if r2 else None
            if blob is None:
                stale.append(row)
                continue
            try:
                dim = len(_np.frombuffer(blob, dtype=_np.float32))
                if dim != target_dim:
                    stale.append(row)
            except Exception:
                stale.append(row)

        # 批处理重建（E.7 同款：低峰批量，stale 标记语义）
        rebuilt = 0
        try:
            for i in range(0, len(stale), batch_size):
                batch = stale[i:i + batch_size]
                texts = [r["content"] for r in batch]
                # 用 __call__（兼容 batch；LocalEmbedding.encode 只接受单条 str）
                vecs = model(texts)
                for j, row in enumerate(batch):
                    vec = _np.asarray(vecs[j], dtype=_np.float32)
                    c.execute(
                        "UPDATE memory_pool SET embedding = ?, updated_at = ? WHERE memory_id = ?",
                        (vec.tobytes(), datetime.now(timezone.utc).isoformat(), row["memory_id"]))
                rebuilt += len(batch)
        except Exception as e:
            conn.rollback()
            return {"status": "error", "detail": f"重建中断: {e}", "rebuilt_mem": rebuilt}

        conn.commit()
        return stale, rebuilt


    async def rebuild_embeddings(self, requester: str = "", batch_size: int = 100) -> dict:
        """K1b 旧向量作废 + 全量重建（附录 F 2026-08-06）。

        换 embedding 模型后维度变化（384→512 等）→ 旧向量全部作废：
        1. 全量重跑当前 provider 编码（memory_pool 的 embedding blob）
        2. 更新 blob + ChromaDB 重建
        3. 重建期间检索降级：_ensure_embedding_model 加载失败返回 None
           → semantic_search 自动走 ILIKE 降级链（现有三级降级复用）
        4. 审计 rebuild 事件

        幂等：重复调用只处理 embedding 为空或维度不匹配的行（stale 标记语义）。
        document_chunks 不存 embedding blob（H1 表无此列）→ 不参与重建。
        """
        from db import get_embedding_provider
        import numpy as _np

        provider = CONFIG.EMBEDDING_PROVIDER or "hasher"
        try:
            if provider == "sentence":
                model = get_embedding_provider("sentence", model_path=CONFIG.EMBEDDING_MODEL_PATH)
            else:
                model = get_embedding_provider("hasher", n_features=384)
        except Exception as e:
            return {"status": "error", "detail": f"模型加载失败: {e}",
                    "hint": "检索将保持 ILIKE 降级"}

        try:
            probe = model.encode("维度探测")
            target_dim = int(len(probe))
        except Exception:
            target_dim = 0

        # 主数据重建事务（db_facade；fn 内保留原 rollback/commit 语义 → write=False）
        def _txn(conn):
            return self._rebuild_embeddings_txn(conn, model, target_dim, batch_size)

        _txn_result = await db_facade.run_in_conn(_txn, write=False)
        if isinstance(_txn_result, dict):
            return _txn_result
        stale, rebuilt = _txn_result

        # ChromaDB 重建（清空重建，用新向量重灌）
        try:
            if self._chroma_collection is not None and rebuilt > 0:
                # delete 需要非空 where（chromadb 限制：空 where 抛 ValueError）
                self._chroma_collection.delete(where={"owner": {"$ne": "__none__"}})
                # D-11: 门面查询（原 _db() 直连；row_factory=Row 语义一致）
                rows = await db_facade.query(
                    "SELECT memory_id, owner_agent_id, memory_key, content, summary, importance, kind, confidence, source_type, tags, embedding FROM memory_pool WHERE embedding IS NOT NULL")
                batch_ids, batch_embs, batch_meta = [], [], []
                for row in rows:
                    batch_ids.append(row["memory_id"])
                    batch_embs.append(_np.frombuffer(row["embedding"], dtype=_np.float32).tolist())
                    # ChromaDB metadatas 不允许 None（None 值转 MetadataValue 失败）
                    batch_meta.append({
                        "owner": row["owner_agent_id"] or "",
                        "key": row["memory_key"] or "",
                        "content": (row["content"] or "")[:500],
                        "summary": row["summary"] or "",
                        "importance": float(row["importance"] or 0.0),
                        "kind": row["kind"] or "",
                        "confidence": float(row["confidence"] or 0.0),
                        "source_type": row["source_type"] or "",
                        "tags": json.dumps(json.loads(row["tags"] or "[]")),
                    })
                if batch_ids:
                    self._chroma_collection.add(ids=batch_ids, embeddings=batch_embs, metadatas=batch_meta)
        except Exception as e:
            # Chroma 重建失败不影响 SQLite 主数据（向量是冗余索引）
            print(f"[K1b] ChromaDB 重建失败（SQLite 主数据已更新）: {e}")

        # 审计
        await self._log_event("embeddings_rebuilt", requester or "", {
            "provider": provider, "target_dim": target_dim,
            "rebuilt_mem": rebuilt, "stale_total": len(stale),
        })

        return {
            "status": "ok",
            "provider": provider,
            "target_dim": target_dim,
            "rebuilt_mem": rebuilt,
            "stale_total": len(stale),
        }

    # ═══════════════════════════════════════════════════════════
    # K2 实体抽取（附录 F v1.7，2026-08-06）
    # 铁律：NONE 不送 LLM / 审查队列防幻觉 / 级别继承图谱下钻过披露
    # ═══════════════════════════════════════════════════════════


    async def _llm_config(self) -> dict:
        """读取 Hub Agent LLM 配置（hub_agent_config 表）"""
        try:
            _rows = await db_facade.query("SELECT key, value FROM hub_agent_config")
            cfg = {row[0]: row[1] for row in _rows}
            return {
                "api_key": cfg.get("api_key", ""),
                "model": cfg.get("model", "deepseek-chat"),
                "api_base": cfg.get("api_base", ""),
            }
        except Exception:
            return {"api_key": "", "model": "deepseek-chat", "api_base": ""}


    async def extract_and_queue(self, doc_id: str, content: str,
                                level: str = "summary",
                                source: str = "llm",
                                chunk_ids: list = None) -> dict:
        """K2a+b 实体抽取并入审查队列（不直接进图谱）。

        铁律 1：level=none → 不送 LLM，直接返回（敏感内容零接触）
        铁律 2：抽取结果入 entity_review（pending），review 放行后才进 knowledge_base
        铁律 3：实体带 level（继承证据 chunk 级别），图谱下钻复用 N4 过滤
        """
        from entity_extraction import extract_entities

        # 铁律 1：NONE 级不送 LLM
        if level == "none":
            return {"status": "skipped", "reason": "level=none 不送 LLM（K2 铁律 1）"}

        llm_cfg = await self._llm_config()
        result = await extract_entities(content, level=level, llm_config=llm_cfg)
        entities = result.get("entities", [])
        if not entities:
            return {"status": "no_entities", "source": result.get("source")}

        # 入审查队列（铁律 2：pending 状态，不直接污染图谱）
        # D-11: 原「一连接多 insert + 末尾 commit」→ run_in_conn 单事务（原子性不变）
        def _queue_txn(conn):
            c = conn.cursor()
            _queued = 0
            for ent in entities[:20]:
                # 铁律 3：实体级别继承证据 chunk 级别（LLM 结果统一覆盖为 chunk 级别）
                ent_level = level if level in ("full", "summary", "none") else "summary"
                c.execute(
                    """INSERT INTO review_queue
                       (item_type, doc_id, name, detail, level, status, source)
                       VALUES ('entity', ?, ?, ?, ?, 'pending', ?)""",
                    (doc_id, (ent.get("name") or "")[:100],
                     json.dumps({"type": (ent.get("type") or "other")[:30],
                                 "evidence": (ent.get("evidence") or "")[:200]},
                                ensure_ascii=False)[:400],
                     ent_level, result.get("source") or source),
                )
                _queued += 1
            return _queued

        queued = await db_facade.run_in_conn(_queue_txn, write=True)

        # 审计
        await self._log_event("entity_extracted", "", {
            "doc_id": doc_id, "queued": queued, "source": result.get("source"),
            "relations": len(result.get("relations", [])),
        })
        return {
            "status": "queued", "queued": queued,
            "source": result.get("source"),
            "relations": len(result.get("relations", [])),
        }


    async def review_entity(self, review_id: int, decision: str,
                            reviewer: str = "") -> dict:
        """K2b 审查放行/拒绝：approved → 进 knowledge_base（图谱节点）；rejected → 丢弃。

        仅 manager/orchestrator 可调用（routes 层鉴权）。
        """
        # D-11: 门面查询（原 _db() 直连；Row 语义一致）
        row = await db_facade.query_one("SELECT * FROM review_queue WHERE id = ?", (review_id,))
        if not row:
            # 兼容旧 entity_review 数据（迁移前遗留）
            row = await db_facade.query_one("SELECT * FROM entity_review WHERE id = ?", (review_id,))
        if not row:
            return {"status": "error", "detail": "review 不存在"}
        if row["status"] != "pending":
            return {"status": "error", "detail": f"已处理（{row['status']}）"}

        if decision == "approved":
            # 进 knowledge_base（图谱节点）—— entry_id 前缀 ent: 区分文档聚合条目
            entry_id = f"ent:{row['doc_id']}:{row['name']}"
            # D6: review_queue 通用表——实体类型在 detail 字段（JSON），兼容旧 entity_review 的 entity_type
            if "entity_type" in row.keys() and row["entity_type"]:
                _etype = row["entity_type"]
            else:
                try:
                    _detail = json.loads(row["detail"] or "{}")
                    _etype = _detail.get("type", "other") if isinstance(_detail, dict) else "other"
                except Exception:
                    _etype = "other"
            # D-11: 三条写语句原为一连接单事务 → run_in_conn(write=True) 保原子性
            def _approve_txn(conn):
                c = conn.cursor()
                c.execute(
                    """INSERT OR REPLACE INTO knowledge_base
                       (entry_id, title, content, tags, links, category, importance,
                        created_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (entry_id, row["name"], row["detail"] or "",
                     json.dumps([_etype]), "[]", "entity",
                     0.5, row["reviewed_by"] or reviewer,
                     datetime.now(timezone.utc).isoformat(),
                     datetime.now(timezone.utc).isoformat()),
                )
                c.execute("UPDATE review_queue SET status='approved', reviewed_at=?, reviewed_by=? WHERE id=?",
                          (datetime.now(timezone.utc).isoformat(), reviewer or "", review_id))
                c.execute("UPDATE entity_review SET status='approved', reviewed_at=?, reviewed_by=? WHERE id=?",
                          (datetime.now(timezone.utc).isoformat(), reviewer or "", review_id))

            await db_facade.run_in_conn(_approve_txn, write=True)
            # 阶段3-P1: 影子双写（直写路径，K2 实体审查通过后落库）
            try:
                if getattr(self, "_shadow", None) is not None:
                    self._shadow.submit("knowledge", {
                        "entry_id": entry_id,
                        "title": row["name"],
                        "content": row["detail"] or "",
                        "created_by": row["reviewed_by"] or reviewer or "",
                        "tags": [_etype],
                        "date": datetime.now(timezone.utc).isoformat()[:10],
                    })
            except Exception:
                pass  # 影子失败不阻塞主链路（D4）
            await self._log_event("entity_reviewed", reviewer or "", {
                "review_id": review_id, "decision": "approved", "entry_id": entry_id,
                "level": row["level"],
            })
            return {"status": "approved", "entry_id": entry_id}
        else:
            def _reject_txn(conn):
                c = conn.cursor()
                c.execute("UPDATE review_queue SET status='rejected', reviewed_at=?, reviewed_by=? WHERE id=?",
                          (datetime.now(timezone.utc).isoformat(), reviewer or "", review_id))
                c.execute("UPDATE entity_review SET status='rejected', reviewed_at=?, reviewed_by=? WHERE id=?",
                          (datetime.now(timezone.utc).isoformat(), reviewer or "", review_id))

            await db_facade.run_in_conn(_reject_txn, write=True)
            await self._log_event("entity_reviewed", reviewer or "", {
                "review_id": review_id, "decision": "rejected",
            })
            return {"status": "rejected"}


    async def list_entity_reviews(self, status: str = "pending") -> dict:
        """K2b 审查队列列表"""
        # D-11: 门面查询（原 _db() 直连；Row 语义一致）
        # D6: 通用审查队列（entity 类型）优先 + 旧 entity_review 合并（兼容）
        if status:
            _rows = await db_facade.query("SELECT * FROM review_queue WHERE item_type='entity' AND status = ? ORDER BY id DESC LIMIT 100", (status,))
        else:
            _rows = await db_facade.query("SELECT * FROM review_queue WHERE item_type='entity' ORDER BY id DESC LIMIT 100")
        rows = [dict(r) for r in _rows]
        _legacy = await db_facade.query("SELECT * FROM entity_review ORDER BY id DESC LIMIT 100")
        legacy = [dict(r) for r in _legacy]
        seen = {r["id"] for r in rows}
        for r in legacy:
            if r["id"] not in seen:
                rows.append(r)
        return {"status": "ok", "items": rows, "count": len(rows)}


