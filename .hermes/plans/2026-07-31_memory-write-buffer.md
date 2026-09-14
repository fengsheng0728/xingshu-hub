# 记忆池写入缓冲 实现计划

> **For Hermes:** 直接实施，每步验证。

**Goal:** 将 `memory_pool` 的写入（store_memory/delete_memory）也纳入 asyncio.Queue 缓冲，批量落库。

**Architecture:** 复用现有 `_write_queue` → `_write_buffer_worker` → `_batch_write_knowledge` 管道，增加 memory action 分支。

**Tech Stack:** asyncio.Queue, SQLite WAL, 现有 SyncHub 框架

---

### Task 1: 扩展 `_batch_write_knowledge` 支持 memory 写入

**文件:** `E:\sync-hub-case\hub_core.py`

在 `_batch_write_knowledge` 的 action 分支中增加 `store_memory` 和 `delete_memory`：

```python
elif action == "store_memory":
    mem = payload
    c.execute("""
        INSERT OR REPLACE INTO memory_pool
        (memory_id, owner_agent_id, memory_key, content, summary,
         embedding, importance, tags, kind, source_session_id,
         confidence, source_type, disclosure_level,
         allowed_viewers, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?)
    """, (mem["memory_id"], mem["owner_agent_id"], mem["memory_key"],
          mem["content"], mem["summary"], mem["embedding"],
          mem["importance"], mem["tags_json"], mem["kind"],
          mem["source_session_id"], mem["confidence"], mem["source_type"],
          mem["disclosure_level"], mem["allowed_viewers"],
          mem["created_at"], mem["updated_at"]))
elif action == "delete_memory":
    c.execute("DELETE FROM memory_pool WHERE memory_id = ?",
              (payload["memory_id"],))
```

### Task 2: 改写 `store_memory` 为队列模式

**文件:** `E:\sync-hub-case\hub_core.py`

将 `store_memory` 从 `async with self._lock + 直接写 SQLite + ChromaDB` 改为 `await self._write_queue.put + ChromaDB 异步写`。

```python
async def store_memory(self, agent_id: str, entry: MemoryEntry) -> dict:
    memory_id = f"{agent_id}:{entry.memory_key}:{int(time.time()*1000)}"[:32]
    now = datetime.now(timezone.utc).isoformat()
    
    # ChromaDB 仍然同步写入（向量索引需要即时可用）
    await self._store_chromadb(memory_id, agent_id, entry)
    
    # SQLite 走队列
    await self._write_queue.put(("store_memory", {
        "memory_id": memory_id,
        "owner_agent_id": agent_id,
        "memory_key": entry.memory_key,
        "content": entry.content,
        "summary": entry.summary or entry.content[:200],
        "embedding": None,
        "importance": entry.importance,
        "tags_json": json.dumps(entry.tags),
        "kind": entry.kind,
        "source_session_id": entry.source_session_id,
        "confidence": entry.confidence,
        "source_type": entry.source_type,
        "disclosure_level": entry.disclosure_level.value,
        "allowed_viewers": json.dumps(entry.allowed_viewers),
        "created_at": now,
        "updated_at": now,
    }))
    
    self._record_trace(action="store_memory", agent_id=agent_id,
                       title=entry.memory_key, entry_id=memory_id)
    
    return {"status": "ok", "memory_id": memory_id}
```

### Task 3: 改写 `delete_memory` 为队列模式

同样入队。

### Task 4: 验证

- 写入 100 条记忆 → queue 深度
- 等待 flush → DB 一致性
- disclosure 查询仍正常工作
