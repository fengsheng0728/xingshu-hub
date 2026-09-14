> ⚠️ **历史文档**：本文件是 v1 立项期的实施 Spec，**不代表当前实现**。
> 归档日期：2026-09-10 ｜ 归档原因：头部「现状基线」已过期（表内 main.py 1114 行 / client.py 537 行等均为 v1 立项时数据，现行为 hub_core + hub_mixins 拆分结构，client.py 已 866 行）。
> 当前结构以仓库根 README / `hub_core.py` + `hub_mixins/` 拆分结构 / `docs/` 下各验收与设计文档为准。

---

# 星枢（Sync Hub）重构 Spec v1

> 面向 DeepSeek 执行的工程文档。按 Phase 顺序实施，每个 Phase 独立可交付、独立可验证。
> 项目路径：E:\sync-hub-case  端口：3060
> 技术栈：FastAPI + SQLite + WebSocket + 原生 HTML/CSS/JS（无前端构建工具）

---

## 现状基线

> ⚠️ 下表为 v1 立项时基线，行数/文件结构均已过期，仅供追溯。

| 文件 | 行数 | 作用 |
|------|------|------|
| main.py | 1114 | Hub 服务器：Agent注册、记忆池、渐进式披露引擎、任务调度、WebSocket、Dashboard API |
| client.py | 537 | Agent SDK + 完整演示场景（企业） |
| dashboard/index.html | 416 | 监控面板（深色 slate 蓝调） |
| showcase/index.html | 589 | 作品集展示页（Linear 风深色） |
| FAQ.md | - | 面向小公司老板的 10 个问答 |
| docker-compose.yml | - | Docker 部署配置 |
| sync_hub.db | - | SQLite 数据库（已有演示数据） |

---

## 待修复问题清单（7 项）

| # | 问题 | 严重度 | 所在 Phase |
|---|------|--------|------------|
| P1 | agents 字典纯内存态，Hub 重启后丢失，无恢复机制 | 高 | Phase 1 |
| P2 | 披露引擎 query 过滤形同虚设，不管查什么都返回全部 20 条 | 高 | Phase 1 |
| P3 | FAQ 承诺的离线写入+补传，client.py 完全没实现 | 高 | Phase 1 |
| P4 | 所有 API 无认证，裸奔 | 高 | Phase 1 |
| P5 | Dashboard 和 Showcase 两套设计语言，割裂 | 中 | Phase 2 |
| P6 | 任务生命周期不完整：只有 pending→assigned，无完成/失败 | 高 | Phase 1 |
| P7 | 无通知系统，披露升级请求只能靠轮询发现 | 中 | Phase 2 |

---

## Phase 1 — 生产可用基座

目标：修复 P1/P2/P3/P4/P6，让星枢从"能演示"变成"能跑生产"。

### 1.1 持久化与恢复（P1）

**问题**：`self.agents` 字典纯内存，Hub 重启后清空，SQLite 里的 agent 记录 status 全变 offline 但内存里不存在，心跳打过来返回 `{"status": "unknown"}`。

**修复方案**：

1. `SyncHub.__init__` 增加 `_restore_agents()` 方法：
   - 从 SQLite agents 表读取全部记录
   - 重建 `self.agents` 字典，status 统一设为 `"offline"`
   - `last_heartbeat` 保留数据库里的值

2. `lifespan` 启动时调用 `hub._restore_agents()`

3. 心跳接口兼容未知 Agent：当 `heartbeat()` 收到 unknown agent_id 时，先从 SQLite 查该 agent 是否存在，存在则重建到内存字典并设为 online，不存在才真正返回 unknown。

4. SQLite 启用 WAL 模式提升并发读写：`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`

**验证标准**：
- 启动 Hub → 注册 3 个 Agent → kill Hub → 重新启动 → agents 字典自动恢复，status=offline
- 发一次心跳 → status 变 online，无需重新注册

### 1.2 披露引擎 query 过滤修复（P2）

**问题**：`request_disclosure()` 中 query 过滤逻辑：

```python
# 当前代码（main.py 约 430-440 行）
if req.query:
    if req.query.lower() not in json.dumps(mem["tags"]).lower() and req.query not in (mem["memory_key"] or ""):
        if level != DisclosureLevel.METADATA:
            if req.query not in (mem.get("summary") or "").lower():
                pass  # ← 这里 pass 了，等于没过滤
```

三层 if 嵌套最后 `pass`，所有记忆无条件保留。

**修复方案**：

重写 query 匹配逻辑，分三档匹配 + relevance_score：

```python
def _match_query(self, memory: dict, query: str) -> tuple[bool, float]:
    """
    返回 (是否匹配, 相关度分数 0.0-1.0)
    匹配规则：
      - tags 精确匹配：+0.5
      - memory_key 模糊匹配：+0.3
      - content/summary 全文匹配：+0.2
    阈值：score > 0 才算匹配
    """
    score = 0.0
    q = query.lower()

    tags = json.loads(memory.get("tags") or "[]")
    if any(q == t.lower() for t in tags):
        score += 0.5
    elif any(q in t.lower() for t in tags):
        score += 0.3

    key = (memory.get("memory_key") or "").lower()
    if q in key:
        score += 0.3

    content = (memory.get("content") or "").lower()
    summary = (memory.get("summary") or "").lower()
    if q in content or q in summary:
        score += 0.2

    return score > 0, min(score, 1.0)
```

在 `request_disclosure()` 循环中：
- 无 query 时：返回全部（保持现有行为）
- 有 query 时：只返回 `matched=True` 的记忆，并在结果中带 `relevance_score` 字段
- METADATA 级别也参与过滤（之前 METADATA 被跳过过滤是设计意图，但实际效果是泄露不相关记忆的元数据）

**验证标准**：
- 写入 3 条记忆（tags 分别含"色差"、"橱柜"、"售后"）
- query="色差" → 只返回 tags 含"色差"的那条
- query="" → 返回全部

### 1.3 离线写入 + 补传（P3）

**问题**：FAQ.md 第 2 条承诺"Hub 连不上 Agent 仍能写本地、恢复后补传"，但 client.py 的 `write_memory()` 直接发 HTTP 请求，Hub 挂了就报错，没有任何本地缓存。

**修复方案**：

在 client.py 的 `HermesSyncClient` 中增加本地缓存层：

1. 新增 `LocalCache` 类：
   - 使用 SQLite（路径：`./{agent_id}_local_cache.db`）
   - 表结构：`(local_id, memory_key, content, importance, tags, disclosure_level, disclosure_scope, created_at, synced)`
   - `synced` 字段：0=未同步，1=已同步

2. `write_memory()` 改为先写本地缓存，再尝试同步到 Hub：
   ```
   write_memory():
     1. 写入本地缓存（synced=0）—— 永远成功
     2. 尝试 POST /api/v1/memory/store
     3. 成功 → 更新本地 synced=1
     4. 失败 → 保持 synced=0，启动后台同步任务
   ```

3. 新增 `_sync_loop()` 后台协程：
   - 每 30 秒检查本地未同步的记忆
   - 批量 POST 到 Hub
   - 成功的标记 synced=1
   - 连续失败 3 次后降频到 2 分钟一次（指数退避）

4. `connect()` 时先触发一次 `_sync_loop()`，补传离线期间积累的记忆

5. 本地缓存保留已同步记忆 7 天后自动清理（避免无限膨胀）

**验证标准**：
- 启动 Hub → 启动 client → 写 3 条记忆 → 全部同步成功
- kill Hub → 写 2 条记忆 → 本地缓存有 2 条 synced=0，无报错
- 重启 Hub → 30 秒内 2 条记忆自动补传成功

### 1.4 API 认证（P4）

**问题**：所有 API 无认证，任何人知道 3060 端口就能注册 Agent、查披露、创建任务。

**修复方案**：

1. Agent 注册时 Hub 返回 `api_key`：
   - 生成方式：`secrets.token_urlsafe(32)`
   - 存入 agents 表新增字段 `api_key`
   - 注册响应中返回 api_key（仅此一次明文返回，后续靠它认证）

2. 新增中间件 `AuthMiddleware`：
   - 从 `Authorization: Bearer {api_key}` 提取 token
   - 查 agents 表匹配 api_key → 获取 agent_id
   - 将 agent_id 注入 request.state.authenticated_agent_id
   - 例外路径（不需要认证）：`/health`、`/`、`/showcase`、`/api/v1/agents/register`、`/static/*`

3. 各端点校验：
   - `/memory/store`：request.state.agent_id 必须等于 query param agent_id（不能冒充别人写记忆）
   - `/memory/disclose`：requester_agent_id 必须等于认证身份
   - `/tasks/create`：creator_agent_id 必须等于认证身份
   - `/tasks/{id}/schedule`：只有 manager/orchestrator 角色可调用
   - `/tasks/{id}/advance`：认证身份必须是该任务的 assigned_agent
   - `/api/v1/dashboard`：任意已认证 Agent 可查看（但看到的数据受角色限制）

4. client.py 的 `SyncConfig` 新增 `api_key` 字段，所有请求自动带 Bearer header。注册时自动保存返回的 api_key。

5. 兼容模式：通过环境变量 `SYNC_HUB_NO_AUTH=1` 可关闭认证（仅限开发调试，生产默认开）。

**验证标准**：
- 无 token 访问 /api/v1/memory/store → 401
- 用 A 的 token 冒充 B 写记忆 → 403
- 用 worker token 调 /tasks/{id}/schedule → 403
- 正确 token + 正确身份 → 200

### 1.5 任务生命周期补全（P6）

**问题**：任务只有 `pending` → `assigned` 两个状态，`advance_disclosure` 之后任务就悬在 assigned 状态，没有完成/失败/取消。

**修复方案**：

1. 任务状态机：
   ```
   pending → assigned → in_progress → completed
                      → in_progress → failed
                      → cancelled（任意阶段可取消）
   ```

2. 新增 API 端点：
   - `POST /api/v1/tasks/{task_id}/start` — assigned → in_progress
     - 仅 assigned_agent 可调用
   - `POST /api/v1/tasks/{task_id}/complete` — in_progress → completed
     - 仅 assigned_agent 可调用
     - 接收 `result` 字段写入 tasks.result
   - `POST /api/v1/tasks/{task_id}/fail` — in_progress → failed
     - 仅 assigned_agent 可调用
     - 接收 `failure_reason` 字段
   - `POST /api/v1/tasks/{task_id}/cancel` — 任意状态 → cancelled
     - creator 或 orchestrator 可调用

3. 状态转换校验：非法转换返回 400 + 当前状态

4. Dashboard 数据增加任务状态分布统计

5. client.py 对应增加 `start_task()`、`complete_task()`、`fail_task()`、`cancel_task()` 方法

**验证标准**：
- 创建任务 → 调度 → start → complete，查 DB 状态为 completed，result 有值
- pending 直接 complete → 400
- 非 assigned_agent 调 start → 403

---

## Phase 2 — UI 重做 + 通知系统

目标：修复 P5/P7，让星枢的监控面板和展示页风格统一、交互完整。

### 2.1 Dashboard 重做（P5）

**设计方向**：简洁现代，不局限于单一设计系统。可以参考 Linear / Vercel / Notion 的简洁感但不需要死板照搬。核心是：

- 深色主题（但可以用比 #08090a 更柔和的底色，如 #0d1117 或 #16161a）
- 信息密度适中，不过度堆砌
- 卡片式布局，圆角 12px，微妙的边框和悬停效果
- 一个主强调色（不一定是紫色，可以是青绿/蓝等）
- 字体用系统字体栈或 Inter，但不强制 cv01/ss03

**页面结构**：

```
┌─────────────────────────────────────────────────┐
│  顶部栏：Logo · 在线统计 · 时间                    │
├─────────────────────────────────────────────────┤
│  ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│  │ Agent    │ │ 记忆池   │ │ 任务     │        │
│  │ 在线/总数 │ │ 总条数   │ │ 各状态   │        │
│  └──────────┘ └──────────┘ └──────────┘        │
│                                                 │
│  ┌─────────────────────┐ ┌──────────────────┐  │
│  │ Agent 列表           │ │ 任务流水线        │  │
│  │ ID·角色·部门·状态    │ │ 状态列·拖拽看     │  │
│  │ ·心跳时间            │ │ 任务详情         │  │
│  └─────────────────────┘ └──────────────────┘  │
│                                                 │
│  ┌─────────────────────────────────────────┐   │
│  │ 披露审计流（最近 20 条）                   │   │
│  │ 谁→谁·级别·时间·原因                     │   │
│  └─────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

**技术要求**：
- 用 WebSocket 替代轮询获取实时数据
- Hub 端新增 `GET /ws/dashboard` WebSocket 端点，有事件时推送
- 保留 `GET /api/v1/dashboard` REST 接口作为降级方案
- Agent 上线/下线、任务状态变更、披露记录 → 实时推送

**关键交互**：
- Agent 卡片点击 → 展开该 Agent 的最近记忆（摘要级别，受角色限制不展示 full）
- 任务卡片点击 → 展开任务详情 + 披露历史
- 披露升级请求 → 顶部弹窗提醒 orchestrator（如果有 WebSocket 连接）

### 2.2 通知系统（P7）

**问题**：披露升级请求只能靠轮询 dashboard 发现，没有主动推送。

**修复方案**：

1. Hub 端新增 `NotificationManager` 类：
   - 维护 `Dict[agent_id, List[WebSocket]]`（一个 agent 可能有多个 WS 连接）
   - `send_notification(agent_id, notification)` 方法

2. 通知类型：
   ```python
   class NotificationType:
       DISCLOSURE_REQUEST = "disclosure_request"  # 有人请求披露升级
       DISCLOSURE_GRANTED = "disclosure_granted"  # 披露升级已批准
       TASK_ASSIGNED = "task_assigned"            # 新任务分配给你
       TASK_COMPLETED = "task_completed"          # 你创建的任务已完成
       TASK_FAILED = "task_failed"                # 任务失败
       AGENT_OFFLINE = "agent_offline"            # 你管理的 Agent 掉线
       AGENT_ONLINE = "agent_online"              # 你管理的 Agent 上线
   ```

3. 触发点：
   - `advance_disclosure()` → 通知 task 的 creator_agent_id（通常是 orchestrator/manager）
   - `schedule_task()` → 通知 assigned_agent_id（已有 WebSocket 推送，统一到 NotificationManager）
   - `complete_task()` / `fail_task()` → 通知 creator_agent_id
   - `_cleanup_loop()` 检测到 Agent 超时 → 通知该 Agent 的 manager

4. client.py 新增 `_notification_handler()` 回调：
   - 收到通知时打印格式化消息
   - 可自定义回调函数处理特定通知类型

5. Dashboard WebSocket 端点也接收通知，在 UI 上实时展示

**验证标准**：
- 小王申请披露升级 → 店长的终端立即打印通知
- 小王完成任务 → 店长终端立即打印通知
- 小王掉线 → 主管小李终端打印告警

---

## Phase 3 — 扩展能力（按需开启）

### 3.1 语义搜索（ChromaDB 集成）
- 代码已预留 EMBEDDING 级别和 ChromaDB 注释
- 实施时：pip install chromadb，在 store_memory 中同步写入 chroma collection
- 新增 `/api/v1/memory/semantic_search` 端点
- 向量模型用 sentence-transformers 本地推理（不依赖外部 API）

### 3.2 日报 Agent
- 注册一个 system Agent（role=orchestrator, capabilities=["daily_report"]）
- 每天凌晨查询所有 Agent 的记忆摘要，汇总成日报
- 日报写入 memory_pool，disclosure_scope=all
- Dashboard 新增"日报"视图

### 3.3 PostgreSQL 迁移路径
- 提供迁移脚本：SQLite → PostgreSQL
- 配置项 `DB_TYPE=sqlite|postgresql`
- 100+ Agent 时切换 PG，SQL 语句用 SQLAlchemy ORM 或保持手写 SQL + 适配层

### 3.4 多 Hub 分片
- 500+ Agent 场景：按 department 分片到不同 Hub 实例
- Hub 间通过内部 API 交叉查询（跨部门协作时）
- 配置中心记录 Agent → Hub 映射关系

---

## 实施注意事项（DeepSeek 必读）

### 代码层面

1. **不要破坏现有 API 契约**。现有 7 个端点的 URL、请求体、响应体结构必须保持兼容。Phase 1 的新端点是新增不是修改。认证中间件是新增层，不影响已有端点的业务逻辑。

2. **SQLite 线程安全**。当前代码每次操作 `sqlite3.connect()` 新建连接，这在 asyncio 环境下勉强能用但不够健壮。Phase 1 改为用 `aiosqlite` 或至少确保每个连接在同一线程内使用。不要用全局单连接（多协程并发会炸）。

3. **`self.agents` 字典和 SQLite 的双写一致性**。所有修改 agents 的地方（register、heartbeat、cleanup）必须同时更新内存字典和 SQLite，不能只改一边。建议封装 `_update_agent(agent_id, **fields)` 统一处理。

4. **认证中间件不要挡住 WebSocket**。WebSocket 握手时的认证方式不同于 HTTP——需要在 `ws_endpoint` 中手动从 query param 或首条消息中提取 token。建议 WS 连接 URL 带 `?api_key=xxx`。

5. **本地缓存 DB 命名**。用 `{agent_id}_local_cache.db` 而不是统一的 `local_cache.db`，因为多 Agent 同进程运行时不能共用一个缓存库。

6. **任务状态机用枚举**。不要用裸字符串 `"completed"`，定义 `class TaskStatus(str, Enum)` 避免拼写错误。

7. **披露引擎的 `_calculate_disclosure_level` 有 7 条规则**，修改时逐条对照，不要漏掉任何一条。特别是规则 6（同级 Worker 同任务协作）和规则 7（记忆自身策略限制），这两个容易被忽略。

### 测试层面

8. **每个 Phase 交付后必须跑通完整 demo**。`python main.py` + `python client.py --demo` 全流程不能报错。这是最低验收标准。

9. **离线写入测试要真的 kill Hub**。不要用 mock 或 mock server，要实际启动 uvicorn、实际 kill 进程、实际重启。验证本地缓存和补传的真实行为。

10. **认证测试要覆盖越权场景**。不是只测"有 token 能访问"，必须测"用 A 的 token 访问 B 的资源"返回 403。

### 兼容性层面

11. **sync_hub.db 已有演示数据**。Phase 1 的数据库 schema 变更（新增 api_key 字段、新增任务状态等）必须用 `ALTER TABLE` 增量迁移，不能 DROP 重建。如果字段不存在再 ALTER，已存在就跳过。

12. **环境变量 `SYNC_HUB_NO_AUTH=1`**。开发调试时关认证用，但要在日志里打印 WARNING 提醒"认证已关闭，仅限开发环境"。

13. **Docker 部署兼容**。docker-compose.yml 的 volume 挂载了 `./main.py` 和 `./dashboard`，代码变更后 `docker-compose restart` 就能生效。不要引入需要额外构建步骤的前端依赖。

### 风格层面

14. **Dashboard 重做时保留现有 HTML 文件名**（dashboard/index.html），Hub 的 `/` 路由直接读这个文件。不要改路由结构。

15. **代码注释保持中文**。现有代码全部中文注释，保持一致。docstring 也是中文。

16. **演示场景不要删**。client.py 里的 `demo_customer_service_scenario()` 是核心验证手段，Phase 1 的改动要同步更新演示代码（比如新增加认证后的注册流程、任务完整生命周期的演示步骤），但不要删减现有演示步骤。

---

## 交付检查清单

### Phase 1 验收
- [ ] Hub 重启后 agents 字典自动恢复
- [ ] 心跳兼容未知 Agent（从 DB 重建）
- [ ] SQLite WAL 模式已启用
- [ ] query 过滤按三档匹配 + relevance_score
- [ ] 离线写入缓存到本地 SQLite
- [ ] Hub 恢复后自动补传
- [ ] API 认证中间件生效
- [ ] 越权访问返回 403
- [ ] SYNC_HUB_NO_AUTH=1 可关认证
- [ ] 任务状态机完整（pending→assigned→in_progress→completed/failed/cancelled）
- [ ] 非法状态转换返回 400
- [ ] `python client.py --demo` 全流程通过
- [ ] sync_hub.db 增量迁移，旧数据不丢

### Phase 2 验收
- [ ] Dashboard 新 UI 响应式布局
- [ ] WebSocket 实时推送替代轮询
- [ ] Agent 卡片展开看记忆摘要
- [ ] 任务卡片展开看详情+披露历史
- [ ] 披露审计流实时更新
- [ ] NotificationManager 6 种通知类型全部触发
- [ ] 披露升级请求实时推给 orchestrator
- [ ] 任务完成实时通知创建者
- [ ] Agent 掉线实时通知 manager
- [ ] `python client.py --demo` 全流程通过

### Phase 3 验收
- [ ] ChromaDB 语义搜索可用
- [ ] 日报 Agent 自动生成日报
- [ ] PostgreSQL 迁移脚本可用
- [ ] 多 Hub 分片方案文档化（可暂不实现）
