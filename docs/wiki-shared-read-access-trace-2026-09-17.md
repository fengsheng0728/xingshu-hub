# wiki / 共享文档 读路径现状取证（2026-09-17）

> 用途：CD-033（网关旁路收编）后半段需要先定"wiki 页 / 共享文档的披露语义"，本报告提供决策所需的实测事实。
> 取证方式：只读扫描（`wiki/**` 文件 + `sync_hub.db` + `routes_*.py` / `hub_mixins/*` 代码），未改任何代码。

## 一、wiki 页面构成（415 页，实测分类）

分类依据 = frontmatter 里的来源键（`memory_id` / `entry_id` / 都没有）：

| 类别 | 页数 | 说明 |
|---|---|---|
| **memory 派生**（带 `memory_id` + `owner`） | **395** | 其中 **382 页属 owner=`p2-stress-agent`（压测遗留）**；其余 13 页：`fushi-cs-zero` 5、`workbench-verify`/`u4-worker`/`tc-remote-owner`/`split-smoke-a0abaa`/`e2e2-coldstart`/`electron-dashboard`/`p0-e2e`/`guard-test-A` 各 1 |
| **knowledge 派生**（带 `entry_id`） | 12 | 对应 `knowledge_base` 12 行（含 `integration:*` 测试行） |
| **手写页**（无 `entry_id`/`memory_id`） | **5** | `concepts/finaltest.md`、`concepts/v.md`、`concepts/xss测试.md`、`concepts/验收测试.md`、`entities/_vfy-all.md` |
| 聚合/旁支 | 3 | `index.md`、`log.md`、`SCHEMA.md` |

**关键代码事实**：手写页是被**显式保护**的——
`wiki_sync.py:231-259` 的 `_clean_orphans()` 扫描 `entities/concepts/comparisons/queries`，
凡"路径不在 DB 派生预期集合里"的页面会进一步检查 frontmatter 是否有 `entry_id`/`memory_id`；
**没有 DB 引用则 `continue`（保留，注释原文："手动创建的 wiki 页面，保留"）**。
→ 所以"手写页"在本产品里是一个**被支持的既有概念**，不是我会话里临时造的分类。

**页面正文与来源一致**（样本实测）：`wiki/concepts/memory-tc-secret.md` 正文 == `memory_pool.content`
（`"这是一条敏感信息，只有 manager 能看到全文"`），页脚带"来源: 数据库记忆池 | 同步时间"。
→ 说明"页面按来源过滤"技术上可落地：`page → memory_id/entry_id → owner + 自身密级 → 复用 8 规则披露链`。

## 二、现在谁能读到（读路径守卫现状）

**wiki：任何"已认证 Agent"可读全部 415 页全文。** 逐端点实测（`routes_wiki.py`）：

| 端点 | 守卫 | 是否剥离 |
|---|---|---|
| `GET /api/v1/wiki/pages` | 仅 `Depends(get_current_agent)` | 无（元数据） |
| `GET /api/v1/wiki/page/{page_path:path}` | 仅认证 | **无（正文全文）** |
| `GET /api/v1/wiki/search` | 仅认证 | **无（含正文 snippet）** |
| `GET /api/v1/wiki/search/hybrid` | 仅认证 | **无（≈160 字片段）** |
| `GET /api/v1/wiki/export` | 仅认证 | **无（全量 markdown dict）** |
| `GET /api/v1/wiki/graph` | 仅认证 | 不适用（无正文） |

**没有**角色门、**没有**按 owner/密级过滤、**不过网关**、**无读审计**——与 `docs/gateway-bypass-inventory.md`（D3 清点表）判定一致。

**共享文档**（`routes_shared.py` / `shared_workspace.py:301-323`）：有一套**可见性**语义但无密级分级：
`can_access()` = 创建者本人 **OR** `visibility != "private"`（即 `team` 默认 → **全员可读**）**OR** 在 `allowed_agents` 白名单。
读端点（`GET /shared/docs`、`GET /shared/docs/{doc_id}`）同样只挂认证、不经网关、无读审计。

**当前库内身份**（`agents` 表 29 条，绝大多数是历史测试账号；`role` 分布含 worker/manager/orchestrator）：
即"任何一条注册 Agent 都能读全部 wiki 全文 + 全部 team 可见的共享文档"。

## 三、数据真实性（会影响语义定档与后续验收口径）

| 表 | 行数 | 备注 |
|---|---|---|
| `memory_pool` | 395 | **382 是 `p2-stress-agent` 压测残留**（真实业务记忆只有个位数） |
| `knowledge_base` | 12 | 含 `integration:example_connector` 4 / `integration:dummy2_finance` 2 等测试行 |
| `wiki_inbox` | 393 pending + 3 approved | pending 主要是压测产物 |
| `shared_docs` | 218 | 大量 `Pytest Report` 等测试文档 |

→ **定语义/验收之前应先把压测与测试残留清掉**：否则任何过滤规则都会淹没在 382 条垃圾里，
且 `GET /api/v1/wiki/sync` 的成本被抬到 20s+ 量级（已知 CD-041/CD-042 的坑）。

## 四、三个候选语义的影响面（供拍板）

| 选项 | 含义 | 影响面 / 风险 |
|---|---|---|
| **A 按来源过滤（推荐）** | wiki 页跟随来源：有 `memory_id` 的走该记忆的披露级别判定；有 `entry_id` 的走知识披露语义；手写页另定 | 改动集中：`routes_wiki.py` 4 个读端点 + 一个"页→来源"解析；395+12 页都有明确来源，覆盖完整。**前提**：知识/记忆的"企业已发布对全员可见"语义得先定（见下） |
| **B A + 手写页视为"企业已发布=全员可见"** | 同上，并把 5 个手写页显式定为全员可见（与 `_clean_orphans` 的保护语义一致） | 与 A 同批落地，只是把手写页说清楚。**我建议 A、B 合并采纳** |
| **C 一律走现有披露链兜底** | 所有页面按现有 8 规则链判定，判不出即 NONE（不放行） | **危险**：worker 查他人页面全部 NONE，等于员工读 wiki 基本为空——与已实测的"worker 查知识库 = 0 条"是同一条死路径；落地即穿帮 |

## 五、我的建议（待拍板）

1. **先清残留**（压测页 + `p2k-*` 知识行 + 测试文档），把 wiki 恢复到"能看清真实内容"的状态——这一步不涉及语义决策，可立即做；
2. **采纳 A+B**：wiki 页跟随来源；5 个手写页视为"企业已发布=全员可见"；
3. **同时定"企业已发布知识对全员可见"的语义**（写入时给 `allowed_viewers` 全员标记，或在披露链给 knowledge/wiki 层一条公共区规则）——否则 wiki 页面过滤后 worker 依然看不到任何东西，收编成了"收得更严但更没用"；
4. CD-033 的 memory/knowledge 半边语义已清楚（走既有披露链），**可以先行收编**，不必等 wiki 半边。
