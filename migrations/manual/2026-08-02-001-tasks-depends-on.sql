-- 功能完整性轮 P1: tasks 表加 depends_on 列（JSON 数组，默认 '[]'）
-- 编号: 2026-08-02-001 日期: 2026-08-02 说明: 任务依赖 DAG 支持（D4 手动 SQL 存档）
-- 状态: 已执行（在 sync_hub.db 生产库）
ALTER TABLE tasks ADD COLUMN depends_on TEXT DEFAULT '[]';
