# arch-site 应用层设计文档（边车架构 · 只定契约不写码）

日期：2026-09-02 · 状态：设计定稿（待实现） · 依据：`docs/architecture-decision-node-integration.md`（边车决策，已定）

## 0. 范围与前提

- arch-site（`E:\sync-hub-case\examples\arch-site\`）现状：纯静态展示站，`App.tsx` 仅一条 `/` 路由渲染 `pages/Home.tsx`（两个架构 section），无 API、无鉴权、无页面应用。
- 架构决策：**Node 集成走边车**——Python Hub（:3060，见 `config.example.yaml:64` `port: 3060`）是唯一数据通道与治理引擎，Node 层（Express :7101）只做 HTTP 客户端 + 鉴权接线 + 降级，**不移植、不重写** Python 侧 5 个治理模块（sensitivity / key_scopes / disclosure / audit_chain / entity_extraction）。
- Python 侧 API 契约**不动**：网关读取 `POST /api/v1/gateway/read`（`routes_gateway.py:100`，三 kind）为唯一数据通道。
- 本文档只写设计，不落码；引用的符号均经 grep/Read 核对（见文末「符号核对清单」）。

## 1. 页面清单与定位

对齐 `SystemArchitecture.tsx:13-19` 规划的五页 + 主干档案视图：

| 页面 | 路由 | 用途 | 入口 | 权限需求（最小 scope） | 首期？ |
|---|---|---|---|---|---|
| Inbox 页 | `/app/inbox` | 上传 → 队列 → ingest 落库 | 主导航 | 登录态 + `endpoints` 含 `/api/v1/chunks/ingest` | ✅ 首期 |
| Audit 页 | `/app/audit` | 读审计检索 + 哈希链完整性卡片 | 主导航 | 登录态 + manager/orchestrator 角色 | ✅ 首期 |
| Vault 页 | `/app/vault` | 目录树 + 编辑器 + git 历史 | 主导航 | 登录态 + `data_domain` 限定 | ⛔ 依赖流②反哺与网关 doc 完整化 |
| Chat 页 | `/app/chat` | bot 检索回答 + 引用展示 | 主导航 | 登录态 + `level_cap` ≤ summary | ⛔ 依赖网关三 kind 完整化 + 反哺 |
| Persona 页 | `/app/persona` | 七段式 · roles×members 渲染（展示页，已咬合） | 主导航 | 无需鉴权（静态渲染） | — 已在静态站，平移即可 |
| 主干档案视图 | `/app/archive` | customers/ canonical 档案 + 归并队列 | 主导航 | 登录态 + manager/orchestrator | ⛔ 依赖数据底座主干开发（SystemArchitecture 标注「已拍板 · 待开发」） |

**首期范围建议：Inbox + Audit 先行**。理由：两者依赖的 Python 端点已全部验收落地（`/api/v1/chunks/ingest`、`/api/audit/reads` 等），不依赖待建的反哺/主干；Persona 页为纯展示，随壳平移。Vault/Chat/主干档案视图待网关 kind=doc 的文档侧配套与流②反哺建成后再接。

所有 `/app/*` 页面挂在现有 React Router 结构下（`App.tsx` 已用 `react-router` v7 的 `<Routes>`），静态架构页保留在 `/` 不动。

## 2. API 契约（Node :7101 ↔ Python 网关 :3060）

### 2.1 基址与鉴权头

- 基址：`http://<hub-host>:3060`（本地开发 `http://127.0.0.1:3060`），Node 侧配置项 `HUB_BASE_URL`，禁止前端浏览器直连 Python（CORS/key 暴露），一律经 Node 转发。
- 鉴权头：`Authorization: Bearer sk-<token>`。Python 侧 `get_current_agent`（`routes_common.py:64`）从 `Authorization` 头取 `Bearer ` 前缀后的 token，经 `auth_provider.authenticate` 统一校验；scoped key 由 `ScopedKeyStore.lookup_by_hash`（`key_scopes.py:78`）按 SHA256 哈希匹配 `agent_keys` 表。NO_AUTH 模式下 Python 侧改从 query param `agent_id` 取身份——Node 层**不依赖**此模式，始终带 key。

### 2.2 scoped key 三层 scope 语义（参照 `key_scopes.py:5-12`）

```
{
  "endpoints":   ["前缀匹配", ...],   // 该 key 能调哪些端点；空 = 全部
  "data_domain": ["部门/项目标签", ...], // 数据域；disclosure 判定时叠加
  "level_cap":   "summary"            // 最高披露级别；与 disclosure 判定取 min（零平行逻辑）
}
```

Node 层按页发 key（见 §3），scope 语义完全由 Python 侧 `disclose_for_principal` 叠加执行，Node 不做任何披露判定。

### 2.3 端点清单（Node 层消费的 Python 端点）

| 用途 | 方法/路径 | 来源 | 备注 |
|---|---|---|---|
| 网关读取（唯一数据通道） | `POST /api/v1/gateway/read` | `routes_gateway.py:100` | body `{kind, query?, target_agent_id?, doc_id?, n_results?, required_level?}`；`kind ∈ semantic/memory/doc`（`_VALID_KINDS`，`routes_gateway.py:23`）；全部请求落 `gateway_read_log` 读审计 |
| Inbox ingest 提交 | `POST /api/v1/chunks/ingest` | `routes_pipeline.py:49` | body `{doc_id, content, kind?, source_agent_id?, trust_level?}`；切割 → 敏感度打标 → 幂等去重 → 落库 |
| 审计事件检索 | `GET /api/audit/events` | `routes_audit.py:132` | query `time_from/time_to/entry_type/ref_table/q/limit/offset`；返回 `rows + facets`；`_require_manager` 角色门（`routes_audit.py:79`） |
| 读审计检索 | `GET /api/audit/reads` | `routes_audit.py:148` | query `requester/kind/limit/offset`；读 `gateway_read_log`；manager 门 |
| 链完整性卡片 | `GET /api/audit/last-verify` | `routes_audit.py:174` | 上次 verify 结果 + 各链覆盖条数；manager 门 |
| 哈希链校验（触发） | `POST /api/audit/verify` | `routes_audit.py:23` | body `{start_id?, end_id?}`；校验动作本身入审计 |
| 审计导出 | `GET /api/audit/export` | `routes_audit.py:215` | `format=csv|json`；导出动作入审计；manager 门 |
| 披露模拟 | `POST /api/audit/disclosure/simulate` | `routes_audit.py:257` | U4 模拟器：判定级别 + 命中规则 + 逐规则轨迹；manager 门 |
| 披露审批（批准/拒绝） | `POST /api/v1/disclosure/approve` · `POST /api/v1/disclosure/deny` | `routes_disclosure.py:23,36` | 店长批准/拒绝披露升级；403 校验「不能以他人身份操作」 |
| key 生命周期 | `POST /api/v1/keys` · `DELETE /api/v1/keys/{key_id}` · `GET /api/v1/keys` | `routes_keys.py:23,50,65` | 创建（明文仅返回一次）/吊销（60s 内全端点失效）/列表；均 manager/orchestrator 门 |
| 存活/就绪探针 | `GET /healthz` · `GET /readyz` | `routes.py:826,832` | 免鉴权；Node 健康检查与降级判定用 |

### 2.4 错误码约定

| 场景 | Python 侧行为 | Node 侧处理 |
|---|---|---|
| 无/失效/吊销 key | `401 Unauthorized: 缺少有效的 API Key`（`routes_common.py:84`） | 跳转 key 配置页，提示重新配置 |
| 角色/身份不足 | `403`（如 `_require_manager`「仅主管/店长可查审计」、`disclosure/approve` 身份校验） | 页面内权限提示，不重试 |
| 资源不存在 | `404`（如网关 doc kind「文档不存在或无分块」、`routes_gateway.py:174`） | 空态展示 |
| 参数非法 | `400`（如 `kind` 非法、`query` 必填、`doc_id` 必填） | 表单级错误提示 |
| 业务降级 | **HTTP 200 + 响应体 `degraded: true`**（网关 semantic 透传 `disclosure.semantic_search` 的 CD-016 降级标记，`routes_gateway.py:131`、`disclosure.py:367`；`search_chunks` 防拼接滑窗降级同理 `disclosure.py:723`） | 显式降级横幅（见 §6），**不是错误** |
| 网关不可达 | 连接失败/超时 | Node 层降级（见 §6），**绝不 5xx 透传白屏** |

## 3. 鉴权流程

### 3.1 key 生命周期

1. **生成**：管理员经 `POST /api/v1/keys`（`routes_keys.py:23`，仅 manager/orchestrator）创建，`ScopedKeyStore.create`（`key_scopes.py:54`）返回明文 key **仅此一次**，库存 SHA256 哈希（`key_hash`，`key_scopes.py:28`），不存明文。
2. **分发**：按页发 key——每个页面一把独立 scoped key，scope 最小化（见 3.2）。key 随 arch-site 部署环境分发，不进代码库。
3. **吊销**：`DELETE /api/v1/keys/{key_id}` → `ScopedKeyStore.revoke`（`key_scopes.py:127`）状态置 `revoked`，`lookup_by_hash` 立即拒绝，**60s 内全端点失效**（与 SystemArchitecture.tsx 身份认证卡「吊销 60s」对齐）。

### 3.2 Node 侧 key 存储

- 本地配置文件（如 `examples/arch-site/.env.local` / `config/local.json`，gitignore）或环境变量（`HUB_KEY_INBOX` / `HUB_KEY_AUDIT` / ...），**明确不落库明文、不进前端 bundle**。
- key 只存在于 Node 进程内，浏览器永远拿不到——前端只跟 Node 会话（cookie/token 由 Node 自建，本设计不展开 Node 自身登录，可先用单 key 配置的简化门禁）。
- Node 侧零 key 管理界面：增删吊销一律回源 `routes_keys.py` 端点（管理员操作）。

### 3.3 每页最小 scope

| 页面 | key 的 endpoints | level_cap | data_domain |
|---|---|---|---|
| Inbox | `["/api/v1/chunks/ingest"]` | `full`（写入不受 cap） | 按项目分 |
| Audit | `["/api/audit/"]`（前缀） | `metadata`（读审计只看元数据） | 空（manager 全局） |
| Chat（后期） | `["/api/v1/gateway/read"]` | `summary` | 按成员部门 |
| Vault（后期） | `["/api/v1/gateway/read", "/api/v1/chunks/search"]` | `summary` | 按项目 |

## 4. 前端技术方案

- **组件复用**：arch-site 已装完整 shadcn/ui（`src/components/ui/` 60+ 组件：table/form/dialog/command/sonner 等）+ Tailwind 3 + `react-router` v7 + `react-hook-form`/`zod`。新页面全部基于现有 ui 组件拼装；`sections/Shared.tsx` 的 `StatusBadge`/`SectionTitle` 复用为页面内状态标注。风格与 `Home.tsx` 的 zinc 色系保持一致。
- **路由**：扩展现有 `App.tsx` 的 `<Routes>`（已用 react-router v7），新增 `/app/*` 子路由组（`/app/inbox`、`/app/audit` 等），`/` 保留静态架构展示站。**建议不引入新的路由库**，react-router 已装且在用。
- **API 客户端封装**：`src/lib/hub-client.ts`（新建）
  - 统一 `fetch` 封装：注入基址（经 Node `/hub-api` 反代路径）、超时（默认 10s）、错误归一化（`{code: 401|403|404|degraded|unreachable, ...}`）。
  - 统一错误处理：401→key 配置引导；403→权限提示；`degraded:true`→页面级降级横幅；网络失败→降级组件。
  - **绝不静默吞错**：所有异常路径都有用户可见反馈（sonner toast + 页面横幅）。

## 5. 数据流（逐页）

### 5.1 Inbox 页（首期）
- **写路径**：浏览器上传/粘贴 → Node 接收 → `POST /api/v1/chunks/ingest`（`routes_pipeline.py:49`）→ Python 五段管线（切割 → 敏感度打标 → 幂等去重 → 落库 → 三路分流，`hub.ingest_chunks`）。
- **读路径**：Node 本地队列状态（上传任务列表），ingest 结果回显 `chunks/inserted/skipped_hash/locked_none/locked/disclosure_level`。
- **依赖网关 kind**：无（首期 ingest 不走网关读）；后续「队列 → 清洗打标」补齐后接网关。

### 5.2 Audit 页（首期）
- **读路径**：四区块——
  1. 链完整性卡片：`GET /api/audit/last-verify`（上次 verify + 覆盖条数）+ 手动触发 `POST /api/audit/verify`；
  2. 读审计表格：`GET /api/audit/reads`（`gateway_read_log`：谁/哪把 key/看了什么/给到哪级/剥离多少）；
  3. 审计事件检索：`GET /api/audit/events`（时间/类型/来源表/关键词 + facets 筛选）；
  4. 导出：`GET /api/audit/export?format=csv|json`。
- **写路径**：仅触发 verify / export（两动作自身入审计链）。
- **依赖网关 kind**：无（直读审计端点）。

### 5.3 Chat 页（后期）
- **读路径**：用户提问 → Node → `POST /api/v1/gateway/read` `kind=semantic`（首选，`n_results`≤50）→ 条目含可选 `origin` 字段（git 路径 + commit hash，`routes_gateway.py:63` `_attach_origin`）→ 前端引用展示；fallback `kind=memory`（关键词）/`kind=doc`（文档段落，`doc_id` 必填）。
- **引用展示**：`origin` 有则显示真相源定位；无（影子未启用/存量未镜像）则只显示条目本身——与 `_attach_origin` 的静默兼容语义对齐。
- **依赖网关 kind**：semantic / memory / doc 三者全用，故须在网关完整化后接。

## 6. 错误降级（Python 网关不可达时的 Node 层行为）

对齐网关降级链语义（CD-016：ChromaDB 故障 → 200 + `degraded:true` + SQLite 关键词降级，**不静默吞错**）：

1. **显式降级横幅**：网关不可达（连接失败/超时/5xx）→ Node 返回缓存数据 + 页面顶部醒目横幅「治理引擎离线，展示为最近一次成功数据（时间戳）」；空缓存则显示离线空态 + 重试按钮。
2. **缓存 last-good**：Node 内存缓存每页最近一次 200 响应（带时间戳，TTL 不强制——离线期间一直可用，恢复后自动刷新）。Node 无状态原则仅针对治理状态，缓存属展示层容忍项。
3. **不静默吞错**：所有失败写 Node 日志（时间/端点/错误类型），前端必有反馈；禁止 catch 后返回空数据冒充成功。
4. **业务降级透传**：Python 返回 200 + `degraded:true` 时，前端显示「语义检索降级为关键词检索」类提示——这是**可用结果**，与「不可达」两种降级分开标注。
5. **健康探测**：Node 后台轮询 `GET /healthz`（存活）与 `GET /readyz`（就绪，含 ChromaDB degraded 状态），驱动全局横幅的升起/撤下。

## 7. 与 SystemArchitecture.tsx 的咬合（落地后 status 变化）

| 卡片（`SystemArchitecture.tsx`） | 现 status | 本设计落地后 | 说明 |
|---|---|---|---|
| Inbox 页（L14） | warning「管线在 · 缺清洗打标」 | **半咬合 → 已咬合（ingest 接线部分）** | 页面+上传+ingest 落地；清洗打标仍属 Python 流①缺②③段，不变 |
| Audit 页（L18） | warning「写审计在 · 缺读审计」 | **已咬合** | 读审计端点（`routes_audit.py` U3 四件套 + `/api/audit/reads`）全部接上 |
| 应用层 Connector（L22） | 「当前无鉴权 → 需接 scoped key」 | **已接 scoped key** | key_scopes 三层 scope 落地到 Node |
| Agent 运行时 · 身份认证（L27） | danger「待接入 · 资产在 key_scopes.py」 | **已接入**（应用层侧） | Node 侧消费 key 生命周期 |
| Vault 页（L15） | danger「待改造 · 单仓拆分干」 | **仍待改造** | 依赖数据底座主干/分干开发（「已拍板 · 待开发」），首期不接 |
| Chat 页（L16） | danger「待改造 · 换主干索引+身份」 | **仍待改造** | 依赖网关三 kind 完整化 + 流②反哺，首期不接 |
| 主干档案视图（L19） | danger「待建 · 五页之外新页面」 | **仍待建** | 依赖 customers/ canonical 档案与归并队列（流②反哺，「最后上线」） |

预计从「待建/待改造」变「已建/已咬合」的：**Audit 页读审计、应用层鉴权（Connector + 身份认证）**、Inbox 页 ingest 接线。Chat/Vault/主干档案视图维持原 status 并标注依赖原因。

## 8. 实施排期建议（commit 批次 + 验收标准）

| 批次 | 内容 | 验收标准 |
|---|---|---|
| B1 | Express 壳（:7101）+ scoped key 配置 + `/hub-api` 反代 + hub-client 封装 + 降级横幅组件 | 网关在线时反代通；拔网关后页面出降级横幅不白屏；401 引导页生效 |
| B2 | Audit 页：读审计表格 + 链完整性卡片 + 事件检索 + 导出 | manager key 下四区块数据真实渲染；非 manager key 403 有提示；verify/export 动作落审计链 |
| B3 | Inbox 页：上传 → Node 队列 → `/api/v1/chunks/ingest` → 结果回显 | 上传文档后 `inserted`/`disclosure_level` 正确回显；重复上传 `skipped_hash` 命中幂等 |
| B4 | Chat 页：网关 semantic 检索 + 引用（origin）展示 + degraded 提示 | `degraded:true` 时降级提示出现；`origin` 有/无两种渲染正确（依赖：网关完整化） |
| B5 | Vault 页 + 主干档案视图 | 目录树/git 历史渲染；归并队列只读列表（依赖：数据底座主干 + 流②反哺建成） |

每批次独立 commit、独立可演示；B1–B3 为首期，B4/B5 排期跟随网关与数据底座进度。

## 附：符号核对清单（均经 Read/grep 验证，2026-09-02）

- `routes_gateway.py:100` `POST /api/v1/gateway/read`；`:23` `_VALID_KINDS=("semantic","memory","doc")`；`:131` `degraded` 透传；`:174` doc 404；`:63` `_attach_origin`
- `key_scopes.py:5-12` 三层 scope docstring；`:28` `key_hash`；`:54` `ScopedKeyStore.create`（明文仅一次）；`:78` `lookup_by_hash`；`:127` `revoke`（60s 失效）
- `routes_common.py:64` `get_current_agent`（`Authorization: Bearer`）；`:84` 401 detail；`:88` `get_current_principal`
- `routes_audit.py:23/53/60/132/148/174/215/257` verify / rules / replay / events / reads / last-verify / export / simulate；`:79` `_require_manager`
- `routes_disclosure.py:23,36` approve / deny（403 身份校验）
- `routes_keys.py:23,50,65` keys 创建/吊销/列表（manager/orchestrator 门）
- `routes_pipeline.py:49` `POST /api/v1/chunks/ingest`；`:23` `chunks/search`（degraded 标记）
- `disclosure.py:327-369` `semantic_search` CD-016 降级（`degraded:true`）
- `routes.py:826,832` `/healthz` `/readyz`
- `config.example.yaml:64` `port: 3060`
- arch-site：`App.tsx`（react-router v7 单路由）、`pages/Home.tsx`、`sections/SystemArchitecture.tsx:13-19`（五页+档案视图 status）、`components/ui/`（shadcn/ui 已装）
