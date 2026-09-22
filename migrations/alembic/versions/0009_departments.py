"""0009: departments 表（身份供给 · 部门目录）（2026-09-20）

背景：控制台要一个「身份供给」页 —— 新建部门、在部门下建员工、配员工信息权限。
员工底座阶段 1e 已存在（`employee_accounts` + `/api/v1/access/accounts*` 七端点，
模板 owner/dept_head/staff/external 决定披露级别上限与数据域），缺的是**部门实体**：
此前"部门"只是员工记录上的一串自由文本，没有对象可建、没有部门级默认。

口径（用户 2026-09-20 认可）：
- 本表**只做目录 + 默认值**（显示名/描述/建员工默认模板/创建人时间）；
- **权限判定完全不走它** —— 仍走 `employee_accounts.department` / `project_scope`，
  CD-025「记忆域读时派生」口径不动，不引入第二真相源；
- 与员工记录按 `name` join → 存量员工建部门后自动归位，**零数据迁移**；
- 改名可经 `PATCH /api/v1/access/departments/{id}` 的 `sync_employees` 同步员工字段。

必须与 `db.py` init_db 内联 DDL **双侧同步**（CD-060 硬等式门禁要求差异 = 0）。
幂等（CREATE TABLE IF NOT EXISTS）；downgrade 只丢本表。
"""
from alembic import context

revision = "0009_departments"
down_revision = "0008_growth_indexes"
branch_labels = None
depends_on = None

# 与 db.py init_db 内联段逐字一致（规范化后须相等）
_DDL = """CREATE TABLE IF NOT EXISTS departments (
        department_id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        default_role_template TEXT DEFAULT 'staff',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )"""


def _conn():
    # 同 0002/0005/0007/0008 形态：sqlite3 原生连接，不走 op.execute 参数化
    return context.get_context().connection.connection.driver_connection


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    cur.execute(_DDL)
    conn.commit()


def downgrade():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS departments")
    conn.commit()
