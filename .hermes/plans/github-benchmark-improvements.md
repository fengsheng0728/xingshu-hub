# 星枢 GitHub 对标改进计划

> 2026-07-31 | 三个可改进模块，逐个过关

## 改进清单

| # | 模块 | 来源 | 改动 | 涉及文件 |
|---|------|------|------|---------|
| 1 | 记忆池 | nocturne_memory | 记忆回滚 + 版本化 | Hub: hub_core.py, db.py, routes.py |
| 2 | Agent Loop | headroomlabs | 工具输出语义压缩 | Agent: agent_client.py, context/manager.py |
| 3 | Wiki 引擎 | ThinkWiki | 收件箱审查模式 | Hub: wiki_engine.py, routes.py |

---

## 1. 记忆池 — 回滚 + 版本化

### 目标
- 每条记忆支持版本历史（INSERT 新版本，旧版本归档）
- 支持回滚到历史版本
- 新增 REST: `GET /api/v1/memory/{key}/versions`, `POST /api/v1/memory/{key}/rollback`

### 实现
- memory_pool 表结构不变，新增 `memory_versions` 表
- store_memory 时如果 key 已存在 → 旧版本插入 versions 表 → 新版本覆盖
- rollback 时从 versions 表取回指定版本

### 验证
- 创建记忆 → 更新记忆 → 查版本历史 → 回滚 → 验证内容恢复

---

## 2. Agent Loop — 工具输出压缩

### 目标
- 对长工具输出做语义压缩而非简单截断
- 基于 LLM 摘要保留关键信息，丢弃冗余

### 实现
- context/manager.py 新增 `_compress_tool_output()` 
- 对超过 2000 字符的工具输出 → 调用一次快速 LLM 摘要
- 摘要保留在上下文中，原始输出仅保留引用

### 验证
- 模拟长工具输出 → 压缩后 token 数显著减少 → 关键信息保留

---

## 3. Wiki — 收件箱审查

### 目标
- 自动同步的新页面先进入 inbox 状态
- 新增 REST: `GET /api/v1/wiki/inbox`, `POST /api/v1/wiki/inbox/{path}/approve`, `POST /api/v1/wiki/inbox/{path}/reject`
- hub-core 写缓冲 worker 写 wiki 时标记 pending，不直接发布

### 实现
- wiki_pages 表新增 `status` 字段: published/pending/rejected
- 写缓冲 worker 同步 wiki 时设置 status='pending'
- 新增收件箱 API 供管理员审核

### 验证
- 自动同步创建页面 → 状态为 pending → 审批 → 变为 published
