"""0005: knowledge_base.embedding 补列（CD-055，2026-09-19）

0001_baseline 的 knowledge_base 带 `embedding BLOB`（历史运行时 ALTER 冻结进基线），
但 db.py 内联 CREATE 长期缺该列——未跑迁移的新库上 wiki_sync 向量写回必然失败
（UPDATE knowledge_base SET embedding = ? → no such column），轨 B 向量段静默失效。

幂等：PRAGMA table_info 查列集合，缺才 ADD COLUMN（sqlite 无 ADD COLUMN IF NOT EXISTS；
沿用 0002 的 P2 坑①结论，用 driver_connection 原生连接，不走 op.execute 参数化）。
对「已有该列的现库」与「缺该列的老库」均可跑通、不抛错。
"""
from alembic import context

revision = "0005_kb_embedding_column"
down_revision = "0004_event_outbox"
branch_labels = None
depends_on = None


def _conn():
    # P2 坑①：用 sqlite3 原生连接执行 DDL，不走 op.execute
    return context.get_context().connection.connection.driver_connection


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "knowledge_base" not in tables:
        return  # 表缺失（异常库）时跳过，不崩迁移（同 0002 形态）
    existing = {r[1] for r in cur.execute("PRAGMA table_info(knowledge_base)")}
    if "embedding" not in existing:
        cur.execute("ALTER TABLE knowledge_base ADD COLUMN embedding BLOB")
    conn.commit()


def downgrade():
    # no-op：sqlite 删列需整表重建（3.35 前的旧 sqlite 无 DROP COLUMN），代价/风险
    # 远高于收益；且 0001 基线本身已含 embedding，回滚保留该列与基线方向一致。
    pass
