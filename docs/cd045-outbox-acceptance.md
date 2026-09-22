# CD-045 验收：审计原子性（outbox 同事务）

- 项目：星枢 Sync Hub｜仓库 `E:\sync-hub-case`
- 基线：`07c3333`（本轮队列冻结）｜实施：外部 agent（kimi，session_02583ff9）+ Hermes 验收修正
- 任务书：`E:\星枢-待办\星枢任务书-2026-09-17\T1-裂缝4-审计outbox-任务书.md`
- 判定依据：`docs/database-crack-triage-2026-09-17.md`（裂缝4 + 漏项 L1）

## 一、修的是什么

原写路径在 SQLite 事务体内、`conn.commit()` 之前直调 `audit_memory()` 追加独立哈希链，
commit 失败（磁盘满/锁超时）回滚后，链上永久留下**一次从未发生的写入**，且链不可改 = 删不掉。
同区漏项 L1：审计写失败侧静默（`audit/memory_audit.py` 原 `except Exception: pass`）。

修法（拍板）：事务内只写 `event_outbox` 事件行（与业务数据**同生共死**），后台消费者按 id 顺序
消费 → 落审计链；失败 `attempts+1` 留 pending，达上限标 failed 并告警，重启自动 replay。
事件表是通用事件日志（带 `event_type`），CD-047 影子镜像事件复用同表。

## 二、改动清单（全部在白名单内）

| 文件 | 改动 |
|---|---|
| `db.py` | `SCHEMA_VERSION` 6→7；新增 conn7 迁移段建 `event_outbox` + `idx_event_outbox_status`（幂等） |
| `migrations/alembic/versions/0004_event_outbox.py` | 新建；`down_revision=0003_hash_agents_api_key`；sqlite 原生 driver_connection 执行 DDL（沿用 0002 手法） |
| `hub_mixins/outbox.py` | 新建（181 行）：`enqueue()`（用业务事务连接，不自建连接/不 commit/不吞异常）+ `OutboxConsumer`（daemon 线程、顺序消费、attempts/failed/last_error、`stats_snapshot()`） |
| `hub_mixins/memory.py` | 事务内 5 处 + delete 路径 1 处改走 `_outbox_enqueue`；read 审计（:575）保持直调；参数与值一字未改 |
| `hub_core.py` | 新增消费者启动块（独立 try，不依赖 `DATA_TRUNK_ENABLED`） |
| `audit/memory_audit.py` | `audit_memory(..., raise_on_error=False)`：默认旧语义（静默）不变，`True` 时上抛供消费者判失败；jsonl 字段/顺序未动 |
| `tests/test_outbox_audit_atomicity.py` | 新建 8 用例 |
| `tests/test_alembic_0002_schema.py` | 仅 head 断言 0004 + 表清单加 `event_outbox` |
| `tests/test_shadow_pending.py` | **Hermes 验收修正**：`SCHEMA_VERSION`/`user_version` 断言 6→7（kimi 主动上报「任务书内部矛盾」——该文件硬断言 v6 却要求我升到 v7；属 T7 同类版本常量断言，我自行同步） |

## 三、验收证据（Hermes 独立复跑，非采信 kimi 自报）

**新用例（先红后绿由 kimi 完成，绿由我复跑）**
`python -m pytest tests/test_outbox_audit_atomicity.py -q` → **8 passed**
含核心断言 T6-2：注入 `commit()` 失败 → `memory_pool` 无行 **且** `event_outbox` 无行 **且** 审计 jsonl 无该 key（三处同时为空）。

**相关套件**：`test_outbox_audit_atomicity + test_shadow_pending + test_alembic_0002_schema + test_s2_audit_chain` → **28 passed**（含 P99<5ms 契约、alembic 幂等）。

**全量离线集（门禁）**
```
python -m pytest tests/ -q -k "not test_cross_agent_403 and not test_memory_search_self_access and not test_memory_list_self_access" --ignore=tests/test_team_integration.py
→ 731 passed, 60 skipped, 5 deselected, 0 failed in 286.84s
```
基线 723 passed → **+8，只增不减，failed 0**。日志：`E:\星枢-待办\T1-regression-20260917.log`

**schema 硬等式**（空库 `alembic upgrade head` vs 现库，归一化 sqlite_master 逐项比对）
→ 两侧 **41 个对象，无增无缺**；`event_outbox` 与 `idx_event_outbox_status` DDL **逐字一致**。
唯一差异 `agents` 表 = **既有偏差、与本轮无关**：现库未跑 0003（缺 `api_key_hash`/`api_key_prev_hash`），
`auth_provider.py:266` 按列集检测自动降级为明文模式，故现状功能不受影响；此差异登记给 CD-030（「新库必须走 alembic」口径）。

**端到端真实 Hub（单测覆盖不到的部分：hub_core 装配 + 消费者线程真实运行）**
脚本 `E:\星枢-待办\_sync\cd045\e2e_outbox.py`，结果 `E:\星枢-待办\_sync\cd045\e2e-result-20260917.json`
（独立 config + 独立 db/chroma + 端口 3077，不碰生产）：

- E2E-0/1 真实进程就绪 + `POST /agents/register` 200 拿 api_key
- E2E-2 两次 `POST /memory/store` 200 → `memory_pool` 2 行 → `event_outbox` 2 行 → 消费者自动 drain 成 `done`（attempts=0）→ 审计 jsonl 追加 2 行（action=write，key 与写入一致）→ `audit_落链=true`
- E2E-3 预置一行 pending（模拟上次进程崩溃遗留）→ **运行中的 Hub 自动 replay 成 done 并落链**（`replay落链=true`）
- E2E-4 `DELETE /memory/{key}` 200 → delete 事件经 outbox 落链（action=delete）→ `memory_pool` 只剩未删的那条

## 四、kimi 主动上报项核对（验收时逐条证伪/证实）

1. **delete 审计原不在事务内** —— 证实（`git diff` 原文：原 `audit_memory` 在 `run_in_conn` 返回之后）。
   我任务书里「6 处均在事务内」写错了，kimi 的改动是**等价增强**（条件 `deleted>0 and old` 保持），采纳。
2. **T7 注释行连带改动** —— 证实，属同一处断言，采纳。
3. **`import db` 触发模块级 init_db，把仓库本地 `sync_hub.db` 迁到 v7** —— 证实且为预期；迁移前自动备份已落
   `backups/pre_migrate_20260917-184421.db`。生产库现 `user_version=7` 且 `event_outbox` 7 列齐备。

## 五、Hermes 验收期的两处修正

1. `tests/test_shadow_pending.py` 版本断言 6→7（见 §二）。
2. `hub_mixins/outbox.py` `_loop()`：原实现每轮固定 `wait(0.5s)`，吞吐上限 = `OUTBOX_BATCH/0.5s = 200 事件/秒`，
   高峰持续写入时事件表会越积越深 → 改为**有积压即连续 drain，空表才回到等待节奏**。

## 六、已知观察（不做改动，登记备查）

- 回归日志末行 `outbox drain 连接级异常: no such table: event_outbox`：来自**手写 schema 的测试库**
  （部分既有测试自建 DDL 而不走 `db.init_db`）。消费者按 D4 降级（记日志、丢连接重建、线程不死），
  不影响任何用例（731 全绿）。设计上这是**有意 fail-closed**：`enqueue` 在业务事务内、表缺失即写失败回滚
  ——数据与审计同生共死，不允许「数据落了审计没落」。
- 生产库不属此列（`db.py` 模块级 `init_db()` 保证 v7 迁移已落）。
