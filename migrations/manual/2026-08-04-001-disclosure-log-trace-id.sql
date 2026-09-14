-- P1 O1 可观测性 - disclosure_log 加 trace_id 列（贯穿请求→规则判定→审计链路）
-- 模式同 depends_on 手动迁移; 全新库由 db.py DDL 覆盖
ALTER TABLE disclosure_log ADD COLUMN trace_id TEXT;
