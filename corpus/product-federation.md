---
title: 团队联邦：多 Hub 协同
category: product
tags: [团队联邦, UDP 发现, 配对码, 跨 Hub 披露]
source: examples/showcase/index.html（Architecture / Modules 段）
---

# 团队联邦：多 Hub 协同

星枢支持多 Hub 联邦：UDP 局域网发现、6 位配对码、X25519 DH 密钥交换、AES-GCM 加密传输、跨 Hub 披露代理。

- UDP 发现：局域网内自动发现对端 Hub
- P2P 配对：6 位配对码人工确认
- 加密：X25519 DH 协商 + AES-GCM 加密
- 跨 Hub 披露代理：本端 Agent 可经联邦链路查询对端 Hub 的披露内容，权限按组交集计算
