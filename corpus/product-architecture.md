---
title: 架构：Hub + Agent 端双层架构
category: product
tags: [架构, WebSocket, Electron, 披露引擎, ChromaDB]
source: examples/showcase/index.html（Architecture 段）
---

# 架构：Hub + Agent 端双层架构

Hub 负责核心逻辑与数据持久化，Agent 端是通用协同终端。双方通过 WebSocket 全双工通信，REST 端点覆盖全部管理操作。

- Electron Agent 端：渲染进程 · Python 后端 · 主进程 IPC · 系统托盘
- WebSocket 全双工通信
- Sync Hub Core：hub_core + 10+ Mixin · 21 个路由模块 · 166 路由
- 披露引擎：469 行 · 8 条规则 · 4 级披露
- SQLite + ChromaDB：语义搜索 · 降级策略 · 版本回滚
- 团队联邦：UDP 发现 · P2P 配对 · AES-GCM 加密
- 共享工作区：pycrdt CRDT · YRoom · 实时协同
- 自动化调度：1s tick · croniter · heartbeat 触发
