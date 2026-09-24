# 星枢 Hub 集成方接入契约（v1 草案 · 2026-09-22）

> 面向**本企业以外或企业内的其他系统/Agent 接入方**：读完这份就能自己接上，不必来回问。
> 本文只讲「怎么接、边界在哪」。我们**怎么发钥匙**见 `docs/external-api-access.md`；
> 重运维端点清单见 `docs/ops-gated-endpoints.md`；已知能力边界见 `known_limitations.py`。
>
> 落地状态（如实）：本文是台账 **CD-079 的前半**——契约与可跑示例。**后半**（黄金 JSON 夹具 +
> 契约测试，防止我们将来改动打死接入方）**尚未产出**，见 §9。
> 产出轮次：2026-09-22 升龙轮后续；**无外部执行方**（kimi 月度配额 403、claude/codex 未装、
> opencode 按量 API 未派），由 Hermes 自实现并逐条对本仓代码取证。

---

## 1. 认证：两条路，先选一条

传输层前提：**未开 TLS 前不要把凭据发到企业外**——Bearer 是明文过网的（唯一加密的是 Hub↔Hub 联邦信道）。
开启方式 `server.tls.enabled: true` + `certfile/keyfile`（详见 `docs/external-api-access.md` §3），
开后可照抄本文所有示例，把 `http://` 换 `https://`、`ws://` 换 `wss://`。

### 路径 A：对方是「登记在册的 Agent 身份」

1) 注册（拿到 `api_key`，**明文仅此一次返回**）：

```bash
curl -s -X POST "$HUB/api/v1/agents/register" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $HUB_TOKEN" \        # 见下方「注意」
  -d '{"agent_id":"ext-partner-a","agent_name":"外部集成方A","role":"worker"}'
# → {"status":"registered","agent_id":"ext-partner-a","api_key":"<52位明文>"}
```

- **注意（最常踩）**：部署里配了 `auth.hub_token` 时，`register` / `bootstrap` **必须带这把 hub_token**，
  否则 `401`（`routes.py:221-224`；防的是无凭据者随便注册新身份）。
- `auth.registration: guarded` 模式下，管理员必须先预建身份，否则 `403 registration guarded: agent 'x' 未预签发`
  （预建：`python hub_cli.py agent create --id ext-partner-a --name "外部集成方A" --role worker --db <库>`）。
  默认 `open`：任何持有 hub_token（或未配 hub_token 时的匿名）都能注册。
- 需要一次拿全（注册 + 工作区 + 配置 + 最近会话 + 漏跑自动化）用 `POST /api/v1/agents/bootstrap`（同一份 body）。
- 之后**每个请求**：`-H "Authorization: Bearer $API_KEY"`。
- 可选心跳：`POST /api/v1/agents/{agent_id}/heartbeat`。

### 路径 B：对方只要「一把受限钥匙」（推荐给外部集成方）

按 `docs/external-api-access.md` 的三步发放，要点复述：

```bash
python hub_cli.py key create --agent ext-partner-a \
  --endpoints "/memory/disclose,/memory/search,/tasks,/gateway/read" \
  --methods "GET,HEAD,POST" \                # 只读交付就写 GET,HEAD
  --data-domain proj-ext --level-cap summary \
  --expires 2026-12-31T00:00:00 --db ./sync_hub.db
```

| scope 字段 | 语义 | 不写会怎样 |
|---|---|---|
| `endpoints` | 路径白名单，精确 + 边界匹配（`/mem` **不会**放行 `/memory/*`） | 空 = **全部端点**（对外交付不要留空） |
| `methods` | 方法白名单（大写精确匹配） | 空 = 不限方法 |
| `data-domain` | 数据域（部门/项目标签），叠加在披露判定上 | 不叠加 |
| `level-cap` | 披露上限 `full\|summary\|metadata\|none`，与规则链结果取 `min` | 按规则链 |
| `expires` | ISO 过期时间 | 永不过期（对外不建议） |
| `ws` | 是否允许建 WS 连接 | **默认拒绝**（WS 是全双工，会绕过前两层白名单） |

**fail-closed 硬边界（不看白名单，直接拒）**：`/api/v1/keys`、`/api/v1/access`、`/api/v1/server`、
`/api/v1/agents/quota`、`/api/v1/agents/full-access`、`/api/v1/agents/register`、`/api/v1/agents/bootstrap`
——这些只能由全权 `api_key` 或 `hub_token` 调用（否则受限钥匙能自我提权成全权）。

### WebSocket 认证：首帧，不是 query 参数

连接：`ws://<host>:3060/ws/<agent_id>`，**连上后立刻发一帧**：

```json
{"type": "auth", "token": "<api_key 或 hub_token>"}
```

| 情况 | 结果 |
|---|---|
| 首帧不是 `{"type":"auth",...}` / 无 token / token 无效 | `close 4401`（失败计数累加，超阈值按 IP 封禁） |
| 首帧迟迟不发（超 `WS_AUTH_TIMEOUT_SEC`，默认 3s） | `close 4401 Auth timeout` |
| `api_key` 连接的 key 归属 ≠ 路径 `<agent_id>` | `close 4401`（防冒充） |
| `hub_token` 连接 | agent_id 取路径/声明（部署级凭据，无身份语义） |
| scoped key 且 `scope.ws ≠ true` | `close 4401 Forbidden: scoped key cannot open WS` |

`/ws/dashboard`、`/ws/buffer` 另有特权门（须 hub_token 或 `manager`/`orchestrator`）。
讨论室两条通道（`/ws/shared/watch/{doc_id}`、`/ws/shared/{doc_id}`）同此首帧鉴权——scoped key 未开 `scope.ws` 同样 4401；两条怎么选见 §3.4。

---

## 2. REST 端点：接入必需子集

完整清单（**155 个端点**）不需要本文维护：Hub 自带 OpenAPI——
`GET /openapi.json`、Swagger UI `/docs`（两者都在认证豁免前缀里，**无凭据即可打开**）。
下表是接入最小集（★ = 需特权：hub_token 或 `manager`/`orchestrator`）：

| 分组 | 端点 | 说明 |
|---|---|---|
| 身份 | `POST /api/v1/agents/register`、`POST /api/v1/agents/bootstrap`、`POST /api/v1/agents/{id}/heartbeat` | 见 §1；前两者配了 hub_token 时须带它 |
| 记忆 | `POST /api/v1/memory/store`、`POST /api/v1/memory/search`、`POST /api/v1/memory/semantic_search`、`GET /api/v1/memory`、`GET /api/v1/memory/{key}/versions`、`POST /api/v1/memory/{key}/rollback`、`POST /api/v1/memory/batch`、`DELETE /api/v1/memory/{key}` | 记忆读写；`memory/store` 带 `?agent_id=`，非本人需特权 |
| 知识 | `POST /api/v1/knowledge`、`GET /api/v1/knowledge`、`GET /api/v1/knowledge/{entry_id}`、`DELETE /api/v1/knowledge/{entry_id}`、`GET /api/v1/knowledge/graph/data`、`POST /api/v1/knowledge/reindex`★ | 知识的**统一读出口是网关**（见下行）；直读端点按披露规则剥离 |
| 网关 | `POST /api/v1/gateway/read` | **推荐入口**：按主体+披露级别剥离后返回（`kind=doc/memory/...`），并落读审计 |
| 任务 | `POST /api/v1/tasks/create`（必填 `task_id` + `description`，`creator_agent_id` 写自己，否则 403）、`GET /api/v1/tasks`、`GET /api/v1/tasks/{id}/subtasks`、`POST /api/v1/tasks/{id}/{schedule\|start\|complete\|fail\|cancel\|update}`、`POST /api/v1/tasks/{id}/advance` | 四条实测口径：① 状态推进类端点（`start`/`complete`/`fail`/`cancel`）**必带 `?agent_id=<自己>`**，缺则 `422`（不是 403）；② `start`/`complete` 还要求任务**已 assigned 给自己**（`assigned_agent_id` 相符）；③ `schedule` 触发派单匹配，**需 manager/orchestrator 或 hub_token**，且只从 `online` 的 Agent 里挑人；④ ⚠️ `advance` **不是调度推进**——语义是「申请提升该任务的披露级别」 |
| 披露审批 | `POST /api/v1/disclosure/approve`、`POST /api/v1/disclosure/deny`★ | 跨主体披露的审批动作 |
| Wiki | `GET /api/v1/wiki/pages`、`GET /api/v1/wiki/page/{path}`、`GET /api/v1/wiki/search`、`POST /api/v1/wiki/import`、`GET /api/v1/wiki/export`、`GET /api/v1/wiki/inbox`★、`POST /api/v1/wiki/sync`★ | 运维类（★）见 `docs/ops-gated-endpoints.md` |
| 共享文档 | `GET/POST /api/v1/shared/docs`、`GET /api/v1/shared/docs/{doc_id}`、`POST /api/v1/shared/docs/{doc_id}/blocks`、`DELETE /api/v1/shared/docs/{doc_id}` | 协作写入 |
| 密钥（仅特权）★ | `POST /api/v1/keys`、`GET /api/v1/keys`、`DELETE /api/v1/keys/{key_id}` | 签发/吊销受限钥匙 |
| 观测 | `GET /api/v1/stats`、`GET /health`、`GET /healthz` | `/health`、`/healthz` 免认证；`/stats` 需凭据 |

> **在线语义**：`POST /api/v1/agents/{id}/heartbeat` 或 WS 上的 `ping`/`pong` 都会把你标成 `online`
> （`hub_core.record_pong` 是唯一入口）。**想被派单就必须保持在线**——离线 Agent 拿不到 `dispatch`，
> 错过的事件在通知侧记为 `missed_runs`。心跳间隔建议 30s（与 `L5-TIMEOUT-001` 的 90s 判定配套）。

### 2.1 接入子集的最低权限对照（CD-079c）

> 本小节为增量补充。**最低凭据**＝能调通该端点的最低级别身份；「受限 key」指 §1 路径 B 签发的 scoped key
>（还需 `endpoints`/`methods` 白名单命中）；「配对凭据」指 `team_members.remote_api_key`（见本小节末尾说明）。

| 端点 | 最低凭据 | 说明 |
|---|---|---|
| `POST /api/v1/memory/store` | 本人 api_key | `routes_memory.api_memory_store`：`current_agent != agent_id` 即 403 |
| `POST /api/v1/memory/search` | 本人 api_key | `routes_memory.api_memory_search`：`current_agent != req.agent_id` 即 403 |
| `POST /api/v1/memory/disclose` | 本人 api_key | `routes_memory.api_disclose`：`current_agent != req.requester_agent_id` 即 403 |
| `POST /api/v1/memory/semantic_search` | 本人 api_key | `routes_memory.api_semantic_search`：同上 |
| `POST /api/v1/knowledge` | manager / orchestrator / hub_token | `routes_knowledge.api_knowledge_upsert`：`require_role` roles=("manager","orchestrator") |
| `GET /api/v1/knowledge` | 本人 api_key | `routes_knowledge.api_knowledge_list`：`get_current_agent`，正文按披露规则剥离 |
| `GET /api/v1/knowledge/{entry_id}` | 本人 api_key | `routes_knowledge.api_knowledge_get`：同上 |
| `GET /api/v1/wiki/pages` | 本人 api_key | `routes_wiki.api_wiki_pages`：`get_current_agent`；`principal_is_privileged` 影响可见范围 |
| `GET /api/v1/wiki/export` | 配对凭据 或 manager / orchestrator / hub_token | `routes_wiki.api_wiki_export`：路由层豁免 + `authorize_federated_caller` 双通道自认证 |
| `POST /api/v1/wiki/import` | 配对凭据 或 manager / orchestrator / hub_token | `routes_wiki.api_wiki_import`：同 export |
| `GET /api/v1/wiki/inbox` | manager / orchestrator / hub_token | `routes_wiki.api_wiki_inbox`：`require_ops_privilege` |
| `GET /api/v1/shared/docs` | 本人 api_key | `routes_shared.api_shared_list`：`get_current_agent` |
| `POST /api/v1/shared/docs` | 本人 api_key | `routes_shared.api_shared_create`：`get_current_agent` |
| `GET /api/v1/shared/docs/{doc_id}` | 本人 api_key | `routes_shared.api_shared_get`：披露级别门 + `_log_deny` 留痕 |
| `GET /api/v1/team/members` | 本人 api_key | `routes_team.api_team_members`：`get_current_agent` |
| `GET /api/v1/team/discover` | 本人 api_key | `routes_team.api_team_discover`：`get_current_agent` |
| `GET /api/v1/team/ping` | 本人 api_key | `routes_team.api_team_ping`：`get_current_agent` |
| `POST /api/v1/team/pair/exchange` | 6 位配对码 | `routes_team.api_team_pair_exchange`：免标准认证，函数内用请求体 `code` 自认证 + IP 指数退避 |
| `POST /api/v1/team/proxy/disclose` | 配对凭据 | `routes_team.api_team_proxy_disclose`：免标准认证，函数内查 `team_members` 表自认证 |
| `GET /api/v1/federation/snapshot/{kind}` | 配对凭据 或 manager / orchestrator / hub_token | `routes_federation.api_snapshot`：路由层豁免 + `authorize_federated_caller` |
| `POST /api/v1/federation/pull` | manager / orchestrator / hub_token | `routes_federation.api_pull`：**不豁免**，走标准认证 + `require_role` |
| `GET /api/audit/reads` | manager / orchestrator / hub_token | `routes_audit.api_audit_reads`：`require_role`（canonical 角色门） |
| `GET /api/audit/events` | manager / orchestrator / hub_token | `routes_audit.api_audit_events`：同上 |
| `POST /api/audit/anchor/stamp` | manager / orchestrator / hub_token | `routes_audit.api_audit_anchor_stamp`：`require_ops_privilege` |
| `GET /api/v1/stats` | 本人 api_key | `routes_dashboard.api_stats`：`get_current_agent` |
| `GET /health` | 无需凭据 | `routes_server.api_health`：在 `AUTH_ALLOWLIST_PATHS` 里 |
| `GET /metrics` | 无需凭据 | `routes_server.api_metrics`：在 `AUTH_ALLOWLIST_PATHS` 里 |

**配对凭据的有效范围**（最容易混淆的一点）：配对凭据（`team_members.remote_api_key`）只对「路由层豁免 + 端点函数内自认证」的端点有效，已核实共 **4 条**：

1. `POST /api/v1/team/proxy/disclose` —— `routes_team.api_team_proxy_disclose` 函数内查 `team_members` 表
2. `GET /api/v1/federation/snapshot/{kind}` —— `routes_federation.api_snapshot` 经 `authorize_federated_caller`
3. `GET /api/v1/wiki/export` —— `routes_wiki.api_wiki_export` 经 `authorize_federated_caller`
4. `POST /api/v1/wiki/import` —— `routes_wiki.api_wiki_import` 经 `authorize_federated_caller`

**不在配对凭据有效范围内的两条**（务必区分）：

- `POST /api/v1/federation/pull` **不豁免**——走标准认证 + `require_role`（manager/orchestrator/hub_token）；配对凭据不在 `agents`/`agent_keys` 表里，过不了 `get_current_agent`。
- `POST /api/v1/team/pair/exchange` 用的是 **6 位配对码**（请求体 `code` 字段），**不是** `remote_api_key`——这是配对握手协议的入口，不是配对凭据的使用端点。

---

## 3. WebSocket 协议（实时通道）

### 3.1 信封（envelope）——**硬契约**

所有**结构化**帧都是这个形状（`envelope.py`）：

```json
{"type":"...", "id":"<uuid4 hex>", "session_id":"<会话>", "via":"human|automation|ws",
 "ts": 1790059159000, "version": 2, "payload": { ...业务字段... }}
```

- `version` **必须 ≥ 2**：平铺旧格式（`version<2`）自 2026-09-21（CD-032）**一律不接受**，整帧静默丢弃。
- `type` ∈ `dispatch | result | ack | ping | pong | hello`（Hub 还会发 `response`，见 §3.3）。
- 必填：`type`、`id`、`ts`。`payload` 内字段名**不得**与信封字段名重名（重名帧会被判无效并丢弃）。
- 时间戳单位毫秒；`serialize()` 用 `ensure_ascii=False`（中文不转义）。

### 3.2 客户端 → Hub

| type | payload | 作用 |
|---|---|---|
| `hello` | `{"agent_id": "...", "last_checkpoint_id": "...", "agent_version": "1.2.3"}` | 上线握手；`last_checkpoint_id` 触发**未确认派单重放**；`agent_version` 低于 `AGENT_MIN_VERSION`（默认 `1.0.0`）→ `close 426` 拒连 |
| `ping` / `pong` | `{}` | 心跳（间隔 30s；`pong` 超时 90s 才判半开 → 最长 90s 才发现真断线，见 L5-TIMEOUT-001） |
| `ack` | `{"dispatch_id": "<派单帧 id>"}` | 确认派单已收到（Hub 据此 `dec_in_flight`） |
| `request` | `{"method": "memory_search\|memory_store\|memory_list", "params": {...}}` | WS 上的同步请求，Hub 回 `response`。`memory_search` 的 params：`{"query": "...", "limit": 10}`（`limit` 映射 `top_k`；可选 `kind`/`top_k`/`min_confidence`），`result` 与 REST `/memory/search` 同构（`results`/`total`/`degraded` 等，CD-091 已修）；`memory_list` / `memory_store` 实测可用 |

### 3.3 Hub → 客户端

| 形态 | 例子 | 说明 |
|---|---|---|
| envelope：`pong` | `{"type":"pong",...}` | 对 `ping` 的回应 |
| envelope：`dispatch` | `{"type":"dispatch","payload":{...派单...}}` | 派单（含 `hello` 后的重放） |
| envelope：`response` | `{"type":"response","payload":{"id":"<req id>","result":{...}}}` 或 `{"...,"error":"..."}` | 对 `request` 的响应；**错误也在 result 位**（WS 层不抛 HTTP 状态码） |
| 平铺：通知 | `{"type":"push","event":"notification","data":{...}}` | 业务通知（落库的 notification 同体） |
| 平铺：自动化 | `{"type":"automation.run","job_id":"...","name":"...","instruction":"...","guardrail":{...},"delivery":[...]}` | 定时自动化触发 |
| 平铺：协作在线 | `{"type":"shared_presence","doc_id":"...", ...}` | `/ws/shared/*` 通道 |
| 平铺：缓冲统计 | 每秒一个 JSON | 仅 `/ws/buffer`（特权通道） |

> **接入方必须按 `type` 分发，不要假设每一帧都是 envelope**——目前两类形态并存（这是**现状**，不是建议）。

### 3.4 接入讨论室（共享文档的两条 WS 通道）

同一个 `doc_id` 有两条 WS 端点，**按你要不要「参与编辑」来选**：

| 端点 | 形态 | 适用 |
|---|---|---|
| `/ws/shared/watch/{doc_id}` | **纯 JSON 文本事件流**（只收不发，keepalive 随意发文本帧即可） | **外部接入首选**：订阅动态、机器人旁听、Webhook 前的实时触发源 |
| `/ws/shared/{doc_id}` | **pycrdt 二进制协作通道**（YRoom，y-protocol 同步 + awareness 帧） | 要**参与 CRDT 协同编辑**的客户端（需 pycrdt/yjs 协议栈，不要手写解析） |

**鉴权（两条通道同一套，见 §1「WebSocket 认证」）**：连上后首帧 `{"type":"auth","token":"..."}`，
超 `WS_AUTH_TIMEOUT_SEC`（默认 3s）不发 / 令牌无效 → `close 4401`（失败按 IP 计数熔断）。
**scoped key 必须 `scope.ws = true` 才放行**（CD-069 口径：fail-closed，未声明一律 `close 4401`）。
watch 通道鉴权后的身份：api_key → key 归属 agent；hub_token → 取 `?agent_id=` 声明，缺省记为 `__anon__`。

**watch 通道事件形态**（每帧都是单个 JSON 对象，按 `type` 分发；除 `shared_archived` 外
广播事件都会附加 `online: [agent_id...]` 在线协作者列表）：

| type | 字段 | 何时 |
|---|---|---|
| `shared_presence` | `doc_id`、`agent_id`、`joined: true\|false` | 有 watcher 加入/离开该文档 |
| `shared_update` | `doc_id`、`agent_id`、`preview`（新 block 前 100 字） | 有人经 REST `POST /shared/docs/{doc_id}/blocks` 追加内容 |
| `shared_archived` | `doc_id`、`reason: "doc archived"` | 文档被归档（讨论结束）——**收此帧后通道随即 `close 4404`**；归档后再进房也是先收此帧再被 4404 关掉 |

**二进制通道补充**：文档已归档 → 同样先送一帧 JSON `shared_archived` 再 `close 4404`；
工作区未就绪 → `close 4000`。

**重连与追平**：两条通道都**没有断线重放**——事件不补发、无 checkpoint 语义。
重连 = 重新建连 + 重发首帧 auth；错过内容用 REST `GET /api/v1/shared/docs/{doc_id}` 拉全文追平。
注意 `4401` 失败有计数熔断（§4），重连要退避，不要死循环猛连；批量建连遵守 §5.1 的分批口径。

---

## 4. 错误语义

| 状态码 | 何时 | 接入方应做什么 |
|---|---|---|
| `400` | 请求体/参数不合法（含 `--methods`、`--level-cap` 非法值，**不会静默放宽**） | 修请求，别重试 |
| `401` | 无凭据 / 凭据无效 / `register` 未带 hub_token（部署已配） | 重新取凭据；别当成权限问题 |
| `403` | 凭据有效但越权：不在 `endpoints`/`methods` 白名单、`level_cap` 不足、角色不够 | 找发钥匙的人改 scope；**不要靠 detail 文案区分原因** |
| `404` | 资源不存在（对特权主体或 owner） | — |
| `409` | 状态冲突（如删除非空部门） | 先处理依赖 |
| `429` | 触发每 IP 限流（默认 1000 次/秒/IP） | 退避重试 |
| `4401`（WS close code） | WS 鉴权失败/超时/scoped key 不许连 WS/特权通道角色不足 | 不要疯狂重连——**失败有计数，超阈值按 IP 封禁** |
| `426`（WS close code） | `hello` 上报的 `agent_version` 低于 `AGENT_MIN_VERSION` | 升级客户端版本 |

三条硬语义（照抄自 `docs/api-error-policy.md`，2026-09-19 冻结）：

1. **存在性与无权同响应**：对**非特权**调用方，「资源不存在」与「存在但无权」返回**同一状态码 + 同一 detail 文本**
   （防 id/key 枚举预言机）。特权主体与 owner 侧才保留 `404`/`403` 可区分。**别用 detail 反推资源是否存在**。
2. **`detail` 文案不构成契约**：可能随版本改进，接入方的判定只能依赖**状态码 + 响应体字段**。
3. **被拒也留痕**：403 类拒绝自 CD-059 起在 Hub 侧落 `denied` 读审计行（只记事实、不区分存在性）——
   接入方查自己的调用画像可看 `GET /api/v1/audit/*`（需特权）。

---

## 5. 配额与限流

| 机制 | 现状 | 接入方注意 |
|---|---|---|
| 每 IP 限流 | `rate_limit.per_ip`，默认 **1000 次/秒/IP**；超限 `429` | **按 IP 而非按 key**；企业部署若前置反代，注意代理头可信问题（CD-080 未修） |
| 每 Agent 配额 | 表 `agent_quotas`：`agent_id / qps_limit / mode / window_sec / burst`；`GET|POST /api/v1/agents/quota`★ | 默认 `mode=alert_only`——**只告警不拦**。对外交付请显式设 `mode=reject` |
| 多实例 | 计数与实时流都是**进程内** | 单实例部署下成立；多实例/多进程时不是全局配额（见 CD-089，条件触发再上） |

### 5.1 客户端上线方式：分批建连 + 重试（**实测口径，2026-09-22**）

- **建连必须分批**：一次性并发打开大量 WebSocket 会被拒——实测一次并发建 1000 条（500 Agent × 2 通道）只成功 777 条，154 条 `ConnectionRefusedError [WinError 1225]`（Hub 侧无异常日志，是 accept 队列被瞬时冲爆）。改成 **100 条一批、批间隔 0.4s** → **1000/1000 全成**。
  → 接入方做法：每批 **≤100 条**、批间 sleep **≥0.4s**、按指数退避**重试**（`ConnectionRefused` 属可重试，不是永久失败）。
- **容量按「同时发言人数」算，不是「在线人数」**：实测在线 10→500 Agent 全绿（0 错误 0 断连、扇出完整率 100%、RSS 228→667MB）；但扇出是 O(N²)——**100 人同时发言 p95≈0.75s、300 人 ≈6.1s、500 人 ≈18.0s**；而 500 在线仅 5 人发言时 p95≈0.24s。
  → 对客户承诺容量请按「**同时发言 ≤100 人**」写；要超过需另做设计（扇出批量合并 / 通知 + 按需拉取），当前无该诉求。
- 口径条件：本机 `127.0.0.1` 单进程、无 TLS、默认档 auth（**未开** `SYNC_HUB_AUTH_ROW_CACHE`），未叠加向量检索/知识写入等其它负载。

---

## 6. 幂等与重放（**接入方必读**）

- Hub 在收到 `hello` 时，按 `payload.last_checkpoint_id` **重放未确认的派单**。
- 接入方确认收到用 `ack`（`payload.dispatch_id`）；未 `ack` 的派单在重连后**可能再次下发**。
- **因此：幂等由接入方自行保证**（协议层口径，2026-09-21 CD-075 随自研 Agent 端下线定稿）。
  这意味着：**同一个 `dispatch` 帧可能被处理两次**——按派单 `id` 去重，或让业务操作本身幂等。
- 传输层另有审计：Hub 侧记录收发帧（`transport_audit`），可用于事后对账，但**不是**给接入方的确认机制。

---

## 7. 已知限制（对客户不要承诺超出这些）

| 限制 | 影响 | 出处 |
|---|---|---|
| 未开 TLS 时 Bearer 明文过网 | 只在企业内网/已开 TLS 时放凭据 | `external-api-access.md` §0 |
| 本地审计不构成「防篡改举证」 | 本地自校验能查行级篡改；整段重写要域外锚（RFC3161 TSA / 外部保管方） | `known_limitations.py` L2-ANCHOR-001 |
| `pong` 超时 90s | 真断线最长 90s 才触发重连 | L5-TIMEOUT-001 |
| scoped key 默认不能连 WS | 必须 `scope.ws=true` 且在 `endpoints` 里列 `/ws` 类路径 | CD-069 |
| 限流按 IP、进程内 | 伪代理头可绕过；多实例非全局 | CD-080 / CD-089 |
| 知识正文多处副本 | 统一走 `POST /api/v1/gateway/read` 拿剥离后结果 | CD-052 / CD-055 |

---

## 8. 版本与兼容承诺

- **信封 `version=2` 是硬契约**：`version<2` 一律拒收（无兼容分支）。
- `agent_version` 在 `hello` 里上报；低于 `AGENT_MIN_VERSION`（配置项，默认 `1.0.0`）→ `close 426`。
- 新增字段视为向后兼容（接入方应忽略未知字段）；**删除/改语义**会走轮次登记并更新本文。
- Hub 版本可从 `GET /health` 的 `version` 读到（当前 `2.1.0`，单一来源 `models.HUB_VERSION`）。

---

## 9. 一份可跑的示例 + 本文的边界

- 可跑示例：`examples/quickstart.sh`（注册 → REST 读写 → WS 首帧鉴权 + hello + request）——
  对着任意 Hub 实例执行，退出码 0 = 全绿。
- **本文尚未覆盖（CD-079 后半，待做）**：
  1. **黄金 JSON 夹具**（`testdata/wire/*.json` 形式的收发帧快照）——现在只有散文描述，没有机器可比对的快照；
  2. **契约测试**：把夹具挂进 CI，我们改动打死接入方时能拦住；
  3. 面向接入方的**错误码表枚举**（现在只有状态码层语义，业务错误码尚未收敛成稳定枚举）。
- 接入方如果发现本文与实测不符：以**实测 + `/openapi.json`** 为准，并把差异反馈给我们——
  本文是契约不是承诺，**改它比让接入方猜便宜**。

---

## 10. 集成方最容易踩的坑（CD-079c）

> 本节为增量补充。每条格式：**现象 → 为什么 → 怎么避**。全部有代码判定点依据。

1. **受限 key 的 `endpoints` 写 /api/v1/shared 会被拒，写 /shared 才通**
   → 为什么：`routes._endpoint_allowed` 的匹配规则是「请求路径归一剥离 /api/v1 前缀后与条目做精确 + 边界比对」，不是裸前缀匹配（/mem 不会放行 /memory/*，详见 §1 路径 B 表格「endpoints」行）。
   → 怎么避：`endpoints` 里写不带 /api/v1 前缀的路径段；写错了会被 `scoped key: endpoint 不在白名单` 403 拒掉。

2. **payload 里出现 `type`/`id`/`session_id`/`via`/`ts`/`version` 会被静默丢帧**
   → 为什么：`envelope.RESERVED_PAYLOAD_KEYS` 是信封保留键集合（= 信封字段减 `payload`）；构造时 `_build` 抛 `ValueError`，解析时 `parse_envelope` 返 `None`（整帧丢弃，无错误回报）。
   → 怎么避：业务字段一律放 `payload` 内且避开这 6 个键名；收到无响应时先自查帧结构。

3. **WS 连上后发 query 参数 token 不会生效，错令牌是 `close 4401`**
   → 为什么：WS 认证是**首帧** `{"type":"auth","token":"..."}`（`routes_ws._ws_auth_accept`），不是 URL query 参数；错令牌/空 token/首帧不是 auth 帧一律 `close 4401`。
   → 怎么避：连上后立刻发 auth 帧；超 `WS_AUTH_TIMEOUT_SEC`（默认 3s）也是 `close 4401`，别等。

4. **请求体超限返回 413，不是 400**
   → 为什么：`routes._send_413` 在中间件层拦截超限请求体（默认 2MB；`BODY_LIMIT_LARGE_PATHS` 里的端点——`/api/v1/hub-agent/chat` 与 `/api/v1/wiki/import`——8MB）；400 是参数/请求体**不合法**（CD-101 口径）。
   → 怎么避：把 413 当「太大」处理（拆包/压缩），把 400 当「格式错」处理（修请求）；两者重试策略不同。

5. **/health /metrics 与静态/页面壳免认证且免限速；免认证的联邦端点仍参与限速；POST /api/v1/team/proxy/disclose 免认证且免限速**
   → 为什么：`routes.py` 中间件对 `AUTH_ALLOWLIST_PREFIXES`/`AUTH_ALLOWLIST_PATHS` 跳过认证；限速参与判定是 `path.startswith(RATE_LIMITED_ALLOWLIST)` **或**「不在豁免名单里」。`RATE_LIMITED_ALLOWLIST` 含 4 条（CD-117 收口）：/api/v1/wiki/import、/api/v1/wiki/export、/api/v1/federation/snapshot、/api/v1/team/pair/exchange——这 4 条虽免认证但**仍参与每 IP 限速**。/health /metrics /static /docs 等不在 `RATE_LIMITED_ALLOWLIST` 里 → 免限速。POST /api/v1/team/proxy/disclose 在 `AUTH_ALLOWLIST_PATHS` 里但**不在** `RATE_LIMITED_ALLOWLIST` 里 → **免认证且免限速**。
   → 怎么避：对限速敏感的联邦调用（wiki import/export、federation snapshot、pair exchange）做好 429 退避；/health /metrics 可放心高频探测；不要拿 proxy/disclose 当压测入口。

6. **幂等责任在接入方——同一个 `dispatch` 帧可能被处理两次**
   → 为什么：Hub 在 `hello` 时按 `last_checkpoint_id` 重放未确认派单；未 `ack` 的派单重连后可能再次下发（详见 §6）。
   → 怎么避：按派单 `id`（即 `dispatch_id`）去重，或让业务操作本身幂等。

7. **`/tasks/{id}/{start,complete,fail,cancel}` 缺 `?agent_id=<自己>` 是 422，不是 403**
   → 为什么：`agent_id` 是 FastAPI 必填 query 参数（`routes_tasks` 的 `api_task_start` 等签名），缺失走参数校验 → 422；403 是「参数齐了但越权」。
   → 怎么避：状态推进类请求一律带 `?agent_id=<自己的 agent_id>`；收到 422 先查参数，收到 403 再查权限。

8. **OTel trace 默认关闭——不要以为调了就会有 span**
   → 为什么：`tracing.py` 可选依赖，未装即 no-op；需设 `SYNC_HUB_LLM_TRACE=1` 或 `OTEL_EXPORTER_OTLP_ENDPOINT` 才启用（CD-085c 口径）。
   → 怎么避：需要 trace 时确认环境变量已设 + 依赖已装，否则静默无数据。
