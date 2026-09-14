# 功能完整性轮 P2 验收表 — Alembic schema 迁移

> 方案：《星枢-功能完整性与稳定性轮-执行方案.md》P2（70 分钟预算）
> 目标：Alembic 接管 schema，硬等式验收——空库 upgrade head 与现库逐表一致
> 工具：alembic 1.18.5（Python 3.14.4 兼容）
> commit：`（待填）`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T2-1 硬等式 | 空库 upgrade + sqlite_master 对比 | ✅ **差异 = 0**（24 对象：21 业务表 + memory_pool_fts + 2 索引，DDL 逐项一致） | t21_hard_eq.py |
| T2-2 回环 | upgrade→downgrade base→upgrade | ✅ 两次 head 结构一致，差异 = 0 | t22_loop.py |
| T2-3 现库无损 | stamp head + 行数对比 | ✅ 8 表行数零变化（agents 21/tasks 11/mem 10/...）；Hub /health ok | _t23_before/after |
| T2-4 依赖证据 | pip freeze diff | ✅ 仅新增 alembic==1.18.5 + Mako==1.3.12 | /tmp/pip_before/after.txt |
| T2-5 回归 | pytest | ✅ Hub 174 全绿（166+8 DAG） | 回归输出 |

## 实现要点

1. **初始化**：`alembic init migrations/alembic`（migrations/ 已含 manual/，alembic 放子目录）；`alembic.ini` script_location 指向子目录
2. **env.py**：读 `models.CONFIG.DB_PATH`（导入项目根）+ `SYNC_HUB_DB` env 覆盖（硬等式验证空库用）
3. **baseline migration `0001_baseline`**：
   - 项目无 SQLAlchemy ORM → **autogenerate 不可用**（env.py 无 MetaData），改手写
   - DDL 逐字取自现库 sqlite_master（含全部历史手动 SQL 产物：channel_status 列 / idx_buffer_log_entry / depends_on 列）
   - **执行用 sqlite3 原生连接**：DDL 含 `--` 注释、`DEFAULT (datetime('now'))` 表达式，SQLAlchemy 参数化执行器误解析 `?`/`(10)` 绑定参数（实测踩坑）
   - **FTS5 影子表剔除**：memory_pool_fts 虚拟表创建时自动生成 data/idx/docsize/config 影子表，显式创建会冲突（实测踩坑）
4. **stamp**：现库 `alembic stamp head`（不跑 DDL，只写 alembic_version=0001_baseline）
5. **文档**：docs/schema-migration-guide.md「改表标准流程」（今后改表=写 migration，含 sqlite3 原生执行 + FTS5 影子表两个坑的说明）

## 实测踩坑记录（已写入文档）

1. `op.execute` 对 sqlite DDL 注释/默认值表达式误解析 → 改 sqlite3 原生连接
2. FTS5 影子表显式创建冲突 → baseline 剔除 4 影子表（虚拟表自动生成，对比口径同步排除）
3. autogenerate 需 MetaData → 手写（无 ORM 项目标准做法）

## 本阶段明确不做（已遵守）

- 不做启动自动 migrate（D5：手动触发，写进文档）
- 不做数据迁移/数据修复；不改任何表结构（baseline 只描述现状）
- 不引入 Alembic 之外 schema 工具
