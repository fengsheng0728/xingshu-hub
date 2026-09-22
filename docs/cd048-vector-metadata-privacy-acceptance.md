# CD-048 验收：索引最小披露（memory 层 metadata 去明文 + 密级字段）

- 项目：星枢 Sync Hub｜实施：外部 agent（kimi，session_023053e3，worktree `E:\xingshu-wt-cd048` 分支 `cd048-vector-privacy`）
- 基线：`0e582e1`｜任务书：`E:\星枢-待办\星枢任务书-2026-09-17\T4-裂缝2-索引最小披露-任务书.md`
- 台账项：CD-048（裂缝2：索引权限盲 + 索引内明文）

## 一、问题与修法

Chroma 是活检索路径，但索引里存着 `content[:500]` 正文片段、且没有密级字段（权限盲）。现状无 API 泄露
（正文与级别都在回查环节由 8 规则链判定），真实风险是**静态暴露面**：离线拿到 `chroma_db` 即得一批正文片段。

修法（最小披露，不改权限权威）：

| 项 | 改动 |
|---|---|
| memory metadata | 键集合 `owner,key,tags,importance,kind,confidence,source_type,layer,level` —— **删 `content`/`summary`**，**加 `level`**（= 记忆自身 disclosure_level） |
| 统一构造 | `hub_mixins/memory._vector_metadata()` 为唯一出口；`ingest.py` 重建段与 `hub_core._reindex_vector_sync` 补偿段均 import 复用（重建 SQL 补取 `disclosure_level` 列） |
| 检索 where | `disclosure._chroma_search` 追加 `{"level": {"$ne": "none"}}`（粗过滤，防历史脏向量）；注释写明**where 只做粗过滤、权限权威仍是回查后的 disclose_for_principal** |
| knowledge 层 | **本轮不动**（chunk 正文只存在于 chroma metadata，去掉需先定"正文从哪来"）→ kimi 出了三候选方案报告，见 §五 |

## 二、验收（Hermes 独立复跑）

**新用例**：`tests/test_vector_metadata_privacy.py` 6 条 → 我复跑 **6 passed**（含重建一致/补偿一致/where 排除 none/旧格式向量兼容）。

**旧契约断言更新（我做的，非 kimi）**：`tests/test_vector_index_consistency.py` 3 条断言（我在 CD-046 写的）
曾钉死"metadata 含 content"这一**旧（不安全）契约** → 按新契约改为「断言 content/summary 不在 metadata、
level 存在」。改后 `test_vector_index_consistency + test_vector_metadata_privacy` → **14 passed**；
其文件集（含 `test_embedding_k1 / test_kb_unified_retrieval / test_semantic_degrade`）→ **32 passed**。

**真实 Hub + 真实 ChromaDB E2E**（脚本 `E:\星枢-待办\_sync\cd048\e2e_metadata_privacy.py`，结果同目录 JSON；
独立 config/db/chroma/端口 3079）：

| 断言 | 实测 |
|---|---|
| P-1 写入后 metadata | 键集合 **恰好 9 键**（新集合），`content`/`summary` 不存在，`level="summary"` |
| **P-2 字节级硬证据** | 对整个 chroma 目录**按字节搜写入的唯一标记串 → 零命中**（索引内无任何正文） |
| P-3 检索仍取正文 | owner 自查 `semantic_search` 200，命中该条且 content 逐字等于写入内容（来自 SQLite 回查） |
| P-4 披露不变 | 另一 worker 查同一条 → 结果 0 条（fail-closed 保持） |
| P-5 旧向量兼容 | kimi 单测（真 EphemeralClient）覆盖通过；我另用第二个客户端直写旧格式向量做注入，导致 Hub 侧查询 `Internal error: Error finding id` → **优雅降级**（200 + degraded + SQLite 关键词兜底，无崩溃、无错数据）。**运维提示**：不要用第二个 chromadb 客户端直写运行中的 chroma_db |

**CRLF**：4 个被改文件 `\r\r\n` 计数 0。

## 三、kimi 上报偏差的处理

1. **任务书内部矛盾（它选择执行安全目标、不擅自改断言）**：硬约束"禁止改既有测试断言"与"metadata 去明文"不可兼得——
   那 3 条断言钉死旧契约。**它判断正确**（改断言属验收方职责，且安全目标是本轮本体）；我已按新契约更新，见 §二。
2. **组合运行偶发干扰**：它报告 `test_semantic_degrade::t02/t03` 偶发红、单跑绿——**与我独立观察一致**（并行 agent
   同时起固定端口 3062 的测试 Hub 造成端口踩踏），非本改动引入。已作为流程坑沉淀。
3. **测试副作用**：跑回归会写仓库跟踪文件 `audit/memory_pool.jsonl`（它已逐字节恢复）。属既有卫生问题，记录备查。
4. **`_reindex_vector_sync` 的 tags 透传**：与旧代码同款（NULL tags 行会传 None）。旧行为、本轮未扩 scope，已记录。
5. **`$ne` 对缺键文档的命中行为**：它用真 chromadb 1.5.9 实测确认 `$ne` 对**缺 level 键**的旧向量与知识 chunk 照样命中
   → 新 where 不会误杀 K-1 统一检索。**这是本轮最容易踩的坑，它主动验了**。

## 四、残留

- **knowledge 层 chunk 正文仍在 chroma metadata**（`disclosure._knowledge_hit` 直接从 metadata 取正文）。
  三候选方案（① 检索时按 entry_id+piece_index 从 `knowledge_base.content` 重切 ② 写入时把 chunk 正文落 `document_chunks`
  ③ 保留但截断/脱敏）与各自风险见 kimi 报告 → **待拍板后另派**（建议登记 CD-052）。
- 旧明文向量**零迁移成本**（不回归），清理指引已给（全量 rebuild 或按 id 投 `vector_index` 事件重灌），不写进代码路径。
