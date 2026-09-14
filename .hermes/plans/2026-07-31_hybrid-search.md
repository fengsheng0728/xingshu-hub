# 向量+关键词混合搜索 实现计划

> commit: 0a3288b

**Goal:** Wiki 搜索同时使用关键词匹配和向量相似度，合并排序。

**Architecture:** 利用已有的 `LocalEmbedding`（HashingVectorizer 384维），在 wiki_sync 时对每个页面生成 embedding 并存入 SQLite，搜索时同时做关键词+向量检索，按加权分数排序。

**Step 1:** 在 `knowledge_base` 表加 `embedding BLOB` 字段
**Step 2:** `wiki_sync.py` 同步时为每个页面生成 embedding 并写回 DB
**Step 3:** `wiki_engine.py` 的 `wiki_search` 改为混合搜索
**Step 4:** MCP `wiki_search` 工具同步更新
**Step 5:** 验证：搜索质量对比
