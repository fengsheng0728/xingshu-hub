---
title: 能力中枢：AI 中台
category: product
tags: [AI 中台, LLM 工具, LangChain, 语义搜索]
source: examples/showcase/index.html（Capability Hub / AI 中台段）
---

# 能力中枢：AI 中台

Hub 作为 AI 能力中枢，统一调度 11 个 LLM 级工具。Agent 无需关心底层实现，通过自然语言即可调用记忆搜索、知识查询、任务创建等全部能力。

- 语义搜索记忆池——ChromaDB 向量化 + SQLite 降级双路径
- 知识库查询——结构化知识 + 图谱关系 + 自动补全
- 在线 Agent 感知——列出可用 Agent、创建并分配任务
- 披露审批门控——LangChain 工具直接 approve / deny
- Wiki 内容注入——从记忆自动构建知识、增量去重入库
