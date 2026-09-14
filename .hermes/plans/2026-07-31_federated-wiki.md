# 多Hub联邦 Wiki 实现计划

> commit: 54d2006

**Goal:** 局域网内多个 Hub 自动共享 Wiki 知识库

**Architecture:** Hub A 的 wiki_sync 完成后，向已配对的 Hub B 推送新页面。或 Hub B 主动从 A 拉取。

**范围限定（最小可用版）:**
- 添加 wiki 导出/导入 API
- wiki_sync 增加 `--federate` 参数
- 同步时自动从已配对 Hub 拉取页面
- 冲突策略：时间戳较新的覆盖旧的

**Step 1:** `/api/v1/wiki/export` — 导出所有 wiki 页面为 JSON
**Step 2:** `/api/v1/wiki/import` — 从远程 Hub 导入页面
**Step 3:** wiki_sync `--federate` — 同步前从配对 Hub 拉取
**Step 4:** 端到端测试
