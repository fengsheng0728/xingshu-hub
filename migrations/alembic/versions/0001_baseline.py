"""baseline: 星枢 sync_hub 全量 schema（21 业务表 + 2 索引 + FTS5 影子表）

DDL 内嵌（自包含），逐字取自现库 sqlite_master（含历次手动 SQL 产物）。
执行用 sqlite3 原生连接（DDL 含 SQLite 特有语法，SQLAlchemy 参数化会误解析）。
"""
from alembic import context

DDLS = [
 {
  "type": "table",
  "name": "agents",
  "sql": "CREATE TABLE agents (\n            agent_id TEXT PRIMARY KEY,\n            agent_name TEXT,\n            department TEXT,\n            capabilities TEXT,\n            role TEXT DEFAULT 'worker',\n            managed_agents TEXT,\n            disclosure_policy TEXT,\n            endpoint TEXT,\n            registered_at TEXT,\n            last_heartbeat TEXT,\n            status TEXT DEFAULT 'offline'\n        , api_key TEXT)"
 },
 {
  "type": "table",
  "name": "automation_jobs",
  "sql": "CREATE TABLE automation_jobs (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    name TEXT NOT NULL,\n    trigger_type TEXT NOT NULL DEFAULT 'cron',     -- cron | interval | event\n    trigger_spec TEXT NOT NULL,                      -- cron expr | seconds | event_name\n    instruction TEXT NOT NULL,                       -- free-text AI instruction\n    delivery TEXT NOT NULL DEFAULT '[\"notification\"]', -- JSON array: notification, memory_pool\n    guardrail TEXT NOT NULL DEFAULT '{\"max_iterations\":10,\"max_tokens\":50000,\"permission\":\"read_memory+write_memory\"}',\n    enabled INTEGER NOT NULL DEFAULT 1,\n    owner_agent_id TEXT NOT NULL,\n    run_count INTEGER DEFAULT 0,\n    last_run_at TEXT,\n    last_status TEXT,                                -- success | failed | timeout\n    last_run_duration_ms INTEGER,\n    last_result_summary TEXT,                        -- first 200 chars of result\n    next_run_at TEXT,\n    created_at TEXT DEFAULT (datetime('now')),\n    updated_at TEXT DEFAULT (datetime('now'))\n, consecutive_failures INTEGER DEFAULT 0, missed_runs INTEGER DEFAULT 0, allow_auto_source INTEGER DEFAULT 0, schedule_kind TEXT DEFAULT 'every', payload_type TEXT DEFAULT 'instruction', heartbeat_file TEXT DEFAULT '', delete_after_run INTEGER DEFAULT 0)"
 },
 {
  "type": "table",
  "name": "automation_runs",
  "sql": "CREATE TABLE automation_runs (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    job_id INTEGER NOT NULL REFERENCES automation_jobs(id),\n    agent_id TEXT NOT NULL,\n    status TEXT NOT NULL DEFAULT 'running',           -- running | success | failed | timeout | cancelled\n    started_at TEXT DEFAULT (datetime('now')),\n    finished_at TEXT,\n    duration_ms INTEGER,\n    result_summary TEXT,\n    full_result TEXT,                                  -- full output\n    error TEXT,\n    audit_ref TEXT,                                    -- audit log reference\n    iterations INTEGER DEFAULT 0,\n    tokens_used INTEGER DEFAULT 0\n)"
 },
 {
  "type": "table",
  "name": "buffer_log",
  "sql": "CREATE TABLE buffer_log (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                action TEXT, agent_id TEXT, title TEXT, entry_id TEXT,\n                queued_at TEXT, flushed_at TEXT, synced_at TEXT,\n                flush_latency_ms REAL\n            )"
 },
 {
  "type": "table",
  "name": "cron_jobs",
  "sql": "CREATE TABLE cron_jobs (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            name TEXT NOT NULL,\n            schedule TEXT NOT NULL,\n            action TEXT NOT NULL DEFAULT 'report',\n            action_params TEXT DEFAULT '{}',\n            enabled INTEGER DEFAULT 1,\n            created_by TEXT NOT NULL,\n            created_at TEXT DEFAULT (datetime('now')),\n            last_run TEXT,\n            next_run TEXT,\n            run_count INTEGER DEFAULT 0\n        )"
 },
 {
  "type": "table",
  "name": "disclosure_log",
  "sql": "CREATE TABLE disclosure_log (\n            log_id INTEGER PRIMARY KEY AUTOINCREMENT,\n            task_id TEXT,\n            from_agent_id TEXT,\n            to_agent_id TEXT,\n            memory_id TEXT,\n            disclosed_level TEXT,\n            disclosed_content TEXT,\n            disclosed_at TEXT,\n            reason TEXT\n        )"
 },
 {
  "type": "table",
  "name": "disclosure_requests",
  "sql": "CREATE TABLE disclosure_requests (\n            request_id TEXT PRIMARY KEY,\n            task_id TEXT NOT NULL,\n            agent_id TEXT NOT NULL,\n            reason TEXT,\n            new_phase INTEGER,\n            status TEXT DEFAULT 'pending',\n            created_at TEXT,\n            resolved_at TEXT,\n            resolved_by TEXT\n        , audit_decision TEXT, audit_reason TEXT, audit_risk_level TEXT)"
 },
 {
  "type": "table",
  "name": "events",
  "sql": "CREATE TABLE events (\n            event_id INTEGER PRIMARY KEY AUTOINCREMENT,\n            event_type TEXT,\n            agent_id TEXT,\n            payload TEXT,\n            timestamp TEXT\n        )"
 },
 {
  "type": "table",
  "name": "hub_agent_config",
  "sql": "CREATE TABLE hub_agent_config (\n            key TEXT PRIMARY KEY,\n            value TEXT,\n            updated_at TEXT\n        )"
 },
 {
  "type": "table",
  "name": "hub_agent_conversations",
  "sql": "CREATE TABLE hub_agent_conversations (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                session_id TEXT NOT NULL,\n                role TEXT NOT NULL,\n                content TEXT NOT NULL,\n                created_at TEXT NOT NULL\n            )"
 },
 {
  "type": "table",
  "name": "knowledge_base",
  "sql": "CREATE TABLE knowledge_base (\n            entry_id TEXT PRIMARY KEY,\n            title TEXT NOT NULL,\n            content TEXT,\n            tags TEXT,\n            links TEXT,\n            category TEXT DEFAULT 'general',\n            importance REAL DEFAULT 1.0,\n            created_by TEXT,\n            created_at TEXT,\n            updated_at TEXT\n        , embedding BLOB)"
 },
 {
  "type": "table",
  "name": "memory_pool",
  "sql": "CREATE TABLE memory_pool (\n            memory_id TEXT PRIMARY KEY,\n            owner_agent_id TEXT NOT NULL,\n            memory_key TEXT,\n            content TEXT,\n            summary TEXT,\n            embedding BLOB,\n            importance REAL,\n            tags TEXT,\n            disclosure_level TEXT DEFAULT 'summary',\n            disclosure_scope TEXT DEFAULT 'manager',\n            allowed_viewers TEXT,\n            created_at TEXT,\n            access_count INTEGER DEFAULT 0,\n            last_accessed TEXT\n        , kind TEXT DEFAULT 'fact', source_session_id TEXT DEFAULT '', confidence REAL DEFAULT 1.0, source_type TEXT DEFAULT 'user', updated_at TEXT DEFAULT NULL)"
 },
 {
  "type": "table",
  "name": "memory_pool_fts",
  "sql": "CREATE VIRTUAL TABLE memory_pool_fts USING fts5(\n            content, summary, tags,\n            content='memory_pool',\n            content_rowid='rowid'\n        )"
 },
 {
  "type": "table",
  "name": "memory_versions",
  "sql": "CREATE TABLE memory_versions (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            memory_id TEXT NOT NULL,\n            memory_key TEXT NOT NULL,\n            version INTEGER NOT NULL DEFAULT 1,\n            content TEXT,\n            summary TEXT,\n            confidence REAL,\n            archived_at TEXT DEFAULT (datetime('now')),\n            archived_by TEXT\n        )"
 },
 {
  "type": "table",
  "name": "notifications",
  "sql": "CREATE TABLE notifications (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            agent_id TEXT NOT NULL,\n            type TEXT NOT NULL,\n            title TEXT NOT NULL,\n            body TEXT,\n            related_task_id TEXT,\n            related_agent_id TEXT,\n            is_read INTEGER DEFAULT 0,\n            created_at TEXT NOT NULL\n        , source TEXT DEFAULT \"\", artifact_path TEXT DEFAULT '', channel_status TEXT DEFAULT '')"
 },
 {
  "type": "table",
  "name": "pairing_codes",
  "sql": "CREATE TABLE pairing_codes (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            code TEXT NOT NULL,\n            hub_id_a TEXT NOT NULL,\n            hub_id_b TEXT,\n            created_at TEXT DEFAULT (datetime('now')),\n            expires_at TEXT NOT NULL,\n            attempts INTEGER DEFAULT 0,\n            used INTEGER DEFAULT 0\n        , agent_id_a TEXT)"
 },
 {
  "type": "table",
  "name": "session_archives",
  "sql": "CREATE TABLE session_archives (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            agent_id TEXT NOT NULL,\n            local_session_id INTEGER NOT NULL,\n            title TEXT DEFAULT '',\n            summary TEXT DEFAULT '',\n            key_facts TEXT DEFAULT '[]',\n            msg_count INTEGER DEFAULT 0,\n            created_at TEXT DEFAULT (datetime('now')),\n            updated_at TEXT DEFAULT (datetime('now'))\n        )"
 },
 {
  "type": "table",
  "name": "shared_docs",
  "sql": "CREATE TABLE shared_docs (\n    doc_id      TEXT PRIMARY KEY,\n    title       TEXT NOT NULL,\n    created_by  TEXT NOT NULL,\n    created_at  REAL NOT NULL,\n    updated_at  REAL NOT NULL,\n    archived    INTEGER DEFAULT 0,\n    block_count INTEGER DEFAULT 0\n)"
 },
 {
  "type": "table",
  "name": "tasks",
  "sql": "CREATE TABLE tasks (\n            task_id TEXT PRIMARY KEY,\n            status TEXT,\n            creator_agent_id TEXT,\n            assigned_agent_id TEXT,\n            description TEXT,\n            required_capabilities TEXT,\n            required_memories TEXT,\n            disclosure_plan TEXT,\n            current_phase INTEGER DEFAULT 1,\n            priority INTEGER,\n            result TEXT,\n            created_at TEXT,\n            updated_at TEXT\n        , depends_on TEXT DEFAULT '[]')"
 },
 {
  "type": "table",
  "name": "team_members",
  "sql": "CREATE TABLE team_members (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            local_agent_id TEXT NOT NULL,\n            remote_hub_id TEXT NOT NULL,\n            remote_hub_url TEXT NOT NULL,\n            remote_agent_id TEXT NOT NULL,\n            remote_api_key TEXT NOT NULL,\n            hostname TEXT,\n            user_name TEXT,\n            role TEXT DEFAULT 'worker',\n            department TEXT,\n            paired_at TEXT NOT NULL,\n            key_expires_at TEXT NOT NULL,\n            last_heartbeat TEXT,\n            revoked_at TEXT, team_id INTEGER REFERENCES teams(id), shared_secret TEXT,\n            UNIQUE(local_agent_id, remote_hub_id)\n        )"
 },
 {
  "type": "table",
  "name": "teams",
  "sql": "CREATE TABLE teams (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        name TEXT NOT NULL,\n        description TEXT,\n        owner_agent_id TEXT NOT NULL,\n        created_at TEXT DEFAULT (datetime('now'))\n    )"
 },
 {
  "type": "table",
  "name": "wiki_inbox",
  "sql": "CREATE TABLE wiki_inbox (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            page_path TEXT NOT NULL UNIQUE,\n            title TEXT,\n            status TEXT DEFAULT 'pending',\n            source TEXT,\n            created_at TEXT DEFAULT (datetime('now')),\n            reviewed_at TEXT,\n            reviewed_by TEXT\n        )"
 },
 {
  "type": "index",
  "name": "idx_buffer_log_entry",
  "sql": "CREATE INDEX idx_buffer_log_entry ON buffer_log(entry_id, flushed_at)"
 },
 {
  "type": "index",
  "name": "idx_session_archives_agent_session",
  "sql": "CREATE UNIQUE INDEX idx_session_archives_agent_session\n        ON session_archives(agent_id, local_session_id)\n    "
 }
]

def _conn():
    return context.get_context().connection.connection.driver_connection

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    conn = _conn(); cur = conn.cursor()
    for o in DDLS:
        cur.execute(o["sql"])
    conn.commit()

def downgrade():
    conn = _conn(); cur = conn.cursor()
    for o in reversed(DDLS):
        if o["type"] == "index":
            cur.execute(f'DROP INDEX IF EXISTS "{o["name"]}"')
        else:
            cur.execute(f'DROP TABLE IF EXISTS "{o["name"]}"')
    conn.commit()
