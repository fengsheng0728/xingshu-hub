"""0004: event_outbox 审计事件表（CD-045 审计原子性 outbox 同事务，2026-09-17）

事务内只写 event_outbox 事件行（随业务数据原子提交），后台消费者顺序消费 →
落审计链；失败 attempts+1 留 pending，达上限标 failed，重启自动 replay。
事件表为通用事件日志（带 event_type），后续影子镜像事件（CD-047）复用同表。

DDL 与 db.py v7 conn7 段逐字一致。执行用 sqlite3 原生连接
（driver_connection）：DDL 含 DEFAULT (datetime('now','localtime')) 表达式，
op.execute 的 SQLAlchemy 参数化会误解析（沿用 0002 的 P2 坑①结论）。
建前查 sqlite_master，存在则跳过（幂等）。
"""
from alembic import context

revision = "0004_event_outbox"
down_revision = "0003_hash_agents_api_key"
branch_labels = None
depends_on = None

TABLE_SQL = """CREATE TABLE event_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
)"""

INDEX_SQL = ("CREATE INDEX idx_event_outbox_status "
             "ON event_outbox(status, attempts)")


def _conn():
    # P2 坑①：用 sqlite3 原生连接执行 DDL，不走 op.execute
    return context.get_context().connection.connection.driver_connection


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "event_outbox" not in tables:
        cur.execute(TABLE_SQL)
    existing_idx = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    if "idx_event_outbox_status" not in existing_idx:
        cur.execute(INDEX_SQL)
    conn.commit()


def downgrade():
    conn = _conn()
    cur = conn.cursor()
    existing_idx = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    if "idx_event_outbox_status" in existing_idx:
        cur.execute('DROP INDEX IF EXISTS "idx_event_outbox_status"')
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "event_outbox" in tables:
        cur.execute('DROP TABLE IF EXISTS "event_outbox"')
    conn.commit()
