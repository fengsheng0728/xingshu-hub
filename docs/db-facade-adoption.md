# db 门面采用口径（D-10 / 3-1a）

> 基线 HEAD：`1d869d1`（2026-09-10 派发日实测）｜配套：`db_facade.py`、`tools/db_call_sites.py`、`tests/test_db_facade.py`

## 1. 门面定位

`db_facade.py` 是星枢**唯一的 db 访问入口**：新代码查库一律走门面
（`execute` / `executemany` / `query` / `query_one` / `run_sync` / `run_in_conn`）。
门面内部用 `asyncio.to_thread` 跑同步 sqlite3（CD-017 教训：慢的从来不是 SQLite
本身，是"在事件循环上同步等 SQL"），连接语义与 `hub_core._db()` 对齐
（`row_factory=Row` + `PRAGMA busy_timeout=5000`，用完即关）。

按上游决议（`docs/architecture-decision-data-backbone.md` §四，路线甲
SQLite → PostgreSQL → 多 Hub 分片），门面即**将来替换 asyncpg 的接缝**：
调用点只认门面签名，换驱动时业务代码零改动。存量同步调用点随 D-11（热点迁移）
与 PG 迁移逐步收敛。

## 2. 「唯一入口」的验收口径

本次（D-10）验收 = 以下四项**全部成立**：

1. 门面存在且 API 签名固定（`db_facade.py`，测试覆盖全部 6 个入口 + 2 个观测函数）；
2. 已被生产路径调用（`/health` 的 `checks["db_facade"] = stats_snapshot()`，
   try/except 包住，观测面不许让 health 500）；
3. 慢查询可观测（`>= CONFIG.DB_SLOW_QUERY_MS` 记 WARNING + 计数器，阈值经
   `config.yaml database.slow_query_ms` 可调，默认 200ms）；
4. 热点路径迁移清单明确（`tools/db_call_sites.py` 基线：52 文件 / 合计 608 处
   文本级调用点，口径固定，D-11 复跑对照数字下降）。

**不是**「608 处存量调用点全部已迁」——存量迁移是 D-11 与 PG 迁移的范围，
本任务有意只让 `/health` 与测试调用门面（底座先立、迁移后做）。

## 3. 已迁 / 未迁清单

### 已迁（本任务）

| 调用点 | 说明 |
|---|---|
| `routes_server.py` `/health` | 并入门面观测面 `db_facade.stats_snapshot()`（只读观测，非业务查询迁移） |
| `tests/test_db_facade.py` | 门面自身测试（10 项，含 run_in_conn 事务语义与不阻塞事件循环实证） |

### 未迁（登记，随 D-11 热点迁移或 PG 迁移一起换）

- 存量全部生产调用点：`tools/db_call_sites.py` 基线 52 文件 / 608 处
  （`db.py` 82、`audit_chain.py` 32、`hub_mixins/dashboard.py` 27、
  `hub_core.py` 26、`hub_mixins/ingest.py` 25、`hub_mixins/tasks.py` 25 …）。
- 四种连接工厂变体（口径登记，本任务不统一）：
  - `hub_core._db()` —— `sqlite3.connect(CONFIG.DB_PATH)` + Row + busy_timeout=5000（门面已对齐此语义）
  - `disclosure_rules._connect()` —— busy_timeout 5000 + Row
  - `key_scopes.py:45` —— 同类变体
  - `fed_crypto._db_conn()` —— timeout=5 变体
