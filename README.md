# 星枢 Sync Hub

**写入隔离 + 渐进式披露 —— 面向 99 人小团队的多 Agent 协同中间件**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green)](https://python.org)

---

## 这是什么？

当一个小团队有多个 AI Agent 同时工作（客服、售后、跟单、质检...），它们之间怎样共享信息，又不泄露不该泄露的内容？

传统方案："所有 Agent 共享一个数据库" → 信息过载 + 隐私风险。

星枢的方案：

| 原则 | 说明 |
|------|------|
| **写入隔离** | 每个 Agent 写自己的记忆池，零广播 |
| **按需披露** | 需要时才通过 Hub 查询，受身份和策略控制 |
| **渐进披露** | 分阶段给信息：元数据 → 摘要 → 完整内容 |
| **层级管控** | 三级角色映射真实企业组织（调度者 / 主管 / 执行者） |
| **完整审计** | 谁看了什么、什么级别、什么时间，全部可追溯 |

## 典型场景

```
客服小王接待客户 → 写入工单到自己的记忆池（其他人看不见）
主管小李查看团队 → 看到小王的工单摘要（看不到完整记录）
小王遇到纠纷需要完整历史 → 申请提升披露 → 授权后获得完整内容
店长老陈创建投诉任务 → 调度给小王 → 先给摘要，完成后披露完整记录
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置（首次使用）
cp config.example.yaml config/config.yaml
# 根据需要编辑 config/config.yaml

# 3. 启动 Hub
python main.py

# 4. 另开终端，运行演示
python examples/demo_workflow.py
```

浏览器打开 http://localhost:3060 查看 Dashboard。

## 项目结构

```
├── main.py               # 入口（FastAPI 服务）
├── hub_core.py           # 核心引擎（Agent 管理 / 记忆 / 任务 / 披露）
├── hub_agent.py          # Hub Agent（LLM 审计 / 日报 / 分析）
├── hub_agent_lc.py       # LangChain 工具集成
├── disclosure.py         # 渐进式披露引擎（8 条规则）
├── models.py             # 数据模型 / 配置
├── db.py                 # 数据库 / 嵌入
├── routes.py             # API 路由装配层
├── notifications.py      # 通知管理
├── examples/             # 演示/示例资产（非运行必需）
│   ├── client.py         # Agent SDK（Python 客户端）
│   ├── demo_workflow.py  # 一页纸端到端演示（7 幕）
│   └── showcase/         # 销售落地页
├── config.example.yaml   # 配置文件模板
├── dashboard/            # Web Dashboard
└── tests/                # 测试（pytest）
```

## 核心 API

| 端点 | 说明 |
|------|------|
| `POST /api/v1/agents/register` | 注册 Agent |
| `POST /api/v1/memory/store` | 写入记忆（隔离，不广播） |
| `POST /api/v1/memory/disclose` | 按需披露查询 |
| `POST /api/v1/tasks/create` | 创建任务 |
| `POST /api/v1/tasks/{id}/advance` | 提升披露级别 |
| `GET /api/v1/dashboard` | 监控面板数据 |
| `WS /ws/{agent_id}` | WebSocket 实时推送 |

## 披露级别

| 级别 | 内容 | 场景 |
|------|------|------|
| `NONE` | 不披露 | 默认隔离 |
| `METADATA` | 标签 / 时间 / 重要性 | 存在性查询、统计 |
| `SUMMARY` | 前 200 字摘要 | 主管日常工作 |
| `FULL` | 完整内容 | 纠纷处理、需审批升级 |

## 角色

| 角色 | 对应岗位 | 权限 |
|------|---------|------|
| `worker` | 执行者 | 写自己的记忆，查自己的内容 |
| `manager` | 主管 | 查看下属摘要，调度团队任务 |
| `orchestrator` | 调度者 | 全局视角（受披露策略约束），跨团队调度 |

## 技术栈

- **后端**: FastAPI + SQLite + WebSocket
- **AI**: LangChain（可选，用于 AI 审计 / 日报 / 语义搜索）
- **向量搜索**: ChromaDB（可选）
- **前端**: 原生 HTML/CSS/JS（零构建）
- **部署**: Docker Compose / PyInstaller

## 部署

```bash
# 直接运行（5-20 Agent）
python main.py

# Docker（20-50 Agent）
docker-compose up -d

# PyInstaller 打包（无需 Python 环境）
python build_hub.py
```

## Agent 端协同能力（第一波）

Hub 为 Agent 端提供以下支撑，全部过五层 harness：

- 文件工具：挂载目录前缀校验、read/write/move/search、大文件截断
- Shell 三档管控：只读自动放行 / 写入审批 / 禁止硬拦截
- 产物生成：docx/xlsx/html，保存路径强制落在挂载目录内
- 通知系统：`notifications` 表支持 `artifact_path`，自动化交付可点击打开产物
- 工作台：`/api/v1/agent/workspace` 聚合任务、系统通知、团队摘要

## Token 鉴权配置（安全底线 P0）

部署级单 token 模型：一个 `hub_token` 把守 Hub 大门，所有 Agent/dashboard 共用。

1. 生成强随机值（不要用示例值）：
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. 写入 `config/config.yaml`（已被 .gitignore 排除，不会进仓库）：
   ```yaml
   auth:
     enabled: true
     hub_token: "粘贴生成的随机值"
   ```
3. 带外分发：把该值配到每台 Agent 端「设置 → Hub 连接令牌」；dashboard 页面首次打开会提示输入并保存在浏览器 localStorage。
4. 生效：改配置后重启 Hub。**轮换 = 改 token 重启**，无需其他步骤。

行为：
- 除 `/health`、6 个页面壳、`/docs` 及注册/引导端点外，所有 REST 请求无有效凭据一律 401。
- 凭据两种任选：`hub_token`（部署级）或 Agent 注册时下发的 `api_key`（agents 表）。
- `hub_token` 留空 = 退化为仅 `api_key` 认证（旧部署兼容）。
- 401 时 Agent 端 stderr 推送 `auth_failed` 事件，前端显示「鉴权失败」状态，不会静默挂起。
- WS 五通道首帧鉴权见 P1（`/ws/dashboard`、`/ws/buffer`、`/ws/{agent_id}`、`/ws/shared/{doc_id}`、`/ws/shared/watch/{doc_id}`）。

开发/测试：`SYNC_HUB_NO_AUTH=1` 全放行（仅限源码运行，PyInstaller 打包拒绝启动）。

## 参与贡献

欢迎 Issue 和 PR。请先阅读 `FAQ.md` 了解设计理念。

## 关键词

多Agent协同 / 信息中间件 / 写入隔离 / 渐进式披露 / AI Agent协作 / 小团队工具 /
Agent Orchestration / Disclosure Control / Privacy-Preserving / Multi-Agent / Team Collaboration /
FastAPI / LangChain / Python / SQLite / WebSocket

## 许可

[MIT](LICENSE) © 2026 星枢 Sync Hub Contributors
