---
title: 能力中枢：企业知识库
category: product
tags: [企业知识库, 语义检索, 知识图谱, Wiki]
source: examples/showcase/index.html（Capability Hub / 企业知识库段）
---

# 能力中枢：企业知识库

三层模型驱动的企业知识引擎：Markdown 编辑层、[[wikilink]] 链接层、D3 图谱可视化层。新知识先进入收件箱审查，人工 approve 后才正式发布。

- 语义检索——ChromaDB 向量存储，自然语言即可命中知识
- 知识图谱——节点关系可视化，发现隐性关联
- Wiki 引擎——EasyMDE 本地化编辑、增量 SHA256 去重
- 收件箱审查——ThinkWiki 模式：pending → approve / reject
- 记忆自动构建——从 Agent 对话记忆中提取并 upsert 到知识库
