"""0007: team_members.team_id 存量补列（CD-060 内联 DDL 对齐，2026-09-20）

背景：0001 基线的 team_members 含 `team_id INTEGER REFERENCES teams(id)`
（历史运行时 ALTER 冻结进基线），但 db.py 内联 CREATE 长期缺该列——未跑
alembic 的新库缺列（与 CD-030/CD-055 同族）。T27 已把内联 DDL 补齐并对齐
列序（新库生效）；本 revision 只为**存量库**补这一列。

口径（用户 2026-09-20 冻结）：严禁重建表/改列序——存量库按列名访问不受
列序影响，故只做最小幂等 ALTER ADD COLUMN（追加在末位，不与新库列序一致
属预期，已在任务书冻结口径中声明「存量库不动」）。

幂等：PRAGMA table_info 查列集合，缺才 ADD COLUMN（sqlite 无 ADD COLUMN
IF NOT EXISTS；沿用 0002 的 P2 坑①结论，用 driver_connection 原生连接，
不走 op.execute 参数化）。表缺失（异常库）时跳过不崩（同 0002/0005 形态）。
"""
from alembic import context

revision = "0007_inline_ddl_align"
down_revision = "0006_memory_pool_fts_sync"
branch_labels = None
depends_on = None


def _conn():
    # P2 坑①：用 sqlite3 原生连接执行，不走 op.execute 参数化
    return context.get_context().connection.connection.driver_connection


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "team_members" not in tables:
        return  # 表缺失（异常库）时跳过，不崩迁移（同 0002/0005 形态）
    cols = {r[1] for r in cur.execute("PRAGMA table_info(team_members)")}
    if "team_id" not in cols:
        cur.execute(
            "ALTER TABLE team_members ADD COLUMN team_id INTEGER REFERENCES teams(id)")
    conn.commit()


def downgrade():
    # no-op：0001 基线本身已含 team_id，回滚保留该列与基线方向一致（同 0005 口径）；
    # 且 sqlite 删列需整表重建，代价/风险远高于收益。
    pass
