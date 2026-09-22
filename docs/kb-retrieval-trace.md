# 星枢检索链路取证（Phase 0 / Task 0.1 交付物）

日期：2026-09-14
方法：静态代码取证（行号为当日 HEAD `2a977b7`），辅以 chroma/sqlite 实测
结论一句话：**当前有两条互不相通的检索轨，各有各的向量存储与出口；`chroma` 里没有知识条目，知识条目的向量存在 SQLite BLOB 列里。**

---

## 1. 两条轨（实测事实）

### 轨 A：记忆池 → ChromaDB（面向 Agent/网关）
| 环节 | 位置 | 事实 |
|---|---|---|
| 向量生成 | `hub_mixins/ingest.py:235` `_rebuild_embeddings_txn` | `SELECT memory_id, content FROM memory_pool` → 逐行编码 → 写回 `memory_pool.embedding` |
| 向量库写入 | `hub_core.py:169` `get_or_create_collection` | chroma 集合名 `sync_hub` |
| 实测状态 | `chroma_db/chroma.sqlite3` | 集合 `sync_hub`：**5 条向量，维度 384**；metadata 是 `'旧维度内容'`/`key:k1`/`owner:a` 等**测试残留** → 无真实内容 |
| 查询 | `disclosure.py:383` `semantic_search` → `:452` `_chroma_collection.query` | 入参 `n_results`（调用方给，网关侧限 1..50）；出参 `memories[]` + `degraded` |
| 出口 | `routes_memory.py:136` `POST /api/v1/memory/semantic_search`；`routes_gateway.py:119` `POST /api/v1/gateway/read {kind:"semantic"}` | 网关侧已带：`principal.scope` 作用域剥离（`disclose_for_principal`）、`_attach_origin(memories)` 出处、`gateway_read_log` 审计 |
| 降级 | `disclosure.py` CD-016 分支 | chroma 不可用或模型缺失 → `content LIKE` 关键词检索，`degraded=True` |

### 轨 B：知识条目 → SQLite BLOB 向量（面向 Wiki/MCP）
| 环节 | 位置 | 事实 |
|---|---|---|
| 向量生成 | `wiki_sync.py:205` | `UPDATE knowledge_base SET embedding = ? WHERE entry_id = ?` → **向量存 SQLite BLOB 列**，不进 chroma |
| 查询 | `wiki_engine.py:282` | `SELECT entry_id, title, category, embedding FROM knowledge_base WHERE embedding IS NOT NULL` → 取出后在应用层算相似度 |
| 出口 | `mcp_server.py:14` `wiki_search(field="hybrid", top_k)`（MCP 工具）+ wiki_get/wiki_list/wiki_graph/wiki_sync | 给 MCP 客户端（外部 AI 客户端）用 |
| 实测状态 | `knowledge_base` | **12 行**（几乎空）→ 这条轨现在也等于没有内容 |

### 关键差异（这才是"两个出口不一致"的根）
- 同样是"语义检索"，轨 A 查**记忆池**、轨 B 查**知识库**；两者语料不同、向量存储不同（chroma vs SQLite BLOB）、出口不同（REST gateway/memory vs MCP）。
- 因此：**同一个问题，走 MCP 和走网关会拿到不同的结果集**；客户/Agent 走哪条决定了他看到什么。
- 员工侧（`E:/sync-hub-agent/backend/agent_client.py`）两条都不调（只调 workspace/tasks/memory/shared docs）→ **没有任何人工问答出口**（已定：出口由我们做，见计划 Phase 6）。

---

## 2. 对后续工作的直接约束

1. **方案 B（已定）的落点**：统一到一个 collection + metadata 过滤。要落的就是把轨 B 的 `knowledge_base` chunk 灌进同一 chroma 集合（`owner=knowledge` / `source_type`），轨 A 的 memory 保持 `owner=<agent_id>`；查询侧统一经 `disclosure.semantic_search` 的 scope 过滤。
2. **评测落点**：Phase 2 的 golden 集必须打**实际出口**——轨 B 打 `wiki_search`，轨 A 打 `gateway/read`；合并后打统一出口。**不能只测一条**。
3. **换 embedding 模型**：轨 A 换维度要重建 chroma 集合（`hub_mixins/ingest.py:330` 的 delete+重建路径）；轨 B 换维度要重刷 `knowledge_base.embedding` 全部行（`wiki_sync` 重跑）。两边必须同维度，否则"统一 collection"不成立。
4. **降级语义**：`degraded=True` 目前只在轨 A 出现；轨 B（SQLite 相似度）没有 `degraded` 标记 → 统一契约时轨 B 也要补，否则无出处/降级时客户与员工无从分辨。
5. **待登记**：轨 A/轨 B 双出口语义不一致 → 台账 CD-044（与 Phase 6 契约补齐同一批处理）。

---

## 3. 尚未取证（留给 Phase 1）

- `wiki_sync` 生成 embedding 时用的档位是否与 `hub_core` 建 chroma 时的档位一致（两边都用 `CONFIG.EMBEDDING_PROVIDER`？需实测确认，避免"同维度不同模型"的静默错配）
- `memory_pool.embedding`（BLOB）与 chroma 里同一 content 的向量是否同源（是否双写）
