"""星枢 SyncHub — memory Mixin"""
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

from deps import DisclosureLevel, MemoryEntry, SemanticSearchRequest, logger
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong
import db_facade

class MemoryMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    async def store_memory(self, agent_id: str, memory: MemoryEntry) -> dict:
        """
        写入记忆池。M3 增强：三段去重 + 防投毒 + 审计 JSONL。

        去重语义：
          > 0.90  重复 → 合并，不新增行
          0.75~0.90 冲突候选 → 覆盖 + audit（穷人版版本历史）
          < 0.75  新事实 → 新增行

        防投毒：source_type='tool' → confidence=0.3，不进长期池
        """
        from audit.memory_audit import audit_memory

        now = datetime.now(timezone.utc).isoformat()
        kind = memory.kind or "fact"
        source_type = memory.source_type or "user"
        source_session_id = memory.source_session_id or ""
        # S3 taint: trust_level（显式传值或按来源推断）
        trust_level = memory.trust_level or self._trust_from_source(source_type)
        confidence = memory.confidence if memory.confidence is not None else (
            0.3 if source_type == "tool" else 1.0
        )

        # H2 敏感度打标（附录 E v1.4）：写入时判定，命中 PII/UNTRUSTED → 强制 NONE（locked）
        # 读取时由 8 规则链 r8 消费 → min(请求方判定, 存储级别)，杜绝两套判定漂移（E.2）
        _locked = False
        _sens_reasons = []
        _pii_masked = []
        try:
            from sensitivity import classify
            _sens = classify(
                memory.content,
                kind=kind,
                trust_level=trust_level,
                owner_role=(self.agents.get(agent_id, {}) or {}).get("role", "worker"),
            )
            if _sens["locked"] or _sens["level"] == "none":
                # NONE 按值构造（DisclosureLevel 是 str Enum，"none" 合法）
                memory.disclosure_level = type(memory.disclosure_level)("none") \
                    if hasattr(memory.disclosure_level, "value") else None
                _locked = True
                _pii_masked = _sens.get("pii_hits", [])
            _sens_reasons = _sens["reasons"]
            # E.1：PII 命中记审计——只记掩码样本（类型+位置+掩码串），原始串零落库
            if _pii_masked:
                await self._log_event("memory_locked_pii", agent_id, {
                    "memory_key": memory.memory_key,
                    "pii_hits": _pii_masked,  # 全掩码样本，无原始串
                    "level": "none",
                })
        except Exception as _e:
            logger.warning(f"[H2] 敏感度打标异常（不阻塞写入）: {type(_e).__name__}: {_e}")

        async with self._memory_lock:
            # 1. 生成 embedding（E.6：NONE 级不建向量——防语义检索绕过披露直接送内容）
            embedding_blob = None
            chroma_embedding = None
            embedding_array = None
            if _locked:
                logger.info(f"[H2/E.6] 敏感内容锁定 NONE，跳过 embedding: agent={agent_id} key={memory.memory_key}")
            else:
                try:
                    model = await self._ensure_embedding_model()
                    if model is not None:
                        loop = asyncio.get_event_loop()
                        emb = await asyncio.wait_for(
                            loop.run_in_executor(
                                None, lambda: model.encode(memory.content).tolist()
                            ),
                            timeout=30,
                        )
                        chroma_embedding = emb
                        embedding_array = np.array(emb, dtype=np.float32)
                        embedding_blob = embedding_array.tobytes()
                except asyncio.TimeoutError:
                    logger.warning(f"[Embedding 生成超时] agent={agent_id} key={memory.memory_key}")
                except (RuntimeError, ValueError, MemoryError) as e:
                    logger.warning(f"[Embedding 生成失败] {type(e).__name__}: {e}")
                except Exception as e:
                    logger.exception(f"[Embedding 生成异常] agent={agent_id}")

            # 2. 去重：查同 owner 的所有 embedding，计算 cosine similarity
            def _txn(conn):
                c = conn.cursor()

                # 先检查同 memory_key 的硬冲突
                c.execute(
                    "SELECT memory_id, content, confidence, trust_level FROM memory_pool "
                    "WHERE memory_key = ? AND owner_agent_id = ?",
                    (memory.memory_key, agent_id),
                )
                key_conflict = c.fetchone()

                action = "write"
                if key_conflict is not None:
                    # 同 key 直接覆盖
                    old_content = key_conflict[1]
                    old_confidence = key_conflict[2]
                    action = "conflict_overwrite"
                    c.execute(
                        """UPDATE memory_pool SET content=?, summary=?, confidence=?,
                           kind=?, source_type=?, source_session_id=?, updated_at=?,
                           embedding=?, importance=?, tags=?, disclosure_level=?,
                           disclosure_scope=?, allowed_viewers=?, trust_level=?
                           WHERE memory_key=? AND owner_agent_id=?""",
                        (memory.content, memory.summary or memory.content[:200],
                         max(confidence, old_confidence or 1.0),
                         kind, source_type, source_session_id, now,
                         embedding_blob, memory.importance,
                         json.dumps(memory.tags),
                         memory.disclosure_level.value, memory.disclosure_scope.value,
                         json.dumps(memory.allowed_viewers),
                         self._merge_trust(key_conflict[3] or "internal", trust_level),
                         memory.memory_key, agent_id),
                    )
                    memory_id = key_conflict[0]
                    # 归档旧版本到 memory_versions（回滚支持）
                    c.execute(
                        """INSERT INTO memory_versions (memory_id, memory_key, version, content, summary, confidence, archived_by)
                           SELECT ?, ?, COALESCE((SELECT MAX(version) FROM memory_versions WHERE memory_key=?),0)+1, ?, ?, ?, ?""",
                        (memory_id, memory.memory_key, memory.memory_key,
                         old_content, memory.summary or old_content[:200],
                         old_confidence, source_type))
                    # 审计：旧值入 audit
                    audit_memory(action="conflict_overwrite", agent_id=agent_id,
                        memory_key=memory.memory_key, memory_id=memory_id,
                        old_content=old_content[:200], new_content=memory.content[:200],
                        session_id=source_session_id, actor=source_type)

                elif embedding_array is not None:
                    # 三段去重：查所有同 owner 的记忆 embedding
                    c.execute(
                        "SELECT memory_id, memory_key, content, confidence, embedding, trust_level "
                        "FROM memory_pool WHERE owner_agent_id = ? AND embedding IS NOT NULL",
                        (agent_id,),
                    )
                    rows = c.fetchall()

                    best_sim = 0.0
                    best_row = None
                    for row in rows:
                        if row[4] is None:
                            continue
                        try:
                            existing_emb = np.frombuffer(row[4], dtype=np.float32)
                            if len(existing_emb) != len(embedding_array):
                                continue
                            sim = float(np.dot(embedding_array, existing_emb) /
                                       (np.linalg.norm(embedding_array) * np.linalg.norm(existing_emb) + 1e-10))
                            if sim > best_sim:
                                best_sim = sim
                                best_row = row
                        except Exception:
                            continue

                    if best_sim > 0.90 and best_row is not None:
                        # 重复 → 合并：保留原 content，刷新 meta
                        c.execute(
                            """UPDATE memory_pool SET confidence=MAX(confidence, ?),
                               updated_at=?, last_accessed=?, access_count=access_count+1,
                               trust_level=?
                               WHERE memory_id=?""",
                            (confidence, now, now, best_row[0],
                             self._merge_trust(best_row[5] or "internal", trust_level)),
                        )
                        memory_id = best_row[0]
                        action = "merge"
                        audit_memory(action="merge", agent_id=agent_id,
                            memory_key=best_row[1], memory_id=best_row[0],
                            confidence=confidence, session_id=source_session_id,
                            similarity=round(best_sim, 4), actor=source_type)

                    elif 0.75 <= best_sim <= 0.90 and best_row is not None:
                        # 冲突候选 → 覆盖旧值
                        old_content = best_row[2]
                        c.execute(
                            """UPDATE memory_pool SET content=?, summary=?, confidence=?,
                               kind=?, source_type=?, source_session_id=?, updated_at=?,
                               embedding=?, importance=?, tags=?, disclosure_level=?,
                               disclosure_scope=?, allowed_viewers=?, trust_level=?
                               WHERE memory_id=?""",
                            (memory.content, memory.summary or memory.content[:200],
                             max(confidence, best_row[3] or 1.0),
                             kind, source_type, source_session_id, now,
                             embedding_blob, memory.importance,
                             json.dumps(memory.tags),
                             memory.disclosure_level.value, memory.disclosure_scope.value,
                             json.dumps(memory.allowed_viewers),
                             self._merge_trust(best_row[5] or "internal", trust_level),
                             best_row[0]),
                        )
                        memory_id = best_row[0]
                        action = "conflict_overwrite"
                        audit_memory(action="conflict_overwrite", agent_id=agent_id,
                            memory_key=best_row[1], memory_id=best_row[0],
                            old_content=old_content[:200], new_content=memory.content[:200],
                            similarity=round(best_sim, 4),
                            session_id=source_session_id, actor=source_type)

                    else:
                        # < 0.75 或无匹配 → 新事实
                        action, memory_id = self._insert_new_memory_sync(
                            c, agent_id, memory, kind, source_type, source_session_id,
                            confidence, embedding_blob, chroma_embedding, now, trust_level)
                        audit_memory(action="write", agent_id=agent_id,
                            memory_key=memory.memory_key, memory_id=memory_id,
                            confidence=confidence, source_type=source_type,
                            session_id=source_session_id, actor=source_type)
                else:
                    # 无 embedding → 直接新增
                    action, memory_id = self._insert_new_memory_sync(
                        c, agent_id, memory, kind, source_type, source_session_id,
                        confidence, embedding_blob, chroma_embedding, now, trust_level)
                    audit_memory(action="write", agent_id=agent_id,
                        memory_key=memory.memory_key, memory_id=memory_id,
                        confidence=confidence, source_type=source_type,
                        session_id=source_session_id, actor=source_type)

                conn.commit()
                return action, memory_id

            action, memory_id = await db_facade.run_in_conn(_txn, write=False)

            # 阶段3-P1: 影子双写（SQLite 落库后镜像 git 仓库群，零阻塞入队）
            try:
                if self._shadow is not None:
                    self._shadow.submit("memory", {
                        "memory_id": memory_id,
                        "owner": agent_id,
                        "memory_key": memory.memory_key,
                        "content": memory.content,
                        "trust": trust_level,
                        "level": memory.disclosure_level.value if not _locked else "none",
                        "tags": list(memory.tags),
                        "date": now[:10],
                    })
            except Exception:
                pass  # 影子失败不阻塞主链路（D4）

            # 安全告警：投毒尝试
            if source_type == "tool":
                await self._log_event("memory_poisoning_attempt", agent_id, {
                    "memory_key": memory.memory_key,
                    "source_session_id": source_session_id,
                    "content_snippet": memory.content[:100],
                })

            # FTS5 同步
            try:
                await db_facade.execute(
                    "INSERT INTO memory_pool_fts(memory_pool_fts, rowid, content, summary, tags) "
                    "VALUES('delete', (SELECT rowid FROM memory_pool WHERE memory_id=?), ?, ?, ?)",
                    (memory_id, memory.content, memory.summary or "", json.dumps(memory.tags)),
                )  # FTS 写入在首个 commit 之后，需单独提交否则 close 回滚丢失（门面单语句写自动 commit）
            except Exception:
                pass  # FTS5 写入失败不阻塞

            return {
                "status": "stored",
                "memory_id": memory_id,
                "action": action,
                "confidence": confidence,
                "source_type": source_type,
                # H2 locked 回执（E.1）：已接收但已锁定——PII/UNTRUSTED 内容强制 NONE，
                # 不静默丢弃，写入方明确感知
                "locked": _locked,
                "disclosure_level": memory.disclosure_level.value if _locked else None,
                "sensitivity": _sens_reasons if _locked else None,
            }


    async def _insert_new_memory(self, c, agent_id, memory, kind, source_type,
                                  source_session_id, confidence, embedding_blob,
                                  chroma_embedding, now, trust_level="internal"):
        """插入新记忆（公共逻辑）— 兼容存量 asyncio.run 调用方的异步包装"""
        return self._insert_new_memory_sync(
            c, agent_id, memory, kind, source_type, source_session_id,
            confidence, embedding_blob, chroma_embedding, now, trust_level)


    def _insert_new_memory_sync(self, c, agent_id, memory, kind, source_type,
                                 source_session_id, confidence, embedding_blob,
                                 chroma_embedding, now, trust_level="internal"):
        """插入新记忆（公共逻辑）"""
        import hashlib as _hashlib
        memory_id = _hashlib.sha256(
            f"{agent_id}:{memory.memory_key}:{time.time()}".encode()
        ).hexdigest()[:20]

        summary = memory.summary or memory.content[:200] + "..."

        c.execute(
            """INSERT INTO memory_pool
            (memory_id, owner_agent_id, memory_key, content, summary, embedding,
             importance, tags, kind, source_session_id, confidence, source_type,
             disclosure_level, disclosure_scope, allowed_viewers, created_at, updated_at,
             trust_level, source_agent_id, tainted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, agent_id, memory.memory_key, memory.content, summary,
             embedding_blob, memory.importance, json.dumps(memory.tags),
             kind, source_session_id, confidence, source_type,
             memory.disclosure_level.value, memory.disclosure_scope.value,
             json.dumps(memory.allowed_viewers), now, now,
             trust_level, agent_id, now),
        )

        # ChromaDB 同步
        if chroma_embedding is not None and self._chroma_collection is not None:
            try:
                self._chroma_collection.add(
                    ids=[memory_id],
                    embeddings=[chroma_embedding],
                    metadatas=[{
                        "owner": agent_id, "key": memory.memory_key,
                        "tags": json.dumps(memory.tags),
                        "content": memory.content[:500], "summary": summary,
                        "importance": memory.importance, "kind": kind,
                        "confidence": confidence, "source_type": source_type,
                    }],
                )
            except (ValueError, RuntimeError, OSError) as e:
                logger.warning(f"[ChromaDB 写入失败] {type(e).__name__}: {e}")

        return "write", memory_id


    async def delete_memory(self, memory_key: str, agent_id: str) -> dict:
        """删除 Agent 自己的记忆（仅允许删除自己的）+ M3 审计"""
        from audit.memory_audit import audit_memory

        def _txn(conn):
            c = conn.cursor()
            # 删除前先读旧值（审计用）
            c.execute(
                "SELECT memory_id, content FROM memory_pool WHERE memory_key = ? AND owner_agent_id = ?",
                (memory_key, agent_id),
            )
            old = c.fetchone()

            c.execute(
                "DELETE FROM memory_pool WHERE memory_key = ? AND owner_agent_id = ?",
                (memory_key, agent_id),
            )
            return old, c.rowcount
        old, deleted = await db_facade.run_in_conn(_txn, write=True)
        if deleted > 0:
            await self._log_event("memory_deleted", agent_id, {"memory_key": memory_key})
            if old:
                audit_memory(action="delete", agent_id=agent_id,
                    memory_key=memory_key, memory_id=old[0],
                    old_content=(old[1] or "")[:200], actor="user")
            return {"status": "deleted", "memory_key": memory_key}
        return {"status": "not_found", "detail": f"记忆 {memory_key} 不存在或无权删除"}


    async def get_memory_versions(self, memory_key: str, agent_id: str) -> dict:
        """获取记忆版本历史"""
        rows = await db_facade.query(
            """SELECT v.id, v.version, v.content, v.summary, v.confidence,
                      v.archived_at, v.archived_by
               FROM memory_versions v
               WHERE v.memory_key = ?
               ORDER BY v.version DESC LIMIT 20""",
            (memory_key,))
        return {"memory_key": memory_key, "versions": [{
            "id": r[0], "version": r[1], "content": r[2],
            "summary": r[3], "confidence": r[4],
            "archived_at": r[5], "archived_by": r[6]
        } for r in rows]}


    async def rollback_memory(self, memory_key: str, version_id: int, agent_id: str) -> dict:
        """回滚记忆到指定历史版本"""
        def _txn(conn):
            c = conn.cursor()
            c.execute(
                "SELECT content, summary, confidence FROM memory_versions WHERE id=? AND memory_key=?",
                (version_id, memory_key))
            ver = c.fetchone()
            if not ver:
                return None

            # 先归档当前版本
            c.execute(
                "SELECT memory_id, content, summary, confidence FROM memory_pool WHERE memory_key=? AND owner_agent_id=?",
                (memory_key, agent_id))
            cur = c.fetchone()
            if cur:
                c.execute(
                    """INSERT INTO memory_versions (memory_id, memory_key, version, content, summary, confidence, archived_by)
                       SELECT ?, ?, COALESCE((SELECT MAX(version) FROM memory_versions WHERE memory_key=?),0)+1, ?, ?, ?, 'rollback'""",
                    (cur[0], memory_key, memory_key, cur[1], cur[2], cur[3]))

            # 恢复到目标版本
            c.execute(
                "UPDATE memory_pool SET content=?, summary=?, confidence=?, updated_at=datetime('now') WHERE memory_key=? AND owner_agent_id=?",
                (ver[0], ver[1], ver[2], memory_key, agent_id))
            return ver
        ver = await db_facade.run_in_conn(_txn, write=True)
        if ver is None:
            return {"status": "not_found", "detail": "版本不存在"}
        await self._log_event("memory_rollback", agent_id,
                              {"memory_key": memory_key, "to_version_id": version_id})
        return {"status": "rolled_back", "memory_key": memory_key, "version_id": version_id}

    # ============ M3: Memory Pool 读路径 ============


    async def memory_search(self, req) -> dict:
        """M3: 语义检索 + FTS5 降级"""
        from audit.memory_audit import audit_memory

        kinds = list(req.kind or ["fact"])
        placeholders = ",".join("?" * len(kinds))
        results = []
        embedding_unavailable = False

        # 尝试 embedding 检索
        try:
            model = await self._ensure_embedding_model()
        except Exception:
            model = None

        if model is not None:
            def _emb_search(conn):
                c = conn.cursor()
                c.execute(
                    f"""SELECT memory_id, memory_key, content, summary, confidence,
                       source_type, kind, access_count, tags,
                       owner_agent_id, disclosure_level
                       FROM memory_pool
                       WHERE owner_agent_id = ? AND kind IN ({placeholders})
                       AND confidence >= ? AND embedding IS NOT NULL""",
                    (req.agent_id, *kinds, req.min_confidence),
                )
                rows = c.fetchall()

                scored = []
                for row in rows:
                    c2 = conn.cursor()
                    c2.execute("SELECT embedding FROM memory_pool WHERE memory_id=?", (row[0],))
                    emb_row = c2.fetchone()
                    if emb_row and emb_row[0]:
                        try:
                            existing = np.frombuffer(emb_row[0], dtype=np.float32)
                            if len(existing) == len(query_emb):
                                sim = float(np.dot(query_emb, existing) /
                                           (np.linalg.norm(query_emb) * np.linalg.norm(existing) + 1e-10))
                                scored.append((sim, row))
                        except Exception:
                            continue

                scored.sort(key=lambda x: x[0], reverse=True)
                for sim, row in scored[:req.top_k]:
                    results.append({
                        "memory_id": row[0], "memory_key": row[1],
                        "content": row[2], "summary": row[3],
                        "confidence": row[4], "source_type": row[5],
                        "kind": row[6], "access_count": row[7],
                        "score": round(sim, 4),
                        "owner_agent_id": row[9], "disclosure_level": row[10],
                    })
            try:
                loop = asyncio.get_event_loop()
                query_emb = np.array(
                    await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: model.encode(req.query).tolist()),
                        timeout=10,
                    ),
                    dtype=np.float32,
                )

                await db_facade.run_in_conn(_emb_search, write=False)
            except (asyncio.TimeoutError, RuntimeError, ValueError) as e:
                logger.warning(f"[Embedding 检索失败] {type(e).__name__}, 降级到 FTS5")
                embedding_unavailable = True
        else:
            embedding_unavailable = True

        # FTS5 降级
        if embedding_unavailable or not results:
            try:
                # 简单分词：按空格/标点拆分
                import re
                tokens = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', req.query)
                fts_query = " OR ".join(tokens[:10])

                rows = await db_facade.query(
                    f"""SELECT mp.memory_id, mp.memory_key, mp.content, mp.summary,
                       mp.confidence, mp.source_type, mp.kind, mp.access_count,
                       mp.tags, mp.embedding
                       FROM memory_pool mp
                       LEFT JOIN memory_pool_fts fts ON mp.rowid = fts.rowid
                       WHERE mp.owner_agent_id = ? AND mp.kind IN ({placeholders})
                       AND mp.confidence >= ?
                       AND memory_pool_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (req.agent_id, *kinds, req.min_confidence, fts_query, req.top_k),
                )
                for row in rows:
                    if row[0] not in [r.get("memory_id") for r in results]:
                        results.append({
                            "memory_id": row[0], "memory_key": row[1],
                            "content": row[2], "summary": row[3],
                            "confidence": row[4], "source_type": row[5],
                            "kind": row[6], "access_count": row[7],
                            "score": 0.5,  # FTS5 匹配分固定 0.5
                            "owner_agent_id": row[9], "disclosure_level": row[10],
                        })
            except Exception as e:
                logger.warning(f"[FTS5 降级检索失败] {type(e).__name__}")

            if embedding_unavailable and not results:
                # 最终降级：LIKE
                rows = await db_facade.query(
                    f"""SELECT memory_id, memory_key, content, summary, confidence,
                       source_type, kind, access_count
                       FROM memory_pool
                       WHERE owner_agent_id = ? AND kind IN ({placeholders})
                       AND confidence >= ?
                       AND (content LIKE ? OR summary LIKE ?)
                       ORDER BY access_count DESC LIMIT ?""",
                    (req.agent_id, *kinds, req.min_confidence,
                     f"%{req.query[:20]}%", f"%{req.query[:20]}%", req.top_k),
                )
                for row in rows:
                    if row[0] not in [r.get("memory_id") for r in results]:
                        results.append({
                            "memory_id": row[0], "memory_key": row[1],
                            "content": row[2], "summary": row[3],
                            "confidence": row[4], "source_type": row[5],
                            "kind": row[6], "access_count": row[7],
                            "score": 0.3,
                            "owner_agent_id": row[9], "disclosure_level": row[10],
                        })

        # 更新 access_count
        def _bump_access(conn):
            c = conn.cursor()
            for r in results:
                c.execute(
                    "UPDATE memory_pool SET access_count=access_count+1, last_accessed=? "
                    "WHERE memory_id=?",
                    (datetime.now(timezone.utc).isoformat(), r["memory_id"]),
                )
                audit_memory(action="read", agent_id=req.agent_id,
                    memory_key=r["memory_key"], memory_id=r["memory_id"],
                    score=r.get("score"), session_id=req.query[:20], actor="agent")

        await db_facade.run_in_conn(_bump_access, write=True)

        return {
            "results": results,
            "total": len(results),
            "embedding_unavailable": embedding_unavailable,
        }


    def _calculate_disclosure_level(
        self,
        memory: dict,
        requester: str,
        task: dict,
        required_level: DisclosureLevel,
    ) -> DisclosureLevel:
        """核心披露决策 — 委托给 DisclosureEngine"""
        return self.disclosure._calculate_disclosure_level(
            memory, requester, task, required_level
        )


    async def semantic_search(self, req: SemanticSearchRequest, scope: dict = None) -> dict:
        """语义搜索 — 委托给 DisclosureEngine（1e: scope 透传）"""
        return await self.disclosure.semantic_search(req, scope=scope)


    def _extract_by_level(self, memory: dict, level: DisclosureLevel) -> str:
        """按披露级别提取 — 委托给 DisclosureEngine"""
        return self.disclosure._extract_by_level(memory, level)

    # ============ 任务调度 ============


