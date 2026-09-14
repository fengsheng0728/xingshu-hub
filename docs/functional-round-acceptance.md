# 功能完整性与稳定性轮 E2E 总验收表

> 方案：《星枢-功能完整性与稳定性轮-执行方案.md》（300 分钟预算）
> 顺序：P0 → P1 → P2 → E2E，每阶段独立 commit，回归门槛 Hub ≥166 / Agent ≥158

## 验收总表

| 阶段 | 用例 | 方法 | 结果 | commit | 证据 |
|------|------|------|------|--------|------|
| P0 | T0-1 版本上限 | pytest 连写 15 次 | ✅ 恰 10 快照 + 最旧淘汰 + 内容逐版可辨 | 8baf65a（Agent） | tests/test_file_versions.py |
| P0 | T0-2 覆盖即快照 | pytest 参数化 3 路径 | ✅ write/create/move 三路径旧内容入快照 | 8baf65a | 同上 |
| P0 | T0-3 恢复真实链路 | 工具函数实操 | ✅ 写→覆盖→list→restore→sha256 逐字节一致；restore 前内容已快照 | 8baf65a | t03_real.py |
| P0 | T0-4 大文件跳过 | 50MB+1 | ✅ 无快照 + 日志跳过 + 写入正常 | 8baf65a | test_t04 |
| P0 | T0-5 回归 | pytest Agent | ✅ 163（158+5） | — | — |
| P1 | T1-1 环检测 | pytest | ✅ 环拒绝指明路径 / 自依赖 400 / 依赖不存在 400 | 650aef3（Hub） | tests/test_task_dag.py |
| P1 | T1-2 依赖门 | pytest | ✅ pending/failed/cancelled 拒绝（fail-closed）+ complete 后成功 | 650aef3 | 同上 |
| P1 | T1-3 自动解除 | HTTP 真实端点 | ✅ B start 拒→complete A→B start 成功；blocked_by [A]→[]（7/7） | 650aef3 | t13_real.py |
| P1 | T1-4 看板真实路径 | 数据链路 | ✅ GET /tasks 返回 blocked_by；app.js 徽标+esc 防 XSS；UI 视觉复核待用户 | 650aef3 | 端点实测 |
| P1 | T1-5 向后兼容 | pytest | ✅ 旧任务全流程不变 | 650aef3 | test_t15 |
| P1 | T1-6 回归 | pytest 双端 | ✅ Hub 174（166+8）/ Agent 163 | — | — |
| P2 | T2-1 硬等式 | 空库 upgrade + 对比 | ✅ **差异 = 0**（24 对象逐项一致） | 9a51e51（Hub） | t21_hard_eq.py |
| P2 | T2-2 回环 | downgrade/upgrade | ✅ 两次 head 结构一致 | 9a51e51 | t22_loop.py |
| P2 | T2-3 现库无损 | stamp + 行数 | ✅ 8 表零变化 + /health ok | 9a51e51 | _t23 |
| P2 | T2-4 依赖证据 | pip freeze diff | ✅ 仅新增 alembic+Mako | 9a51e51 | pip diff |
| P2 | T2-5 回归 | pytest | ✅ Hub 174 | — | — |
| E2E | E2E-1 全量回归 | 双端 pytest | ✅ Hub 174 / Agent 163 | — | — |
| E2E | E2E-2 全链路 | 真实客户端 | ✅ 8/8：建链→拒→解除→blocked_by 清空 + 文件版本 restore 逐字节 + 通知落库 | — | e2e2_full2.py |
| E2E | E2E-3 硬等式复验 | 空库 upgrade | ✅ 差异 = 0 | — | t21 复跑 |
| E2E | E2E-4 断网复跑 | S5 30 轮 | ✅ 30/30 全绿 + 通知恢复后可达 | — | /tmp/e2e4c.log |

## 收尾交付物

1. **验收总表**：本文件（20/20 已绿）
2. **carried_debts 台账滚动**：
   - 旧债：T3-3 双机 / kanban 命名 / 子代理 / exe / api_key 分工 / CD-018 真实渠道联调 / langchain warning —— 本轮不动，保持登记
   - 新登记：看板 UI 视觉确认待用户复核（T1-4）
3. **进度文档更新**：6.3 文件版本/任务依赖勾选；6.4 Alembic 勾选；第十节测试数 174/163；commit 链入第八节
4. **时间消耗表**：见下

## 时间消耗表（实际 vs 预算）

| 阶段 | 预算 | 实际 | 说明 |
|------|------|------|------|
| 前置 | 15 min | ~20 min | 现状确认表 5 锚点 |
| P0 | 75 min | ~90 min | 快照模块 + staging 关系探明（create_file/move_file 绕过 staging 发现） |
| P1 | 95 min | ~150 min | 环检测 DFS + 依赖门 + blocked_by + GET /tasks 新端点（顺带修复）+ 看板 |
| P2 | 70 min | ~110 min | op.execute 误解析 sqlite DDL + FTS5 影子表冲突两个坑 + 自包含化 |
| E2E | 25 min | ~40 min | 全链路 8/8 + 硬等式复验 + 断网 |
| 机动 | 20 min | — | 超预算消耗在真实问题深度（非低效） |

**总评**：P0/P1/P2 通过条件全部达成。超预算根因：P1 发现 GET /tasks 端点缺失（顺带修复）、P2 两个 SQLite DDL 执行坑（已写入文档防复发）。无砍尾。
