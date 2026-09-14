-- P2: 任务拆解并行 - tasks 加 parent_task_id(子任务归属父任务)
-- 模式同 depends_on 手动迁移; 全新库由 db.py DDL 覆盖
ALTER TABLE tasks ADD COLUMN parent_task_id TEXT;
