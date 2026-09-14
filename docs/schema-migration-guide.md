# 星枢改表标准流程（Alembic 接管后）

> 生效：功能完整性轮 P2（alembic 1.18.5 baseline `0001_baseline`）
> 取代「口头改表 + 手动 SQL 存档」的过渡模式（D4→D5）。**今后改表 = 写 migration，不再手动 SQL。**

## 改表步骤（开发期）

1. **写 migration**：
   ```
   cd E:\sync-hub-case
   alembic revision -m "描述: 改了什么"
   ```
   编辑 `migrations/alembic/versions/<新文件>.py` 的 upgrade()/downgrade()。
   ⚠️ 本项目无 SQLAlchemy ORM（裸 sqlite3），**autogenerate 不可用**——手写 DDL。
   ⚠️ DDL 含 SQLite 特有语法（`--` 注释、`DEFAULT (datetime('now'))` 表达式）时，
   用 sqlite3 原生连接执行（`context.get_context().connection.connection.driver_connection`），
   不要用 `op.execute`（SQLAlchemy 参数化会误解析 `?`/`(10)`）。

2. **本地验证**（三条全过才算完）：
   ```
   # 空库升级（硬等式参考）
   set SYNC_HUB_DB=<临时空库路径>
   alembic upgrade head
   # 回环
   alembic downgrade base && alembic upgrade head
   # 现库升级（先备份！）
   alembic upgrade head
   ```
   对照 `docs/p2-alembic-acceptance.md` 的 T2-1/T2-2 口径：sqlite_master 逐项一致、数据零变化。

3. **提交**：migration 文件 + 对应代码同一 commit。

## 部署期（生产库）

```
# 1. 备份
# 2. 升级（D5: 手动触发，无启动自动 migrate）
alembic upgrade head
# 3. 验证 /health 200 + 关键表行数不变
```

## 约定

- migration 只做**结构迁移**，不做数据迁移/修复
- 不改已发布的 migration（新变更写新文件）
- FTS5 影子表（memory_pool_fts_data/idx/docsize/config）由虚拟表自动生成，
  baseline 与对比口径均排除它们
- alembic_version 表是 alembic 元数据，不参与 schema 对比
