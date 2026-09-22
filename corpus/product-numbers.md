---
title: 工程数据：测试与性能口径
category: product
tags: [测试, 压测, 性能, 回归]
source: examples/showcase/index.html（Numbers 段）
---

# 工程数据：测试与性能口径

每一个数字背后都是一次 commit、一轮压测、一份验收表。

- 392（+279）：Hub / Agent 测试全绿
- 6.3s：200 并发峰值入队延迟（原 31.1s）
- 0：锁竞争 / 超时 / 丢失
- 153：REST 端点覆盖

注：以上数字为 2026.08 展示页发布时点的口径，后续版本以 `/api/v1/stats` 与当轮回归统计为准。
