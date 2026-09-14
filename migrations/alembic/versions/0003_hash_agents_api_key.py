"""0003: agents.api_key 哈希化（T1-2，2026-09-09）

背景（审查 S3）：agents.api_key / api_key_prev 明文存储、明文查询——脱库（含备份）
即泄露全部 Agent 凭据。员工/scoped key 侧已对齐 SHA256 规范（employee_accounts.key_hash、
agent_keys.key_hash），本 revision 把 agents 表对齐同一规范。

动作：
1. agents 加 api_key_hash / api_key_prev_hash 两列（TEXT，SHA256 hexdigest）。
   新建列而非改写 api_key 列——改写会让「旧列无意义但还有代码读」更难审计。
2. 存量迁移：Python 逐行 sha256(api_key) → api_key_hash（sqlite 无 sha256 函数，
   量小可接受）；api_key_prev 同理 → api_key_prev_hash。
3. 清明文：api_key / api_key_prev 置 ''。
   2026-09-09 T1-2 起 Hub 库不存明文 api_key——明文只在签发时刻回显一次
   （register 首注响应 / hub-cli agent create stdout），重引导/重注册不再回吐。

兼容窗口：代码侧（auth_provider._lookup_agent / routes_agents._check_reregister_credential /
routes_common._authenticate / hub_core.register / hub_cli agent create）按 PRAGMA 检测
hash 列是否存在，未迁移老库降级旧明文行为；已迁移库跑旧代码会因明文列清空而认证失效
——部署顺序：先升代码，再跑本迁移。

幂等：PRAGMA table_info 检测列存在性；hash 回填只动「明文非空且 hash 为空」的行，
清明文只动非空行——重复执行安全。

不可逆：downgrade 只删 hash 列，被清空的明文无法恢复（这是本迁移的目的）。

执行用 sqlite3 原生连接（driver_connection），同 0002 口径（P2 坑①）。
"""
import hashlib

from alembic import context

revision = "0003_hash_agents_api_key"
down_revision = "0002_freeze_incremental_alters"
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
    # 存量迁移：逐行 sha256（只动「明文非空且 hash 为空」的行，幂等）
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
    # 清明文：2026-09-09 T1-2 起 Hub 库不存明文 api_key
    cur.execute("UPDATE agents SET api_key = '' WHERE api_key IS NOT NULL AND api_key != ''")
    cur.execute(
        "UPDATE agents SET api_key_prev = '' "
        "WHERE api_key_prev IS NOT NULL AND api_key_prev != ''")
    conn.commit()


def downgrade():
    # 不可逆：明文已被清空，无法恢复；仅删除 hash 列
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
