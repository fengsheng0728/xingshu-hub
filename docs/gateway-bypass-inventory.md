# 网关旁路清点表（D-3 / 3-4）

> 仓库：`E:\sync-hub-case`｜基线 HEAD：`90c595b`｜清点日期：2026-09-10
> 任务书：`E:\星枢-待办\星枢任务书-2026-09-10\D-3-3-4-网关旁路清点-任务书.md`
> **本表只清点登记，不收编、不改代码。** 收编属后续任务（与 D-7/D-8 routes 拆分冲突，禁止在本任务动手）。

---

## 一、T1 全量枚举（命令与结果）

枚举脚本（只读扫描 `@router.<method>("<path>")` 装饰器，含多行装饰器与 `path:path` 参数的逐行正则）：

```python
python - <<'EOF'
import re, glob
total = {}
grand = 0
gets = {}
ggrand = 0
pat = re.compile(r'@router\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']')
for f in sorted(glob.glob('routes*.py')):
    src = open(f, encoding='utf-8').read()
    ms = pat.findall(src)
    if not ms:
        print(f'{f}: 0 (no decorators)')
        continue
    mod = f.replace('routes_','').replace('routes','(routes)').replace('.py','')
    n = len(ms)
    g = sum(1 for m,_ in ms if m=='get')
    total[mod] = n
    gets[mod] = g
    grand += n
    ggrand += g
    print(f'{mod}: {n} endpoints, GET={g}')
print('---')
print('modules:', len(total), 'total endpoints:', grand, 'GET total:', ggrand)
EOF
```

实际输出（照抄）：

```
routes.py: 0 (no decorators)
access: 8 endpoints, GET=3
agents: 5 endpoints, GET=1
audit: 8 endpoints, GET=5
routes_automation.py: 0 (no decorators)
routes_common.py: 0 (no decorators)
disclosure: 2 endpoints, GET=0
federation: 2 endpoints, GET=1
gateway: 1 endpoints, GET=0
hubagent: 8 endpoints, GET=2
integrations: 8 endpoints, GET=2
keys: 3 endpoints, GET=1
knowledge: 7 endpoints, GET=4
maintenance: 5 endpoints, GET=4
memory: 9 endpoints, GET=2
n1: 3 endpoints, GET=1
notifications: 6 endpoints, GET=2
pipeline: 9 endpoints, GET=2
report: 1 endpoints, GET=1
server: 2 endpoints, GET=1
sessions: 3 endpoints, GET=1
shared: 5 endpoints, GET=2
tasks: 10 endpoints, GET=2
team: 11 endpoints, GET=4
wiki: 11 endpoints, GET=8
---
modules: 22 total endpoints: 127 GET total: 49
```

**核对结论：模块数 22、端点总数 127、GET 合计 49，与任务书基准完全一致（差 0）。** 各模块端点数（team 11 / wiki 11 / tasks 10 / memory 9 / pipeline 9 / access 8 / audit 8 / hubagent 8 / integrations 8 / knowledge 7 / notifications 6 / agents 5 / maintenance 5 / shared 5 / keys 3 / n1 3 / sessions 3 / disclosure 2 / federation 2 / server 2 / gateway 1 / report 1）逐项与基准相符。`routes.py` / `routes_automation.py` / `routes_common.py` 无 `@router` 装饰器（装配层/公共依赖），不计入。

读语义端点 = 49 GET + **17 个读语义 POST**（见下表，POST 按读码判定：返回数据而非改状态）= **66 行**。写语义端点（61 个）不入表。

读语义 POST 判定明细（17 个）：`/api/audit/verify`、`/api/audit/disclosure/replay`、`/api/audit/disclosure/simulate`、`/api/v1/gateway/read`、`/api/v1/memory/disclose`、`/api/v1/memory/semantic_search`、`/api/v1/memory/search`、`/api/v1/knowledge/auto-complete`、`/api/v1/chunks/search`、`/api/v1/embeddings/calibrate`、`/api/v1/team/proxy/disclose`、`/api/v1/team/disclose/remote`、`/api/v1/agents/bootstrap`、`/api/v1/integrations/{name}/test`、`/api/v1/hub-agent/test`、`/api/v1/hub-agent/audit/{request_id}`、`/api/v1/hub-agent/chat`。

---

## 二、T2 逐端点判定表（66 行，一行不漏）

判定口径：`已经网关` = 内容读取且走 `POST /api/v1/gateway/read`；`结构性例外` = 非内容读取；`待收编` = 内容读取且直接返回未剥离正文（本表把「有剥离但绕网关入口、不落 `gateway_read_log`」的 3 个内容读取端点也归入 `待收编`，单列标注，见 T3 注）。

### gateway（1）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| POST /api/v1/gateway/read | gateway | memory_pool 记忆正文（kind=semantic/memory）、document_chunks 段落正文（kind=doc） | 已经网关 | 有 | routes_gateway.py:150-156 逐条 `hub.disclosure.disclose_for_principal(...)` 后按 `_rank` 过滤；:188-202 doc 路径按 chunk `disclosure_level` 逐段剥离计 `stripped`；:131/:159/:204 三种 kind 均 `_log_read(...)` 落 `gateway_read_log` |

### memory（5 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/memory | memory | memory_pool 表正文 content+summary 全量列表（owner 范围） | 待收编 | 无 | hub_core.py:527-534 `SELECT memory_id, memory_key, content, summary ... FROM memory_pool WHERE owner_agent_id = ?`；:542 `"content": row[2], "summary": row[3]` 原样入 dict 返回；全程无 disclose_for_principal |
| GET /api/v1/memory/{memory_key}/versions | memory | memory_versions 表历史版本正文 content | 待收编 | 无 | hub_mixins/memory.py:371-384 `SELECT v.id, v.version, v.content, v.summary ... FROM memory_versions` 直接 `return {..."content": r[2]...}`，无任何披露判定；仅 routes_memory.py:98 owner 自查 |
| POST /api/v1/memory/search | memory | memory_pool 表正文 content+summary（embedding/FTS5/LIKE 三路检索） | 待收编 | 无 | hub_mixins/memory.py:452-487 `SELECT memory_id, memory_key, content, summary ... FROM memory_pool`，:482-483 `"content": row[2], "summary": row[3]`；disclosure_level 被 SELECT 出（row[10]）仅展示不参与过滤，无 disclose_for_principal |
| POST /api/v1/memory/disclose | memory | memory_pool 记忆正文（按需披露申请应答） | 待收编（有剥离，绕网关入口） | 有 | hub_mixins/disclosure_ops.py:27 委托 → disclosure.py:318-342 `level = self.disclose_for_principal(...)`、`if level == DisclosureLevel.NONE: continue`、`disclosed_content = self._extract_by_level(...)`；落 `_log_disclosure`（disclosure.py:359）但不落 gateway_read_log |
| POST /api/v1/memory/semantic_search | memory | memory_pool 记忆正文（语义检索） | 待收编（有剥离，绕网关入口；网关 kind=semantic 即包它，功能重复） | 有 | hub_mixins/memory.py:589 委托 → disclosure.py:449-456 `disclose_for_principal` + NONE 跳过 + `_extract_by_level`；降级路径 disclosure.py:505-511 同样逐条判定。非 gateway/read 路径 |
### knowledge（5 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/knowledge | knowledge | knowledge_base 全表（含 content 正文） | 待收编 | 无 | routes_knowledge.py:44-47 → hub_mixins/knowledge.py:69-71 `SELECT * FROM knowledge_base ...`；`_knowledge_row_to_dict`（knowledge.py:51）只剔 bytes 字段，content 原样返回；仅登录即可，无角色门 |
| GET /api/v1/knowledge/{entry_id} | knowledge | knowledge_base 单条正文 content | 待收编 | 无 | routes_knowledge.py:50-59 → hub_mixins/knowledge.py:63-68 `SELECT * FROM knowledge_base WHERE entry_id = ?` 原样返回 |
| GET /api/v1/knowledge/from-memories | knowledge | memory_pool 全库 tags + content 前 200 字截段 + owner | 待收编 | 无 | routes_knowledge.py:22-25（**无 get_current_agent 认证依赖**）→ hub_agent.py:306-336 `SELECT tags, content, owner_agent_id FROM memory_pool`，:316 `"suggested_content": sample.get("content", "")`（content[:200]），跨全 agent 无剥离 |
| GET /api/v1/knowledge/graph/data | knowledge | 图谱节点元数据（id/title/category/importance/tags）+ 边，不含正文 | 结构性例外（控制台聚合可视化数据，且有节点级权限过滤） | 有（节点级，非段落级） | hub_mixins/knowledge.py:98-108 `DisclosureEngine(self)._calculate_disclosure_level(...)`，NONE → `continue` 隐藏节点；:111-117 nodes 只含 title/tags 等，无 content 字段 |
| POST /api/v1/knowledge/auto-complete | knowledge | 不读库——把 title 发给 LLM，返回生成的 entry | 结构性例外（AI 生成辅助，非库内容读取） | 不适用 | routes_knowledge.py:13-19（handler 无认证依赖；中间件仍要求 bearer token）→ hub_agent.py:266-298 只 POST 给 LLM，不查任何表 |

### wiki（8 GET，全部入表）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/wiki/pages | wiki | wiki 页面元数据清单（path/title/type/tags，来自 frontmatter），不含正文 | 待收编（弱：仅元数据，仍旁路网关） | 无 | routes_wiki.py:19-20 → wiki_engine.py:145-176 `list_pages()` 只提取 frontmatter，不返回 body；无任何过滤 |
| GET /api/v1/wiki/export | wiki | 全部 wiki .md 文件全文 dict（pages[path]=全文） | 结构性例外（联邦快照通道，任务书点名例外类） | 无 | routes_wiki.py:36-37 `with open(path...) pages[...] = f.read()`；消费方为联邦拉取 wiki_sync.py:156 `f"{hub_url}/api/v1/wiki/export"`。**注意**：任何持 key 的 agent 也能直接调到未剥离全文，见 T3 灰区注 |
| GET /api/v1/wiki/page/{page_path:path} | wiki | 单个 wiki 页面 markdown 原文全文（或 md_to_html 渲染）+ frontmatter meta | 待收编 | 无 | routes_wiki.py:93-94 `content = f.read()` → :112-118 `return {..., "content": content}` 直接返回，无剥离 |
| GET /api/v1/wiki/search | wiki | 搜索结果（path/title/type/tags + snippet 正文片段） | 待收编 | 无 | routes_wiki.py:159-160 `content = f.read()` → :191 `snippet = body[start:end]` → :198-204 直接返回；snippet 是原文切片 |
| GET /api/v1/wiki/search/hybrid | wiki | 搜索结果（path/title/tags/score + snippet ≈160 字正文片段） | 待收编 | 无 | routes_wiki.py:130-131 → wiki_engine.py:334-345 `snippet = body[start:end]`；:349 `r.pop("content")` 弹出全文字段但 snippet 仍为原文切片 |
| GET /api/v1/wiki/graph | wiki | 图谱结构（节点 id/label=页面标题/group + wikilink 边），无正文 | 结构性例外（结构聚合视图，仅标题与链接关系） | 不适用 | routes_wiki.py:212-214 → wiki_engine.py:182-209 `get_graph()` 只输出 `{"id","label","group"}` 节点与 `{"source","target"}` 边 |
| GET /api/v1/wiki/sync | wiki | 不返回内容——GET 形态的写动作触发（DB→Wiki 落盘同步 + 可选联邦拉取） | 结构性例外（同步触发动作，非内容读取） | 不适用 | routes_wiki.py:223-224 `sync(federate=federate)` → wiki_sync.py:270-285 实为落盘同步，改状态 |
| GET /api/v1/wiki/inbox | wiki | wiki_inbox 表待审行（id/page_path/title/status/source/created_at），无正文 | 结构性例外（审批队列元数据/控制台审查视图） | 不适用 | routes_wiki.py:239 `SELECT id, page_path, title, status, source, created_at FROM wiki_inbox WHERE status='pending'` |

### shared（2 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/shared/docs | shared | shared_docs 表元数据列表（doc_id/title/created_by/block_count/visibility/allowed_agents），按 visibility 过滤，无正文 | 待收编（弱：仅元数据，仍旁路网关） | 无（有 visibility 过滤但非 disclosure 引擎） | routes_shared.py:61 → shared_workspace.py:251-254 `SELECT doc_id, title, ... FROM shared_docs`；:259-268 private 过滤为自研可见性逻辑 |
| GET /api/v1/shared/docs/{doc_id} | shared | 共享文档正文全文（CRDT content 字符串） | 待收编 | 无（仅 can_access 可见性门，非段落级剥离） | routes_shared.py:83-88 `content = await ws_inst.get_doc_content(doc_id)` → `return {"doc_id":..., "content": content}`；shared_workspace.py:276-277 `return str(ydoc["content"])` 原样返回 |

### hubagent（5 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| POST /api/v1/hub-agent/chat | hubagent | memory_pool 正文 + knowledge_base 正文（经 LLM 工具拼进 reply 返回） | 待收编 | 无 | hub_agent_lc.py:108-110 `SELECT content ... FROM memory_pool WHERE ... LIKE`；:126 `SELECT title, content ... FROM knowledge_base ... LIKE`；全函数无 disclose/chunk_level 调用；routes_hubagent.py:144 `return {"status":"ok","reply": reply}` 直接出参；**端点无 get_current_agent 鉴权依赖**（routes_hubagent.py:74-75） |
| GET /api/v1/hub-agent/chat/history | hubagent | hub_agent_conversations 全量 content（工具结果——即未剥离记忆/知识正文——被持久化为 assistant 消息） | 待收编 | 无 | routes_hubagent.py:162-168 直接返回 `m.content`；hub_agent_lc.py:44-47 `SELECT role, content FROM hub_agent_conversations`；写侧 :62-64 把 ToolMessage 当 assistant 落库，无剥离；**无鉴权依赖**（routes_hubagent.py:163） |
| GET /api/v1/hub-agent/config | hubagent | hub_agent_config dict（api_key 默认脱敏） | 结构性例外（自身配置读） | 不适用 | routes_hubagent.py:38-41 → hub_agent.py:126-133 `_get_config()`。附带观察：`?raw=true` 返回 api_key 原文且端点无 get_current_agent 依赖 |
| POST /api/v1/hub-agent/test | hubagent | LLM 连通性 ping 响应 | 结构性例外（运维自检） | 不适用 | hub_agent.py:181-207 只发一次 chat/completions |
| POST /api/v1/hub-agent/audit/{request_id} | hubagent | disclosure_requests 单行元数据 + LLM 判定（不写库） | 结构性例外（审计引擎动作） | 不适用 | routes_hubagent.py:53-66 → hub_agent.py:209-264 `audit_disclosure` 仅 LLM 调用返回 decision，无 DB 写 |

### audit（8 读端点，全为读语义）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| POST /api/audit/verify | audit | 链完整性布尔 + 各链 checked 计数（无内容行） | 结构性例外（审计自检） | 不适用 | routes_audit.py:43 `verify_all(...)` → audit_chain.py:674-682 只做 hash 校验，返回 valid/计数 |
| GET /api/audit/disclosure/rules | audit | 披露规则表 dict（规则定义） | 结构性例外（披露引擎自身规则配置读） | 不适用 | routes_audit.py:59-60 `from disclosure_rules import rule_table; return {"rules": rule_table()}`；disclosure_rules.py:55-57 返回 RULES 副本 |
| POST /api/audit/disclosure/replay | audit | disclosure_log 重放比对统计 + mismatch 条目（log_id/memory_id/级别/规则 ID，无正文） | 结构性例外（审计自检：模拟器 vs 历史判定一致性） | 不适用 | routes_audit.py:69 `replay_disclosure_log(...)` → disclosure_rules.py:205-213 返回字段仅计数与判定差异 |
| GET /api/audit/events | audit | audit_log 审计行（log_id/entry_type/ref_table/payload/entry_hash）+ facets | 结构性例外（审计中心查审计数据本身；manager/orchestrator 角色门） | 不适用 | routes_audit.py:141 角色门；:120-130 `_query_audit_events` 直接 SELECT audit_log，:144-148 返回 |
| GET /api/audit/reads | audit | gateway_read_log 读审计行（requester/kind/query/剥离数） | 结构性例外（读审计自身检索；manager 门） | 不适用 | routes_audit.py:155 角色门；:168-173 `SELECT * FROM gateway_read_log ...` 返回；query 字段存检索词（截 200 字符，routes_gateway.py:52），非内容正文 |
| GET /api/audit/last-verify | audit | 上次 verify 结果 + 三链覆盖计数 | 结构性例外（运维统计/审计自检卡片；manager 门） | 不适用 | routes_audit.py:180 角色门；:191-201 只取 verify payload 摘要 + COUNT(*) |
| GET /api/audit/export | audit | audit_log 行 CSV/JSON 导出 | 结构性例外（审计导出；manager 门，导出动作本身入链） | 不适用 | routes_audit.py:224 角色门；:226-227 复用 `_query_audit_events`；:229-235 导出动作入审计链；:245-253 写 CSV/JSON 响应 |
| POST /api/audit/disclosure/simulate | audit | 披露判定级别 + 命中规则 + 逐规则轨迹 + 输入回显（不返回记忆正文） | 结构性例外（披露模拟器，审计自检工具；manager 门） | 不适用 | routes_audit.py:277 角色门；虽 :293 真实 `SELECT * FROM memory_pool` 加载行，但响应 :362-376 只含 level/hit_rule/trace/inputs，无 content/summary 字段 |

### team（6 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/team/stats | team | 计数聚合：agents 状态、tasks by-status、memory_pool by-kind、automation 状态 | 结构性例外（运维统计，只取 kind/status 列计数，无正文） | 不适用 | routes_team.py:51-53 `SELECT status, assigned_agent_id FROM tasks` / `SELECT kind FROM memory_pool`；:79-83 仅返回 dict 计数 |
| GET /api/v1/team/discover | team | UDP 发现的对端 Hub peers 列表 | 结构性例外（运维/发现） | 不适用 | routes_team.py:91-96 返回 `discovery.peers` |
| GET /api/v1/team/ping | team | hub_id 可达性 | 结构性例外（健康检查） | 不适用 | routes_team.py:102 返回 `{"status":"ok","hub_id":...}` |
| GET /api/v1/team/members | team | team_members 表配对记录（hub_url/role/心跳，不含正文） | 结构性例外（自身配置/联邦成员快照读） | 不适用 | routes_team.py:108 → hub_mixins/team.py:180-181 `SELECT id, remote_hub_id, remote_hub_url, ... FROM team_members`（无 content 字段） |
| POST /api/v1/team/proxy/disclose | team | memory_pool 记忆正文（跨 Hub 检索，被调端） | 结构性例外（联邦披露通道——自带披露引擎裁决+剥离+审计，属联邦快照类） | 有 | routes_team.py:323 `hub.disclosure.disclose_for_remote(virtual_agent, ...)` → disclosure.py:617-626 `level = self._calculate_disclosure_level(...)`、`if level == NONE: continue`、`content = self._extract_by_level(mem_dict, level)` |
| POST /api/v1/team/disclose/remote | team | 远端 Hub 返回的已剥离记忆（调用端转发） | 结构性例外（联邦披露调用端——不取本地内容，仅转发；剥离在对端执行） | 有（对端执行） | routes_team.py:384-392 组装请求 POST 到对端 `/api/v1/team/proxy/disclose`，原样返回其响应 |

### n1 / agents / sessions（4 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/n1/reviews | n1 | review_queue 审批行（detail 为 endpoint+params，如 memory_key/doc_id，无内容正文） | 结构性例外（审批队列自检，manager/orchestrator 角色门） | 不适用 | routes_n1.py:146-149 角色门；:153-154 `SELECT * FROM review_queue WHERE item_type='n1_delete' ...` |
| GET /api/v1/agents/quota | agents | agent_quotas 配额配置行 | 结构性例外（运维配置读） | 不适用 | routes_agents.py:30 `SELECT agent_id, qps_limit, mode, window_sec, burst FROM agent_quotas` |
| POST /api/v1/agents/bootstrap | agents | 自身 workspace（tasks SELECT * 含 title/description）、自身 session_archives 摘要、hub_agent 配置（api_key 已脱敏）、missed 计数（内含 register 写） | 结构性例外（连接初始化握手；仅取注册者本人数据，T0-2 凭据门）。边界项：sessions 段返回 summary/key_facts 正文无剥离，见 T3 灰区注 | 无 | routes_agents.py:140 `hub.get_agent_workspace(...)` → hub_mixins/dashboard.py:207 `SELECT * FROM tasks`；routes_agents.py:146 `hub.get_recent_sessions(...)` → hub_core.py:782 `SELECT ... title, summary, key_facts ... FROM session_archives`；无任何 disclosure 调用 |
| GET /api/v1/sessions/recent | sessions | session_archives 的 title/summary/key_facts 正文 | 待收编（边界：硬身份门仅本人可读，缓释；是否收编取决于治理口径是否把 session_archives 归入内容） | 无 | routes_sessions.py:52 `hub.get_recent_sessions(agent_id, limit)` → hub_core.py:782-802 `SELECT agent_id, local_session_id, title, summary, key_facts, ... FROM session_archives`，:795-796 `"summary": r["summary"], "key_facts": json.loads(...)` 原样返回；全链路无 disclose_for_principal / chunk_level；身份门 routes_sessions.py:48-50 |

### tasks / notifications（4 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/tasks | tasks | tasks 表全行（含 description/result）+ blocked_by + subtask_summary | 结构性例外（任务协作平面，非记忆/知识/wiki 内容存储） | 不适用 | routes_tasks.py:32-37 `SELECT * FROM tasks ... LIMIT 200` 直接返回 |
| GET /api/v1/tasks/{task_id}/subtasks | tasks | tasks 表子任务行 + 完成聚合 | 结构性例外（同上，任务协作平面） | 不适用 | hub_mixins/tasks.py:388-396 `SELECT * FROM tasks WHERE parent_task_id = ?` 直返 |
| GET /api/v1/messages | notifications | messages 表私聊正文（content 字段） | 结构性例外（点对点私聊通道，自带 ACL 仅限本人；非记忆/知识/wiki 内容域）。注：返回未剥离 content，见 T3 灰区注 | 无 | routes_notifications.py:60-65 `SELECT * FROM messages WHERE from_agent_id=? OR to_agent_id=?` → `dict(r)` 直返，无 disclosure 调用；身份门 :55-57 |
| GET /api/v1/notifications | notifications | notifications 表行（title/body，body 可含私聊前 200 字摘要） | 结构性例外（自身通知箱，运维面；自带 agent_id==current_agent 门）。注：body 未剥离 | 无 | hub_mixins/notifications.py:137-151 `SELECT * FROM notifications WHERE agent_id=?` 直返；身份门 routes_notifications.py:76-77 |

### pipeline（3 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| POST /api/v1/chunks/search | pipeline | document_chunks 段落正文 content | 待收编（有剥离，绕网关入口；功能与网关 kind=doc 重复，另有网关 doc 路径没有的防拼接滑窗） | 有 | routes_pipeline.py:42-46 → disclosure.py:723-737 逐 chunk `_calculate_disclosure_level` → NONE 跳过 → `min(请求方, chunk存储级)`（:729-733）→ 滑窗降级（:734-736）→ `_extract_by_level`（:737）。非 gateway/read，不落 gateway_read_log |
| POST /api/v1/embeddings/calibrate | pipeline | 仅返回调用方自带 samples 算出的阈值统计 p10/p25/p50/suggested | 结构性例外（运维标定统计，不读库存内容） | 不适用 | routes_pipeline.py:114-143 `calibrate_cos_threshold(model, samples)`，无 DB 查询 |
| GET /api/v1/sensitivity/words | pipeline | 机密词库本身（配置） | 结构性例外（自身配置读，manager/orchestrator 角色门，且落审计事件） | 不适用 | routes_pipeline.py:146-163 `_load_secret_keywords()` + 角色门（:154-157）+ `_log_event`（:162） |
| GET /api/v1/entities/review | pipeline | review_queue + entity_review 行（实体 name/detail/evidence 文本） | 结构性例外（审批/审查工作台队列，manager/orchestrator 角色门）。注：detail/evidence 未剥离，仅角色门兜底 | 无（角色门替代） | routes_pipeline.py:198-211（角色门 :207-210）→ hub_mixins/ingest.py:491-503 `SELECT * FROM review_queue ...` + `SELECT * FROM entity_review` 整行 dict 返回，无披露过滤 |

### access / keys / integrations（7 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/access/accounts | access | agents 身份表行（无正文） | 结构性例外（账号/身份管理视图） | 不适用 | routes_access.py:33-35 `SELECT agent_id, agent_name, ... FROM agents` |
| GET /api/v1/access/exceptions | access | disclosure_requests（approved）审批行 + agent_keys 到期元数据 | 结构性例外（披露治理/审计自检视图） | 不适用 | routes_access.py:65-75 两段 SELECT，均只取元数据列 |
| GET /api/v1/access/accounts/employees | access | employee_accounts 行（含 has_key 布尔，不出 hash） | 结构性例外（员工账号管理视图） | 不适用 | routes_access.py:158-163 |
| GET /api/v1/keys | keys | agent_keys 元数据（scope/status/调用画像，不含 hash） | 结构性例外（自身凭证管理读） | 不适用 | routes_keys.py:73 → key_scopes.py:150-163 `list_keys()` 注释明写「不含 key_hash 明文」，SELECT 列无 content |
| GET /api/v1/integrations | integrations | integrations_state 行 + 掩码后 config（redact_config） | 结构性例外（集成运维配置视图） | 不适用 | routes_integrations.py:32 → integrations/registry.py:70-109，`"config": redact_config(cfg)`（:86/:104） |
| GET /api/v1/integrations/meta/available | integrations | 连接器类型元信息（name/display_name/category） | 结构性例外（连接器目录元数据） | 不适用 | routes_integrations.py:118 → integrations/registry.py:56-60 |
| POST /api/v1/integrations/{name}/test | integrations | 连通性测试 ok/detail/latency_ms | 结构性例外（运维自检动作，不改状态） | 不适用 | integrations/registry.py:179-185 仅调 `inst.test_connection()` 无 DB 写入 |

### maintenance / report / server / federation（7 读端点）

| 端点（method + path） | 模块 | 读什么（数据对象） | 判定 | 是否做披露剥离 | 依据（file:line + 关键语句） |
|---|---|---|---|---|---|
| GET /api/v1/maintenance/db-stats | maintenance | 各表 COUNT + DB 大小 | 结构性例外（运维统计，只返计数不返行） | 不适用 | hub_mixins/maintenance.py:139-153 `SELECT COUNT(*) FROM {table}` |
| GET /api/v1/maintenance/shadow-stats | maintenance | 影子双写统计快照 | 结构性例外（运维自检统计） | 不适用 | routes_maintenance.py:34-39 `w.stats_snapshot()` |
| GET /api/v1/maintenance/network | maintenance | 本机 LAN IP + 防火墙状态 | 结构性例外（运维信息） | 不适用 | routes_maintenance.py:45-48 |
| GET /api/v1/maintenance/backup-status | maintenance | 备份文件清单（文件名/大小/mtime）+ 配置 | 结构性例外（运维信息，不返库内容） | 不适用 | routes_maintenance.py:70-87 os.listdir + os.stat |
| GET /api/v1/report/daily | report | 今日统计计数 + top tags + LLM 摘要 | 结构性例外（控制台聚合视图，全是 COUNT/聚合，tags 只返标签字符串计数） | 不适用 | routes_report.py:90-123 全部 `COUNT(*)`/tags 计数，无正文返回 |
| GET /api/v1/server/config | server | 自身 config.yaml（host/port/auth/ui）+ LAN IP | 结构性例外（自身配置读） | 不适用 | routes_server.py:37-53 读 config.yaml 选定键 |
| GET /api/v1/federation/snapshot/{kind} | federation | memory_pool / knowledge_base / wiki_inbox 全表行（含正文）或 agents（剔密钥列） | 结构性例外（联邦快照，任务书点名例外类）。注意：kind=memory/knowledge/wiki 时整表 `SELECT *` 直返未剥离正文，见 T3 灰区注 | 无 | routes_federation.py:31-32 → federation_sync.py:42 `c.execute(f"SELECT * FROM {table}")`，仅 agents 剔列（:51-53），memory/knowledge/wiki 无任何剥离 |

---

## 三、T3 汇总与待收编清单

### 3.1 统计

| 项 | 数 |
|---|---|
| 读语义端点总数（49 GET + 17 读语义 POST） | **66** |
| 已经网关 | **1**（POST /api/v1/gateway/read 自身） |
| 结构性例外 | **47** |
| 待收编 | **18**（其中 15 个剥离=无；3 个剥离=有但绕网关入口/不落 gateway_read_log） |

### 3.2 风险清单（`待收编` 且 `是否剥离=无`，共 15 项，按风险排序）

排序理由：**内容敏感度 × 跨主体可见性**。handler 无身份/角色门（任意持证 principal 可打，非匿名）＞ 仅登录无角色门（任意持证 agent 跨主体读他人内容）＞ 有 owner/visibility 门。返回全文正文 ＞ 正文截段/snippet ＞ 仅元数据。

| # | 端点 | 模块 | 暴露面 | 收编去路 |
|---|---|---|---|---|
| 1 | POST /api/v1/hub-agent/chat | hubagent | **handler 无身份/角色门（非匿名：中间件要求 bearer token）** + LLM 工具直查全库 memory_pool/knowledge_base 正文（LIKE 检索，跨全 agent），reply 直接出参 | 工具 `query_memories`/`query_knowledge` 改经网关 kind=semantic（外加补身份/角色门） |
| 2 | GET /api/v1/hub-agent/chat/history | hubagent | **handler 无身份/角色门（非匿名）** + 会话历史内嵌上述未剥离正文（ToolMessage 落库为 assistant 消息） | 随 chat 收编；历史侧至少加身份/角色门 + 按 session 属主过滤 |
| 3 | GET /api/v1/knowledge/from-memories | knowledge | **handler 无身份/角色门（非匿名）** + 跨全 agent 记忆 content[:200] 截段 | 改经网关 kind=semantic（或先补身份/角色门） |
| 4 | GET /api/v1/knowledge | knowledge | 仅登录无角色门，知识库全表正文 | 改经网关 kind=semantic（知识库检索） |
| 5 | GET /api/v1/knowledge/{entry_id} | knowledge | 仅登录无角色门，单条知识正文 | 同上 |
| 6 | POST /api/v1/memory/search | memory | 持证 agent 可对任意 agent_id 三路检索记忆正文；disclosure_level 只展示不过滤 | 改经网关 kind=memory |
| 7 | GET /api/v1/memory | memory | owner 全量记忆正文直返（owner 门但无段落剥离） | 改经网关 kind=memory |
| 8 | GET /api/v1/memory/{memory_key}/versions | memory | 历史版本正文直返（owner 门） | 改经网关 kind=memory（版本快照需网关支持或维持 owner 门+剥离） |
| 9 | GET /api/v1/wiki/page/{page_path:path} | wiki | 任意持证 agent 读 wiki 页面全文 | 改经网关 kind=doc（wiki 需先入 document_chunks 或网关扩展 wiki kind） |
| 10 | GET /api/v1/wiki/search | wiki | 搜索 snippet 为未剥离原文切片 | 同上 |
| 11 | GET /api/v1/wiki/search/hybrid | wiki | 同上（snippet ≈160 字原文） | 同上 |
| 12 | GET /api/v1/shared/docs/{doc_id} | shared | CRDT 文档全文直返；有 can_access visibility 门但无段落剥离 | 改经网关 kind=doc |
| 13 | GET /api/v1/wiki/pages | wiki | 仅元数据清单（frontmatter），无正文 | 弱：收编优先级低；可随 wiki 收编一并处理 |
| 14 | GET /api/v1/shared/docs | shared | 仅元数据清单，有 visibility 过滤 | 弱：同上 |
| 15 | GET /api/v1/sessions/recent | sessions | 会话摘要正文（title/summary/key_facts），硬身份门仅本人可读 | 边界项：本人可见缓释；是否收编取决于 session_archives 是否计入内容域 |

### 3.3 待收编但「剥离=有」的 3 项（收编理由 = 统一入口 + 补 gateway_read_log，非补剥离）

| 端点 | 模块 | 现状 | 收编去路 |
|---|---|---|---|
| POST /api/v1/memory/semantic_search | memory | 已过 disclosure 引擎（disclosure.py:449-456），但绕网关、不落读审计 | 与网关 kind=semantic 功能完全重复，收编=调用方改打网关后下线此端点 |
| POST /api/v1/chunks/search | pipeline | 已过披露引擎且有防拼接滑窗（disclosure.py:723-737），但绕网关 | 与网关 kind=doc 重复；注意网关 doc 路径无滑窗，收编时需把滑窗语义带进网关 |
| POST /api/v1/memory/disclose | memory | 披露协议专用端点，已过披露引擎并落 disclosure_log | 属主动披露协议而非检索旁路，收编优先级最低；若要单一口径可改经网关 |

### 3.4 灰区注（不算待收编，但如实登记，供 Hermes 台账参考）

- **GET /api/v1/federation/snapshot/{kind}**：按任务书口径归「结构性例外（联邦快照）」，但 kind=memory/knowledge/wiki 时整表 `SELECT *` 直返未剥离正文（federation_sync.py:42），是联邦通道的披露盲区。
- **GET /api/v1/wiki/export**：同为联邦快照通道（wiki_sync.py:156 消费），但任何持证 agent 可直接调到全 wiki 未剥离全文（routes_wiki.py:36-37），建议评估加角色门。
- **GET /api/v1/messages / GET /api/v1/notifications**：私聊/通知正文未剥离，有「仅本人」身份门兜底，按口径归结构性例外；若治理口径把私聊 content 计入内容域，需升级。
- **POST /api/v1/agents/bootstrap**：握手返回注册者本人 tasks/session 摘要正文无剥离，仅本人数据，归结构性例外（边界项）。
- **附带缺陷（与旁路无关，仅登记）**：① 网关 kind=memory 是 keep/drop 而非按级别降级截取——`memory_search` 原始返回含全文 content（hub_mixins/memory.py:482），routes_gateway.py:154-156 判定通过即整条原样进响应，存储级别 summary 但允许 full 时会返回全文；② `hub.search_memory` 不存在（routes_memory.py:48、routes.py:601 调用，网关 docstring routes_gateway.py:7 亦提及），memory/batch 的 search 子操作为死路径。

---

### 3.5 验收更正（Hermes，2026-09-10）

本文三处「**无认证**」表述经中间件核证后**更正为「handler 无身份/角色门（非匿名）」**——原表述夸大了风险等级：

- 依据：`routes.py:122` `AUTH_ALLOWLIST_PREFIXES = ("/static","/assets","/legacy","/docs","/openapi.json")`、`routes.py:129` `AUTH_ALLOWLIST_PATHS`（仅 6 页面壳 + register/bootstrap + team 两个自认证端点）——`/api/v1/hub-agent/*` 与 `/api/v1/knowledge/*` **均不在两者内**，故 `routes.py:180-205` 的 TokenAuthMiddleware 会要求 bearer token。
- 真实缺口：这些 handler 函数签名里没有 `Depends(get_current_agent)`，因此**没有 per-agent 身份与角色门**——任意持证 principal（含 worker api_key）都能调用。
- 影响：风险清单第 1~3 项从「匿名可打」降级为「任意持证 principal 可打」，但**仍是未剥离的内容读取，仍属待收编**，排序位置不变。
- 其余发现（`federation/snapshot` 整表 `SELECT *` 无剥离、网关 kind=memory 为 keep/drop 而非按级别降级截取、`wiki/export` 任意持证可拉全 wiki）经独立核证**属实**，未作改动。

## 四、本表口径与维护

**三分类判据：**

- `已经网关`：内容读取且唯一经由 `POST /api/v1/gateway/read`（认证 → 检索 → 段落级剥离 → 落 `gateway_read_log`）。
- `结构性例外`：非内容读取——健康/运维统计、控制台聚合视图、自身配置读、审计自检、联邦快照、审批队列等，本就不该走内容网关。判定关键是「读什么」而非端点名：返回的是计数/状态/配置/审计行/队列元数据，而非记忆/知识/wiki/文档正文。
- `待收编`：内容读取且直接返回未剥离正文（剥离=无）；本表额外把 3 个「剥离=有但绕网关入口、不落读审计」的内容读取端点归入此类并单列（3.3 节），因为收编的含义是「统一入口 + 读审计」，不只是补剥离。

**什么时候需要更新本表：**

- 新增/删除任何 `routes*.py` 读端点（GET 或读语义 POST）时，重跑第一节枚举脚本并把新端点按 T2 口径补行。
- 任何收编落地后（端点改经网关），把对应行从「待收编」移到「已经网关」，并在此表标注收编批次。
- 灰区注中的端点若治理口径变化（如私聊/会话摘要计入内容域、联邦快照加剥离），需重判。

**重要声明：收编未做，本表仅为登记。** 本任务未修改任何代码；3.2/3.3 节的「收编去路」是建议方向，实际收编需等待后续任务（且须避开 D-7/D-8 routes 拆分冲突）。

---

## 五、验收证据

枚举脚本与输出见第一节（GET 合计 49、模块 22、端点 127，与任务书基准**差 0**）。其余命令原始输出照抄：

```
$ grep -c "^| " docs/gateway-bypass-inventory.md
104

$ ls docs/gateway-bypass-inventory.md
docs/gateway-bypass-inventory.md
```

`grep -c "^| "` = 104 含各表表头行/分隔行/统计表/风险清单表，非纯端点行数；端点行数用下面的口径核对：

```
$ awk '/^## 二、/,/^## 三、/' docs/gateway-bypass-inventory.md | grep -c "^| GET\|^| POST"
66

$ awk '/^## 二、/,/^## 三、/' docs/gateway-bypass-inventory.md | grep "^| GET\|^| POST" | grep -c "待收编"
18
$ ... | grep -c "结构性例外"
47
$ ... | grep -c "已经网关"
1
```

**核对结论：T2 表内端点行数 66 = 枚举读语义端点数（49 GET + 17 读语义 POST），差 0。** 分类计数 1 + 47 + 18 = 66，自洽。其中「待收编 且 剥离=无」15 项（3.2 节风险清单一一对应）、「待收编 且 剥离=有」3 项（3.3 节）。
