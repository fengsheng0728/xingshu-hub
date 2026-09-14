# P2 任务拆解并行验收表(2026-08-03)

> 方案：《星枢-团队协作型个人工作台-执行方案.md》P2
> 目标：1 个任务拆成 N 个子任务分派不同 Agent 并行推进, 子任务全完成后父任务可完成——从「任务列表」升级为「团队协作」
> 测试：`tests/test_task_decompose.py`（6 用例，含三 agent 并行 E2E）
> commit：`（待填）`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T2-1 反向断言 | EXPECTED_ROUTES: ("GET","/api/v1/tasks/{task_id}/subtasks") | ✅ 已注册 | test_t2_1 |
| T2-2 建父子 | create 父 + 3 子(parent_task_id) → subtasks 返回 3 + 聚合 0/3 | ✅ | test_t2_2 |
| T2-3 父完成门 | 子未全完成 → 父 complete 拒绝(含 pending_subtasks 缺失清单)；全完成 → 成功 | ✅ | test_t2_3 |
| T2-4 列表聚合 | GET /tasks 父任务带 subtask_summary，完成一个子任务后 1/2 更新 | ✅ | test_t2_4 |
| T2-5 防环 | parent 指向不存在 / 自指 → 拒绝 | ✅ | test_t2_5 |
| T2-6 三 agent 并行 E2E | 3 子任务分 3 agent 各自 start/complete → 中途父被拒 → 全完成后父 complete | ✅ | test_t2_6 |
| 回归 | Hub pytest 全量 | （待填） | 回归输出 |

## 实现要点

1. **schema**：tasks 加 `parent_task_id`（db.py DDL 同步 + `migrations/manual/2026-08-03-001-tasks-parent-task-id.sql` 增量迁移，模式同 depends_on）
2. **TaskCreate** 加 `parent_task_id: Optional[str]`；create_task 校验 parent 存在性 + 非自指（fail-closed）
3. **父完成门**（complete_task）：子任务未全部 completed → error + `pending_subtasks` 缺失清单（fail-closed，D3）
4. **聚合**：`GET /api/v1/tasks/{task_id}/subtasks`（子列表 + completed_count/total）+ GET /tasks 每任务附 subtask_summary（一次聚合查询，无 N+1）
5. **看板**（Agent app.js loadKanban）：父任务卡片「拆解 x/y」徽标（subtask_summary 非空），esc() 防 XSS

## 实测注意

- **schedule 匹配内存 dict 不可靠**（skill 已记录）：测试用直接 UPDATE 模拟分配，聚焦 P2 门逻辑本身；schedule 分配是既有功能不回归
- **生产库需跑增量迁移**：`ALTER TABLE tasks ADD COLUMN parent_task_id TEXT`（已执行）；全新库由 db.py DDL 覆盖

## 本阶段明确不做

- 不做子任务完成自动推进父任务（complete 时动态计算门，无常驻推进器——同 P1 DAG 设计）；不做子任务重排/拖拽；不做跨任务进度看板页
