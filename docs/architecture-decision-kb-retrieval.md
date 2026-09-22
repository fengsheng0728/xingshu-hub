# 架构决策：知识库检索统一 collection（方案 B：统一 collection + metadata 过滤）

日期：2026-09-14 拍板 / 2026-09-16 落档 · 决策者：用户 · 起草：kimi-code（K-1 任务，Hermes 验收）· 状态：**已定 —— Phase 0 定档 + Phase 1 落地（commit 见 `docs/carried_debts.md` CD-044 行）**

上位文件：`.hermes/plans/2026-09-14_213000-kb-retrieval-baseline.md`（实施计划，决策见其 §5）；取证依据 `docs/kb-retrieval-trace.md`（Phase 0 链路取证）。

---

## 一、要决策的问题

「企业知识库 + 语义检索」存在两条互不相通的检索轨（取证见 trace 文档）：

- **轨 A**：`memory_pool` → ChromaDB 集合 `sync_hub` → `disclosure.semantic_search`（REST `/api/v1/memory/semantic_search`、网关 `kind=semantic`）
- **轨 B**：`knowledge_base` → SQLite BLOB 向量列 → MCP `wiki_search`

同一个问题走两个出口拿到不同答案，且知识库条目根本不进 chroma 集合（实测仅 5 条测试残留向量），「员工问制度/FAQ」在产品上不成立。三个候选：方案 A（只做 memory 层）、方案 B（统一 collection + metadata 过滤）、方案 C（拆两个 collection）。

## 二、决策

**走方案 B：统一 collection + metadata 过滤。** 不拆两个 collection，不退回只做 memory 层。

- 知识条目按 `chunker.chunk_document`（512/50 token，不传 embed_fn）切片后，与记忆写入**同一个** chroma 集合（`CONFIG.CHROMA_COLLECTION`）。
- **层区分键新增 `layer ∈ {memory, knowledge}`**（见 §六 与计划原文的偏差——计划 §5 写的是用 `owner` 区分层，实测 `owner` 已被占用为「记忆的 owner agent_id」，改用 `layer`，`owner` 语义一字不改）。
- `source_type` 保留现有语义；知识侧用它承载 `category`（来源区分）。
- 检索侧 `disclosure._chroma_search` 按命中 metadata 的 `layer` 分流回查，并新增可选 `layer` 过滤参数（`SemanticSearchRequest.layer`，默认 `""` = 不过滤，向后兼容）。

## 三、理由

1. **产品成立性**：方案 A 下员工问制度/FAQ 永远查不到（知识条目零向量），不成立。
2. **改动面**：方案 C 要维护两套 embedding/检索/出口，且换模型要重建两个集合；方案 B 只加一个 metadata 键 + 一处回查分流，披露过滤、降级链、网关审计全部复用。
3. **一致性**：统一出口后「同一问题同一结果集」，双轨语义漂移（CD-044）从根上消失。

## 四、metadata 键清单（与代码逐一核对，2026-09-16）

写侧两处：`hub_mixins/memory.py`（记忆写入同步写向量）与 `hub_mixins/ingest.py rebuild_embeddings()`（清空重建）写 **memory 层**；`hub_mixins/knowledge.py knowledge_upsert()`（经 `_embed_knowledge_chunks`）与 `ingest.py rebuild` 知识段写 **knowledge 层**。chromadb 不接受 None 值 → 一律 `""`/`0.0` 兜底。

**memory 层**（`layer="memory"`）：

| 键 | 取值 | 来源 | 用途 | 可为空 |
|---|---|---|---|---|
| `layer` | `"memory"`（固定） | 代码常量 | 层区分 + 回查分流 | 否 |
| `owner` | 记忆的 owner agent_id | `memory_pool.owner_agent_id` | `filter_owner` 过滤 / rebuild 清空 where 依赖 | 可（`""`） |
| `key` | memory_key | `memory_pool.memory_key` | 展示/调试 | 可 |
| `tags` | JSON 字符串 | `memory_pool.tags` | 展示 | 可 |
| `content` | 内容前 500 字 | `memory_pool.content[:500]` | 命中展示兜底 | 可 |
| `summary` | 摘要 | `memory_pool.summary` | 展示 | 可 |
| `importance` | float | `memory_pool.importance` | 排序参考 | 可（`0.0`） |
| `kind` | fact/todo/... | `memory_pool.kind` | 展示 | 可 |
| `confidence` | float | `memory_pool.confidence` | 展示 | 可（`0.0`） |
| `source_type` | user/agent/tool/... | `memory_pool.source_type` | 来源区分（现有语义不变） | 可 |

**knowledge 层**（`layer="knowledge"`，chunk id = `kb:{entry_id}:{piece_index}`）：

| 键 | 取值 | 来源 | 用途 | 可为空 |
|---|---|---|---|---|
| `layer` | `"knowledge"`（固定） | 代码常量 | 层区分 + 回查分流 + 更新时删除旧 chunk 的 where 条件 | 否 |
| `entry_id` | 知识条目 id | `knowledge_base.entry_id` | 回查 `knowledge_base` 的键；更新删除条件 | 否 |
| `title` | 条目标题 | `knowledge_base.title` | 出处展示 | 可（`""`） |
| `content` | chunk 文本前 500 字 | chunker 切片 | 命中展示（回查条目全文前的就地内容） | 可 |
| `source_type` | = `category`（process/policy/product/faq/...） | `knowledge_base.category` | 知识侧来源区分 | 可 |
| `importance` | float | `knowledge_base.importance` | 排序参考 | 可（`0.0`） |
| `chunk_hash` | 内容规范化 sha256 | `chunker.chunk_hash` | 幂等/调试 | 否 |

## 五、回查分流决策

chroma 命中 id → 按 metadata 分流：

- `layer == "knowledge"`，或无 `layer` 但带 `entry_id`（防御旧数据）→ **回查 `knowledge_base`（按 `entry_id`）**；
- 其余（**含无 `layer` 键的旧向量**）→ 走原 `memory_pool` 回查（`WHERE memory_id = ?`），一字不改。

**为什么知识侧查 `knowledge_base` 而不是 `document_chunks`**：知识 chunk 只存在于 chroma（`knowledge_upsert` 不写 `document_chunks`）；`document_chunks` 是 ingest 管道（H1）的产物，两表 id 空间不同（`{doc_id}-c{i}` vs `kb:{entry_id}:{i}`），查它必然全落空。条目级信息（title/category/created_by/tags）都在 `knowledge_base`，回查一次拿全。

**为什么不复用 memory_pool 的 id 空间**：memory_id 是 `sha256(agent_id:key:时间戳)[:20]` 的 hex，知识 chunk 若塞进同一空间，要么伪造 memory_pool 行（污染记忆轨的披露/统计语义），要么回查必然落空被 `continue` 静默丢弃（K-1 任务书 §三.7 红线）。独立 `kb:` 前缀 id 从结构上杜绝冲突。

**披露过滤（知识侧）**：复用 `disclose_for_principal` 既有 8 规则链，不自创放行规则。`knowledge_base` 无 `disclosure_level` 列 → 构造伪 memory dict（`owner_agent_id=created_by`、自身级别 `summary`、allowed_viewers 空）进规则链；判 NONE（如规则 7 同级 worker 无协作）→ 跳过（fail-closed）。条目级 NONE 标记属后续出口契约事项（Phase 6.3）。

**回查不到的 id**：与原行为一致 `continue`（不抛异常、不让整批变空）。

## 六、与计划原文的偏差（必须记录）

计划 §5 写「`owner` 字段区分 `memory` / `knowledge`」。实测 `owner` 已被占用：现有语义 = 记忆的 owner agent_id，且被 `disclosure.py` 的 `filter_owner` 过滤与 `ingest.py` rebuild 的清空 where（`{"owner": {"$ne": "__none__"}}`）依赖。**故层区分改用新增键 `layer`，`owner` 语义一字不改。** 决策本身（统一 collection + metadata 过滤）不变。

## 七、兼容策略

1. **无 `layer` 键的旧向量按 memory 处理**（现存 5 条测试残留向量不报错、不静默全丢；`tests/test_kb_unified_retrieval.py` 有用例锚定）。
2. **不传 `layer` 参数 = 不过滤**（memory+knowledge 都查），memory 命中返回体字段结构与改动前完全一致（测试锚定字段集）。
3. **显式 `layer="memory"` 时不含旧向量**（chroma where 不匹配缺失键）——属预期语义，在此明示。
4. **降级路径（CD-016）**：chroma 不可用 / 模型缺失 → `_sqlite_keyword_search`（`memory_pool` content LIKE）+ `degraded=true`。**降级态只覆盖 memory 轨，knowledge 轨不返回**——本阶段不重写降级 SQL（CD-016 已验收），此限制随 Phase 6.3 出口契约一并收口。
5. **写侧降级**：knowledge 向量写入失败（chroma 不可用/模型缺失/异常）只记 warning，不阻塞知识落库；向量可由 `POST /api/v1/embeddings/rebuild` 事后补齐。

## 八、embedding 档的诚实描述

默认档 `EMBEDDING_PROVIDER=hasher` 是**词面哈希向量（HashingVectorizer 词袋，384 维），不是语义模型**——只能命中词面重叠，不得对外称「语义检索」（计划 §4.3）。真语义档 = `sentence` provider + 本地模型目录（`embedding.model_path`，离线包分发；换模型必须重建集合）。本任务不改默认档。

## 九、影响面

| 对象 | 影响 |
|---|---|
| `hub_mixins/knowledge.py` | `knowledge_upsert` 落库后同步切片入向量（降级不阻塞）；更新先删旧 chunk（`$and:[layer=knowledge, entry_id]`）再写 |
| `hub_mixins/memory.py` / `hub_mixins/ingest.py` | 两处 memory metadata 补 `layer="memory"`，其余键一字不动 |
| `hub_mixins/ingest.py rebuild_embeddings()` | chroma 清空段连知识向量一起重建（`knowledge_base.content` 现切现编码，与写侧同一切片口径）；`document_chunks` 仍不参与（无 embedding blob 列） |
| `disclosure.py` | `_chroma_search` layer 过滤 + 分流回查；新增 `_knowledge_hit`；降级链不动 |
| `models.py` | Config 新增 `KB_EMBED_MAX_CHUNKS=200`（写侧延迟护栏：sentence 档 ~10-30ms/chunk，200 封顶最坏个位数秒；hasher 档可忽略；`embedding.kb_embed_max_chunks` 可选配置）；`SemanticSearchRequest` 新增可选 `layer` |
| `routes_memory.py` | 仅文档注释：layer 随 req 整体透传，无新增代码 |
| 响应契约 | memory 命中字段不变；knowledge 命中额外带 `origin="knowledge"` / `entry_id` / `title`（出处）；`total` = 两层合并后通过披露过滤的条目数 |

## 十、后果与代价

- 同一集合内 id 空间混排，靠 `kb:` 前缀 + `layer` 键区分——任何新写侧都必须带 `layer`，否则会落入 memory 回查并静默丢失（测试已锚定旧向量按 memory 处理的行为）。
- 知识条目更新 = 删旧 chunk + 全量重写该条目 chunk（chromadb 无原位更新）；条目内容很大时受 `KB_EMBED_MAX_CHUNKS` 截断（超出部分不进向量，检索不到——护栏代价，在此明示）。
- 降级态 knowledge 轨不返回（见 §七.4），Phase 6.3 前对外口径不得承诺降级态可查知识库。

## 十一、未决 / 后续

Phase 2 golden 集、Phase 3 三档对比（hasher/bge/关键词）、Phase 4 延迟阶梯、Phase 5 质量卡与 CI 门禁、Phase 6 员工问答出口与出口契约（含轨 B `degraded` 与出处字段对齐、`docs/kb-answer-contract.md`）——按实施计划推进，本 ADR 不展开。

## 十二、复核记录

- 2026-09-14 用户拍板：方案 B（计划 §5 决策 1）
- 2026-09-16 kimi-code 落档 + Phase 1 实现落地（K-1 任务书）；偏差记录见 §六
