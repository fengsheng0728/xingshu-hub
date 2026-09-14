# 星枢自动化改进计划

> 2026-07-31 | 依据：GitHub 同类项目调研 + 铁律

## 现状问题

1. **两套调度系统分散**：automation_jobs（routes_automation.py）+ cron_jobs（hub_core.py），两张表、两套 API
2. **cron_jobs 没有 tick loop**：list_cron_jobs/create_cron_job 存在但无自动调度器
3. **不支持 cron 表达式**：只有 interval 秒数
4. **缺少 heartbeat 模式**：不能定时读文件让 LLM 判断是否行动
5. **LLM 做确定性工作**：自动化结果解析、文件提取等可用 Python 做的也丢给了 Agent LLM

## 目标

合并为统一 SchedulerService，支持 3 种触发 + 3 种调度，一个 tick loop，一张表。

## 改动清单

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| A | hub_core.py | 新增 `SchedulerService` 类 | 统一调度引擎：tick loop + 3种调度 + dispatch |
| B | hub_core.py | 删 `list_cron_jobs/create_cron_job/delete_cron_job` | 被 SchedulerService 替代 |
| C | routes.py | 新增 `/api/v1/scheduler/*` 8端点 | 统一 REST API |
| D | routes.py | 删旧 cron API 注册 | `/api/v1/cron/*` 废弃 |
| E | routes_automation.py | 重构调度器调用 | 替换 `automation_scheduler` 为新 SchedulerService |
| F | db.py | 新增 `scheduled_jobs` 表 | 合并 automation_jobs + cron_jobs schema |
| G | tests/ | 新增 test_scheduler.py | 覆盖 3 种触发 + heartbeat + 熔断 |

## SchedulerService 设计

```
SchedulerService
├── Tick Loop (1s tick，原来是30s)
│   ├── 检查 schedule="at" → 到期则 fire + delete_after_run
│   ├── 检查 schedule="every" → 到期则 fire + 更新 next_run
│   └── 检查 schedule="cron" → croniter 计算 → 到期则 fire
├── Trigger 类型
│   ├── schedule (at/every/cron) — tick loop 驱动
│   ├── event — 外部事件驱动（已有 _dispatch_event_automation）
│   └── manual — API 手动触发
├── Payload 类型
│   ├── instruction — 发给 Agent LLM 执行
│   ├── heartbeat — 读文件 → LLM 判断 → 决定才 dispatch
│   └── webhook — 调外部 URL
├── 安全保障（继承现有）
│   ├── 熔断：连续 5 次失败 → auto-disable
│   ├── 防重复：dispatch_id 30s 缓存
│   ├── 补跑：Agent offline → missed_runs++
│   └── 断环：source=automation 默认不过滤（allow_auto_source 控制）
└── 持久化
    └── SQLite scheduled_jobs 表（一个表替代两个旧表）
```

## scheduled_jobs 表

```sql
CREATE TABLE scheduled_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    -- 触发
    trigger_type TEXT NOT NULL,  -- 'schedule' | 'event' | 'manual'
    schedule_kind TEXT,          -- 'at' | 'every' | 'cron' (仅 schedule)
    schedule_spec TEXT,          -- at:ISO时间 / every:秒数 / cron:表达式
    event_filter TEXT,           -- JSON event 过滤条件 (仅 event)
    -- 载荷
    payload_type TEXT NOT NULL,  -- 'instruction' | 'heartbeat' | 'webhook'
    payload_data TEXT NOT NULL,  -- JSON: {instruction, heartbeat_file, webhook_url, ...}
    -- 交付
    delivery TEXT DEFAULT '["notification"]', -- JSON 数组
    guardrail TEXT DEFAULT '{}', -- JSON
    -- 状态
    enabled INTEGER DEFAULT 1,
    owner_agent_id TEXT NOT NULL,
    next_run_at TEXT,            -- ISO 时间
    last_run_at TEXT,
    last_status TEXT DEFAULT 'pending',
    last_result_summary TEXT,
    last_run_duration_ms INTEGER,
    run_count INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    missed_runs INTEGER DEFAULT 0,
    delete_after_run INTEGER DEFAULT 0, -- 一次性任务
    allow_auto_source INTEGER DEFAULT 0, -- 允许自动化源
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

## REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/v1/scheduler/jobs | 列出所有任务 |
| POST | /api/v1/scheduler/jobs | 创建任务 |
| GET | /api/v1/scheduler/jobs/{id} | 查看任务详情 |
| PUT | /api/v1/scheduler/jobs/{id} | 更新任务 |
| DELETE | /api/v1/scheduler/jobs/{id} | 删除任务 |
| POST | /api/v1/scheduler/jobs/{id}/toggle | 启用/禁用 |
| POST | /api/v1/scheduler/jobs/{id}/run | 手动触发 |
| GET | /api/v1/scheduler/jobs/{id}/runs | 运行历史 |

## 向后兼容

- 旧 `/api/v1/automation/*` 端点保留但不推荐使用，内部转发到 scheduler
- 旧 `/api/v1/cron/*` 端点标记 deprecated
- automation_jobs 表数据迁移到 scheduled_jobs（一次性 migration）

## 验证项（过关清单）

- [ ] 创建 schedule:every 任务 → tick loop 自动 fire
- [ ] 创建 schedule:cron 任务 → croniter 正确计算
- [ ] 创建 event 任务 → 事件到达时触发
- [ ] heartbeat 任务 → 读 TASK.md → LLM 判断
- [ ] 连续 5 次失败 → auto-disable + 通知
- [ ] Agent offline → missed_runs++
- [ ] 补跑 / 跳过 / 手动触发
- [ ] 旧 automation API 兼容
- [ ] pytest 全量回归
