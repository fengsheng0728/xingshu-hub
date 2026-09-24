"""0012: evaluation_tasks 评估结果表（CD-081 最小骨架，KB 评估框架）

背景：CD-081「KB 评估框架」的最小骨架——评估指标口径钉死三项
（recall@5 / 拒答率 / p95 延迟），评估结果**落库不存内存**，本表即落地层：
`tools/kb_eval.py` 跑完一组评估 → INSERT 一行（数据集 / 任务名 / 三项指标 /
样本数 / 配置 hash / 配置快照），供后续对比与回归门禁取数。
（评估入口是 CLI 工具，刻意不做 HTTP 端点，避开路由层。）

口径：
- 指标列 REAL 可空：dry-run / 中断等只登记任务不产出指标的场景允许 NULL；
- created_at 由 DB 默认 datetime('now') 落 UTC（与 dead_letters.failed_at 同款口径）；
- config_hash = 评估配置（provider/top_k/阈值/语料指纹）的 sha256 前 16 位，
  同 hash 即同口径可比；config_json 留完整快照便于复盘。

必须与 `db.py` init_db 内联 DDL 双侧同步（CD-060 硬等式门禁要求差异 = 0）。
幂等：CREATE TABLE/INDEX IF NOT EXISTS；downgrade 只丢本表与本索引。
"""
from alembic import context

revision = "0012_evaluation_tasks"
down_revision = "0011_dead_letters"
branch_labels = None
depends_on = None

# 与 db.py init_db 内联段逐字一致（规范化后须相等）
_DDL = """CREATE TABLE IF NOT EXISTS evaluation_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    recall_at_5 REAL,
    refusal_rate REAL,
    p95_ms REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)"""

_IDX = ("CREATE INDEX IF NOT EXISTS idx_evaluation_tasks_dataset"
        " ON evaluation_tasks(dataset, created_at)")


def _conn():
    # 同 0002/0005/0007/0008/0009/0010/0011 形态：sqlite3 原生连接
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
    cur.execute("DROP INDEX IF EXISTS idx_evaluation_tasks_dataset")
    cur.execute("DROP TABLE IF EXISTS evaluation_tasks")
    conn.commit()
