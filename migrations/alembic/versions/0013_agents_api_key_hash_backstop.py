"""0013: agents hash 列兜底补列（终审断点 3，数据层修复轮后续）

背景：db.py init_db 的内联 agents DDL **有意不含** api_key_hash / api_key_prev_hash
（CD-060 登记差异：代码按 PRAGMA 列存在性自动切换明文/哈希模式，内联补齐会破坏
注册/重注册回执行为，全量回归实测 5 例红）。但 CD-082 起 init_db 对全新库
stamp alembic head —— 0003 的补列迁移因此被跳过，新部署永久停留在明文模式，
且 main.py run_startup_migration 报 up_to_date（误导）。

修法（本 revision = 兜底）：对缺列库补 api_key_hash / api_key_prev_hash 两列，
并按 0003 同口径回填 hash、清明文。db.py 侧配套：_stamp_alembic_head 对缺列的
全新库只登记到本 revision 的前一版，使启动迁移 / `alembic upgrade head`
必然执行本 revision（内联 DDL 本身不动，保持 CD-060 口径）。

与 0003 的关系：0003 是「正常迁移链上的哈希化」，本 revision 是「stamp 跳过
0003 的库」的兜底——动作同构（补列 / 回填 / 清明文），全部幂等守卫：
- PRAGMA table_info 探测列存在性，已有列跳过；
- hash 回填只动「明文非空且 hash 为空」的行；
- 清明文只动非空行。
已全量库（经 0003 正常迁移）upgrade 本 revision = no-op。

执行用 sqlite3 原生连接（driver_connection），同 0002/0003 口径（P2 坑①）。
"""
import hashlib

from alembic import context

revision = "0013_agents_api_key_hash_backstop"
down_revision = "0012_evaluation_tasks"
branch_labels = None
depends_on = None

HASH_COLUMNS = [
    ("api_key_hash", "TEXT"),
    ("api_key_prev_hash", "TEXT"),
]


def _conn():
    # P2 坑①：用 sqlite3 原生连接执行，不走 op.execute
    return context.get_context().connection.connection.driver_connection


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "agents" not in tables:
        return  # 表缺失（异常库）时跳过，不崩迁移
    cols = {r[1] for r in cur.execute("PRAGMA table_info(agents)")}
    for col_name, col_decl in HASH_COLUMNS:
        if col_name not in cols:
            cur.execute(f"ALTER TABLE agents ADD COLUMN {col_name} {col_decl}")
    # 存量回填：逐行 sha256（只动「明文非空且 hash 为空」的行，幂等；同 0003 口径）
    rows = cur.execute(
        "SELECT agent_id, api_key, api_key_prev, api_key_hash, api_key_prev_hash "
        "FROM agents"
    ).fetchall()
    for agent_id, api_key, api_key_prev, cur_hash, cur_prev_hash in rows:
        if api_key and not cur_hash:
            cur.execute(
                "UPDATE agents SET api_key_hash = ? WHERE agent_id = ?",
                (hashlib.sha256(api_key.encode("utf-8")).hexdigest(), agent_id))
        if api_key_prev and not cur_prev_hash:
            cur.execute(
                "UPDATE agents SET api_key_prev_hash = ? WHERE agent_id = ?",
                (hashlib.sha256(api_key_prev.encode("utf-8")).hexdigest(), agent_id))
    # 清明文：Hub 库不存明文 api_key（同 0003 口径）
    cur.execute("UPDATE agents SET api_key = '' WHERE api_key IS NOT NULL AND api_key != ''")
    cur.execute(
        "UPDATE agents SET api_key_prev = '' "
        "WHERE api_key_prev IS NOT NULL AND api_key_prev != ''")
    conn.commit()


def downgrade():
    # 同 0003：只删 hash 列，被清空的明文无法恢复
    conn = _conn()
    cur = conn.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "agents" not in tables:
        return
    cols = {r[1] for r in cur.execute("PRAGMA table_info(agents)")}
    for col_name, _decl in HASH_COLUMNS:
        if col_name in cols:
            cur.execute(f"ALTER TABLE agents DROP COLUMN {col_name}")
    conn.commit()
