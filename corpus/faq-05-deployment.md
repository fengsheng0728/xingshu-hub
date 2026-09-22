---
title: 部署和维护复杂吗？需要专门请 IT 吗？
category: faq
tags: [部署, 运维, Docker, 一键启动]
source: FAQ.md#L68-L84
---

# 部署和维护复杂吗？需要专门请 IT 吗？

**最低要求：一台能跑 Windows/Linux 的电脑，懂基础命令行操作。**

部署方式（按难度从低到高）：

| 方式 | 难度 | 说明 |
|------|------|------|
| 一键启动 | ★☆☆ | `python main.py` 直接跑 |
| Docker 部署 | ★★☆ | `docker-compose up -d` |
| 服务注册 | ★★★ | 写入 systemd/Windows 服务自动启动 |

99 人以内的小公司场景：
- **5-10 个 Agent**：单机 `python main.py` 完全够用
- **20-50 个 Agent**：建议用 Docker 部署（资源隔离）

**不需要专职 IT。** 能把电脑开机、能打开命令行就能跑。
