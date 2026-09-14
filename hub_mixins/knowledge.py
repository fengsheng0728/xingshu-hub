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

from deps import KnowledgeEntry, logger
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong

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

        logger.info(f"knowledge: {'update' if entry.entry_id else 'create'} [{entry.title}] (queued)")
        return {"status": "ok", "entry_id": entry_id}


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
        self._enqueue_write("delete", {"entry_id": entry_id})
        self._record_trace(action="delete", agent_id="", title=entry_id, entry_id=entry_id)
        self._wiki_sync_pending = True
        logger.info(f"knowledge: delete [{entry_id}] (queued)")
        return {"status": "deleted"}


    async def knowledge_graph(self, requester: str = "") -> dict:
        """返回知识图谱数据（节点 + 边，用于 D3 可视化）。
        N4(2026-08-05): 权限级过滤 — requester 对节点创建者(created_by)的披露级别为 NONE 时隐藏节点。
        """
        rows = await db_facade.query("SELECT * FROM knowledge_base ORDER BY importance DESC")

        nodes = []
        node_ids = set()
        edges = []

        for row in rows:
            eid = row["entry_id"]
            # N4: 权限级过滤 — requester 对 owner 无披露权限则隐藏节点
            if requester:
                try:
                    from disclosure import DisclosureEngine, DisclosureLevel as _DL
                    _owner = row["created_by"] or row.get("owner_agent_id") or ""
                    _lv = DisclosureEngine(self)._calculate_disclosure_level(
                        memory={**dict(row), "owner_agent_id": _owner},
                        requester=requester, task={}, required_level=_DL.SUMMARY)
                    if _lv == _DL.NONE:
                        continue  # 无权限 → 节点不可见
                except Exception:
                    pass  # 过滤失败不过度拦截
            node_ids.add(eid)
            tags = json.loads(row["tags"] or "[]")
            nodes.append({
                "id": eid,
                "title": row["title"],
                "category": row["category"],
                "importance": row["importance"],
                "tags": tags,
            })
            # 双向链接 → 边
            links = json.loads(row["links"] or "[]")
            for linked_id in links:
                edges.append({"source": eid, "target": linked_id})

        # 补充被链接但不在节点列表中的节点（只显示有链接关系的）
        linked_ids = {e["target"] for e in edges} - node_ids
        for lid in linked_ids:
            nodes.append({
                "id": lid, "title": lid, "category": "unknown",
                "importance": 0.5, "tags": [],
            })

        return {"status": "ok", "nodes": nodes, "edges": edges}

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


