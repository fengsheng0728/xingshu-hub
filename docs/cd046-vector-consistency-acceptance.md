# CD-046 验收：向量索引一致性（裂缝1）

- 项目：星枢 Sync Hub｜仓库 `E:\sync-hub-case`
- 基线：`c4d9f99`（CD-045 已收口）｜实施：外部 agent（kimi，session_c62c71ff）+ Hermes 验收
- 任务书：`E:\星枢-待办\星枢任务书-2026-09-17\T2-裂缝1-向量一致性-任务书.md`
- 判定依据：`docs/database-crack-triage-2026-09-17.md`（裂缝1：1A 顺序 / 1B 漏建 / 1C 覆盖腐烂 + 核证新增的删除孤儿向量）

## 一、修的是什么

| 缺陷 | 改前行为 | 改后 |
|---|---|---|
| 1A 顺序反了 | `_insert_new_memory_sync` 在事务**体内**调 `collection.add`，commit 失败回滚→索引留脏向量 | 事务内只 append 操作意图；`run_in_conn` 返回（已提交）后才执行 `_apply_vector_ops` |
| 1B 漏建静默 | chroma 写失败只 `logger.warning`，无补偿 | 单 op 失败 → `enqueue_after_commit("vector_index", …)` 落补偿事件；消费者用**库内 embedding blob** 重灌（不调模型） |
| 1C 覆盖腐烂 | 两个 `conflict_overwrite` 分支只改库、完全不碰索引 | 两分支都登记 `upsert`（新 embedding 为 None 时跳过，不用旧向量冒充新内容） |
| 删除孤儿（核证新增） | 全仓无任何地方删 memory 层向量 | `delete_memory` 命中时登记 `delete` op，提交后清索引；失败走同一补偿通道 |

向量索引由此降级为**「提交后副作用 + 事件补偿」**，与 CD-045 建立的 outbox 机制共用一张事件表。

## 二、改动清单

| 文件 | 改动 |
|---|---|
| `hub_mixins/memory.py` | 新增模块级 `_vector_metadata()`（键集合固定，未加密级字段）；`store_memory` 事务内登记 `_vector_ops`（两个覆盖分支各一处）；`_insert_new_memory_sync` 不再碰 chroma；新增 `_apply_vector_ops()`（逐条执行、单条失败不中断、失败入补偿）；`delete_memory` 登记 delete op |
| `hub_mixins/outbox.py` | 新增 `enqueue_after_commit()`（提交后独立入队，失败只告警不抛）；消费者支持 `vector_fn` 注入与 `vector_index` 事件分发（**未注入时标 failed**，不许静默）；`stats_snapshot()` 加 `by_type` |
| `hub_core.py` | 构造消费者注入 `vector_fn=self._reindex_vector_sync`；新增同步方法 `_reindex_vector_sync()`（delete / 查不到 / 无 embedding → 删 id；否则用库内 blob upsert） |
| `routes_dashboard.py` | `/api/v1/stats` 加 `outbox` 观测段（**任务书白名单写的 routes_server.py 是错的**，见 §四） |
| `tests/test_vector_index_consistency.py` | 新建 365 行、8 用例（`_FakeChroma` 内存索引 + 调用计数 + 可注入失败） |

## 三、验收证据（Hermes 独立复跑）

**先红（kimi 提交的原文，改前基线 `58d895e`）**：3 failed —— `assert 1 == 0`（commit 失败前 chroma 已写）、`assert '旧内容-甲' == '新内容-乙'`（覆盖后 metadata 仍旧）、`assert mid not in {...}`（删除后孤儿向量残留）。三条正对应 1A/1C/删除，**缺陷在改前可复现**。

**转绿**：`pytest tests/test_vector_index_consistency.py -q` → **8 passed**。
**定点套件**：`test_vector_index_consistency + test_outbox_audit_atomicity + test_shadow_pending` → **24 passed**（我复跑）。
**全量离线集**：**739 passed / 60 skipped / 5 deselected / 0 failed**（287.51s；基线 731 → +8，failed 0）——我独立复跑，与 kimi 自报一致。日志 `E:\星枢-待办\T2-regression-20260917.log`
**ruff**：5 个改动/新文件 `All checks passed!`
**回退门禁（我独立用 inspect 复核）**：`_insert_new_memory_sync` 源码内 `_chroma_collection` = **False**；`_apply_vector_ops` = True；`memory.py` 全文该符号仅出现 **1 次**。

**端到端真实 Hub + 真实 ChromaDB**（单测用假 collection，故单列此项；独立 config/db/chroma/端口 3078）
脚本 `E:\星枢-待办\_sync\cd046\e2e_vector_consistency.py`，结果 `…\cd046\e2e-result-20260917.json`：

| 步骤 | 实测 |
|---|---|
| V-1 写入 | 200 `write` → 真 chroma 出现该 id，`metadata.content = 客服偏好：不吃辣（v1）`，向量维度 384 |
| V-2 同 key 覆盖 | 200 `conflict_overwrite`（**同一 id**）→ `metadata.content` 刷成 `…改吃微辣（v2）`，`refreshed=true`（旧代码此处不刷新） |
| V-3 删除 | 200 `deleted` → 真 chroma 内该 id **消失**，`orphan_vector_gone=true` |
| V-4 补偿队列 | `event_outbox` 全程只有 `memory_audit` 且全 `done`，**无 vector_index 积压/失败**（在线路径成功，未走补偿） |
| V-5 观测 | `GET /api/v1/stats` 200 且含 outbox 段（pending/done/failed/last_error/drained/by_type） |

**CRLF**：改动文件 `\r\r\n` 计数全 0（kimi 自测；我复核 `git diff` 无整文件重写噪音）。

## 四、kimi 上报偏差的处理（逐条核实）

1. **白名单偏差（我写错了）**：`GET /api/v1/stats` 实际在 `routes_dashboard.py:42`，不在我写的 `routes_server.py`
   （后者只有 `/api/v1/buffer/stats`）。已核验 → kimi 改 `routes_dashboard.py` 是**正确处置**，`routes_server.py` 未动，偏差范围仅 7 行。
   **教训**：任务书里的文件白名单必须自己先 grep 确认端点归属，否则要么白做要么越界。
2. `tests/test_outbox_audit_atomicity.py` 未追加用例——白名单写的是"允许"非"必须"，vector_index 用例已在新文件全覆盖（含未注入 `vector_fn` → failed），接受。
3. `by_type` 实现为「pending 积压按 type 分组」——任务书未定口径，取向合理（观测积压），接受。
4. 用 `upsert` 替代原 `add` —— 幂等，且与消费者 replay 语义一致，接受。
5. `_vector_ops=None` 默认值兼容存量位置参数调用方（`tests/test_s3a_taint.py`），回归已验证，接受。

## 五、残留与观察

- **补偿队列本身的可用性**：`enqueue_after_commit` 失败只告警（业务已提交，不能反悔）+ 模块级计数 `_ENQUEUE_AFTER_COMMIT_FAILED`。
  即「向量写入失败 **且** 补偿入队也失败」时会静默丢补偿——概率极低（同一 SQLite 库，前一步刚成功），且已有 warning。
  更强的做法是在线路径失败时降级为同步重试一次，留作后续观察项，不在本轮扩大改动。
- **`_reindex_vector_sync` 用独立连接、未设 busy_timeout**：高并发下可能 `database is locked` → 该事件 attempts+1 下轮重试（可自愈），可接受。
- 知识层同类问题（`knowledge.py` 先删后加、失败静默、最坏 chunk 全丢）**已登记 CD-049**，不在本轮。
