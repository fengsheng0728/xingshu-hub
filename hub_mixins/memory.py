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
from disclosure import _SCOPE_UNSET  # CD-105：fail-closed 哨兵（与 disclosure 共用，不另造）
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong
import db_facade
from hub_mixins.outbox import enqueue as _outbox_enqueue, enqueue_after_commit


def _vector_metadata(owner, key, tags_json, importance,
                     kind, confidence, source_type, level):
    """向量 metadata 构造（CD-046 统一；CD-048 最小披露改造）。

    键集合固定为 owner/key/tags/importance/kind/confidence/source_type/layer/level：
    - 删除 content/summary 明文（CD-048：离线拿到 chroma_db 即得 500 字正文片段
      的静态暴露面；正文唯一权威来源是回查 SQLite 后的披露链）
    - 新增 level = 该记忆自身的 disclosure_level（粗过滤用，权限权威判定
      仍在 disclose_for_principal，见 disclosure._chroma_search 注释）
    写路径 / rebuild 重建 / outbox 补偿重灌三处统一 import 复用本函数。
    """
    return {
        "owner": owner, "key": key,
        "tags": tags_json,
        "importance": importance, "kind": kind,
        "confidence": confidence, "source_type": source_type,
        "layer": "memory", "level": level,
    }

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
            # CD-046: 事务内只登记向量操作意图（本列表），提交后才执行——
            # commit 失败回滚时不得在索引里留指向不存在数据的脏向量（裂缝1A）
            _vector_ops = []
            # CD-064: 覆盖路径更新前的旧索引值（FTS 'delete' 命令需精确旧值
            # 才能正确摘词；新值提交后从库内读，保证与落库值一致）
            _fts_old = []

            def _txn(conn):
                c = conn.cursor()

                # 先检查同 memory_key 的硬冲突
                c.execute(
                    "SELECT memory_id, content, confidence, trust_level, updated_at FROM memory_pool "
                    "WHERE memory_key = ? AND owner_agent_id = ?",
                    (memory.memory_key, agent_id),
                )
                key_conflict = c.fetchone()

                action = "write"
                if key_conflict is not None:
                    # 同 key 直接覆盖
                    old_content = key_conflict[1]
                    # CD-064: 覆盖前捕获旧索引值（FTS 同步 'delete' 用）
                    _o = c.execute(
                        "SELECT content, summary, tags FROM memory_pool WHERE memory_id=?",
                        (key_conflict[0],)).fetchone()
                    if _o:
                        _fts_old.append((_o[0], _o[1], _o[2]))
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
                    # 审计：旧值入 audit（CD-045：走 outbox 事件行，随事务原子提交）
                    _outbox_enqueue(c, "memory_audit", {
                        "action": "conflict_overwrite", "agent_id": agent_id,
                        "memory_key": memory.memory_key, "memory_id": memory_id,
                        "old_content": old_content[:200], "new_content": memory.content[:200],
                        "session_id": source_session_id, "actor": source_type})
                    # T31: 跨天覆盖 → 同事务追加 shadow_archive 事件
                    if self._shadow is not None and key_conflict[4]:
                        old_date = (key_conflict[4] or "")[:10]
                        new_date = now[:10]
                        if old_date != new_date:
                            _outbox_enqueue(c, "shadow_archive", {
                                "memory_id": memory_id,
                                "old_path": f"vault/memory/{old_date}/{memory_id}.md"})
                    # CD-046(1C): 覆盖必须同步刷新向量（旧代码只改库不改索引 → 覆盖腐烂）
                    # 新 embedding 为 None（无模型/敏感锁定）时不入向量操作——
                    # 不用旧向量冒充新内容
                    if chroma_embedding is not None:
                        _vector_ops.append({
                            "op": "upsert", "memory_id": memory_id,
                            "embedding": chroma_embedding,
                            "metadata": _vector_metadata(
                                agent_id, memory.memory_key, json.dumps(memory.tags),
                                memory.importance, kind,
                                max(confidence, old_confidence or 1.0), source_type,
                                memory.disclosure_level.value),
                        })

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
                            # 实参序须对齐占位符序（confidence, updated_at, last_accessed,
                            # trust_level, memory_id）——旧代码把 memory_id 喂给了
                            # trust_level、WHERE 拿到信任级字符串，实测 rowcount=0
                            # （合并分支永不生效，access_count/confidence 从不刷新）。
                            (confidence, now, now,
                             self._merge_trust(best_row[5] or "internal", trust_level),
                             best_row[0]),
                        )
                        memory_id = best_row[0]
                        action = "merge"
                        _outbox_enqueue(c, "memory_audit", {
                            "action": "merge", "agent_id": agent_id,
                            "memory_key": best_row[1], "memory_id": best_row[0],
                            "confidence": confidence, "session_id": source_session_id,
                            "similarity": round(best_sim, 4), "actor": source_type})

                    elif 0.75 <= best_sim <= 0.90 and best_row is not None:
                        # 冲突候选 → 覆盖旧值
                        old_content = best_row[2]
                        # CD-064: 覆盖前捕获旧索引值（FTS 同步 'delete' 用）
                        _o = c.execute(
                            "SELECT content, summary, tags FROM memory_pool WHERE memory_id=?",
                            (best_row[0],)).fetchone()
                        if _o:
                            _fts_old.append((_o[0], _o[1], _o[2]))
                        # T31: 取旧日期用于跨天归档判断
                        _date_row = c.execute(
                            "SELECT updated_at FROM memory_pool WHERE memory_id=?",
                            (best_row[0],)).fetchone()
                        old_date = (_date_row[0] or "")[:10] if _date_row else ""
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
                        _outbox_enqueue(c, "memory_audit", {
                            "action": "conflict_overwrite", "agent_id": agent_id,
                            "memory_key": best_row[1], "memory_id": best_row[0],
                            "old_content": old_content[:200], "new_content": memory.content[:200],
                            "similarity": round(best_sim, 4),
                            "session_id": source_session_id, "actor": source_type})
                        # T31: 跨天覆盖 → 同事务追加 shadow_archive 事件
                        if self._shadow is not None and old_date:
                            new_date = now[:10]
                            if old_date != new_date:
                                _outbox_enqueue(c, "shadow_archive", {
                                    "memory_id": memory_id,
                                    "old_path": f"vault/memory/{old_date}/{memory_id}.md"})
                        # CD-046(1C): 相似度覆盖同样刷新向量（新 embedding 为 None 时跳过）
                        if chroma_embedding is not None:
                            _vector_ops.append({
                                "op": "upsert", "memory_id": memory_id,
                                "embedding": chroma_embedding,
                                "metadata": _vector_metadata(
                                    agent_id, best_row[1], json.dumps(memory.tags),
                                    memory.importance, kind,
                                    max(confidence, best_row[3] or 1.0), source_type,
                                    memory.disclosure_level.value),
                            })

                    else:
                        # < 0.75 或无匹配 → 新事实
                        action, memory_id = self._insert_new_memory_sync(
                            c, agent_id, memory, kind, source_type, source_session_id,
                            confidence, embedding_blob, chroma_embedding, now, trust_level,
                            _vector_ops=_vector_ops)
                        _outbox_enqueue(c, "memory_audit", {
                            "action": "write", "agent_id": agent_id,
                            "memory_key": memory.memory_key, "memory_id": memory_id,
                            "confidence": confidence, "source_type": source_type,
                            "session_id": source_session_id, "actor": source_type})
                else:
                    # 无 embedding → 直接新增
                    action, memory_id = self._insert_new_memory_sync(
                        c, agent_id, memory, kind, source_type, source_session_id,
                        confidence, embedding_blob, chroma_embedding, now, trust_level,
                        _vector_ops=_vector_ops)
                    _outbox_enqueue(c, "memory_audit", {
                        "action": "write", "agent_id": agent_id,
                        "memory_key": memory.memory_key, "memory_id": memory_id,
                        "confidence": confidence, "source_type": source_type,
                        "session_id": source_session_id, "actor": source_type})

                # CD-047(洞1): 影子镜像改走 outbox 事件行——与业务数据原子提交，
                # 消灭"commit 后 submit 前"的崩溃窗口。只记 id 不打包快照，
                # 内容/级别由消费侧从库内读最新值（天然解决洞2 快照陈旧）。
                # 无影子（_shadow 为 None）时不入队——同 CD-046 无向量栈不登记的
                # 降级语义，避免事件表堆积无意义行。
                if self._shadow is not None:
                    _outbox_enqueue(c, "shadow_mirror",
                                    {"kind": "memory", "memory_id": memory_id})

                # 提交由门面负责（write=True 成功路径自动 commit，异常路径 rollback）——
                # fn 内不得自行 commit（旧代码 write=False + fn 内 commit 违反门面契约：
                # 异常发生在 commit 之后时已提交数据无法回滚，且门面语义被架空）。
                return action, memory_id

            action, memory_id = await db_facade.run_in_conn(_txn, write=True)

            # CD-046: 业务已提交 —— 现在才执行向量操作（单个失败不中断其余，
            # 失败落 vector_index 补偿事件，消费者用库内 blob 重灌）
            await self._apply_vector_ops(_vector_ops)

            # CD-047(洞1): 影子双写不再由调用方直接 submit——已改走上方事务内的
            # shadow_mirror 事件行（outbox 消费者 → hub_core._shadow_mirror_sync →
            # 读库内最新值再 submit）。
            # 延迟语义（有意取舍）：镜像从"调用方立即入队"变为"经 outbox 消费者入队"
            # （≤0.5s 量级 + 排在上游事件之后），换取崩溃窗口归零。
            # 镜像失败由影子自身的 pending/replay 兜底（G1 语义不动）。

            # 安全告警：投毒尝试
            if source_type == "tool":
                await self._log_event("memory_poisoning_attempt", agent_id, {
                    "memory_key": memory.memory_key,
                    "source_session_id": source_session_id,
                    "content_snippet": memory.content[:100],
                })

            # FTS5 索引同步（CD-064 选型乙：写侧正确命令 + alembic 0006 存量 rebuild）。
            # 旧代码只发 'delete' 命令——该语义是「从索引摘词」，对空/未含该值的
            # 索引实测抛 DatabaseError: database disk image is malformed，且被
            # except-pass 吞掉 → 索引从不被维护。正确做法：新增走缺省 INSERT，
            # 覆盖先 'delete' 精确旧值再插新值（merge 不改索引列，跳过同步）。
            # 不选触发器（甲）：触发器会让 T16 锚点用例
            # tests/test_memory_keyword_fallback.py::test_k4a 的裸插断言
            # （索引空→LIKE）失真，而该用例属本任务禁改文件；写侧方案同时保留
            # D4 可用性优先的失败隔离。失败不阻塞业务写入：告警 + 索引漂移由
            # alembic 0006 rebuild 兜底。
            if action != "merge":
                try:
                    await self._sync_memory_fts(
                        memory_id, old=_fts_old[0] if _fts_old else None)
                except Exception as e:
                    logger.warning(
                        f"[FTS5 索引同步失败→不阻塞业务，rebuild 兜底] "
                        f"memory_id={memory_id} {type(e).__name__}: {e}")

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
                                  chroma_embedding, now, trust_level="internal",
                                  _vector_ops=None):
        """插入新记忆（公共逻辑）— 兼容存量 asyncio.run 调用方的异步包装"""
        return self._insert_new_memory_sync(
            c, agent_id, memory, kind, source_type, source_session_id,
            confidence, embedding_blob, chroma_embedding, now, trust_level,
            _vector_ops=_vector_ops)


    def _insert_new_memory_sync(self, c, agent_id, memory, kind, source_type,
                                 source_session_id, confidence, embedding_blob,
                                 chroma_embedding, now, trust_level="internal",
                                 _vector_ops=None):
        """插入新记忆（公共逻辑）

        CD-046：本函数在事务体内执行，**不得直接碰向量索引**——只把操作意图
        append 进 `_vector_ops`（由调用方在 commit 后执行）。`_vector_ops`
        为 None（存量调用方未传）时跳过向量登记。
        """
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

        # CD-046: ChromaDB 写入移出事务——只登记操作意图，提交后由
        # _apply_vector_ops 执行；失败落 vector_index 补偿事件（裂缝1A/1B）
        if chroma_embedding is not None and _vector_ops is not None:
            _vector_ops.append({
                "op": "upsert", "memory_id": memory_id,
                "embedding": chroma_embedding,
                "metadata": _vector_metadata(
                    agent_id, memory.memory_key, json.dumps(memory.tags),
                    memory.importance, kind,
                    confidence, source_type,
                    memory.disclosure_level.value),
            })

        return "write", memory_id


    async def _apply_vector_ops(self, vector_ops) -> None:
        """CD-046: 业务提交后执行向量操作（向量索引降级为"提交后副作用"）。

        单个 op 失败不中断其余 op；失败落 vector_index 补偿事件
        （消费者用库内 embedding blob 重灌索引，见 hub_core._reindex_vector_sync）。
        无向量栈（collection 为 None）→ 与既有降级语义一致，直接跳过。
        """
        if not vector_ops:
            return
        collection = getattr(self, "_chroma_collection", None)
        if collection is None:
            return
        for op in vector_ops:
            try:
                if op["op"] == "delete":
                    # chroma collection 的 upsert/delete 是同步阻塞 IO——直调会把
                    # 事件循环串行化（同 CD-017 教训），包 to_thread 卸载到线程。
                    # 两个调用点（store_memory / delete_memory）均在 async 上下文。
                    await asyncio.to_thread(collection.delete, ids=[op["memory_id"]])
                else:
                    await asyncio.to_thread(
                        collection.upsert,
                        ids=[op["memory_id"]],
                        embeddings=[op["embedding"]],
                        metadatas=[op["metadata"]],
                    )
            except Exception as e:
                logger.warning(
                    f"[ChromaDB {op['op']} 失败→落补偿事件] "
                    f"memory_id={op['memory_id']} {type(e).__name__}: {e}")
                try:
                    await enqueue_after_commit("vector_index", {
                        "op": op["op"], "memory_id": op["memory_id"]})
                except Exception as ee:
                    logger.warning(
                        f"[vector_index 补偿入队异常] {type(ee).__name__}: {ee}")


    async def _sync_memory_fts(self, memory_id: str, old=None) -> None:
        """CD-064: memory_pool_fts 外部内容索引的写侧同步（业务提交后副作用）。

        外部内容表（content='memory_pool'）不自动维护索引：
          - 新增（old=None）：缺省 INSERT 即 FTS5 'insert' 命令；
          - 覆盖（old=(content, summary, tags) 更新前旧值）：先 'delete' 精确
            旧值摘词，再插新值。'delete' 的值必须与索引中实际值一致，否则
            FTS5 会抛 database disk image is malformed（旧写侧的实测死因）。
        新值从库内回读（落库后的权威值，避免与 summary 缺省表达式漂移）。
        本函数抛异常由调用方捕获告警——FTS 失败不阻塞业务写入（D4），
        索引漂移由 alembic 0006 的 'rebuild' 兜底。
        """
        def _txn(conn):
            cur = conn.cursor()
            row = cur.execute(
                "SELECT rowid, content, summary, tags FROM memory_pool WHERE memory_id=?",
                (memory_id,)).fetchone()
            if row is None:
                return
            if old is not None:
                cur.execute(
                    "INSERT INTO memory_pool_fts(memory_pool_fts, rowid, content, summary, tags)"
                    " VALUES('delete', ?, ?, ?, ?)",
                    (row[0], old[0], old[1], old[2]))
            cur.execute(
                "INSERT INTO memory_pool_fts(rowid, content, summary, tags)"
                " VALUES(?, ?, ?, ?)",
                (row[0], row[1], row[2], row[3]))
        await db_facade.run_in_conn(_txn, write=True)  # 门面 write=True 自动 commit


    async def delete_memory(self, memory_key: str, agent_id: str) -> dict:
        """删除 Agent 自己的记忆（仅允许删除自己的）+ M3 审计"""

        # CD-046: 删除命中时提交后清向量（旧代码只删库 → 索引留孤儿向量）
        _vector_ops = []

        def _txn(conn):
            c = conn.cursor()
            # 删除前先读旧值（审计用；CD-064 补 summary/tags/rowid 供 FTS 'delete' 精确摘词）
            c.execute(
                "SELECT memory_id, content, summary, tags, rowid FROM memory_pool WHERE memory_key = ? AND owner_agent_id = ?",
                (memory_key, agent_id),
            )
            old = c.fetchone()

            c.execute(
                "DELETE FROM memory_pool WHERE memory_key = ? AND owner_agent_id = ?",
                (memory_key, agent_id),
            )
            deleted = c.rowcount
            if deleted > 0 and old:
                # CD-045: 审计走 outbox 事件行，随业务事务原子提交
                _outbox_enqueue(c, "memory_audit", {
                    "action": "delete", "agent_id": agent_id,
                    "memory_key": memory_key, "memory_id": old[0],
                    "old_content": (old[1] or "")[:200], "actor": "user"})
                _vector_ops.append({"op": "delete", "memory_id": old[0]})
                # CD-062: 同事务清理 memory_versions（删记忆不留版本残留）
                c.execute(
                    "DELETE FROM memory_versions WHERE memory_id = ?",
                    (old[0],))
                # T31: 同事务追加 shadow_delete 事件（事件不带快照，消费者读库）
                if self._shadow is not None:
                    _outbox_enqueue(c, "shadow_delete",
                                    {"memory_id": old[0]})
            return old, deleted
        old, deleted = await db_facade.run_in_conn(_txn, write=True)
        # 业务已提交 —— 执行向量清理；失败落补偿事件（消费者重放 delete）
        await self._apply_vector_ops(_vector_ops)
        # CD-064: FTS 索引同步删除（'delete' 命令需删除前的精确旧值；行已删，
        # rowid/内容用删除前捕获值）。失败告警不阻塞（D4），漂移由 0006 rebuild 兜底。
        if deleted > 0 and old:
            try:
                await db_facade.execute(
                    "INSERT INTO memory_pool_fts(memory_pool_fts, rowid, content, summary, tags)"
                    " VALUES('delete', ?, ?, ?, ?)",
                    (old[4], old[1], old[2], old[3]),
                )  # 门面单语句写自动 commit
            except Exception as e:
                logger.warning(
                    f"[FTS5 索引删除失败→不阻塞业务，rebuild 兜底] "
                    f"memory_id={old[0]} {type(e).__name__}: {e}")
        if deleted > 0:
            await self._log_event("memory_deleted", agent_id, {"memory_key": memory_key})
            return {"status": "deleted", "memory_key": memory_key}
        return {"status": "not_found", "detail": f"记忆 {memory_key} 不存在或无权删除"}


    async def get_memory_versions(self, memory_key: str, agent_id: str) -> dict:
        """获取记忆版本历史（owner-only；CD-056: 按 memory_id 精确归属，fail-closed）"""
        # 第一步：归属校验——memory_key 跨 agent 不唯一，必须按 owner 锁定 memory_id
        owner = await db_facade.query(
            "SELECT memory_id FROM memory_pool WHERE memory_key = ? AND owner_agent_id = ?",
            (memory_key, agent_id))
        if not owner:
            # 归属不成立（不是他的 key / 该记忆已删）——不区分存在性，统一 forbidden
            return {"status": "forbidden"}
        # 第二步：按 memory_id 精确查版本（不再用 memory_key 单独过滤——那正是漏洞源）
        rows = await db_facade.query(
            """SELECT v.id, v.version, v.content, v.summary, v.confidence,
                      v.archived_at, v.archived_by
               FROM memory_versions v
               WHERE v.memory_id = ?
               ORDER BY v.version DESC LIMIT 20""",
            (owner[0][0],))
        return {"memory_key": memory_key, "versions": [{
            "id": r[0], "version": r[1], "content": r[2],
            "summary": r[3], "confidence": r[4],
            "archived_at": r[5], "archived_by": r[6]
        } for r in rows]}


    async def rollback_memory(self, memory_key: str, version_id: int, agent_id: str) -> dict:
        """回滚记忆到指定历史版本"""
        _fts_rb = []  # CD-064: [(memory_id, (旧content, 旧summary, 旧tags))]

        # CD-053②（T32）：回滚改了 content/summary，必须**同步重算 embedding**。
        # 旧行为只 UPDATE content、`embedding` 列仍是「被覆盖那一版」的 blob，而
        # `vector_index` 消费侧是按**库内 blob** 重灌 → 回滚后向量对的是旧内容
        # （检索命中错位向量；正文回查虽正确，但召回质量受损）。
        # 算不出（无模型/超时/异常）时**写 NULL**：宁可暂时没有向量，也不用错位向量。
        _emb_blob, _emb_state = None, "unavailable"
        try:
            _row = await db_facade.query_one(
                "SELECT content FROM memory_versions WHERE id = ? AND memory_key = ?",
                (version_id, memory_key))
            _target_content = (_row[0] if _row else "") or ""
            if _target_content.strip():
                _model = await self._ensure_embedding_model()
                if _model is not None:
                    _loop = asyncio.get_event_loop()
                    _emb = await asyncio.wait_for(
                        _loop.run_in_executor(
                            None,
                            # 兼容 ndarray（真模型返回）与 list（替代实现）：np.asarray 双向安全
                            lambda: np.asarray(_model.encode(_target_content)).tolist()),
                        timeout=30,
                    )
                    _emb_blob = np.array(_emb, dtype=np.float32).tobytes()
                    _emb_state = "recomputed"
                else:
                    logger.info(
                        f"[CD-053②] rollback 无 embedding 模型可用 → 向量置 NULL key={memory_key}")
            else:
                logger.warning(
                    f"[CD-053②] rollback 目标版本内容为空 → 向量置 NULL key={memory_key} v={version_id}")
        except asyncio.TimeoutError:
            logger.warning(
                f"[CD-053②] rollback embedding 重算**超时** → 向量置 NULL（不错位）key={memory_key}")
        except (RuntimeError, ValueError, MemoryError) as e:
            logger.warning(
                f"[CD-053②] rollback embedding 重算失败 {type(e).__name__}: {e} → 向量置 NULL")
        except Exception as e:
            logger.warning(
                f"[CD-053②] rollback embedding 重算异常 {type(e).__name__}: {e} → 向量置 NULL")

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
                # CD-064: 回滚覆盖内容前捕获旧索引值（FTS 'delete' 需精确旧值摘词）
                _r = c.execute(
                    "SELECT content, summary, tags FROM memory_pool WHERE memory_id=?",
                    (cur[0],)).fetchone()
                if _r:
                    _fts_rb.append((cur[0], (_r[0], _r[1], _r[2])))
                c.execute(
                    """INSERT INTO memory_versions (memory_id, memory_key, version, content, summary, confidence, archived_by)
                       SELECT ?, ?, COALESCE((SELECT MAX(version) FROM memory_versions WHERE memory_key=?),0)+1, ?, ?, ?, 'rollback'""",
                    (cur[0], memory_key, memory_key, cur[1], cur[2], cur[3]))

            # 恢复到目标版本（CD-053②：同步写入重算后的 embedding——算不出则写 NULL，
            # 绝不让「新正文 + 旧向量」的错位状态落库）
            c.execute(
                "UPDATE memory_pool SET content=?, summary=?, confidence=?, updated_at=datetime('now'), "
                "embedding=? WHERE memory_key=? AND owner_agent_id=?",
                (ver[0], ver[1], ver[2], _emb_blob, memory_key, agent_id))
            if cur:
                # CD-047(洞2)/CD-050: rollback 变更点联动——内容变了，影子要重镜像、
                # 向量要重灌（旧代码只改库 → DB/向量/git 三处漂移）。
                # 事件随本事务原子提交；消费侧从库内读最新值。
                # vector_index 无条件入队：CD-046 回退门禁禁止本文件在
                # _apply_vector_ops 之外出现 chroma collection 判定；无向量栈时
                # 消费侧 _reindex_vector_sync 降级 no-op（collection None → return）。
                if self._shadow is not None:
                    _outbox_enqueue(c, "shadow_mirror",
                                    {"kind": "memory", "memory_id": cur[0]})
                _outbox_enqueue(c, "vector_index",
                                {"op": "upsert", "memory_id": cur[0]})
            return ver
        ver = await db_facade.run_in_conn(_txn, write=True)
        if ver is None:
            return {"status": "not_found", "detail": "版本不存在"}
        # CD-064: 回滚改了 content/summary → 同步 FTS 索引（失败告警不阻塞，
        # 索引漂移由 alembic 0006 rebuild 兜底）
        if _fts_rb:
            try:
                await self._sync_memory_fts(_fts_rb[0][0], old=_fts_rb[0][1])
            except Exception as e:
                logger.warning(
                    f"[FTS5 索引同步失败→不阻塞业务，rebuild 兜底] "
                    f"memory_id={_fts_rb[0][0]} {type(e).__name__}: {e}")
        await self._log_event("memory_rollback", agent_id,
                              {"memory_key": memory_key, "to_version_id": version_id,
                               "embedding": _emb_state})  # CD-053②：向量重算结果留痕
        return {"status": "rolled_back", "memory_key": memory_key, "version_id": version_id}

    # ============ M3: Memory Pool 读路径 ============


    async def memory_search(self, req) -> dict:
        """M3: 语义检索 + 关键词降级链（CD-057 修复：FTS5 主语形态 + LIKE 兜底 +
        degraded 标记；降级链全程不静默）"""
        from audit.memory_audit import audit_memory

        kinds = list(req.kind or ["fact"])
        placeholders = ",".join("?" * len(kinds))
        results = []
        embedding_unavailable = False

        # 尝试 embedding 检索
        try:
            model = await self._ensure_embedding_model()
        except Exception as e:
            logger.warning(
                f"[memory_search] embedding 模型获取异常 agent={req.agent_id} "
                f"query={req.query[:20]!r} {type(e).__name__}: {e} — 进入关键词降级链")
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
                logger.warning(
                    f"[Embedding 检索失败] agent={req.agent_id} query={req.query[:20]!r} "
                    f"{type(e).__name__}: {e}, 降级到关键词链")
                embedding_unavailable = True
        else:
            embedding_unavailable = True
            logger.warning(
                f"[memory_search] embedding 模型不可用 agent={req.agent_id} "
                f"query={req.query[:20]!r} — 进入关键词降级链")

        # 关键词降级链（CD-057）
        keyword_path = None  # None | "fts" | "like" —— 关键词链实际由谁服务（观测用）
        fts_failed = False
        tokens = []
        if embedding_unavailable or not results:
            try:
                # 简单分词：按空格/标点拆分
                import re
                tokens = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', req.query)
                fts_query = " OR ".join(tokens[:10])
                fts_rowids = []
                if fts_query:
                    # FTS5：fts 表做主语查 rowid，再回主表做 owner/kind/confidence
                    # 过滤——原写法在 LEFT JOIN 语境对非主表用 MATCH，SQLite 恒抛
                    # OperationalError: unable to use function MATCH in the requested context
                    fts_hits = await db_facade.query(
                        "SELECT rowid FROM memory_pool_fts WHERE memory_pool_fts MATCH ? LIMIT ?",
                        (fts_query, req.top_k * 4),
                    )
                    fts_rowids = [r[0] for r in fts_hits]
                if fts_rowids:
                    rid_ph = ",".join("?" * len(fts_rowids))
                    rows = await db_facade.query(
                        f"""SELECT mp.memory_id, mp.memory_key, mp.content, mp.summary,
                           mp.confidence, mp.source_type, mp.kind, mp.access_count,
                           mp.tags, mp.owner_agent_id, mp.disclosure_level
                           FROM memory_pool mp
                           WHERE mp.rowid IN ({rid_ph})
                           AND mp.owner_agent_id = ? AND mp.kind IN ({placeholders})
                           AND mp.confidence >= ?
                           LIMIT ?""",
                        (*fts_rowids, req.agent_id, *kinds, req.min_confidence, req.top_k),
                    )
                    for row in rows:
                        if row[0] not in [r.get("memory_id") for r in results]:
                            keyword_path = "fts"
                            results.append({
                                "memory_id": row[0], "memory_key": row[1],
                                "content": row[2], "summary": row[3],
                                "confidence": row[4], "source_type": row[5],
                                "kind": row[6], "access_count": row[7],
                                "score": 0.5,  # FTS5 匹配分固定 0.5
                                "owner_agent_id": row[9], "disclosure_level": row[10],
                            })
            except Exception as e:
                # CD-057：不再静默吞——FTS 断链必须可观测（error 级 + 上下文），并落 LIKE 兜底
                fts_failed = True
                logger.error(
                    f"[FTS5 降级检索失败→落 LIKE 兜底] agent={req.agent_id} "
                    f"query={req.query[:20]!r} {type(e).__name__}: {e}")

            if not results:
                # 最终兜底：LIKE 关键词（CD-057——覆盖 FTS 抛错 / FTS 命中为空 /
                # FTS 索引为空 / 模型可用但行无 embedding 四类断链；旧条件
                # `embedding_unavailable and not results` 漏掉后两类 → 恒空。
                # 旧 LIKE SELECT 少取 2 列却读 row[9]/row[10]，命中必抛
                # IndexError，一并修正）
                like_clauses = []
                like_params = []
                for t in tokens[:10]:
                    like_clauses.append("(content LIKE ? OR summary LIKE ?)")
                    like_params.extend([f"%{t}%", f"%{t}%"])
                if not like_clauses:
                    like_clauses = ["1=1"]  # 无可用 token → 按热度兜底（同旧 %% 语义）
                rows = await db_facade.query(
                    f"""SELECT memory_id, memory_key, content, summary, confidence,
                       source_type, kind, access_count, tags,
                       owner_agent_id, disclosure_level
                       FROM memory_pool
                       WHERE owner_agent_id = ? AND kind IN ({placeholders})
                       AND confidence >= ?
                       AND ({' OR '.join(like_clauses)})
                       ORDER BY access_count DESC LIMIT ?""",
                    (req.agent_id, *kinds, req.min_confidence, *like_params, req.top_k),
                )
                for row in rows:
                    if row[0] not in [r.get("memory_id") for r in results]:
                        keyword_path = "like"
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

        # CD-057 降级标记：语义主路径未独立服务（模型不可用 / 无命中转关键词链 /
        # FTS 断链）对外可见；消费方 routes_memory 透传整个 dict、routes_gateway
        # 只取 results，新增键对既有消费方零影响
        degraded = embedding_unavailable or fts_failed or keyword_path is not None
        degraded_reason = ""
        if degraded:
            if fts_failed:
                degraded_reason = "fts_error"
            elif embedding_unavailable:
                degraded_reason = "embedding_unavailable"
            else:
                degraded_reason = "keyword_fallback"
        return {
            "results": results,
            "total": len(results),
            "embedding_unavailable": embedding_unavailable,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "keyword_path": keyword_path,
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


    async def semantic_search(self, req: SemanticSearchRequest, scope=_SCOPE_UNSET,
                              internal: bool = False) -> dict:
        """语义搜索 — 委托给 DisclosureEngine（1e: scope 透传）

        CD-105（2026-09-24）：签名对齐 disclosure.semantic_search 的 fail-closed
        哨兵默认值——默认 `_SCOPE_UNSET`（未声明主体上下文 → metadata 封顶），
        **不能**保留默认 None（否则默认路径会把 fail-closed 吞掉）。internal 逃生门
        原样透传（调用点必须注释理由，见 disclosure.semantic_search docstring）。
        """
        return await self.disclosure.semantic_search(
            req, scope=scope, internal=internal)


    def _extract_by_level(self, memory: dict, level: DisclosureLevel) -> str:
        """按披露级别提取 — 委托给 DisclosureEngine"""
        return self.disclosure._extract_by_level(memory, level)

    # ============ 任务调度 ============


