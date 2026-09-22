# 星枢技术债轮 · 收口结论（2026-09-19 夜）

> 输入：09-17 收口时冻结的「下一轮队列」（`docs/database-crack-triage-2026-09-17.md` §7.3）+ 用户当晚拍板
> 拍板原文：`E:\星枢-待办\星枢任务书-2026-09-19\00-拍板记录-2026-09-19夜.md`
> 基线：本轮开工 HEAD `cca2d89`（09-17 收口）→ 收口 HEAD `0d3be0d`
> 验收口径：每项**独立先红**（在无修复树上跑新用例确认必红）或**独立复核**（Hermes 自建证据），不信执行方自报

## 一、本轮范围与拍板

| 议题 | 拍板 |
|---|---|
| CD-052 知识层正文来源 | **方案 A**（出口剥离 + chroma metadata 去正文 + 分层回查，对齐 CD-048 手法）；**B 不接受**（静态仍存 500 字明文，FDE 场景交付物跑在客户机器上，离线拷走 chroma 目录是真实攻击面） |
| CD-052 四条强制修正 | ① rebuild 清库重建 + 孤儿清理（孤儿哨兵断言）② `knowledge_delete` 补 chroma 删除并入本轮 ③ 验收含静态攻击测试（拷贝 chroma 目录纯文本检索命中必须 0）+ 未剥离出口负向探针 ④ 重建窗口语义冻死（fail-closed + 低峰 + 先 `hub_cli.py backup`） |
| 措辞更正（用户确认） | `document_chunks` **不参与 chroma**（无 embedding 列，`hub_mixins/ingest.py:370-372` 注释写明理由）→ 「按 document_chunks 重灌」不成立；重建语料只有 `knowledge_base` |
| CD-056 越权读 | **owner-only，不给 manager 留旁路**（manager 看下属走披露请求审批流才是正门）；并冻结全局策略「资源存在但无权 = 与不存在同响应」+ 矩阵断言 |
| MCP 两出口 | **拍「甲」**：fail-closed 到已发布摘要级 + 读审计（无主体记 `anonymous-tool`）；不拍「/mcp 收紧为仅 hub_token」（实测 Agent 端零 MCP 调用，乙切断的是未来外部客户端通道）。**CD-058 备注：不做完，MCP 特权全文永不恢复** |
| CD-057 | 优先级上调（性质是**已交付能力的回归**）；验收必须含「强制 chroma 故障 → SQLite 关键词路径真实返回行」；静默吞咽按 T2-6 一并治，不准只修 SQL 不修吞咽 |
| CD-059 | 单独立项，排 CD-057 之后 |

## 二、逐项结果（每项 = agent 一个 commit + Hermes 台账小 commit）

| 项 | 判定 | 修复 commit | 验收证据（Hermes 侧） |
|---|---|---|---|
| T8 · CD-030 + CD-035 残余 | 属实（CD-035 形态与台账不同：真因是 `hub_agent_lc.py:8-12` 模块级无保护 import） | `7eaba0e` → 合并 `fb2156f` | 独立先红 4 failed（`OperationalError: table shared_docs has no column named trust_level` + `ImportError`）→ 定点 13 passed |
| CD-051 知识向量对账（A+B） | 属实 | `3d062d7` → 合并 `c7eb5fd` | **独立真 ChromaDB 复核对账**：漏建补齐 / 清多余 / 幂等 / 全量四项全过 + chunker 独立重算期望 ids 严格相等（`E:\星枢-待办\_sync\cd051\real-chroma-check-20260919.json`）→ 定点 38 passed |
| CD-054 shared 组读审计 | 属实（两读端点零审计） | `a2b332a` → 合并 `ba5c4af` | 独立先红必红（退化红：改动前无审计调用）→ 定点 24 passed |
| CD-054 memory 组读审计 | **修正**（清点表说「无剥离待收编」，实测 5 端点均已有 owner 自查门 → 真缺口只是读审计） | `5b8ab32` → 合并 `c0f810d` | 定点 6 passed；**并上报跨 agent 读 versions 反例（→CD-056）** |
| CD-056 `GET /memory/{key}/versions` 越权读 | 属实（**本轮唯一真安全洞**：SQL 只按 memory_key 过滤，`agent_id` 参数未进 SQL） | `31ffc51` → 合并 `09e22c4` | 独立先红 4 failed（**真拿到 B 的版本全文**）→ 定点 14 passed；既有夹具同步 1 处（Hermes 裁决） |
| CD-055 `knowledge_base.embedding` 缺列 + wiki_sync 静默 | 属实（与 CD-030 同族：内联 DDL 与 alembic 漂移） | `d2fe365` → 合并 `34d587b` | 独立先红（`'embedding' not in cols`、0005 不存在）→ 定点 21 passed；降级标记落 `/api/v1/wiki/sync/status` 控制台可见（不是只进日志） |
| CD-052 方案 A（含四修正） | 属实（本体是「索引内 500 字明文且是检索回显源」） | `faad5ec` → 合并 `eb4bf79` | 新用例 17 passed，含 **S-2 真 chroma 目录字节搜哨兵零命中 + 反向对照**、S-3 owner 回查正文与重切段落逐字相等、S-6 孤儿哨兵 rebuild/reconcile 双向清理、E 组出口负向探针；既有受影响集 45 passed；既有断言同步 1 处（Hermes 裁决：`test_kb_unified_retrieval` 原从 metadata 读 content 验证旧 chunk 已删） |
| CD-054 MCP 出口（甲） | 属实 | `c2688dc` → 合并 `0d3be0d` | 独立先红=**真泄露**（`wiki_get` 把 200 字外尾部哨兵串原样返回，md/html 两档）→ 13 passed；`_log_read` 默认路径兼容性定向复跑 3 文件 16 passed |

## 三、回归基线（本轮新基线）

```
python -m pytest tests/ -q -rf -k "not test_cross_agent_403 and not test_memory_search_self_access and not test_memory_list_self_access" --ignore=tests/test_team_integration.py
→ 830 passed, 60 skipped, 5 deselected, 0 failed  (460.84s / 7:40, exit 0)
```
账目闭合：767（09-17 基线）+ 63（本轮新增：T8 5 / CD-051 13 / shared 4 / memory 6 / CD-056 5 / CD-055 7 / CD-052A 17 / MCP 6）= **830**，一条不多一条不少。

**负载敏感契约定性**：本轮首跑（当时三路 kimi 抢 CPU）`test_p99_latency_under_5ms` 曾 1 failed；机器空闲后全量 **0 failed**，该用例单独复跑 `10 passed` → **定性为负载 flake，非代码问题**（沿用 CD-034 R2 的「空闲窗口」纪律）。

## 四、上线运维动作（CD-052 的最后一公里）

1. 先 `python hub_cli.py backup --out <dir>`，选**业务低峰**
2. 对既有部署跑**一次** `POST /api/v1/embeddings/rebuild`（清库重建：先清空整集合，再按 DB 现存条目重灌）
3. 跑一次 `POST /api/v1/knowledge/reindex`（CD-051 入口）复检：`errors` 为空、`orphan_removed` 符合预期
4. **过渡期语义（冻死，不是故障）**：旧格式 chunk（无 `piece_index`）命中会被 **fail-closed 丢弃** →
   知识检索结果会暂时变少；重建期间检索返回 `degraded_reason=index_rebuilding`，不回退任何含明文旧路径

## 五、残余与后续队列

| 序 | 项 | 状态 |
|---|---|---|
| 1 | **CD-057** memory 关键词降级链断裂（FTS5 恒抛 + 静默吞 + 无 LIKE 兜底） | 任务书现成（`T16-...任务书.md`），执行方死于 kimi 配额，**零半成品**，窗口一开即重派 |
| 2 | **403 策略冻结 + 矩阵断言**（`/shared/docs/{doc_id}` 判定顺序未改） | 任务书现成（`T17-...任务书.md`），同上零半成品 |
| 3 | **CD-059** 403 拒绝不留痕 | 待派（口径已定：落 `gateway_read_log`、`granted_level="denied"`、中间件内惰性导入 `_log_read` 避免环、**禁记 token 明文**） |
| 4 | **CD-061** `POST /api/v1/embeddings/rebuild` 仅认证即可触发全量重建 | **待用户拍板**（是否与 CD-051 的 `reindex` 同门） |
| 5 | **CD-054 knowledge 组收编** | 待派——**骑在 CD-052 的回查层上做**（顺序反了会返工） |
| 6 | **CD-054 wiki 组收编** | 待语义拍板（记忆派生页跟记忆链 / `wiki/export` 联邦通道是否对普通 agent 关门） |
| 7 | CD-058 主体身份注入（contextvars） | 登记不做；备注：不做完 MCP 特权全文永不恢复 |
| 8 | CD-060 内联 DDL 与 alembic 全表漂移清单 | 登记（6 张表仅 alembic 路径有 / `team_members` 缺 `team_id` / 5 表列序漂移） |
| 9 | CD-062 `delete_memory` 不清理 `memory_versions`（孤儿版本不可达） | 登记，并入 CD-053 档案生命周期同族 |
| 10 | CD-063（本轮新登记）MCP `wiki_search` tags 档 snippet 不受 200 字约束 | 登记（超长 tags 有超 200 字泄漏面） |
| 11 | CD-053（CD-047 残余三项）、CD-034-R3（远端锚回拉） | 09-17 队列老项，本轮未动 |

## 六、本轮卡点（可复现）

- **kimi 5 小时配额打满**：T16/T17 在诊断阶段中断，两个 worktree **零半成品**（`git status` 空、HEAD 未变）→ 直接按原任务书重派，无需还原
- **未推提交**：主仓 master 领先 `origin/master` **43 个提交**（含 09-17 收口那批），内部仓未同步

## 七、收口时机器状态（可复现）

- 主仓 `master` HEAD `0d3be0d`，工作区仅运行产物（`wiki/**`、`audit/memory_pool.jsonl`、`ystore.db`），**worktree 仅主工作区**，存活外部 agent 0
- Agent 端 `E:\sync-hub-agent` HEAD `20e5de2`，工作区干净（本轮未动 Agent 端）
- 全量回归 `E:\星枢-待办\_sync\full-regression-20260919-night.log`
