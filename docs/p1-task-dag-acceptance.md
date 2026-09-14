# 功能完整性轮 P1 验收表 — 任务依赖 DAG

> 方案：《星枢-功能完整性与稳定性轮-执行方案.md》P1（95 分钟预算）
> 目标：tasks 支持 depends_on；依赖未完成不得 start（fail-closed D3）；环被拒；看板阻塞徽标
> 测试：Hub `tests/test_task_dag.py`（8 用例）+ 真实链路 t13_real.py（7 项）
> commit：`（待填）`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T1-1 环检测 | pytest | ✅ A→B→C→A 环 update 拒绝（指明路径 C→A）；自依赖 400；依赖不存在 400 | test_t11_* |
| T1-2 依赖门 | pytest | ✅ pending 依赖 start 拒 + 缺失清单；failed 拒（fail-closed）；cancelled 拒（fail-closed）；complete 后 start 成功 | test_t12_* |
| T1-3 自动解除 | HTTP 真实端点 | ✅ B start 被拒（缺失清单含 A）→ complete A → B start 成功；blocked_by [A]→[] | t13_real.py 7/7 |
| T1-4 看板真实路径 | 数据链路验证 | ✅ GET /api/v1/tasks 返回 blocked_by（KB-B=['KB-A']）；app.js 徽标渲染 + esc 防 XSS；UI 视觉确认待复核 | 端点实测 + app.js diff |
| T1-5 向后兼容 | pytest | ✅ 无 depends_on 旧任务创建/启动/完成全流程不变 | test_t15 |
| T1-6 回归 | pytest 双端 | ✅ Hub 174（166+8）/ Agent 163 | 回归输出 |

## 实现要点

1. **schema**：tasks 表加 `depends_on TEXT DEFAULT '[]'`（手动 SQL 已执行 + `migrations/manual/2026-08-02-001-tasks-depends-on.sql` 存档，D4）
2. **`_validate_dependencies`**（hub_core）：自依赖 / 依赖存在性 / DFS 环检测（create 与 update 共用），非法返回 error + 环路径
3. **依赖门**：start_task 里 `_validate_transition` 后查 depends_on，`_blocked_by` 算出未完成依赖 → 拒绝 + `blocked_by` 清单（D3 fail-closed：cancelled/failed 视为未完成）
4. **blocked_by 计算字段**：GET /api/v1/tasks（**新端点**，Agent task_list 一直调它但此前不存在——顺带修复）+ dashboard stats task_list
5. **TaskCreate.depends_on** + update 端点可选 depends_on（JSON 数组字符串）
6. **看板**：app.js 卡片 meta 区阻塞徽标（`⛔ 等待: <来源>` + title tooltip，esc 防 XSS）；新建任务表单加依赖输入（逗号分隔）；_task_create 透传 depends_on

## 实测抓出的关键事实

1. **GET /api/v1/tasks 此前不存在**：Agent `_task_list` 一直调空端点返回空（看板 fallback 源是 get_my_info）。P1 补端点顺带修复
2. **schedule 匹配用 hub.agents 内存 dict**：register API 只写 DB 不更新内存 → 新注册 agent 无法被 schedule 匹配。真实链路脚本改为手动置 assigned（schedule 分配非本阶段验收对象）

## 本阶段明确不做（已遵守）

- 不做 force_start / 人工跳过依赖；不做依赖图可视化；不做跨 Hub 依赖；不加 blocked 状态（D3）
