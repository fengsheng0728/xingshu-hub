# 重运维端点统一门表（CD-061 / T22，2026-09-20）

同类重运维端点（全量重建、重判定、标定、清理、全量同步、待审队列访问）过同一张门表，
与 `POST /api/v1/knowledge/reindex`（CD-051 同门基准）同口径。
门表唯一真相源：`routes_common.OPS_GATED_ENDPOINTS`（routes_common.py:227）。

## 门口径（冻结）

- 判定：`principal_is_privileged(request.scope["principal"])` 为假 → **403**。
- 特权主体：`hub_token`，或归属 agent 的 `role ∈ {manager, orchestrator}`
  （`routes_common.principal_is_privileged`，None / 未知身份 / 查询异常一律 fail-closed）。
- `NO_AUTH`（SYNC_HUB_NO_AUTH=1，测试/开发态）不拦，与既有惯例一致。
- 统一入口：`await require_ops_privilege(request, endpoint, current_agent)`
  （routes_common.py:239），禁止在端点里复制粘贴判定。
- 403 detail 统一格式：`重运维端点 <path> 仅 manager/orchestrator 角色或 hub_token 可触发`。
- 拒绝路径落 `events` 审计（`ops_gate_denied`，payload 含 endpoint/requester/at），不静默；
  成功触发落 `ops_trigger`（payload 含 endpoint/requester/at/counts），
  counts 必须来自端点真实返回值或真实查询，不可得时如实写 `"unavailable"` 并注明原因，
  禁止占位 0。审计写失败只 `logger.warning`、不阻塞响应（D4）。

## 门表

| 方法 | 路径 | 文件:门调用行 | 403 detail | ops_trigger counts 键（来源） |
|---|---|---|---|---|
| POST | `/api/v1/knowledge/reindex` | routes_knowledge.py:176（CD-051 既有内联门，基准未改） | `需要 manager/orchestrator 角色或 hub_token 才能触发知识向量对账` | 既有 `knowledge_reconciled`（不在本任务范围）；拒绝走 T18 `_log_deny` |
| POST | `/api/v1/embeddings/rebuild` | routes_pipeline.py:113 | 统一格式 | `rebuilt_mem` / `stale_total`（`rebuild_embeddings` 返回值） |
| POST | `/api/v1/embeddings/calibrate` | routes_pipeline.py:140 | 统一格式 | `checked`（实际标定样本数 `len(samples)`） |
| POST | `/api/v1/chunks/reclassify` | routes_pipeline.py:88 | 统一格式 | `changed`（`reclassify_chunks` 返回值） |
| POST | `/api/v1/maintenance/cleanup` | routes_maintenance.py:28（本任务补 `Depends(get_current_agent)`） | 统一格式 | `memory_pool/events/tasks` 各 `{before, after}`（`_db_stats()` 真实查询 + `force_cleanup()` 返回值） |
| GET | `/api/v1/wiki/sync` | routes_wiki.py:264 | 统一格式 | `created/updated/skipped`（`wiki_sync.sync()` 返回值）；`background=1` 触发时计数不可得，如实记 `detail: unavailable: 后台异步执行…` |
| GET | `/api/v1/wiki/inbox` | routes_wiki.py:309 | 统一格式 | `total` / `returned`（本次真实 SQL 查询结果） |
| POST | `/api/v1/wiki/inbox/cleanup` | routes_wiki.py:345 | 统一格式 | `scanned` / `removed`（清理函数真实统计） |

机器断言：`tests/test_ops_gate_matrix.py`
- 结构断言：AST 扫描 `routes_*.py`，清单内每个 handler 判定路径上必须存在门调用
  （`require_ops_privilege` / `principal_is_privileged`）；
- 求差机制：扫描路径命中运维语义关键词
  （`reindex|rebuild|calibrate|reclassify|cleanup|wiki/sync|wiki/inbox`，行尾锚定）
  的路由，与 `OPS_GATED_ENDPOINTS` 双向求差，差集非空即红；
- 行为矩阵：清单内每个端点 × 非特权主体 → 403 + `ops_gate_denied`；
  × manager/orchestrator/hub_token → 不被门拦 + `ops_trigger`。

## 新增端点登记规程

新增具有重运维语义（全量重建/重判/清理/大批量导出等）的端点时，必须三件套同时落地，
缺一项结构断言即红：

1. **进门**：handler 首行 `await require_ops_privilege(request, "<路径>", current_agent)`；
   需要 `request: Request` 形参（无则补），计数取自端点真实返回值。
2. **进表**：把 `(METHOD, path)` 追加到 `routes_common.OPS_GATED_ENDPOINTS`。
   路径命中上述关键词时不登记会被求差断言拦下；不命中关键词的运维语义端点
   也要主动登记（求差只管关键词面，行为矩阵按清单全量跑）。
3. **进文档**：在本文件门表登记一行（方法/路径/门调用行/detail/counts 键与来源），
   并跑 `python -m pytest tests/test_ops_gate_matrix.py -q` 确认绿。
