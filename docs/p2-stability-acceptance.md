# 稳定性轮 P2 验收表 — SQLite 峰值并发 + ChromaDB 多进程/降级 定性

> 方案：《星枢-稳定性轮-执行方案.md》P2（55 分钟预算）
> 只测量不修（D4）：发现的每条风险 → 结论 + 拐点数据 + 缓解建议 → 登记台账
> commit：`（待填）`
> 脚本：`tests/stress_qualitative.py`（可复跑，Hub 生命周期自管）+ `tests/chroma_worker.py`
> 证据：`tests/p2-stress-results.json`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T2-1 SQLite 压测 | 阶梯并发 25/50/100/200 × 30s，经 HTTP 真实路径（缓冲路径 + 直写路径双测） | ✅ 四档数据齐全 | tests/p2-stress-results.json + 下方表格 |
| T2-2 ChromaDB 多进程 | 双进程同时打开 chroma_db，5 轮 × 2 进程 | ✅ 10/10 全部成功 | 脚本输出（round 1-5 全 OK，各 ~1.6s） |
| T2-3 降级路径 | 故障注入：ChromaDB 不可用 → 语义搜索回退 SQLite | ✅ 降级真实生效 | memory/search emb_unavail=True + 有结果=True；无 500 |
| T2-4 结论表 | 两条风险各有结论+拐点+缓解建议 | ✅ 见下 | 本表 + 台账 CD-016/017 |
| T2-5 回归 | 压测后双端 pytest 全绿 | （待填） | 回归输出 |

## 实测数据

### T2-1a 缓冲路径（knowledge/upsert → asyncio.Queue 写入缓冲，HTTP 真实路径）

| 并发 | ok | timeout | locked | peak_enq_lat | fallback | db_new | lost | drained |
|------|-----|---------|--------|--------------|----------|--------|------|---------|
| 25 | 799 | 0 | 0 | 2.950s | 0 | 799 | 0 | True |
| 50 | 709 | 0 | 0 | 4.302s | 0 | 709 | 0 | True |
| 100 | 527 | 0 | 0 | 10.983s | 0 | 527 | 0 | True |
| 200 | 415 | 0 | 0 | 31.130s | 0 | 415 | 0 | True |

**db_new == ok 精确匹配（逐条核对无丢失）**。fallback=0 说明 2000 队列在 200 并发 30s 内未满（写入缓冲容量充裕）。

### T2-1b 直写路径（memory/store → 三段去重，HTTP 真实路径，观察项）

| 并发 | ok | timeout | locked | peak_lat | avg_lat | db_total |
|------|-----|---------|--------|----------|---------|----------|
| 25 | 1423 | 0 | 0 | 4.370s | 0.519s | 370 |
| 50 | 1944 | 0 | 0 | 1.058s | 0.768s | 382 |
| 100 | 1889 | 0 | 0 | 2.561s | 1.601s | 382 |
| 200 | 1710 | 0 | 0 | 4.777s | 3.631s | 382 |

**db_total 停在 382 = 三段去重合并**（内容语义相似 → embedding 相似度 >0.90 合并/0.75-0.90 覆盖），设计行为非数据丢失。

### T2-2 ChromaDB 双进程

5 轮 × 2 进程同时 PersistentClient 打开 `./chroma_db` 并写入：round 1-5 全部 2/2 OK，单轮 1.5-1.7s。

### T2-3 降级路径故障注入

| 阶段 | 结果 |
|------|------|
| 正常态 memory/search | total=5, emb_unavail=False |
| 注入（kill Hub → rename chroma_db → 占位文件 → 启动） | Hub ready, chromadb=disabled |
| 故障态 memory/search | total=5, **emb_unavail=True**, 有结果=True（SQLite 向量/FTS5/LIKE 降级生效，无 500） |
| 故障态 semantic_search | status=200, body=`{"status":"error","message":"ChromaDB 未初始化"}`（**无降级**） |
| 恢复 | chromadb=ok |

## 稳定性定性结论

### 1. SQLite 峰值并发（原「假想风险」→ 实测「有条件风险，低危」）

**结论：有条件风险（低危）**。0 locked / 0 timeout / 0 数据丢失（逐条核对），但 200 并发时峰值入队延迟 31s——瓶颈在**入队路径的同步 SQLite 写**（见 CD-017），不在缓冲 worker 本身。

- 拐点：25→200 并发，peak 延迟 2.95s→31.13s 线性爬升；锁竞争未触发（busy_timeout=5000 兜底，但代价是请求排队）
- 缓解建议：① `_record_trace` 的 buffer_log 写改异步（`asyncio.to_thread` 或并入 worker 批写）；② 维持 WAL + 写入缓冲架构；③ 监控 buffer/stats queue_depth，fallback>0 时扩容

### 2. ChromaDB 多进程（原「假想风险」→ 实测「假想风险，无冲突」）

**结论：假想风险**。5 轮 × 2 进程同时读写同一 chroma_db：10/10 成功，无 filelock 冲突。

- 根因澄清：上次「90s 超时」是**首次 embedding 模型下载**（all-MiniLM-L6-v2 79MB ONNX），非文件锁。chromadb 1.5.9 用 SQLite(WAL) 做锁，多进程读写不冲突
- 观察点：Hub 运行中 rename chroma_db 被 WinError 5 拒（Windows 句柄占用，正常现象）
- 缓解建议：无需处理；保持「chroma_db 是冗余向量索引、SQLite memory_pool 是真相源」——P2 实战验证误删后可从 embedding blob 全量重建 433/433

### 3. 降级路径（实测「真实生效，但有盲区」）

**结论：降级真实生效**。ChromaDB 不可用时 memory/search 三级降级（SQLite 向量→FTS5→LIKE）返回结果 + `embedding_unavailable` 标记，无 500。

**新发现盲区（CD-016）**：`semantic_search` 端点（disclosure.py）硬依赖 ChromaDB，无降级——不可用时返回业务 error（HTTP 200 但空结果）。该端点非 Agent 主路径（Agent 用 memory/search），但 dashboard/第三方调用受影响。

## 台账登记

- **CD-016**：`/api/v1/memory/semantic_search` 硬依赖 ChromaDB 无降级（memory/search 有三级降级）| 中 | 待做：补 SQLite 向量降级（复用 memory/search 路径）或文档标注
- **CD-017**：`knowledge_upsert` 入队路径 `_record_trace` 同步 SQLite 写（buffer_log INSERT+commit）在 asyncio 事件循环阻塞——200 并发 peak 31s | 中 | 待做：改 `asyncio.to_thread` 或并入缓冲 worker 批写

## 本阶段明确不做（已遵守）

- 不修压测发现的任何问题（D4）：CD-016/CD-017 仅登记
- 不引入 PostgreSQL/换数据库/调缓冲参数
- 不做长时间 soak 测试（30s/档足够定性）
