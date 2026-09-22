---
title: 核心模块一览
category: product
tags: [模块, 记忆池, 任务调度, Wiki, 联邦, MCP]
source: examples/showcase/index.html（Modules 段）
---

# 核心模块一览

从记忆池到团队联邦，从 Wiki 引擎到自动化调度——星枢提供 Agent 协同所需的完整基础设施。

- 记忆池（Core）：语义搜索、版本历史、回滚、ChromaDB / SQLite 双路径降级
- 任务调度（Core）：状态机 + DAG 依赖 + 环检测 + 看板视图 + 子任务拆解
- 写入缓冲（Core）：异步攒批 + 500ms 落盘 + 5s Wiki 节流 + WAL 持久化 replay
- 渐进披露（Unique）：8 条确定性规则、4 级披露、自动审批 / 人工门控、审计追踪
- 团队联邦（Unique）：UDP 局域网发现、6 位配对码、X25519 DH、跨 Hub 披露代理
- 通知系统（Unique）：站内 WS + 钉钉 WebHook + SMTP、异步 fan-out、channel_status 落库
- Wiki 引擎（New）：收件箱审查、增量 SHA256 去重、EasyMDE 本地化编辑、图谱可视化
- 共享工作区（New）：pycrdt YRoom + SQLiteYStore、awareness 在线列表、REST + WS 双通道
- LangChain Hub Agent（Core）：11 个 Hub 级 LLM 工具、语义搜索记忆池、知识库查询、任务创建
- 安全审计（Core）：Hash chain 审计日志、篡改精确定位、身份接入、API Key 自动轮换
- 自动化调度（New）：croniter 标准表达式、heartbeat 驱动、暂停 / 恢复 / 重试 / 跳过
- MCP Server（Core）：7 个 tools、SSE 传输、Bearer 认证、Wiki / Buffer / 图谱暴露
- 集成层 Integration Hub（New）：Connector 目录扫描注册、AES-GCM 凭证、附录 E 管道 taint=external、出站审批门——接入新系统只写一个适配器文件
- 管理控制台（New）：Vue3 + Pinia 11 页四区：总览 / 运行 / 活动 / 访问 / 审计 / 披露 / 知识 / Wiki / 记忆 / 集成 / 设置，亮暗同稿、完全离线
