"""0011: dead_letters 死信表（CD-084，2026-09-23 运维轮后续）

背景：CD-084「缺死信与告警落地」的可做半边——仓内多条「失败只打日志」路径
（自动化任务连续失败停用、通知发送失败、维护清理/对账异常等）此前没有兜底账本，
失败后除了日志无处可查、无法重试。本表为死信落地层：
`db.record_dead_letter()` 追加行 → `GET /api/v1/maintenance/dead-letters` 面板列出
→ `POST .../dead-letters/{id}/retry` 按 source 的可行最小语义重试或标记。
（告警最后一公里另一半=通知渠道巡检，仍卡 CD-018 凭据，不在本迁移。）

口径：
- 落库自身失败绝不反过来影响主流程（record_dead_letter 内部 try/except 兜底）；
- failed_at 由 DB 默认 datetime('now') 落 UTC（与 events.created_at 等同款口径）；
- retried 0/1 + retried_at 标记重试结果，不做多次重试计数（最小语义）。

必须与 `db.py` init_db 内联 DDL 双侧同步（CD-060 硬等式门禁要求差异 = 0）。
幂等：CREATE TABLE/INDEX IF NOT EXISTS；downgrade 只丢本表与本索引。
"""
from alembic import context

revision = "0011_dead_letters"
down_revision = "0010_employee_keys"
branch_labels = None
depends_on = None

# 与 db.py init_db 内联段逐字一致（规范化后须相等）
_DDL = """CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    failed_at TEXT DEFAULT (datetime('now')),
    retried INTEGER NOT NULL DEFAULT 0,
    retried_at TEXT NOT NULL DEFAULT ''
)"""

_IDX = ("CREATE INDEX IF NOT EXISTS idx_dead_letters_retried"
        " ON dead_letters(retried, failed_at)")


def _conn():
    # 同 0002/0005/0007/0008/0009/0010 形态：sqlite3 原生连接
    return context.get_context().connection.connection.driver_connection


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    cur.execute(_DDL)
    cur.execute(_IDX)
    conn.commit()


def downgrade():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_dead_letters_retried")
    cur.execute("DROP TABLE IF EXISTS dead_letters")
    conn.commit()
