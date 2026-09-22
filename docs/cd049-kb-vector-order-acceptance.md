# CD-049 验收：知识 chunk 向量「先删后加」改序 + 失败显式化

- 项目：星枢 Sync Hub｜实施：外部 agent（kimi，session_c61b685e，worktree `E:\xingshu-wt-cd049` 分支 `cd049-kb-vector`）
- 基线：`0e582e1`｜任务书：`E:\星枢-待办\星枢任务书-2026-09-17\T5-CD049-知识向量改序-任务书.md`
- 台账项：CD-049（核证新发现，原清单未列）

## 一、问题（核证）

`hub_mixins/knowledge.py::_embed_knowledge_chunks()` 原写序为 **先 delete(where=layer+entry_id) 再 add**，
整段由调用方 try/except 包着、失败只 `logger.warning`（D4 降级）。
最坏情况：**delete 成功、add 失败 → 该知识条目的全部 chunk 从索引消失**（知识 chunk 正文只存在于 chroma metadata，
SQLite 只有全文、chunk 需重切重编码），且无补偿、无 error 级告警。中间态：delete 与 add 之间任何读都是"零 chunk"。

## 二、改动

- **写序改为**：`get(现存 ids) → upsert 新 chunk（同 id 覆盖，幂等）→ 只删差集（旧 ids − 新 ids）`
  → 任意时刻该条目都有 chunk 可查；upsert 失败时旧 chunk 仍在（降级但可告警）
- **失败显式化**：upsert 失败 → `logger.error`（含 entry_id + chunk 数）+ 返回 0；差集删除失败 → `logger.error`
  （残留旧 chunk 会被下次同 id 覆盖，不许静默）；调用方 `logger.warning` → `logger.error`
- 未改：`kb_chunk_id` / `kb_chunk_metadata` 口径、返回值契约（正常 `len(ids)`；三个降级分支仍 0）、`if stale:` 守卫

## 三、验收（Hermes 独立复跑）

- **先红**（旧代码 `0e582e1`）：`AssertionError: 出现零 chunk 中间态: [['delete']]`，3 failed / 2 passed（日志 `E:\星枢-待办\T5-red-before-20260917.log`，实测存在 4577B）
- **转绿**：我复跑 `test_kb_vector_order + test_kb_unified_retrieval + test_embedding_k1 + test_chunk_stitch + test_seed_kb`
  → **29 passed**（既有 24 全绿 + 新增 5）
- **额外发现独立复现**：chromadb 1.5.9 下 `collection.delete(ids=[])` 确实抛
  `ValueError: Expected IDs to be a non-empty list, got 0 IDs` → kimi 报告的"`if stale:` 守卫是必需而非省一次空调用"成立
  （没有它，"新旧 chunk 完全一致"的幂等重灌路径会直接炸）；同时复现 `get(include=[])` 只返 ids 可用
- 未改既有测试断言（diff 仅 `hub_mixins/knowledge.py` +29/-7 与新增测试文件）

## 四、接受的取舍（kimi 主动上报）

任务书 T2 写「返回值语义按 §2-2 保持」，T5-3 要求「降级返回（不抛）」，两者字面有张力。
kimi 选择：内部捕获 → `logger.error` → 返回 0（与"降级返回 0"一致），调用方 except 保留为防御分支。
**接受**：失败不再把异常抛给调用方，而调用方原本也只是打日志，语义等价且错误级别更高。

## 五、残留（已登记，待拍板）

知识向量"漏建后如何补齐"三候选方案（见任务书 §3-T3 与 kimi 报告的 A/B/C）：
A 启动时对账 / B 维护端点 / C 失败事件投回主 loop。kimi 倾向 A+B 组合（成本低、无状态、互相补盲），C 待独立设计。
→ 建议登记 **CD-051**，由用户拍板后另派。
