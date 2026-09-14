"""0002: 冻结 db.py 裸增量 ALTER 链 + shared_workspace 运行时 ALTER（T2-3，2026-09-09）

覆盖范围 = 现库（HEAD c2c9fc5 的 sync_hub.db）sqlite_master 与 0001_baseline 的全部差异：
- 新表 13 张：agent_keys / agent_quotas / audit_log / backup_markers / document_chunks /
  employee_accounts / entity_review / gateway_read_log / integrations_state / messages /
  principal_groups / review_queue / shadow_pending
  （DDL 逐字取自现库 sqlite_master，内嵌自包含；messages/integrations_state 原本在 db.py
  建表区、backup_markers 在 hub_cli.py，0001 漏收，本 revision 补齐以实现硬等式）
- 新索引 1 条：idx_shadow_pending_status
- 增量列（sqlite 无 ADD COLUMN IF NOT EXISTS，逐条 PRAGMA table_info 查列集合，缺才加）：
  agents +7: api_key_created_at / api_key_expires_at / api_key_prev /
             api_key_prev_expires_at / api_key_ip_whitelist / last_used_at / full_access
  disclosure_log +3: prev_hash / entry_hash / trace_id
  memory_pool +3: trust_level / source_agent_id / tainted_at（S3 taint）
  shared_docs +5: visibility / allowed_agents（原 shared_workspace.py 运行时 ALTER）/
                  trust_level / source_agent_id / tainted_at（S3 taint）
  tasks +1: parent_task_id
  wiki_inbox +1: trust_level
  （db.py ALTER 链里的 api_key / pairing_codes.agent_id_a / team_members.shared_secret /
  disclosure_requests.audit_* / notifications.source 等 / automation_jobs.* /
  memory_pool.kind 等 0001 已内联覆盖，本 revision 不重复）

执行用 sqlite3 原生连接（driver_connection）：DDL 含 `--` 注释与 DEFAULT (datetime('now'))
表达式，op.execute 的 SQLAlchemy 参数化会误解析（P2 坑①）。
FTS5 影子表（memory_pool_fts_data/idx/docsize/config）由虚拟表自动管理，不在此显式建（P2 坑②）。
"""
from alembic import context

revision = "0002_freeze_incremental_alters"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# DDL 逐字取自现库 sqlite_master（与 0001 同一取材口径），保证空库 upgrade head 后
# sqlite_master.sql 与现库逐字节一致。建表前查 sqlite_master，存在则跳过（幂等）。
NEW_TABLES = [
 {
  "name": "agent_keys",
  "sql": "CREATE TABLE agent_keys (\n        key_id TEXT PRIMARY KEY,\n        agent_id TEXT NOT NULL,\n        key_hash TEXT NOT NULL,\n        scope TEXT DEFAULT '{\"endpoints\": [], \"data_domain\": [], \"level_cap\": \"\"}',\n        status TEXT DEFAULT 'active',\n        created_by TEXT DEFAULT '',\n        created_at TEXT DEFAULT (datetime('now')),\n        expires_at TEXT,\n        last_used_at TEXT,\n        call_count INTEGER DEFAULT 0\n    )"
 },
 {
  "name": "agent_quotas",
  "sql": "CREATE TABLE agent_quotas (\n            agent_id TEXT PRIMARY KEY,\n            qps_limit REAL NOT NULL DEFAULT 50.0,    -- 每秒请求上限\n            mode TEXT NOT NULL DEFAULT 'alert_only', -- reject | throttle | alert_only\n            window_sec REAL NOT NULL DEFAULT 1.0,\n            burst INTEGER NOT NULL DEFAULT 3,\n            updated_at TEXT DEFAULT (datetime('now'))\n        )"
 },
 {
  "name": "audit_log",
  "sql": "CREATE TABLE audit_log (\n        log_id INTEGER PRIMARY KEY AUTOINCREMENT,\n        entry_type TEXT NOT NULL DEFAULT '',\n        ref_table TEXT DEFAULT '',\n        ref_id TEXT DEFAULT '',\n        payload TEXT DEFAULT '',\n        prev_hash TEXT NOT NULL DEFAULT '',\n        entry_hash TEXT NOT NULL DEFAULT '',\n        created_at TEXT\n    )"
 },
 {
  "name": "backup_markers",
  "sql": "CREATE TABLE backup_markers (marker_id TEXT PRIMARY KEY, created_at TEXT)"
 },
 {
  "name": "document_chunks",
  "sql": "CREATE TABLE document_chunks (\n            chunk_id TEXT PRIMARY KEY,\n            parent_doc_id TEXT NOT NULL,\n            piece_index INTEGER NOT NULL,\n            content TEXT NOT NULL,\n            summary TEXT,\n            source_agent_id TEXT DEFAULT '',\n            trust_level TEXT DEFAULT 'trusted',\n            tainted_at TEXT,\n            disclosure_level TEXT DEFAULT 'summary',\n            sensitivity_score REAL DEFAULT 0.0,\n            chunk_hash TEXT NOT NULL,\n            kind TEXT DEFAULT 'fact',\n            pii_hits TEXT DEFAULT '[]',\n            created_at TEXT,\n            updated_at TEXT\n        )"
 },
 {
  "name": "employee_accounts",
  "sql": "CREATE TABLE employee_accounts (\n        employee_id TEXT PRIMARY KEY,\n        name TEXT NOT NULL,\n        email TEXT UNIQUE,\n        role_template TEXT NOT NULL DEFAULT 'staff',\n        department TEXT DEFAULT '',\n        project_scope TEXT DEFAULT '',\n        key_hash TEXT DEFAULT '',\n        status TEXT DEFAULT 'active',\n        created_at TEXT DEFAULT (datetime('now')),\n        lease_expires_at TEXT DEFAULT ''\n    )"
 },
 {
  "name": "entity_review",
  "sql": "CREATE TABLE entity_review (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            doc_id TEXT NOT NULL,\n            name TEXT NOT NULL,\n            entity_type TEXT DEFAULT 'other',\n            evidence TEXT,\n            level TEXT DEFAULT 'summary',\n            status TEXT DEFAULT 'pending',\n            source TEXT DEFAULT 'llm',\n            created_at TEXT DEFAULT (datetime('now')),\n            reviewed_at TEXT,\n            reviewed_by TEXT\n        )"
 },
 {
  "name": "gateway_read_log",
  "sql": "CREATE TABLE gateway_read_log (\n        log_id INTEGER PRIMARY KEY AUTOINCREMENT,\n        requester TEXT NOT NULL,\n        auth_mode TEXT DEFAULT '',\n        scope_json TEXT DEFAULT '',\n        kind TEXT NOT NULL,\n        query TEXT DEFAULT '',\n        target TEXT DEFAULT '',\n        granted_level TEXT DEFAULT '',\n        item_count INTEGER DEFAULT 0,\n        stripped_chunks INTEGER DEFAULT 0,\n        created_at TEXT DEFAULT (datetime('now'))\n    )"
 },
 {
  "name": "integrations_state",
  "sql": "CREATE TABLE integrations_state (\n            name TEXT PRIMARY KEY,\n            display_name TEXT DEFAULT '',\n            enabled INTEGER DEFAULT 0,\n            config_json TEXT DEFAULT '{}',\n            field_mapping TEXT DEFAULT '{}',\n            last_sync_at TEXT DEFAULT '',\n            last_status TEXT DEFAULT 'never',\n            last_error TEXT DEFAULT '',\n            record_count INTEGER DEFAULT 0,\n            pull_interval_min INTEGER DEFAULT 0,\n            created_at TEXT,\n            updated_at TEXT\n        )"
 },
 {
  "name": "messages",
  "sql": "CREATE TABLE messages (\n    message_id INTEGER PRIMARY KEY AUTOINCREMENT,\n    from_agent_id TEXT NOT NULL,\n    to_agent_id TEXT NOT NULL,\n    content TEXT NOT NULL,\n    is_read INTEGER DEFAULT 0,\n    created_at TEXT DEFAULT (datetime('now','localtime'))\n)"
 },
 {
  "name": "principal_groups",
  "sql": "CREATE TABLE principal_groups (\n        principal_id TEXT NOT NULL,\n        group_dn TEXT NOT NULL,\n        synced_at TEXT,\n        PRIMARY KEY (principal_id, group_dn)\n    )"
 },
 {
  "name": "review_queue",
  "sql": "CREATE TABLE review_queue (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        item_type TEXT NOT NULL DEFAULT 'entity',\n        doc_id TEXT NOT NULL,\n        name TEXT NOT NULL,\n        detail TEXT DEFAULT '',\n        level TEXT DEFAULT 'summary',\n        status TEXT DEFAULT 'pending',\n        source TEXT DEFAULT 'llm',\n        created_at TEXT DEFAULT (datetime('now')),\n        reviewed_at TEXT,\n        reviewed_by TEXT\n    )"
 },
 {
  "name": "shadow_pending",
  "sql": "CREATE TABLE shadow_pending (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        kind TEXT NOT NULL,\n        payload TEXT NOT NULL DEFAULT '',\n        created_at TEXT DEFAULT (datetime('now','localtime')),\n        status TEXT NOT NULL DEFAULT 'pending',\n        attempts INTEGER NOT NULL DEFAULT 0\n    )"
 },
]

NEW_INDEXES = [
 {
  "name": "idx_shadow_pending_status",
  "sql": "CREATE INDEX idx_shadow_pending_status\n        ON shadow_pending(status, attempts)"
 },
]

# 增量列：table -> [(col_name, col_decl)]，顺序与 db.py ALTER 链/现库 sqlite_master 追加顺序一致
# （sqlite ALTER 会把列定义原文追加到 sqlite_master.sql，顺序与措辞必须与现库一致才能字节相等）
ADD_COLUMNS = {
 "agents": [
  ("api_key_created_at", "TEXT"),
  ("api_key_expires_at", "TEXT"),
  ("api_key_prev", "TEXT"),
  ("api_key_prev_expires_at", "TEXT"),
  ("api_key_ip_whitelist", "TEXT"),
  ("last_used_at", "TEXT"),
  ("full_access", "INTEGER NOT NULL DEFAULT 0"),
 ],
 "disclosure_log": [
  ("prev_hash", "TEXT NOT NULL DEFAULT ''"),
  ("entry_hash", "TEXT NOT NULL DEFAULT ''"),
  ("trace_id", "TEXT"),
 ],
 "memory_pool": [
  ("trust_level", "TEXT NOT NULL DEFAULT 'internal'"),
  ("source_agent_id", "TEXT DEFAULT ''"),
  ("tainted_at", "TEXT DEFAULT ''"),
 ],
 "shared_docs": [
  ("visibility", "TEXT DEFAULT 'team'"),
  ("allowed_agents", "TEXT DEFAULT '[]'"),
  ("trust_level", "TEXT NOT NULL DEFAULT 'internal'"),
  ("source_agent_id", "TEXT DEFAULT ''"),
  ("tainted_at", "TEXT DEFAULT ''"),
 ],
 "tasks": [
  ("parent_task_id", "TEXT"),
 ],
 "wiki_inbox": [
  ("trust_level", "TEXT NOT NULL DEFAULT 'internal'"),
 ],
}


def _conn():
    # P2 坑①：用 sqlite3 原生连接执行 DDL，不走 op.execute
    return context.get_context().connection.connection.driver_connection


def _table_names(cur):
    return {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def upgrade():
    conn = _conn()
    cur = conn.cursor()
    tables = _table_names(cur)
    for t in NEW_TABLES:
        if t["name"] not in tables:
            cur.execute(t["sql"])
    existing_idx = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    for ix in NEW_INDEXES:
        if ix["name"] not in existing_idx:
            cur.execute(ix["sql"])
    for table, cols in ADD_COLUMNS.items():
        if table not in tables:
            continue  # 表缺失（异常库）时跳过，不崩迁移
        existing = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
        for col_name, col_decl in cols:
            if col_name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_decl}")
    conn.commit()


def downgrade():
    conn = _conn()
    cur = conn.cursor()
    for table, cols in ADD_COLUMNS.items():
        tables = _table_names(cur)
        if table not in tables:
            continue
        existing = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
        for col_name, _decl in cols:
            if col_name in existing:
                cur.execute(f"ALTER TABLE {table} DROP COLUMN {col_name}")
    existing_idx = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    for ix in NEW_INDEXES:
        if ix["name"] in existing_idx:
            cur.execute(f'DROP INDEX IF EXISTS "{ix["name"]}"')
    tables = _table_names(cur)
    for t in reversed(NEW_TABLES):
        if t["name"] in tables:
            cur.execute(f'DROP TABLE IF EXISTS "{t["name"]}"')
    conn.commit()
