"""0010: employee_keys 员工凭据账本（CD-072，2026-09-20）

背景：阶段 1e 的员工凭据是 `employee_accounts.key_hash` 单列 —— 每人一把、补签即覆盖、
无 key_id/过期/调用画像。控制台身份供给页要能回答「这把钥匙谁在用、何时用过、单把吊销」，
故升级为账本（形态对齐 `agent_keys` / `key_scopes.py`）。

口径：
- 明文不落库（SHA256）；明文仅签发时返回一次；
- `employee_accounts.key_hash` **保留**为「最近一把」镜像（双写）→ 老代码与回滚路径仍可用；
- 认证侧先查账本，未命中回落 legacy 列（覆盖回填未跑的库与测试直插行）；
- **回填**：存量 `key_hash != ''` 的员工 → 账本一行（沿用原 hash，老 key 原样继续可用），
  幂等（同 hash 已在账本则跳过），`label` 标注为「迁移前签发」。

必须与 `db.py` init_db 内联 DDL 双侧同步（CD-060 硬等式门禁要求差异 = 0）。
幂等：CREATE TABLE IF NOT EXISTS；downgrade 只丢本表（回滚后最近一把仍经 legacy 列可用）。
"""
import secrets

from alembic import context

revision = "0010_employee_keys"
down_revision = "0009_departments"
branch_labels = None
depends_on = None

# 与 db.py init_db 内联段逐字一致（规范化后须相等）
_DDL = """CREATE TABLE IF NOT EXISTS employee_keys (
        key_id TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        label TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        expires_at TEXT DEFAULT '',
        last_used_at TEXT DEFAULT '',
        call_count INTEGER DEFAULT 0
    )"""


def _conn():
    # 同 0002/0005/0007/0008/0009 形态：sqlite3 原生连接
    return context.get_context().connection.connection.driver_connection


def _backfill(cur) -> int:
    """存量员工凭据回填（幂等）。返回新增条数。"""
    try:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(employee_accounts)").fetchall()}
    except Exception:
        return 0
    if "key_hash" not in cols:
        return 0
    rows = cur.execute(
        "SELECT employee_id, key_hash, created_at FROM employee_accounts"
        " WHERE COALESCE(key_hash, '') != ''").fetchall()
    n = 0
    for emp_id, h, created in rows:
        dup = cur.execute("SELECT 1 FROM employee_keys WHERE key_hash = ?", (h,)).fetchone()
        if dup:
            continue
        cur.execute(
            "INSERT INTO employee_keys (key_id, employee_id, key_hash, label, status,"
            " created_by, created_at, expires_at, last_used_at, call_count)"
            " VALUES (?, ?, ?, ?, 'active', '', COALESCE(?, datetime('now')), '', '', 0)",
            ("key-" + secrets.token_hex(6), emp_id, h, "迁移前签发（legacy）", created))
        n += 1
    return n


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    cur.execute(_DDL)
    _backfill(cur)
    conn.commit()


def downgrade():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS employee_keys")
    conn.commit()
