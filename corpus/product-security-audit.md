---
title: 安全审计与身份接入
category: product
tags: [安全审计, Hash chain, 身份接入, API Key]
source: examples/showcase/index.html（Modules 段 / 角色分级段）
---

# 安全审计与身份接入

- 安全审计（Core）：Hash chain 审计日志、篡改精确定位、身份接入、API Key 自动轮换
- 身份接入：Local / LDAP / OIDC 多模式，权限按组交集计算，细粒度可控
- 角色分级：worker / manager / orchestrator 三级，披露级别与角色关系联动（主管看摘要、店长看全文）

审计链为 append-only 结构，任何篡改都会在校验时被精确定位到位置。
