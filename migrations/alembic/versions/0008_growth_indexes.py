"""0008: 增长型表索引（CD-024，2026-09-20）

背景：`docs/event-loop-blockers.md`（2026-09-02）把 async 链上的同步 sqlite 归为 B 类
（473 处 / 30+ 文件，非热点）**文档化不修**。用户 2026-09-20 拍板选项 A：**只给增长型表
补索引 + 确认慢查询护栏**（明确不做 473 处 to_thread 化）。

慢查询护栏**已由 D-10 门面底座落地**（`db_facade._record`：阈值 `CONFIG.DB_SLOW_QUERY_MS`
默认 200ms / `database.slow_query_ms` 可覆盖 → WARNING + `slow_calls`/`_slow_top` 计数），
本迁移不涉及护栏，只补索引。

索引口径：只给「随业务增长」的表加，且每条都有代码里实际存在的查询模式支撑
（改动前逐条 `EXPLAIN QUERY PLAN` 实测为 SCAN）。**必须与 db.py 内联 DDL 双侧同步** ——
CD-060 的硬等式门禁（`tests/test_schema_hard_equality.py`，比对含索引集合与规范化 DDL）
要求两侧差异 = 0。

幂等：`CREATE INDEX IF NOT EXISTS`；downgrade 只 DROP 本迁移新增的索引（同样幂等）。
"""
from alembic import context

revision = "0008_growth_indexes"
down_revision = "0007_inline_ddl_align"
branch_labels = None
depends_on = None

# (索引名, 建索引 SQL)——与 db.py init_db 内联段逐字一致
_INDEXES = [
    ("idx_memory_pool_owner_key",
     "CREATE INDEX IF NOT EXISTS idx_memory_pool_owner_key ON memory_pool(owner_agent_id, memory_key)"),
    ("idx_memory_pool_level",
     "CREATE INDEX IF NOT EXISTS idx_memory_pool_level ON memory_pool(disclosure_level)"),
    ("idx_memory_pool_updated",
     "CREATE INDEX IF NOT EXISTS idx_memory_pool_updated ON memory_pool(updated_at)"),
    ("idx_document_chunks_parent",
     "CREATE INDEX IF NOT EXISTS idx_document_chunks_parent ON document_chunks(parent_doc_id, piece_index)"),
    ("idx_document_chunks_level",
     "CREATE INDEX IF NOT EXISTS idx_document_chunks_level ON document_chunks(disclosure_level)"),
    ("idx_gateway_read_log_created",
     "CREATE INDEX IF NOT EXISTS idx_gateway_read_log_created ON gateway_read_log(created_at)"),
    ("idx_events_timestamp",
     "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)"),
    ("idx_events_agent",
     "CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, timestamp)"),
    ("idx_wiki_inbox_status",
     "CREATE INDEX IF NOT EXISTS idx_wiki_inbox_status ON wiki_inbox(status, created_at)"),
]


def _conn():
    # P2 坑①：用 sqlite3 原生连接执行，不走 op.execute 参数化（同 0002/0005/0007 形态）
    return context.get_context().connection.connection.driver_connection


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for _name, sql in _INDEXES:
        target = sql.split(" ON ")[1].split("(")[0].strip()
        if target not in tables:
            continue  # 表缺失（异常库）时跳过，不崩迁移（同 0007 形态）
        cur.execute(sql)
    conn.commit()


def downgrade():
    # 只回退本迁移新增的索引（幂等）
    conn = _conn()
    cur = conn.cursor()
    for name, _sql in _INDEXES:
        cur.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()
