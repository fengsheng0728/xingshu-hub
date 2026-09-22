"""0006: memory_pool_fts 存量 rebuild（CD-064，2026-09-20）

背景：memory_pool_fts 是 FTS5 外部内容表（content='memory_pool'），但全库
零触发器、写侧（hub_mixins/memory.py）只发 'delete' 命令（对空/未含该值的
索引实测抛 DatabaseError: database disk image is malformed）且被 except-pass
吞掉 → 索引从不被维护，memory_search 的 FTS 路径恒不命中，关键词检索实际
只靠 CD-057 的 LIKE 兜底。

本迁移只做一件事：对存量库执行一次 FTS5 'rebuild'（按 memory_pool 当前内容
全量重建索引）。不改表结构、不建/删触发器、不重建 memory_pool——写侧同步
由 hub_mixins/memory.py 的提交后副作用承担（CD-064 选型乙；不选触发器的
理由见该文件 store_memory 内注释：触发器会让 T16 锚点用例
tests/test_memory_keyword_fallback.py::test_k4a 的裸插断言失真）。

幂等：FTS5 'rebuild' 语义即先清空再全量重建，重复执行安全；
memory_pool_fts 缺失（异常库）时跳过不崩（同 0002/0005 形态）。
"""
from alembic import context

revision = "0006_memory_pool_fts_sync"
down_revision = "0005_kb_embedding_column"
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
    if "memory_pool_fts" not in tables:
        return  # 虚拟表缺失（异常库）时跳过，不崩迁移（同 0002/0005 形态）
    # FTS5 内部命令 'rebuild'：按外部内容表 memory_pool 全量重建索引（幂等）
    cur.execute("INSERT INTO memory_pool_fts(memory_pool_fts) VALUES('rebuild')")
    conn.commit()


def downgrade():
    # no-op：FTS 索引是 memory_pool 的派生数据，回滚无需动作
    # （重回 0005 后索引数据保留无副作用——写侧旧代码本就不维护它）
    pass
