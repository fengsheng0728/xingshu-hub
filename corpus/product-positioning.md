---
title: 产品定位：信息权限中间件
category: product
tags: [产品定位, 写入隔离, 渐进披露, 角色分级]
source: examples/showcase/index.html（Core Concept 段）
---

# 产品定位：信息权限中间件

星枢 Sync Hub 为 Agent 协同提供写入隔离、渐进披露、角色分级三大基座。不绑定任何行业——客服、法务、销售、项目管理均可即用。

星枢不只是一个 Agent 连接器，而是一个信息权限中间件。它决定了谁能看到什么、能写入什么、何时披露——在数据产生的第一刻就施加控制。

- 写入隔离：所有 Agent 的记忆写入先入队列，后台 worker 批量落库。避免 SQLite 锁竞争，支持 200 并发峰值 3.59s 内完成，零丢失、零超时。
- 渐进披露：8 条确定性披露规则，4 级披露级别（NONE / METADATA / SUMMARY / FULL）。请求 → 规则匹配 → 自动或人工审批 → 返回结果。
- 角色分级：多 Hub 联邦配对、跨 Hub 披露代理、身份接入（Local / LDAP / OIDC）、API Key 自动轮换。权限按组交集计算，细粒度可控。
