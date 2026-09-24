import logging
logger = logging.getLogger("xingshu.db")

from models import CONFIG, DisclosureLevel
"""
星枢 Sync Hub — 数据库初始化、网络工具、日志配置
"""
import os
import sys
import hashlib
import time
import json
from datetime import datetime, timezone
import sqlite3
import logging
import re
from logging.handlers import TimedRotatingFileHandler
import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer


def row_to_dict(row):
    """安全转换 sqlite3.Row → dict。
    sqlite3.Row 对象在 Python 某些版本中 dict(row) 会崩溃（Row 迭代值而非键值对）。
    所有从 sqlite3 读取的 Row 对象都应通过此函数转换。"""
    return {k: row[k] for k in row.keys()}


# ============ 本地 Embedding（替代 sentence-transformers，不依赖网络下载） ============

# ============ 日志配置 ============
import logging
import logging.handlers
import sys as _sys

def _setup_logging():
    log_dir = os.environ.get("SYNC_HUB_LOG_DIR", "./logs")
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("sync_hub")
    logger.setLevel(logging.DEBUG)

    fh = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, "hub.log"), when="midnight", backupCount=30,
        encoding="utf-8"
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler(_sys.stdout)
    is_electron = os.environ.get("SYNC_HUB_ELECTRON") == "1"
    ch.setLevel(logging.WARNING if is_electron else logging.DEBUG)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger

logger = _setup_logging()

class LocalEmbedding:
    """轻量本地 embedding，基于 HashingVectorizer，固定 384 维输出"""

    def __init__(self, n_features: int = 384):
        self._vectorizer = HashingVectorizer(
            n_features=n_features,
            norm="l2",
            alternate_sign=False,
        )
        self._n_features = n_features
        # 预 fit 一次让 vectorizer 就绪
        self._vectorizer.transform(["init"])

    def encode(self, text: str) -> np.ndarray:
        """将文本编码为 embedding 向量（384 维）"""
        vec = self._vectorizer.transform([text])
        return vec.toarray().astype(np.float32)[0]

    def __call__(self, sentences):
        """兼容 batch 调用（逐个编码）"""
        if isinstance(sentences, str):
            return self.encode(sentences)
        return np.array([self.encode(s) for s in sentences])


class SentenceTransformerEmbedding:
    """真语义 embedding（K1，附录 F 2026-08-06）— sentence-transformers 模型封装。

    选型双档（K1 修正 1）：
      bge-small-zh-v1.5  ~100MB  默认分发（内网低配机）
      bge-m3             ~2.2GB  高配可选
    加载路径：model_path 指向本地目录（离线包分发，HuggingFace 不可达时可用）。
    失败时 raise —— 由调用方降级 hasher（fail-open 仅限 embedding，检索降级 ILIKE）。
    """

    def __init__(self, model_path: str, device: str = "cpu"):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model_path, device=device)
        self._model.eval()
        self._n_features = self._model.get_sentence_embedding_dimension()

    @property
    def dim(self) -> int:
        return self._n_features

    def encode(self, text: str) -> np.ndarray:
        vec = self._model.encode(text, convert_to_numpy=True)
        return np.asarray(vec, dtype=np.float32)

    def __call__(self, sentences):
        if isinstance(sentences, str):
            return self.encode(sentences)
        vecs = self._model.encode(list(sentences), convert_to_numpy=True)
        return np.asarray(vecs, dtype=np.float32)


def get_embedding_provider(name: str = "hasher",
                           model_path: str = "",
                           n_features: int = 384):
    """embedding provider 工厂（K1a，方案 4.2 抽象落地）。

    name:
      hasher   — HashingVectorizer 词袋（默认，零依赖，384 维）
      sentence — sentence-transformers 真语义模型（bge-small-zh/bge-m3/MiniLM）

    返回: 有 encode(text)->np.ndarray 和 __call__(list)->np.ndarray 接口的对象。
    sentence provider 加载失败抛异常（由调用方降级 hasher + 检索降级 ILIKE）。
    """
    if name == "sentence":
        if not model_path:
            raise ValueError("sentence provider 需要 model_path（本地模型目录）")
        return SentenceTransformerEmbedding(model_path)
    return LocalEmbedding(n_features=n_features)

# O3(2026-08-05): 升级回滚 — 迁移前自动备份 + 失败恢复
SCHEMA_VERSION = 7  # S1=1, S2=2, S3=3, O4=4, H1=5, G1=6（shadow_pending）（O3 起用 user_version 记录）, CD-045=7（event_outbox）


def _pre_migrate_backup(db_path: str, backup_dir: str) -> str:
    """VACUUM INTO 快照到 backup_dir，返回备份路径。失败返回空串。"""
    import datetime as _dt
    try:
        if not db_path or not os.path.exists(db_path):
            return ""  # 源库不存在 → 不备份
        os.makedirs(backup_dir, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(backup_dir, f"pre_migrate_{ts}.db")
        src = sqlite3.connect(db_path)
        src.execute("VACUUM INTO ?", (dest,))
        src.close()
        return dest
    except Exception:
        return ""


def _alembic_head_revision() -> str:
    """从 migrations/alembic 脚本目录读 head revision（只读脚本，不硬编码版本号）。"""
    from alembic.config import Config as _AlembicConfig
    from alembic.script import ScriptDirectory
    repo_root = os.path.dirname(os.path.abspath(__file__))
    acfg = _AlembicConfig(os.path.join(repo_root, "alembic.ini"))
    acfg.set_main_option("script_location", os.path.join(repo_root, "migrations", "alembic"))
    return ScriptDirectory.from_config(acfg).get_current_head()


# 内联 DDL **有意不含**、必须靠迁移回补的 revision（CD-060 有意差异，逐条登记）。
# 缺列新库的 alembic 登记目标 = 本链**最靠前**一条的前一版（见 _stamp_alembic_head）。
# 新增同类缺口迁移时往这里追加——**不要**改回「取 head 前一版」（会随 head 漂移）。
_INLINE_DDL_GAP_REVISIONS = ("0013_agents_api_key_hash_backstop",)


def _alembic_down_revision(rev: str):
    """读指定 revision 的 down_revision（单头链；多父或无 → None，只读脚本）。"""
    from alembic.config import Config as _AlembicConfig
    from alembic.script import ScriptDirectory
    repo_root = os.path.dirname(os.path.abspath(__file__))
    acfg = _AlembicConfig(os.path.join(repo_root, "alembic.ini"))
    acfg.set_main_option("script_location", os.path.join(repo_root, "migrations", "alembic"))
    dr = ScriptDirectory.from_config(acfg).get_revision(rev).down_revision
    return dr if isinstance(dr, str) else None


def _stamp_alembic_head(db_path: str) -> bool:
    """对无迁移登记的库写入 alembic head 版本（等价 `alembic stamp head`）。

    只处理「无 alembic_version 表」的库；已有登记的库（哪怕落后）不动——
    落后库由 main.py 启动检查的 upgrade / fail-closed 路径负责。
    返回 True = 本次写入了登记。缺 hash 列（CD-060 有意差异）的库只登记到
    **最早一条缺口迁移的前一版**（见 _INLINE_DDL_GAP_REVISIONS），把 0013
    兜底补列留给启动迁移 / upgrade head（见 0013 docstring）。
    取固定锚而非「head 前一版」——否则 head 每前进一次目标就漂一格，
    被跳过的缺口迁移永不再执行（CD-111 连带回归，2026-09-24 修复）。
    """
    head = _alembic_head_revision()
    if not head:
        return False
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if row:
            return False
        conn.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        # 终审断点 3 修正：内联 agents DDL **有意不含** api_key_hash /
        # api_key_prev_hash（CD-060 登记差异）。缺列库若直接登记 head，兜底迁移
        # 0013 永不执行 → 新部署永久明文模式且 run_startup_migration 报 up_to_date
        # 误导。缺列库只登记到 head 的前一版，让启动迁移（main.py
        # run_startup_migration）或手动 `alembic upgrade head` 执行 0013 补列。
        target = head
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        if "api_key_hash" not in cols:
            # CD-111 连带回归修复（2026-09-24，验收期实测发现）：**不能**取
            # 「head 的前一版」——head 每前进一次目标就漂一格，被跳过的缺口
            # 迁移永不再执行。实测：0014 落地后 head=0014，缺列新库登记到
            # 0013，0013 兜底补列被跳过 → agents 永久缺 hash 两列（终审断点 3
            # 的病复发，test_startup_migration::test_fresh_init_db_stamps_head 抓出）。
            # 固定为「最早一条缺口迁移」的前一版，head 再前进也不漂。
            prev = _alembic_down_revision(_INLINE_DDL_GAP_REVISIONS[0])
            if prev:
                target = prev
        conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (target,))
        conn.commit()
        return True
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(CONFIG.DB_PATH)
    c = conn.cursor()

    # 全新库判定（数据层修复轮）：建表前无 agents 表 = 本次 init_db 从零建 schema，
    # 结尾只对全新库登记 alembic head（老库不猜不动，行为不变）。
    _fresh_db = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agents'"
    ).fetchone() is None

    # Agent 表
    # CD-060（2026-09-20）：列序对齐 alembic 基线（0001 基线 + 0002/0003 追加序），
    # 下方冻结 ALTER 段对新库转为 no-op 守卫，存量库仍由其补列（幂等）。
    c.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            agent_name TEXT,
            department TEXT,
            capabilities TEXT,
            role TEXT DEFAULT 'worker',
            managed_agents TEXT,
            disclosure_policy TEXT,
            endpoint TEXT,
            registered_at TEXT,
            last_heartbeat TEXT,
            status TEXT DEFAULT 'offline',
            api_key TEXT,
            api_key_created_at TEXT,
            api_key_expires_at TEXT,
            api_key_prev TEXT,
            api_key_prev_expires_at TEXT,
            api_key_ip_whitelist TEXT,
            last_used_at TEXT,
            -- CD-060 口径（2026-09-20 复核修正）：api_key_hash / api_key_prev_hash **有意不内联**——
            -- 代码按 PRAGMA 列存在性自动切换明文/哈希模式（T1-2 渐进迁移锚点）；
            -- 若内联补齐，新库将直接进 hash 模式、
            -- 注册/重注册回执 api_key 变空串，破坏既有幂等回执行为
            -- （全量回归实测 5 例红，见台账 CD-060 口径变更记录）。
            full_access INTEGER NOT NULL DEFAULT 0
        )
    """)

    # 记忆池（按 Agent 隔离，带披露策略）
    # CD-060（2026-09-20）：列序对齐 alembic 基线（0001 基线 + M3/S3 追加序，
    # updated_at 补 DEFAULT NULL）；conn4/conn6 的幂等 ALTER 对新库转为 no-op。
    c.execute("""
        CREATE TABLE IF NOT EXISTS memory_pool (
            memory_id TEXT PRIMARY KEY,
            owner_agent_id TEXT NOT NULL,
            memory_key TEXT,
            content TEXT,
            summary TEXT,
            embedding BLOB,
            importance REAL,
            tags TEXT,
            disclosure_level TEXT DEFAULT 'summary',
            disclosure_scope TEXT DEFAULT 'manager',
            allowed_viewers TEXT,
            created_at TEXT,
            access_count INTEGER DEFAULT 0,
            last_accessed TEXT,
            kind TEXT DEFAULT 'fact',
            source_session_id TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0,
            source_type TEXT DEFAULT 'user',
            updated_at TEXT DEFAULT NULL,
            trust_level TEXT NOT NULL DEFAULT 'internal',
            source_agent_id TEXT DEFAULT '',
            tainted_at TEXT DEFAULT ''
        )
    """)

    # FTS5 全文索引（embedding 不可用时的降级检索）
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_pool_fts USING fts5(
            content, summary, tags,
            content='memory_pool',
            content_rowid='rowid'
        )
    """)

    # 任务调度表
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT,
            creator_agent_id TEXT,
            assigned_agent_id TEXT,
            description TEXT,
            required_capabilities TEXT,
            required_memories TEXT,
            disclosure_plan TEXT,
            current_phase INTEGER DEFAULT 1,
            priority INTEGER,
            result TEXT,
            created_at TEXT,
            updated_at TEXT,
            depends_on TEXT DEFAULT '[]',
            parent_task_id TEXT
        )
    """)

    # 披露日志（审计）
    # CD-060（2026-09-20）：列序对齐 alembic（0001 基线 + 0002 追加
    # prev_hash/entry_hash/trace_id），下方冻结 ALTER 段对新库转为 no-op。
    c.execute("""
        CREATE TABLE IF NOT EXISTS disclosure_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            from_agent_id TEXT,
            to_agent_id TEXT,
            memory_id TEXT,
            disclosed_level TEXT,
            disclosed_content TEXT,
            disclosed_at TEXT,
            reason TEXT,
            prev_hash TEXT NOT NULL DEFAULT '',
            entry_hash TEXT NOT NULL DEFAULT '',
            trace_id TEXT
        )
    """)

    # 文档切割块表（H1 数据汇入管道，2026-08-06）
    # chunk 是语义完整的披露单元；级别继承父文档只降不升（附录 E v1.4）
    c.execute("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            chunk_id TEXT PRIMARY KEY,
            parent_doc_id TEXT NOT NULL,
            piece_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            summary TEXT,
            source_agent_id TEXT DEFAULT '',
            trust_level TEXT DEFAULT 'trusted',
            tainted_at TEXT,
            disclosure_level TEXT DEFAULT 'summary',
            sensitivity_score REAL DEFAULT 0.0,
            chunk_hash TEXT NOT NULL,
            kind TEXT DEFAULT 'fact',
            pii_hits TEXT DEFAULT '[]',
            created_at TEXT,
            updated_at TEXT
        )
    """)

    # 事件表
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_agent_id TEXT NOT NULL,
        to_agent_id TEXT NOT NULL,
        content TEXT NOT NULL,
        is_read INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")


    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            agent_id TEXT,
            payload TEXT,
            timestamp TEXT
        )
    """)

    # 通知表（P7：持久化通知 + 已读/未读）
    # CD-060（2026-09-20）：列序对齐 alembic（created_at 归位、source/artifact_path/
    # channel_status 按追加序后置，source 默认值沿用现库 "" 写法），c3b ALTER 转 no-op。
    c.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            related_task_id TEXT,
            related_agent_id TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            source TEXT DEFAULT "",
            artifact_path TEXT DEFAULT '',
            channel_status TEXT DEFAULT ''
        )
    """)

    # 披露审批请求表
    c.execute("""
        CREATE TABLE IF NOT EXISTS disclosure_requests (
            request_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            reason TEXT,
            new_phase INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            resolved_at TEXT,
            resolved_by TEXT
        )
    """)

    # 自动化任务表（R1）
    # CD-060（2026-09-20）：列序对齐 alembic（0001 基线 + 追加序）；schedule_kind 默认值
    # 'cron'→'every'、heartbeat_file 补 DEFAULT ''（与现库/0001 逐字一致），c3c ALTER 转 no-op。
    c.execute("""
        CREATE TABLE IF NOT EXISTS automation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            trigger_type TEXT NOT NULL DEFAULT 'cron',
            trigger_spec TEXT NOT NULL,
            instruction TEXT NOT NULL,
            delivery TEXT NOT NULL DEFAULT '["notification"]',
            guardrail TEXT NOT NULL DEFAULT '{"max_iterations":10,"max_tokens":50000,"permission":"read_memory+write_memory"}',
            enabled INTEGER NOT NULL DEFAULT 1,
            owner_agent_id TEXT NOT NULL,
            run_count INTEGER DEFAULT 0,
            last_run_at TEXT,
            last_status TEXT,
            last_run_duration_ms INTEGER,
            last_result_summary TEXT,
            next_run_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            consecutive_failures INTEGER DEFAULT 0,
            missed_runs INTEGER DEFAULT 0,
            allow_auto_source INTEGER DEFAULT 0,
            schedule_kind TEXT DEFAULT 'every',
            payload_type TEXT DEFAULT 'instruction',
            heartbeat_file TEXT DEFAULT '',
            delete_after_run INTEGER DEFAULT 0
        )
    """)

    # 自动化运行记录表（R1）
    c.execute("""
        CREATE TABLE IF NOT EXISTS automation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES automation_jobs(id),
            agent_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT DEFAULT (datetime('now')),
            finished_at TEXT,
            duration_ms INTEGER,
            result_summary TEXT,
            full_result TEXT,
            error TEXT,
            audit_ref TEXT,
            iterations INTEGER DEFAULT 0,
            tokens_used INTEGER DEFAULT 0
        )
    """)

    # 企业知识库表（Obsidian 风格：双向链接 + 标签）
    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_base (
            entry_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT,
            tags TEXT,
            links TEXT,
            category TEXT DEFAULT 'general',
            importance REAL DEFAULT 1.0,
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            embedding BLOB
        )
    """)


    # Hub Agent 配置表（LLM 驱动的披露审计引擎）
    c.execute("""
        CREATE TABLE IF NOT EXISTS hub_agent_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)

    # 集成层（§七 门框）：连接器状态表 — 配置中密钥字段 AES-GCM 加密（enc:<nonce>:<ct>）
    c.execute("""
        CREATE TABLE IF NOT EXISTS integrations_state (
            name TEXT PRIMARY KEY,
            display_name TEXT DEFAULT '',
            enabled INTEGER DEFAULT 0,
            config_json TEXT DEFAULT '{}',
            field_mapping TEXT DEFAULT '{}',
            last_sync_at TEXT DEFAULT '',
            last_status TEXT DEFAULT 'never',
            last_error TEXT DEFAULT '',
            record_count INTEGER DEFAULT 0,
            pull_interval_min INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    # M2: 会话摘要归档表
    c.execute("""
        CREATE TABLE IF NOT EXISTS session_archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            local_session_id INTEGER NOT NULL,
            title TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            key_facts TEXT DEFAULT '[]',
            msg_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_session_archives_agent_session
        ON session_archives(agent_id, local_session_id)
    """)

    # 内网组队 — 团队成员表
    # CD-060（2026-09-20）：补 team_id（对齐 0001 基线；存量库由 alembic 0007 幂等补列），
    # shared_secret 保持在末位（对齐现库追加序），下方 c3 ALTER 转 no-op。
    # CD-111（2026-09-24）：remote_api_key_hash 追加在末位，对齐 0014 ALTER 追加序。
    c.execute("""
        CREATE TABLE IF NOT EXISTS team_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_agent_id TEXT NOT NULL,
            remote_hub_id TEXT NOT NULL,
            remote_hub_url TEXT NOT NULL,
            remote_agent_id TEXT NOT NULL,
            remote_api_key TEXT NOT NULL,
            hostname TEXT,
            user_name TEXT,
            role TEXT DEFAULT 'worker',
            department TEXT,
            paired_at TEXT NOT NULL,
            key_expires_at TEXT NOT NULL,
            last_heartbeat TEXT,
            revoked_at TEXT,
            team_id INTEGER REFERENCES teams(id),
            shared_secret TEXT,  -- P2: 配对握手 HKDF 派生的 AES-GCM 会话密钥（hex），联邦加密信道用
            remote_api_key_hash TEXT,
            UNIQUE(local_agent_id, remote_hub_id)
        )
    """)

    # 配对码临时表（一次性使用，5分钟TTL）
    # CD-060（2026-09-20）：agent_id_a 移到末位（对齐 0001 基线追加序），c3 ALTER 转 no-op。
    c.execute("""
        CREATE TABLE IF NOT EXISTS pairing_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            hub_id_a TEXT NOT NULL,
            hub_id_b TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0,
            agent_id_a TEXT
        )
    """)

    # ============ CD-060（2026-09-20）内联 DDL 补齐：原仅 alembic 路径存在的表 ============
    # 以下 5 表 + 1 索引此前只在 alembic 基线（0001/0002）里，未跑 alembic 的新库会缺表。
    # DDL 与 alembic 侧规范化对齐；全部 IF NOT EXISTS 幂等，存量库重复执行无副作用。
    # teams 组队表（0001 基线）
    c.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            owner_agent_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # cron_jobs 定时任务表（0001 基线）
    c.execute("""
        CREATE TABLE IF NOT EXISTS cron_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            schedule TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT 'report',
            action_params TEXT DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            created_by TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            last_run TEXT,
            next_run TEXT,
            run_count INTEGER DEFAULT 0
        )
    """)
    # buffer_log 写缓冲 WAL 表（0001 基线；运行时 hub_mixins/buffer.py:185 也有
    # IF NOT EXISTS 惰性兜底，DDL 与本段规范化一致，谁先建都幂等）
    c.execute("""
        CREATE TABLE IF NOT EXISTS buffer_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT, agent_id TEXT, title TEXT, entry_id TEXT,
            queued_at TEXT, flushed_at TEXT, synced_at TEXT,
            flush_latency_ms REAL
        )
    """)
    # CD-017: entry_id 索引（0001 基线；buffer.py 同样幂等兜底）
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_buffer_log_entry ON buffer_log(entry_id, flushed_at)
    """)
    # hub_agent_conversations 对话历史表（0001 基线；hub_agent_lc.py:28 惰性兜底，同上幂等）
    c.execute("""
        CREATE TABLE IF NOT EXISTS hub_agent_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    # backup_markers 备份锚点表（0002 补建；hub_cli.py 只读不写建表）
    c.execute("""
        CREATE TABLE IF NOT EXISTS backup_markers (
            marker_id TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)
    # CD-060 lazy 登记：shared_docs 刻意不在此内联建表（避免双份 DDL 冲突）。
    # 实际创建者：shared_workspace.py:29 SHARED_DOCS_DDL，由 SharedWorkspace._init_db()
    # （shared_workspace.py:63，start() 首次调用时）惰性创建；下方 S3 taint ALTER 段
    # 对该表有「不存在则跳过」守卫。硬等式验收（tests/test_schema_hard_equality.py）
    # 按 lazy 白名单单列豁免并注明本创建者。

    conn.commit()
    conn.close()

    # 启用 WAL 模式提升并发读写
    conn2 = sqlite3.connect(CONFIG.DB_PATH)
    conn2.execute("PRAGMA journal_mode=WAL")
    conn2.execute("PRAGMA synchronous=NORMAL")
    conn2.close()

    # ============ schema 增量已冻结（T2-3，2026-09-09）============
    # 本段裸 ALTER/增量建表已冻结进 alembic 0002_freeze_incremental_alters，
    # 此后改表只走 alembic revision（docs/schema-migration-guide.md），禁止在此新增。
    # 本段保留仅兼容未跑 migration 的旧启动路径（老库无 alembic_version 时兜底冷启动）；
    # 所有 ALTER 均有 PRAGMA table_info 幂等守卫，重复执行安全。
    # 增量迁移：api_key 字段（Phase 1 P4 需要）
    conn3 = sqlite3.connect(CONFIG.DB_PATH)
    c3 = conn3.cursor()
    c3.execute("PRAGMA table_info(agents)")
    columns = [col[1] for col in c3.fetchall()]
    if "api_key" not in columns:
        c3.execute("ALTER TABLE agents ADD COLUMN api_key TEXT")
        conn3.commit()

    # 增量迁移（S1 身份接入，2026-08-05）：agents 加 api_key 轮换/白名单/审计列
    c3.execute("PRAGMA table_info(agents)")
    _cols = [col[1] for col in c3.fetchall()]
    if "api_key_created_at" not in _cols:
        c3.execute("ALTER TABLE agents ADD COLUMN api_key_created_at TEXT")
    if "api_key_expires_at" not in _cols:
        c3.execute("ALTER TABLE agents ADD COLUMN api_key_expires_at TEXT")
    if "api_key_prev" not in _cols:
        c3.execute("ALTER TABLE agents ADD COLUMN api_key_prev TEXT")
    if "api_key_prev_expires_at" not in _cols:
        c3.execute("ALTER TABLE agents ADD COLUMN api_key_prev_expires_at TEXT")
    if "api_key_ip_whitelist" not in _cols:
        c3.execute("ALTER TABLE agents ADD COLUMN api_key_ip_whitelist TEXT")
    if "last_used_at" not in _cols:
        c3.execute("ALTER TABLE agents ADD COLUMN last_used_at TEXT")
    # 增量迁移（N1 全访问授权，阶段1/1c 2026-08-30）：full_access 默认 0（fail-closed）
    if "full_access" not in _cols:
        c3.execute("ALTER TABLE agents ADD COLUMN full_access INTEGER NOT NULL DEFAULT 0")
    conn3.commit()
    # T1-2（2026-09-09）：api_key_hash / api_key_prev_hash 哈希列与存量明文清空
    # 在 alembic 0003_hash_agents_api_key，勿在此新增 ALTER（本段已冻结）。
    # 2026-09-09 T1-2 起 Hub 库不存明文 api_key；代码侧按 PRAGMA 检测 hash 列
    # 是否存在自动切换 hash/明文行为，故本冻结段缺失 hash 列不影响老库冷启动。
    # 建 principal_groups 表（S1：LDAP/OIDC 组关系，披露引擎权限交集用）
    c3.execute("""CREATE TABLE IF NOT EXISTS principal_groups (
        principal_id TEXT NOT NULL,
        group_dn TEXT NOT NULL,
        synced_at TEXT,
        PRIMARY KEY (principal_id, group_dn)
    )""")
    conn3.commit()

    # 增量迁移（S2 审计 hash chain，2026-08-05）：audit_log 主链表 + disclosure_log hash 列
    c3.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_type TEXT NOT NULL DEFAULT '',
        ref_table TEXT DEFAULT '',
        ref_id TEXT DEFAULT '',
        payload TEXT DEFAULT '',
        prev_hash TEXT NOT NULL DEFAULT '',
        entry_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT
    )""")
    c3.execute("PRAGMA table_info(disclosure_log)")
    _dcols = [col[1] for col in c3.fetchall()]
    if "prev_hash" not in _dcols:
        c3.execute("ALTER TABLE disclosure_log ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''")
    if "entry_hash" not in _dcols:
        c3.execute("ALTER TABLE disclosure_log ADD COLUMN entry_hash TEXT NOT NULL DEFAULT ''")
    if "trace_id" not in _dcols:
        c3.execute("ALTER TABLE disclosure_log ADD COLUMN trace_id TEXT")
    conn3.commit()


    # 增量迁移：pairing_codes.agent_id_a（P3 配对记录发起方 Agent）
    c3.execute("PRAGMA table_info(pairing_codes)")
    pc_cols = [col[1] for col in c3.fetchall()]
    if "agent_id_a" not in pc_cols:
        c3.execute("ALTER TABLE pairing_codes ADD COLUMN agent_id_a TEXT")
        conn3.commit()

    # 增量迁移（CD-055，2026-09-19）：knowledge_base.embedding 向量列
    # （0001 基线已含该列但内联 CREATE 长期缺失；与 alembic 0005 逐字对齐）
    c3.execute("PRAGMA table_info(knowledge_base)")
    kb_cols = [col[1] for col in c3.fetchall()]
    if "embedding" not in kb_cols:
        c3.execute("ALTER TABLE knowledge_base ADD COLUMN embedding BLOB")
        conn3.commit()

    # CD-111（2026-09-24）：remote_api_key_hash 哈希列在 alembic 0014，
    # 勿在此新增 ALTER（本段已冻结）。读侧按 PRAGMA 检测 hash 列自动切换，
    # 缺列不影响老库冷启动（明文匹配继续有效）。
    # 增量迁移：team_members.shared_secret（P2 联邦加密会话密钥）
    c3.execute("PRAGMA table_info(team_members)")
    tm_cols = [col[1] for col in c3.fetchall()]
    if "shared_secret" not in tm_cols:
        c3.execute("ALTER TABLE team_members ADD COLUMN shared_secret TEXT")
        conn3.commit()

    # 披露请求表审计字段迁移
    c3.execute("PRAGMA table_info(disclosure_requests)")
    dr_columns = [col[1] for col in c3.fetchall()]
    for col_name, col_type in [
        ("audit_decision", "TEXT"), ("audit_reason", "TEXT"), ("audit_risk_level", "TEXT")
    ]:
        if col_name not in dr_columns:
            c3.execute(f"ALTER TABLE disclosure_requests ADD COLUMN {col_name} {col_type}")
            conn3.commit()
    conn3.close()

    # 通知表字段迁移（source / artifact_path）
    conn3b = sqlite3.connect(CONFIG.DB_PATH)
    c3b = conn3b.cursor()
    c3b.execute("PRAGMA table_info(notifications)")
    notif_columns = [col[1] for col in c3b.fetchall()]
    for col_name, col_type, col_default in [
        ("source", "TEXT", "''"),
        ("artifact_path", "TEXT", "''"),
        ("channel_status", "TEXT", "''"),  # P2 通知多渠道: JSON {dingtalk: ok|fail, smtp: ok|fail}
    ]:
        if col_name not in notif_columns:
            c3b.execute(f"ALTER TABLE notifications ADD COLUMN {col_name} {col_type} DEFAULT {col_default}")
            conn3b.commit()
    conn3b.close()

    # 自动化任务表字段迁移（R1 后续新增）
    conn3c = sqlite3.connect(CONFIG.DB_PATH)
    c3c = conn3c.cursor()
    c3c.execute("PRAGMA table_info(automation_jobs)")
    aj_columns = [col[1] for col in c3c.fetchall()]
    for col_name, col_type, col_default in [
        ("consecutive_failures", "INTEGER", "0"),
        ("missed_runs", "INTEGER", "0"),
        ("allow_auto_source", "INTEGER", "0"),
    ]:
        if col_name not in aj_columns:
            c3c.execute(f"ALTER TABLE automation_jobs ADD COLUMN {col_name} {col_type} DEFAULT {col_default}")
            conn3c.commit()
    conn3c.close()

    # M3: memory_pool 新增字段迁移 + FTS5 虚拟表
    conn4 = sqlite3.connect(CONFIG.DB_PATH)
    c4 = conn4.cursor()
    c4.execute("PRAGMA table_info(memory_pool)")
    mp_columns = [col[1] for col in c4.fetchall()]
    for col_name, col_type, col_default in [
        ("kind", "TEXT", "'fact'"),
        ("source_session_id", "TEXT", "''"),
        ("confidence", "REAL", "1.0"),
        ("source_type", "TEXT", "'user'"),
        ("updated_at", "TEXT", "NULL"),
    ]:
        if col_name not in mp_columns:
            c4.execute(f"ALTER TABLE memory_pool ADD COLUMN {col_name} {col_type} DEFAULT {col_default}")
            conn4.commit()
    # 确保 FTS5 虚拟表存在
    c4.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_pool_fts USING fts5(
            content, summary, tags,
            content='memory_pool',
            content_rowid='rowid'
        )
    """)
    conn4.commit()
    conn4.close()

    # 记忆版本历史表（回滚支持）
    conn5 = sqlite3.connect(CONFIG.DB_PATH)
    conn5.execute("""
        CREATE TABLE IF NOT EXISTS memory_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            memory_key TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            content TEXT,
            summary TEXT,
            confidence REAL,
            archived_at TEXT DEFAULT (datetime('now')),
            archived_by TEXT
        )
    """)
    conn5.commit()
    conn5.close()

    # Wiki 收件箱审查表
    conn6 = sqlite3.connect(CONFIG.DB_PATH)
    conn6.execute("""
        CREATE TABLE IF NOT EXISTS wiki_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_path TEXT NOT NULL UNIQUE,
            title TEXT,
            status TEXT DEFAULT 'pending',
            source TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT,
            reviewed_by TEXT
        )
    """)
    conn6.commit()

    # K2 实体审查队列（附录 F v1.7，2026-08-06）：LLM 幻觉实体不直接进图谱
    conn6.execute("""
        CREATE TABLE IF NOT EXISTS entity_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL,
            name TEXT NOT NULL,
            entity_type TEXT DEFAULT 'other',
            evidence TEXT,
            level TEXT DEFAULT 'summary',
            status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'llm',
            created_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT,
            reviewed_by TEXT
        )
    """)

    # D6 通用审查队列（2026-08-07，prompt v1.0 拍板）：类型列 wiki/entity/sensitivity
    # 新功能一律用此组件，禁造 inbox 仿制品；entity_review 数据迁移后保留兼容
    conn6.execute("""CREATE TABLE IF NOT EXISTS review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type TEXT NOT NULL DEFAULT 'entity',
        doc_id TEXT NOT NULL,
        name TEXT NOT NULL,
        detail TEXT DEFAULT '',
        level TEXT DEFAULT 'summary',
        status TEXT DEFAULT 'pending',
        source TEXT DEFAULT 'llm',
        created_at TEXT DEFAULT (datetime('now')),
        reviewed_at TEXT,
        reviewed_by TEXT
    )""")

    # S1K scoped API key（2026-08-07）：key_hash 不存明文 + scope 三层模型
    # scope JSON: {"endpoints": [...], "data_domain": [...], "level_cap": "summary"}
    conn6.execute("""CREATE TABLE IF NOT EXISTS agent_keys (
        key_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        scope TEXT DEFAULT '{"endpoints": [], "data_domain": [], "level_cap": ""}',
        status TEXT DEFAULT 'active',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        expires_at TEXT,
        last_used_at TEXT,
        call_count INTEGER DEFAULT 0
    )""")


    # 1e 员工账号（阶段1/2026-08-30）：SMB 无 AD 的入场券，key 独立管理
    # 模板: owner=全域FULL / dept_head=本部门FULL+他部门METADATA / staff=本部门SUMMARY+公共区FULL / external=指定项目METADATA+租约
    conn6.execute("""CREATE TABLE IF NOT EXISTS employee_accounts (
        employee_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        role_template TEXT NOT NULL DEFAULT 'staff',
        department TEXT DEFAULT '',
        project_scope TEXT DEFAULT '',
        key_hash TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now')),
        lease_expires_at TEXT DEFAULT ''
    )""")

    # 阶段2 网关读审计（2026-08-30）：谁/哪把 key/看了什么/给到哪级/何时
    # 1e 身份供给 · 部门目录（2026-09-20）：部门**只做目录与默认值载体** —— 权限判定仍走
    # employee_accounts.department / project_scope（CD-025 读时派生口径不变），刻意不成为
    # 第二真相源。按 name 与员工记录 join（存量员工建部门后自动归位，零数据迁移）；
    # 改名用 PATCH 的 sync_employees 一次性同步员工字段。
    conn6.execute("""CREATE TABLE IF NOT EXISTS departments (
        department_id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        default_role_template TEXT DEFAULT 'staff',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )""")

    # CD-072（2026-09-20）员工凭据账本：每人可多把、各自过期/状态/调用画像，
    # 可按 key_id 单把吊销。形态对齐 agent_keys（key_scopes.py）。明文仍只存 SHA256；
    # employee_accounts.key_hash 保留为「最近一把」镜像（双写）→ 老路径/回滚仍可用。
    conn6.execute("""CREATE TABLE IF NOT EXISTS employee_keys (
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
    )""")

    conn6.execute("""CREATE TABLE IF NOT EXISTS gateway_read_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        requester TEXT NOT NULL,
        auth_mode TEXT DEFAULT '',
        scope_json TEXT DEFAULT '',
        kind TEXT NOT NULL,
        query TEXT DEFAULT '',
        target TEXT DEFAULT '',
        granted_level TEXT DEFAULT '',
        item_count INTEGER DEFAULT 0,
        stripped_chunks INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    # 增量迁移（S3 taint 注入防御，2026-08-05）：memory_pool/shared_docs/wiki_inbox 加 trust 列
    # 必须在所有建表之后执行（shared_docs/wiki_inbox 由本函数先建）
    # 表不存在（如 shared_docs 由 shared_workspace.py 惰性创建）则跳过——避免 ALTER 崩
    _tbls = {r[0] for r in conn6.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for _t in ("memory_pool", "shared_docs"):
        if _t not in _tbls:
            continue
        c6 = conn6.cursor()
        c6.execute(f"PRAGMA table_info({_t})")
        _tcols = {col[1] for col in c6.fetchall()}
        if "trust_level" not in _tcols:
            c6.execute(f"ALTER TABLE {_t} ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'internal'")
        if "source_agent_id" not in _tcols:
            c6.execute(f"ALTER TABLE {_t} ADD COLUMN source_agent_id TEXT DEFAULT ''")
        if "tainted_at" not in _tcols:
            c6.execute(f"ALTER TABLE {_t} ADD COLUMN tainted_at TEXT DEFAULT ''")
    c6 = conn6.cursor()
    c6.execute("PRAGMA table_info(wiki_inbox)")
    _wcols = {col[1] for col in c6.fetchall()}
    if "trust_level" not in _wcols:
        c6.execute("ALTER TABLE wiki_inbox ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'internal'")
    conn6.commit()

    # 增量迁移（O4 按 Agent 配额，2026-08-05）
    conn6.execute(
        """CREATE TABLE IF NOT EXISTS agent_quotas (
            agent_id TEXT PRIMARY KEY,
            qps_limit REAL NOT NULL DEFAULT 50.0,    -- 每秒请求上限
            mode TEXT NOT NULL DEFAULT 'alert_only', -- reject | throttle | alert_only
            window_sec REAL NOT NULL DEFAULT 1.0,
            burst INTEGER NOT NULL DEFAULT 3,
            updated_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    conn6.commit()
    # 增量迁移（H1 文档切割块，2026-08-06）：document_chunks 表已在建表区创建，
    # 此处补幂等保障 + 老库升级路径（表缺失时重建，不重复建列）
    _tbls2 = {r[0] for r in conn6.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "document_chunks" not in _tbls2:
        conn6.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                chunk_id TEXT PRIMARY KEY,
                parent_doc_id TEXT NOT NULL,
                piece_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                summary TEXT,
                source_agent_id TEXT DEFAULT '',
                trust_level TEXT DEFAULT 'trusted',
                tainted_at TEXT,
                disclosure_level TEXT DEFAULT 'summary',
                sensitivity_score REAL DEFAULT 0.0,
                chunk_hash TEXT NOT NULL,
                kind TEXT DEFAULT 'fact',
                pii_hits TEXT DEFAULT '[]',
                created_at TEXT,
                updated_at TEXT
            )
        """)
    conn6.commit()
    # 增量迁移（G1 影子双写崩溃一致性 批1，2026-09-02）：shadow_pending WAL 表。
    # submit 挂钩在业务 commit 之后落 pending（独立连接，不同事务）；
    # flush 成功软标记 done；启动 replay 未 done 且 attempts 未超限的行。
    conn6.execute("""CREATE TABLE IF NOT EXISTS shadow_pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0
    )""")
    conn6.execute("""CREATE INDEX IF NOT EXISTS idx_shadow_pending_status
        ON shadow_pending(status, attempts)""")
    conn6.commit()
    # O3: 记录 schema 版本（迁移成功后）
    try:
        conn6.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn6.commit()
    except Exception as _exc:
        logger.debug("db silent-except(init_db): %s", _exc)
    conn6.close()
    # CD-045 审计 outbox（事务内事件行 → 后台消费者异步落审计链；重启自动 replay）
    conn7 = sqlite3.connect(CONFIG.DB_PATH)
    conn7.execute("""CREATE TABLE IF NOT EXISTS event_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
)""")
    conn7.execute("""CREATE INDEX IF NOT EXISTS idx_event_outbox_status ON event_outbox(status, attempts)""")
    conn7.commit()
    try:
        conn7.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn7.commit()
    except Exception as _exc:
        logger.debug("db silent-except @conn7: %s", _exc)
    conn7.close()
    # CD-084（2026-09-23）死信表：「失败只打日志」路径（自动化连失败停用/通知发送失败/
    # 维护清理异常等）的兜底账本——落行供 /api/v1/maintenance/dead-letters 面板与
    # /metrics 暴露，POST .../retry 按 source 的可行最小语义重试或标记。
    # **必须与 alembic 0011 双侧同步**（CD-060 硬等式门禁要求两侧 = 0 差异）。
    conn7b = sqlite3.connect(CONFIG.DB_PATH)
    conn7b.execute("""CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    failed_at TEXT DEFAULT (datetime('now')),
    retried INTEGER NOT NULL DEFAULT 0,
    retried_at TEXT NOT NULL DEFAULT ''
)""")
    conn7b.execute("CREATE INDEX IF NOT EXISTS idx_dead_letters_retried"
                   " ON dead_letters(retried, failed_at)")
    conn7b.commit()
    conn7b.close()
    # CD-081 最小骨架（KB 评估框架）：评估结果落库层——tools/kb_eval.py 跑完一组
    # 评估 INSERT 一行（dataset/name/recall_at_5/refusal_rate/p95_ms/样本数/
    # 配置 hash/配置快照）。指标列 REAL 可空（dry-run 等只登记场景）。
    # **必须与 alembic 0012 双侧同步**（CD-060 硬等式门禁要求两侧 = 0 差异）。
    conn7c = sqlite3.connect(CONFIG.DB_PATH)
    conn7c.execute("""CREATE TABLE IF NOT EXISTS evaluation_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    recall_at_5 REAL,
    refusal_rate REAL,
    p95_ms REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)""")
    conn7c.execute("CREATE INDEX IF NOT EXISTS idx_evaluation_tasks_dataset"
                   " ON evaluation_tasks(dataset, created_at)")
    conn7c.commit()
    conn7c.close()
    # ═══ CD-024 增长型表索引（2026-09-20）═══
    # 为**随业务增长**的表补索引，支撑代码里实际存在的 WHERE/ORDER BY 模式
    # （改动前逐条 EXPLAIN QUERY PLAN 实测全为 SCAN，见 tests/test_growth_indexes.py）。
    # 不为小表/低频表加。**必须与 alembic 0008 双侧同步**——CD-060 的硬等式门禁
    # （tests/test_schema_hard_equality.py，比对含索引集合与规范化 DDL）要求两侧 = 0 差异。
    conn8 = sqlite3.connect(CONFIG.DB_PATH)
    for _sql in (
        # memory_pool：按属主检索 / owner+key 冲突检测 / 级别过滤 / 清理扫描
        "CREATE INDEX IF NOT EXISTS idx_memory_pool_owner_key ON memory_pool(owner_agent_id, memory_key)",
        "CREATE INDEX IF NOT EXISTS idx_memory_pool_level ON memory_pool(disclosure_level)",
        "CREATE INDEX IF NOT EXISTS idx_memory_pool_updated ON memory_pool(updated_at)",
        # document_chunks：按父文档取块（含 COUNT）/ 级别过滤
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_parent ON document_chunks(parent_doc_id, piece_index)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_level ON document_chunks(disclosure_level)",
        # gateway_read_log：按时间清理 / 读审计查询
        "CREATE INDEX IF NOT EXISTS idx_gateway_read_log_created ON gateway_read_log(created_at)",
        # events：按时间清理 / 按 agent 取事件
        "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, timestamp)",
        # wiki_inbox：按状态计数（控制台/接口两处 COUNT）
        "CREATE INDEX IF NOT EXISTS idx_wiki_inbox_status ON wiki_inbox(status, created_at)",
    ):
        conn8.execute(_sql)
    conn8.commit()
    conn8.close()

    # ============ alembic 版本登记（数据层修复轮，CD-082 后续）============
    # 背景：此前 init_db 建的全新库没有 alembic_version 表 → main.py 启动迁移
    # 检查把「全新库」与「从未登记迁移的老库」一并归入 skipped_no_version_table
    # 永久跳过，此后新增 revision 永远不会作用到这些库。
    # CD-060 已把内联 DDL + 冻结 ALTER 段与 alembic 基线全链对齐
    # （tests/test_schema_hard_equality.py 硬等式门禁 = 0 差异），init_db 完成
    # 时全新库的 schema 即 head 形态 —— 对它登记 head 版本（等价
    # `alembic stamp head`），使「全新库」与「已迁移库」不可区分。
    # 只对全新库登记：已有库（含无 version 表的老库）不猜不动，仍走 main.py
    # 的 skipped_no_version_table 告警口径（行为不变）。
    if _fresh_db:
        try:
            if _stamp_alembic_head(CONFIG.DB_PATH):
                logger.info("[migration] fresh DB stamped to alembic head")
        except Exception as _exc:
            # alembic 不可用/读取失败不阻塞启动——维持旧行为
            # （启动检查按 skipped_no_version_table 跳过并告警）
            logger.warning("alembic head stamp failed (non-fatal): %s", _exc)


_init_guard = os.environ.get("SYNC_HUB_SKIP_MIGRATE_BACKUP", "")
if not _init_guard:
    # O3: 迁移前自动备份（仅当库存在且版本落后时）
    try:
        _cur_ver = 0
        if os.path.exists(CONFIG.DB_PATH):
            _vc = sqlite3.connect(CONFIG.DB_PATH)
            _cur_ver = _vc.execute("PRAGMA user_version").fetchone()[0]
            _vc.close()
        if _cur_ver < SCHEMA_VERSION:
            _bak = _pre_migrate_backup(CONFIG.DB_PATH, os.path.join(os.path.dirname(CONFIG.DB_PATH), "backups"))
            if _bak:
                print(f"[SyncHub] 迁移前备份: {_bak}")
    except Exception:
        pass  # 备份失败不阻塞启动（ALTER 幂等,失败会中止不损坏）
init_db()

# ============ 网络工具 ============

# LAN IP 缓存（2026-08-07 压测基线发现）：/health 每次 UDP connect 8.8.8.8
# 内网环境等超时 1s+ 阻塞事件循环。TTL 缓存 300s。
_LAN_IPS_CACHE = {"ts": 0.0, "result": None}
_LAN_IPS_CACHE_TTL = 300.0


def get_lan_ips() -> dict:
    """获取本机局域网 IP 列表（TTL 300s 缓存，防 /health 每次连外网 8.8.8.8）"""
    import time as _time
    _now = _time.time()
    if _LAN_IPS_CACHE["result"] is not None and _now - _LAN_IPS_CACHE["ts"] < _LAN_IPS_CACHE_TTL:
        return _LAN_IPS_CACHE["result"]
    result = _get_lan_ips_impl()
    _LAN_IPS_CACHE["ts"], _LAN_IPS_CACHE["result"] = _now, result
    return result


def _get_lan_ips_impl() -> dict:
    """获取本机局域网 IP 列表（实际实现）"""
    import socket
    result = {"hostname": socket.gethostname(), "ips": [], "primary": "127.0.0.1"}
    try:
        # 方法1: 连接一个外网地址获取本机出口 IP（通常就是 LAN IP）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        try:
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
            if not primary.startswith("127."):
                result["primary"] = primary
        except Exception as _exc:
            logger.debug("db silent-except(_get_lan_ips_impl): %s", _exc)
        finally:
            s.close()

        # 方法2: 枚举所有网卡
        hostname = socket.gethostname()
        try:
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in result["ips"]:
                    result["ips"].append(ip)
        except Exception as _exc:
            logger.debug("db silent-except(_get_lan_ips_impl): %s", _exc)

        if not result["ips"] and result["primary"] != "127.0.0.1":
            result["ips"] = [result["primary"]]
    except Exception as e:
        logger.warning(f"获取 LAN IP 失败: {e}")

    return result


def _decode_netsh(b: bytes) -> str:
    """netsh 输出编码不稳定（中文系统 GBK/UTF-8 都可能），容错解码"""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


# firewall 检查缓存（2026-08-07 压测基线发现）：/health 每次 spawn 两个 netsh 子进程
# 最坏 15s 阻塞事件循环 -> 并发一高全卡。TTL 缓存 60s，存活探针不再重复 spawn。
_FIREWALL_CACHE = {"ts": 0.0, "result": None}
_FIREWALL_CACHE_TTL = 60.0


def check_windows_firewall(port: int = 3060) -> dict:
    """检查 Windows 防火墙是否放行指定端口（TTL 60s 缓存，防并发压测卡事件循环）"""
    import time as _time
    _now = _time.time()
    if _FIREWALL_CACHE["result"] is not None and _now - _FIREWALL_CACHE["ts"] < _FIREWALL_CACHE_TTL:
        return _FIREWALL_CACHE["result"]
    import subprocess as sp
    if os.name != "nt":
        return {"checked": False, "reason": "非 Windows 系统"}
    try:
        # ⚠️ 必须用 bytes 模式（不要 text=True）：netsh 输出非 UTF-8 中文时，
        # text=True 解码失败会被 subprocess reader 线程吞掉，stdout 变 None，
        # `in None` 抛 TypeError（Electron spawn 环境实测 /health firewall 恒报 NoneType 错误）。
        r = sp.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name=Sync Hub {port}"],
            capture_output=True, timeout=5,
        )
        exists = r.returncode == 0 and "Sync Hub" in _decode_netsh(r.stdout or b"")
        if exists:
            result = {"checked": True, "status": "ok", "rule_exists": True}
            _FIREWALL_CACHE["ts"], _FIREWALL_CACHE["result"] = _now, result
            return result
        # 检查是否有其他规则覆盖了这个端口（netsh 语法必须带 name=all，否则 rc=1 只输出错误消息）
        r2 = sp.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in"],
            capture_output=True, timeout=10,
        )
        text2 = _decode_netsh(r2.stdout or b"")
        # 中文系统「本地端口」/英文「LocalPort」；值为具体端口或「任何/Any」（=放行所有端口）。
        # 用正则而非子串：原 `f"LocalPort.*{port}" in out` 是正则语法当字面匹配，永远 False。
        port_found = bool(re.search(
            r"(?:本地端口|LocalPort)\s*:\s*(?:[^\r\n]*\b" + str(port) + r"\b|任何|Any)", text2
        ))
        result = {"checked": True, "status": "warning" if not port_found else "ok",
                  "rule_exists": False, "port_covered": port_found,
                  "hint": "建议运行: netsh advfirewall firewall add rule name='Sync Hub 3060' dir=in action=allow protocol=TCP localport=3060"}
        _FIREWALL_CACHE["ts"], _FIREWALL_CACHE["result"] = _now, result
        return result
    except Exception as e:
        return {"checked": True, "status": "error", "error": str(e)}



# ============ 数据库初始化 ============



def record_dead_letter(source, kind="", payload=None, error="", db_path=None):
    """CD-084 死信落库：「失败只打日志」路径的兜底账本。

    语义：主流程失败已发生时**追加**一行账本（不改原失败语义，不吞异常），
    供 GET /api/v1/maintenance/dead-letters 面板、/metrics 暴露与
    POST .../dead-letters/{id}/retry 重试。落库自身失败只 debug 日志，
    绝不反过来影响主流程。payload 任意可 JSON 序列化对象；
    超长字段截断（source 200 / kind 80 / payload 2000 / error 500 字符）。
    返回插入行 id；失败返回 None。
    """
    try:
        if payload is None:
            payload_json = ""
        elif isinstance(payload, str):
            payload_json = payload[:2000]
        else:
            payload_json = json.dumps(payload, ensure_ascii=False, default=str)[:2000]
        conn = sqlite3.connect(db_path or CONFIG.DB_PATH)
        try:
            cur = conn.execute(
                "INSERT INTO dead_letters (source, kind, payload_json, error)"
                " VALUES (?, ?, ?, ?)",
                (str(source)[:200], str(kind or "")[:80], payload_json,
                 str(error or "")[:500]))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as _exc:
        logger.debug("db record_dead_letter 落库失败（不阻塞主流程）: %s", _exc)
        return None
