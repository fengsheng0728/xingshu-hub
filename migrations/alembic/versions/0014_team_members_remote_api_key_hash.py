"""0014: team_members.remote_api_key 哈希化（CD-111，2026-09-24）

背景（终审 L-2）：联邦配对凭据 team_members.remote_api_key 明文落库——脱库（含备份）
即泄露全部配对凭据。同仓 agents 表已按同一规范哈希化（0003），本 revision 把
team_members 对齐。

动作：
1. team_members 加 remote_api_key_hash 一列（TEXT，SHA256 hexdigest）。
2. 存量迁移：逐行 sha256(remote_api_key) → remote_api_key_hash
   （只动「明文非空且 hash 为空」的行，幂等）。
3. **不清明文**（与 0003 的差异）：读侧兼容分支写的是
   `remote_api_key = ? OR remote_api_key_hash = ?`，清空明文会破坏存量行在
   未升级代码的对端 Hub 上的兼容；清明文属另一次决策（见任务书 2.5）。

兼容窗口：读侧 routes_federation._is_paired_member_key() 已按 PRAGMA 检测
remote_api_key_hash 列是否存在，存在则走 hash 比对（OR 兼容存量明文行）。
本迁移只需加列 + 写 hash，读侧零改动自动生效。

幂等：PRAGMA table_info 检测列存在性；hash 回填只动「明文非空且 hash 为空」的行
——重复执行安全。

不可逆说明：downgrade 只删 hash 列。hash 回填不恢复（sha256 单向）；但明文列
本迁移未清空，故可重跑 upgrade 重建 hash。

执行用 sqlite3 原生连接（driver_connection），同 0002/0003 口径（P2 坑①）。
"""
import hashlib

from alembic import context

revision = "0014_team_members_remote_api_key_hash"
down_revision = "0013_agents_api_key_hash_backstop"
branch_labels = None
depends_on = None

HASH_COLUMNS = [
    ("remote_api_key_hash", "TEXT"),
]


def _conn():
    # P2 坑①：用 sqlite3 原生连接执行，不走 op.execute
    return context.get_context().connection.connection.driver_connection


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "team_members" not in tables:
        return  # 表缺失（异常库）时跳过，不崩迁移
    cols = {r[1] for r in cur.execute("PRAGMA table_info(team_members)")}
    for col_name, col_decl in HASH_COLUMNS:
        if col_name not in cols:
            cur.execute(f"ALTER TABLE team_members ADD COLUMN {col_name} {col_decl}")
    # 存量回填：逐行 sha256（只动「明文非空且 hash 为空」的行，幂等）
    rows = cur.execute(
        "SELECT id, remote_api_key, remote_api_key_hash FROM team_members"
    ).fetchall()
    for row_id, api_key, cur_hash in rows:
        if api_key and not cur_hash:
            cur.execute(
                "UPDATE team_members SET remote_api_key_hash = ? WHERE id = ?",
                (hashlib.sha256(api_key.encode("utf-8")).hexdigest(), row_id))
    conn.commit()


def downgrade():
    # 不可逆：hash 回填不恢复；明文列本迁移未清空，故可重跑 upgrade 重建 hash
    conn = _conn()
    cur = conn.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "team_members" not in tables:
        return
    cols = {r[1] for r in cur.execute("PRAGMA table_info(team_members)")}
    for col_name, _decl in HASH_COLUMNS:
        if col_name in cols:
            cur.execute(f"ALTER TABLE team_members DROP COLUMN {col_name}")
    conn.commit()
