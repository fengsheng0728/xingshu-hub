# 共享沙箱 — CRDT 实时协同方案

> **For Hermes:** 按任务逐个实现，每步 commit。

**Goal:** Agent 端加独立共享沙箱，不同电脑的 Agent 通过 Hub 实时协同编辑同一份文档/报表/项目文件。CRDT 无冲突合并。

**Architecture:** Hub 托管 shared_workspace/，每个文档是一组 CRDT Block（LWW Register），WebSocket 实时广播变更。Agent 端通过 MCP tool 读写沙箱，前端显示共享文档列表+实时编辑区。

**Tech Stack:** Python asyncio WebSocket + SQLite + LWW CRDT（纯 Python，零外部依赖）

---

## 文档 CRDT 模型

```
Document
  ├── meta: {doc_id, title, created_by, created_at, updated_at}
  └── blocks: [Block, ...]
       ├── block_id: uuid
       ├── type: "markdown" | "code" | "data"
       ├── content: str
       ├── version: int (Lamport clock)
       ├── agent_id: str
       └── updated_at: float (timestamp)
```

**合并策略：** Per-block LWW。同 block_id → version 高的赢。version 相同 → timestamp 新的赢。
**操作类型：** insert_block, update_block, delete_block, reorder_blocks

**为什么不选 Yjs：** Agent 以 block 为单位输出（不是逐字符打字），LWW per block 足够且简单。如果需要逐字符协同，后续可以换 Yrs（y-py）。

---

## Step 1: Hub 端 — shared_workspace 目录 + DB 表

**文件:** `E:\sync-hub-case\db.py` `E:\sync-hub-case\shared_workspace.py`（新）

### Task 1.1: 创建 shared_docs 表
- 表: shared_docs (doc_id, title, blocks_json, created_by, created_at, updated_at, archived)
- 表: shared_blocks (block_id, doc_id, type, content, version, agent_id, updated_at) — 可选，先存 JSON

### Task 1.2: 创建 SharedWorkspace 引擎
- `E:\sync-hub-case\shared_workspace.py`
- 类 SharedWorkspace:
  - create_doc(title, agent_id) → doc
  - list_docs() → [doc_meta]
  - get_doc(doc_id) → full doc with blocks
  - apply_op(doc_id, op) → merged doc (CRDT merge)
  - delete_doc(doc_id)
  - _merge_blocks(existing, incoming) → merged blocks (LWW)
  - WebSocket 广播: 变更后通知所有已连接客户端

### Task 1.3: REST API
- `POST /api/v1/shared/docs` — 创建文档
- `GET /api/v1/shared/docs` — 列出文档
- `GET /api/v1/shared/docs/{doc_id}` — 获取文档
- `POST /api/v1/shared/docs/{doc_id}/ops` — 提交操作（insert/update/delete block）
- `DELETE /api/v1/shared/docs/{doc_id}` — 删除文档

### Task 1.4: WebSocket 端点
- `WS /ws/shared/{doc_id}` — 实时协同通道
- 连接 → 发送当前完整文档状态
- 任何 agent 提交 op → 合并 → 广播给所有连接者
- 消息格式: `{type: "snapshot"|"op"|"ack", doc_id, blocks?, op?, version?}`

---

## Step 2: Agent 端 — shared_workspace 工具

**文件:** `E:\sync-hub-agent\backend\agent_client.py`

### Task 2.1: 添加 shared_workspace 命令
- `shared_list` — 列出共享文档
- `shared_read <doc_id>` — 读取文档
- `shared_write <doc_id> <block_content>` — 写入/追加 block
- `shared_create <title>` — 创建新文档

### Task 2.2: WebSocket 实时连接
- connect 后自动连 `/ws/shared/{doc_id}`
- 收到其他 agent 的变更 → stderr push 事件 → 前端实时刷新

---

## Step 3: Agent 端 — 共享沙箱 UI

**文件:** `E:\sync-hub-agent\src\renderer\index.html` `E:\sync-hub-agent\src\renderer\app.js`

### Task 3.1: 侧栏加「🤝 协作」tab
- 7 个 tab 基础上加第 8 个
- 页面容器 `page-shared`

### Task 3.2: 共享文档面板
- 左侧: 文档列表（标题 + 创建者 + 更新时间）
- 右侧: 文档内容（markdown 渲染，block 为单位）
- 底部: 输入区（Agent 可以追加 block）
- 实时更新: 收到 push 事件 → 刷新当前文档

---

## Step 4: 跨 Hub 联邦共享

**文件:** `E:\sync-hub-case\shared_workspace.py`

### Task 4.1: 联邦共享
- shared_docs 通过 team_members 同步到已配对 Hub
- `shared_sync` — 从已配对 Hub 拉取共享文档
- 冲突: 同文档 CRDT merge（LWW per block）

---

## 不做清单
- ❌ 逐字符 CRDT（Agent 不需要打字级协同）
- ❌ 文件二进制同步（只做 markdown/code/data 文本）
- ❌ 权限系统（先用 Hub 认证，所有配对成员可读写）
- ❌ 版本历史/回退（后续迭代）

## 验证
- 两个 Agent（不同 agent_id）同时写同一文档 → 无冲突合并
- WebSocket 广播延迟 < 100ms
- 重启 Hub 后文档不丢失
- 联邦同步：A Hub 创建文档 → B Hub 可见
