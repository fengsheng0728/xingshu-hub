# P1 团队仪表盘验收表 — 老板视图(2026-08-03)

> 方案：《星枢-团队协作型个人工作台-执行方案.md》P1
> 目标：dashboard `/team` 页——Agent 在线状态/任务分布/记忆统计/自动化状态, B2B 卖客户时老板看这一页
> 测试：`tests/test_team_dashboard.py`（6 用例）+ auth_matrix 同步 + Edge headless/CDP 真实数据渲染
> commit：`（待填）`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T1-1 反向断言 | EXPECTED_ROUTES: ("GET","/api/v1/team/stats") + ("GET","/team") | ✅ 已注册 | test_t1_1_* |
| T1-2 stats 数据一致性 | register 双 agent + 任务三状态 + 记忆 + 自动化 | ✅ 相对增量断言全过(防生产 DB 污染) | test_t1_2 |
| T1-3 页面壳 | /team 200 + auth.js 注入 + 免 CDN + 关键区块 | ✅ | test_t1_3 |
| T1-3b 状态透传 | 内存标 offline → stats 反映 | ✅ | test_t1_3b |
| T1-4 鉴权 | 独立鉴权 Hub 无 token → 401 | ✅ | test_t1_4 |
| 真实数据渲染 | Edge headless + CDP 设 localStorage → reload → DOM 文本 | ✅ 2 agents/3 任务三状态/1 记忆 fact/1 自动化 与 DB 一致 | cdp_team_check.py |
| 鉴权矩阵 | test_auth_matrix 全量(ALLOWLIST 同步 /team) | ✅ 5/5 | auth_matrix |
| 回归 | Hub pytest 全量 | （待填） | 回归输出 |

## 实现要点

1. **聚合端点 `GET /api/v1/team/stats`**（routes.py，挂 get_current_agent）：agents 从 `hub.agents` 内存（在线/角色/心跳/部门）+ tasks `by_status`/`by_agent` + memory `by_kind` + automation `enabled/disabled/by_last_status`，单连接多次查询
2. **`/team` 页**（dashboard/team.html，新）：GitHub 深色风格 + auth.js 注入 + API Key 输入（localStorage 持久化）+ 4 卡片（Agent 在线/任务分布/记忆池/自动化）+ 全部动态内容过 `esc()` 防 XSS + 零 CDN
3. **导航**：index.html 头部加「👥 团队」链接；allowlist 加 "/team"（页面壳免认证，数据端点仍 401 保护）

## 实测抓出的既有 bug（顺带修复，全新库部署必踩）

**db.py DDL 滞后两处**——生产库靠手动 ALTER 补过列，全新库建表缺列直接 500：
- `tasks` 缺 `depends_on`（P1 DAG 轮手动 SQL 加的）→ create_task 500
- `automation_jobs` 缺 `schedule_kind/payload_type/heartbeat_file/delete_after_run` → 创建自动化 500
- 修复：两处 DDL 补列。**教训：手动 SQL 加列必须同步回 db.py 建表 DDL，否则新部署/独立测试 Hub 必炸**

## 本阶段明确不做

- 不做 Agent 级权限隔离（stats 是团队级视图，任何已认证 agent 可看）；不做实时刷新（手动刷新按钮）；不做折线图/趋势（静态统计卡片）
