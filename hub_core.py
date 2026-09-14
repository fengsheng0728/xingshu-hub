"""星枢 Sync Hub — 核心引擎（按领域拆分后入口）"""
import logging
logger = logging.getLogger("xingshu.hub_core")

import asyncio
import json
import hashlib
import time
import os
import sqlite3
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import numpy as np
# D-5 3-5a: 向量栈（chromadb）可选化 — 缺失时降级启动，语义检索退化为 SQLite 关键词
try:
    import chromadb
    from chromadb.config import Settings
except ImportError as _exc:
    chromadb = None
    Settings = None
    logger.warning("hub_core 向量栈未安装（chromadb 缺失）: %s — 语义检索降级为 SQLite 关键词；如需向量能力请 pip install -r requirements-vector.txt", _exc)

from deps import CONFIG, AgentRegistration
from fastapi import WebSocket  # D-7: 该注解名原靠通配 import 泄漏，现显式导入
from hub_agent import HubAgent
from notifications import NotificationManager, notifications
from disclosure import DisclosureEngine
from db import row_to_dict
from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong

from hub_mixins.buffer import BufferMixin
from hub_mixins.dashboard import DashboardMixin
from hub_mixins.disclosure_ops import DisclosureOpsMixin
from hub_mixins.ingest import IngestMixin
from hub_mixins.knowledge import KnowledgeMixin
from hub_mixins.maintenance import MaintenanceMixin
from hub_mixins.memory import MemoryMixin
from hub_mixins.notifications import NotificationsMixin
from hub_mixins.tasks import TasksMixin
from hub_mixins.team import TeamMixin

# 向后兼容
_row_dict = row_to_dict

# S3 taint 信任级
TRUST_ORDER = {"system": 4, "internal": 3, "federated": 2, "external": 1}

class SyncHub(BufferMixin, DashboardMixin, DisclosureOpsMixin, IngestMixin, KnowledgeMixin, MaintenanceMixin, MemoryMixin, NotificationsMixin, TasksMixin, TeamMixin):
    """Sync Hub 核心：写入隔离 + 渐进式披露

    典型使用流程（企业客服场景）：
      1. 客服 Agent 小王接待客户 → 写入记忆（仅自己可见）
      2. 主管 Agent 需要查看小王的工作情况 → 通过 Hub 查询，得到摘要
      3. 遇到售后纠纷，主管需要完整对话记录 → 申请提升披露级别
      4. 店长（Orchestrator）调度任务 → 按需向客服披露任务相关信息
    """

    def __init__(self):
        self.agents: Dict[str, dict] = {}
        self.active_ws: Dict[str, WebSocket] = {}
        # L3: dispatch tracker for reconnection replay
        self._pending_dispatches: dict[str, list] = {}
        # L4: backpressure
        self._in_flight: dict[str, int] = {}
        self._MAX_IN_FLIGHT = 8
        # L5: heartbeat — 心跳 30s，PONG 超时 90s（3 个心跳周期未收 pong 判半开）
        self._last_pong: dict[str, float] = {}
        self._HEARTBEAT_INTERVAL = 30
        self._PONG_TIMEOUT = 90
        # Phase 3c: 锁粒度拆分（原全局单锁 _lock 包裹所有阻塞 SQLite 操作）
        # _agents_lock — 保护 self.agents / self.active_ws 内存态
        # _task_lock   — 任务状态机 + 披露阶段流转（tasks.py / disclosure_ops.py）
        # _memory_lock — 记忆写入去重的读-改-写原子性（memory.py store_memory）
        # 锁序约定：互不嵌套；任何路径不得同时持有两把锁（经全调用链核查无嵌套）
        self._agents_lock = asyncio.Lock()
        self._task_lock = asyncio.Lock()
        self._memory_lock = asyncio.Lock()
        # 向后兼容别名：register/heartbeat/cleanup_loop/keepalive 及
        # disclosure.py 虚拟 Agent 注入仍用 self._lock（即 _agents_lock）
        self._lock = self._agents_lock
        self._running = True
        self._start_time = time.time()

        self._write_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._trace_persist_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)  # CD-017: trace 攒批持久化队列
        self._wiki_sync_pending = False
        self._last_wiki_sync = 0.0
        self._WIKI_SYNC_COOLDOWN = 5.0
        self._flush_count = 0
        self._total_flushed = 0
        self._last_flush_at = 0.0
        self._flush_latencies = []
        self._write_trace = []

        # 写入缓冲持久化 replay（WAL commit log 模式）：buffer_log 表恢复 trace/计数
        self._load_buffer_log()

        # Hub 身份（用于 UDP 发现和组队）
        import socket as _socket
        # 稳定 hub_id（hash() 每进程随机化导致跨重启变化，改用 hashlib；加端口区分同机多实例）
        import hashlib as _hashlib
        _h = _hashlib.md5(_socket.gethostname().encode()).hexdigest()[:4]
        _p = getattr(CONFIG, "SERVER_PORT", 3060)
        self.hub_id = f"{_socket.gethostname()}-{_h}:{_p}"
        self.hostname = _socket.gethostname()

        # 加载披露策略配置
        self._disclosure_policy = self._load_disclosure_policy()

        # UDP 发现（LAN 模式启动：广播+多播，daemon 线程）
        # P0 S4（2026-08-04）：支持 static 模式——内网安全设备常拦 UDP 多播，
        # static 模式从配置清单声明联邦节点，不启动 UDP 线程（无广播流量）。
        try:
            from udp_discovery import UDPDiscovery
            cfg_port = getattr(CONFIG, "SERVER_PORT", 3060)
            if getattr(CONFIG, "FEDERATION_DISCOVERY", "multicast") == "static":
                static_peers = getattr(CONFIG, "FEDERATION_STATIC_PEERS", None) or []
                self.discovery = UDPDiscovery(self.hub_id, self.hostname,
                                              getattr(self, "_owner_name", "xingshu"), cfg_port)
                self.discovery.static_peers = list(static_peers)
                self.discovery.mode = "static"
                self.discovery._peers = {}
                # 静态清单直接进 peers（不启动 UDP 线程）
                for p in static_peers:
                    if isinstance(p, dict) and p.get("hub_id"):
                        self.discovery._peers[p["hub_id"]] = {
                            "hub_id": p["hub_id"],
                            "hostname": p.get("hostname", ""),
                            "user_name": p.get("user_name", "xingshu"),
                            "ip": p.get("ip", ""),
                            "port": p.get("port", cfg_port),
                            "_last_seen": time.time(),
                        }
            else:
                self.discovery = UDPDiscovery(self.hub_id, self.hostname,
                                              getattr(self, "_owner_name", "xingshu"), cfg_port)
                self.discovery.mode = "multicast"
                self.discovery.start()
        except Exception:
            self.discovery = None

        # 渐进式披露引擎（从 disclosure.py 拆分）
        self.disclosure = DisclosureEngine(self)

        # Hub Agent 占位（在模块底部初始化后注入 self.hub_agent）
        self.hub_agent = None

        # Phase 3.1: ChromaDB 语义搜索集成
        # 注意: Windows 上 ChromaDB PersistentClient 可能因文件锁冲突失败
        # (已知 issue: chromadb 的 SQLite 在多进程访问时可能 filelock)
        # 这里用 try/except 兜底，失败时降级为纯 SQLite 搜索
        if chromadb is None:
            # D-5 3-5a: 向量栈未安装 → 不进 PersistentClient，直接走既有 None 降级路径
            #（消费点 disclosure.py / hub_mixins/* 均已判 _chroma_collection is None）
            logger.warning("hub_core chromadb 未安装，跳过 ChromaDB 初始化（语义检索降级为 SQLite 关键词；pip install -r requirements-vector.txt 可恢复）")
            self._chroma_client = None
            self._chroma_collection = None
            self._embedding_model = None
        else:
            try:
                print(f"[SyncHub] 初始化 ChromaDB（路径: {CONFIG.CHROMA_PATH}）...")
                self._chroma_client = chromadb.PersistentClient(
                    path=CONFIG.CHROMA_PATH,
                    settings=Settings(anonymized_telemetry=False)
                )
                self._chroma_collection = self._chroma_client.get_or_create_collection(
                    name=CONFIG.CHROMA_COLLECTION,
                    metadata={"hnsw:space": "cosine"}
                )
                print(f"[SyncHub] ChromaDB 集合已就绪: {CONFIG.CHROMA_COLLECTION}")

                # 3-7(2026-09-10): 集合向量数水位告警 — 只告警，不改向量写入与检索逻辑
                try:
                    _vcount = self._chroma_collection.count()
                    _vmax = getattr(CONFIG, "CHROMA_MAX_VECTORS", 50000)
                    if _vcount > _vmax:
                        logger.warning(
                            "ChromaDB 集合 %s 向量数 %d 超过水位阈值 %d，请评估清理或扩容",
                            CONFIG.CHROMA_COLLECTION, _vcount, _vmax)
                except Exception as _exc:
                    # count() 失败不得让建集合失败（沿用异常底线风格）
                    logger.warning("ChromaDB 集合计数失败（不影响启动）: %s", _exc)

                # 初始化 sentence-transformers 模型（延迟加载，避免启动阻塞）
                self._embedding_model = None
                self._embedding_lock = asyncio.Lock()
            except OSError as e:
                # Windows 文件锁问题常见于多实例或残留锁文件
                print(f"[SyncHub] ChromaDB 文件锁错误 (Windows 常见): {e}")
                print(f"[SyncHub] 将跳过语义搜索功能，使用 SQLite LIKE 匹配替代")
                self._chroma_client = None
                self._chroma_collection = None
                self._embedding_model = None
            except Exception as e:
                print(f"[SyncHub] ChromaDB 初始化失败: {e}，将跳过语义搜索功能")
                self._chroma_client = None
                self._chroma_collection = None
                self._embedding_model = None

        # 阶段3-P0/P1: 主干-分干数据底座（影子模式，静默初始化，D4 失败不影响主链路）
        self.data_trunk = None
        self._shadow = None
        if getattr(CONFIG, "DATA_TRUNK_ENABLED", False):
            try:
                from data_trunk import DataTrunk
                self.data_trunk = DataTrunk(CONFIG)
                agents_rows = None
                try:
                    import sqlite3 as _sq
                    _conn = _sq.connect(CONFIG.DB_PATH)
                    _conn.row_factory = _sq.Row
                    agents_rows = [dict(r) for r in _conn.execute(
                        "SELECT agent_id, agent_name, role, department, api_key,"
                        " registered_at FROM agents")]
                    _conn.close()
                except Exception:
                    pass  # agents 表未就绪时只建结构
                self.data_trunk.ensure(agents_rows=agents_rows)
                # P1: 影子双写器（SQLite 落库后异步镜像 git 仓库群）
                # P2 交付4: audit_db_path 传入审计库——每批 commit 追加链头互证
                from hub_mixins.shadow import ShadowWriter
                self._shadow = ShadowWriter(self.data_trunk,
                                            audit_db_path=CONFIG.DB_PATH)
                self._shadow.start()
            except Exception:
                print("[SyncHub] data-trunk 初始化失败（影子模式降级，不影响主链路）")


    async def _ensure_embedding_model(self):
        """延迟加载 sentence-transformers 模型（避免启动阻塞）"""
        if self._chroma_client is None:
            return None
        if self._embedding_model is None:
            if getattr(self, '_embedding_load_failed', False):
                return None
            async with self._embedding_lock:
                if self._embedding_model is not None:
                    return self._embedding_model
                try:
                    print(f"[SyncHub] 初始化本地 embedding 模型...")
                    loop = asyncio.get_event_loop()
                    self._embedding_model = await loop.run_in_executor(
                        None, self._load_embedding_model_sync
                    )
                    print(f"[SyncHub] embedding 模型加载完成")
                except Exception as e:
                    print(f"[SyncHub] embedding 模型加载失败: {e}，跳过语义搜索")
                    self._embedding_load_failed = True
                    return None
        return self._embedding_model

    @staticmethod


    def _load_embedding_model_sync():
        """同步初始化本地 embedding 模型（K1 provider 抽象，附录 F 2026-08-06）。

        按 CONFIG.EMBEDDING_PROVIDER 选型：
          hasher   — 词袋（默认，零依赖）
          sentence — 真语义模型（bge-small-zh / bge-m3 / MiniLM，本地 model_path 加载）
        sentence 加载失败（缺模型文件/缺依赖）→ 抛异常，调用方降级 hasher。
        """
        from db import get_embedding_provider
        provider = CONFIG.EMBEDDING_PROVIDER or "hasher"
        if provider == "sentence":
            return get_embedding_provider(
                "sentence",
                model_path=CONFIG.EMBEDDING_MODEL_PATH,
            )
        return get_embedding_provider("hasher", n_features=384)

    @staticmethod


    def _load_disclosure_policy() -> dict:
        """从 config.yaml 加载披露策略，失败时返回默认值"""
        default = {
            "department_peer_visibility": False,
            "default_manager_level": "summary",
            "orchestrator_max_level": "full",
            "allow_peer_disclosure": True,
        }
        try:
            import yaml, os
            config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
            config_path = os.path.join(config_dir, "config.yaml")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                disclosure_cfg = cfg.get("disclosure", {})
                # 合并：用户配置覆盖默认值
                for k, v in disclosure_cfg.items():
                    if k in default:
                        default[k] = v
                print(f"[SyncHub] 披露策略已加载: {default}")
        except Exception as e:
            print(f"[SyncHub] 加载披露策略失败 ({e})，使用默认值")
        # S7：死配置已删除，披露 FULL 无审批门；如需审批门另立需求
        # 剥离老 config.yaml 中可能残留的键，防止「配置存在=有保护」错觉
        default.pop("require_approval_for_full", None)
        return default


    async def _restore_agents(self):
        """从 SQLite 恢复 Agent 字典（P1 持久化修复）"""
        conn = self._db()
        c = conn.cursor()
        c.execute("SELECT * FROM agents")
        rows = c.fetchall()
        conn.close()
        for row in rows:
            agent_dict = _row_dict(row)
            agent_id = agent_dict["agent_id"]
            self.agents[agent_id] = {
                "agent_id": agent_id,
                "agent_name": agent_dict.get("agent_name", ""),
                "department": agent_dict.get("department", ""),
                "capabilities": json.loads(agent_dict.get("capabilities") or "[]"),
                "role": agent_dict.get("role", "worker"),
                "managed_agents": json.loads(agent_dict.get("managed_agents") or "[]"),
                "disclosure_policy": json.loads(agent_dict.get("disclosure_policy") or "{}"),
                "endpoint": agent_dict.get("endpoint", ""),
                "status": "offline",  # 恢复后默认离线，等心跳激活
                "last_heartbeat": agent_dict.get("last_heartbeat", ""),
                "api_key": agent_dict.get("api_key", ""),
            }
        # P1 修复：同步 DB 里的 status 为 offline（问题3修复）
        if rows:
            conn2 = self._db()
            c2 = conn2.cursor()
            c2.execute("UPDATE agents SET status = 'offline'")
            conn2.commit()
            conn2.close()
            print(f"[SyncHub] 已从数据库恢复 {len(rows)} 个 Agent，status 已同步为 offline")


    def _db(self):
        conn = sqlite3.connect(CONFIG.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return _DbCtx(conn)

    # ============ Agent 管理 ============

    @staticmethod


    def _merge_trust(old_trust: str, new_trust: str) -> str:
        """S3: 信任降级规则 — 取较脏者（数值小 = 更不信任）。
        覆盖/合并时新内容混入 → trust 只能降不能升（防 taint 被更新洗掉）。"""
        o = TRUST_ORDER.get(old_trust or "internal", 3)
        n = TRUST_ORDER.get(new_trust or "internal", 3)
        return old_trust if o <= n else new_trust

    @staticmethod


    def _trust_from_source(source_type: str) -> str:
        """S3: 按来源推断默认信任级。tool 输出 = external（文件/shell 读取的不可信内容）。"""
        return "external" if source_type == "tool" else "internal"

    @staticmethod


    def _api_key_expiry(now_iso: str) -> str:
        """S1：api_key 过期时间 = now + API_KEY_ROTATION_DAYS（0=不轮换则永不过期）。"""
        days = getattr(CONFIG, "API_KEY_ROTATION_DAYS", 90)
        if not days or days <= 0:
            return ""
        try:
            from datetime import datetime, timedelta
            dt = datetime.fromisoformat(now_iso)
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt + timedelta(days=days)).isoformat()
        except Exception:
            return ""


    async def register(self, agent: AgentRegistration) -> dict:
        """注册 Agent，登记角色、部门、能力

        T1-2（2026-09-09）：已迁移库（alembic 0003，api_key_hash 列存在）只存
        SHA256 hash——明文 api_key 仅首次创建时在响应回显一次；已存在 agent 的
        重引导/重注册响应 api_key 为空串（Agent 本地已持有 key，出示即证明，
        见 routes_agents._check_reregister_credential）。未迁移老库保持旧明文行为
        （兼容窗口，迁移后明文列由 0003 清空）。
        """
        async with self._lock:
            now = datetime.now(timezone.utc).isoformat()

            # 检查是否已存在，已存在则更新而非覆盖 api_key / role
            conn_check = self._db()
            cc = conn_check.cursor()
            hashed = "api_key_hash" in {
                r[1] for r in cc.execute("PRAGMA table_info(agents)")}
            if hashed:
                cc.execute("SELECT api_key_hash, role FROM agents WHERE agent_id = ?",
                           (agent.agent_id,))
            else:
                cc.execute("SELECT api_key, role FROM agents WHERE agent_id = ?",
                           (agent.agent_id,))
            existing = cc.fetchone()
            # OGA: guarded 受管注册 — 未预签发的 agent_id 一律拒绝且不建号(防匿名自注册)
            if CONFIG.AUTH_REGISTRATION == "guarded" and not existing:
                conn_check.close()
                return {"status": "error", "code": 403,
                        "detail": f"registration guarded: agent '{agent.agent_id}' 未预签发, 请联系管理员用 hub-cli agent create 建号"}
            existing_cred = ""
            if existing:
                existing_cred = (existing["api_key_hash"] if hashed
                                 else existing["api_key"]) or ""
            first_issue = not existing_cred
            if first_issue:
                # 首注签发：明文仅此一次（响应回显）；hash 模式库内只落 hash
                issued_key = secrets.token_urlsafe(32)
                key_hash = (hashlib.sha256(issued_key.encode("utf-8")).hexdigest()
                            if hashed else "")
            else:
                # 重引导/重注册：hash 模式不回吐明文（Agent 本地已有 key）
                issued_key = "" if hashed else existing_cred
                key_hash = existing_cred if hashed else ""
            # CD-020：已注册 agent 的角色真相在服务端——register/bootstrap（连接初始化）
            # 不得改动既有 role（原实现 INSERT OR REPLACE 用请求 role 覆盖，
            # Agent 端 bootstrap 写死 worker 会把 manager/orchestrator 静默降级，
            # 知识写入变 403 且无任何「角色被重置」提示）。首次注册才采信请求 role。
            role = (existing["role"] if existing and existing["role"] else "") or agent.role
            conn_check.close()

            self.agents[agent.agent_id] = {
                "agent_id": agent.agent_id,
                "agent_name": agent.agent_name,
                "department": agent.department,
                "capabilities": agent.capabilities,
                "role": role,
                "managed_agents": agent.managed_agents,
                "status": "online",
                "last_heartbeat": now,
                # T1-2：hash 模式内存 dict 不持明文（backfeed 指纹等消费方退化为空）
                "api_key": "" if hashed else issued_key,
            }

            conn = self._db()
            c = conn.cursor()
            if hashed:
                c.execute(
                    """
                    INSERT OR REPLACE INTO agents
                    (agent_id, agent_name, department, capabilities, role,
                     managed_agents, disclosure_policy, endpoint, registered_at,
                     last_heartbeat, status, api_key, api_key_hash,
                     api_key_created_at, api_key_expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent.agent_id,
                        agent.agent_name,
                        agent.department,
                        json.dumps(agent.capabilities),
                        role,
                        json.dumps(agent.managed_agents),
                        json.dumps(agent.disclosure_policy),
                        agent.endpoint,
                        now,
                        now,
                        "online",
                        "",  # T1-2：明文列保持清空
                        key_hash,
                        now,
                        self._api_key_expiry(now),
                    ),
                )
            else:
                c.execute(
                    """
                    INSERT OR REPLACE INTO agents
                    (agent_id, agent_name, department, capabilities, role,
                     managed_agents, disclosure_policy, endpoint, registered_at,
                     last_heartbeat, status, api_key, api_key_created_at, api_key_expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent.agent_id,
                        agent.agent_name,
                        agent.department,
                        json.dumps(agent.capabilities),
                        role,
                        json.dumps(agent.managed_agents),
                        json.dumps(agent.disclosure_policy),
                        agent.endpoint,
                        now,
                        now,
                        "online",
                        issued_key,
                        now,
                        self._api_key_expiry(now),
                    ),
                )
            conn.commit()
            conn.close()

            # O4：默认配额行（alert_only 零影响；INSERT OR IGNORE 不覆盖已有配置）
            try:
                qc = conn.cursor()
                qc.execute(
                    "INSERT OR IGNORE INTO agent_quotas (agent_id, qps_limit, mode, window_sec, burst) VALUES (?, ?, ?, ?, ?)",
                    (agent.agent_id, 50.0, "alert_only", 1.0, 3))
                conn.commit()
            except Exception:
                pass  # 表不存在(旧库) → 跳过,配额检查默认放行

            await self._log_event("agent_register", agent.agent_id, {
                "role": agent.role, "department": agent.department
            })
            return {"status": "registered", "agent_id": agent.agent_id,
                    "api_key": issued_key}

    # ============ 记忆写入（不广播） ============


    def get_memories(self, agent_id: str, kind: str = "") -> dict:
        """M3: 按 kind 拉取记忆"""
        conn = self._db()
        c = conn.cursor()
        if kind:
            kinds = [k.strip() for k in kind.split(",") if k.strip()]
            placeholders = ",".join(["?"] * len(kinds))
            c.execute(
                f"""SELECT memory_id, memory_key, content, summary, kind, confidence,
                   source_type, access_count, created_at, updated_at
                   FROM memory_pool
                   WHERE owner_agent_id = ? AND kind IN ({placeholders})
                   ORDER BY access_count DESC, created_at DESC""",
                [agent_id] + kinds,
            )
        else:
            c.execute(
                """SELECT memory_id, memory_key, content, summary, kind, confidence,
                   source_type, access_count, created_at, updated_at
                   FROM memory_pool
                   WHERE owner_agent_id = ?
                   ORDER BY access_count DESC, created_at DESC""",
                (agent_id,),
            )
        rows = c.fetchall()
        conn.close()

        memories = []
        for row in rows:
            memories.append({
                "memory_id": row[0], "memory_key": row[1],
                "content": row[2], "summary": row[3],
                "kind": row[4], "confidence": row[5],
                "source_type": row[6], "access_count": row[7],
                "created_at": row[8], "updated_at": row[9],
            })

        return {"memories": memories, "total": len(memories)}


    # ============ 渐进式披露引擎 ============


    def _match_query(self, memory: dict, query: str) -> tuple:
        """三档匹配 — 委托给 DisclosureEngine"""
        return self.disclosure._match_query(memory, query)


    def _agent_from_db_row(self, agent_dict: dict) -> dict:
        """DB 行(dict 化后) → hub.agents 条目(身份字段)，status/last_heartbeat 由调用方定。
        XS-002（2026-09-08）：心跳两条路径统一从 DB 装载/刷新身份字段，
        DB 为单真相源，dict 过期窗口 ≤ 心跳周期。"""
        return {
            "agent_id": agent_dict.get("agent_id", ""),
            "agent_name": agent_dict.get("agent_name", ""),
            "department": agent_dict.get("department", ""),
            "capabilities": json.loads(agent_dict.get("capabilities") or "[]"),
            "role": agent_dict.get("role", "worker"),
            "managed_agents": json.loads(agent_dict.get("managed_agents") or "[]"),
            "disclosure_policy": json.loads(agent_dict.get("disclosure_policy") or "{}"),
            "endpoint": agent_dict.get("endpoint", ""),
            "api_key": agent_dict.get("api_key", ""),
        }

    async def heartbeat(self, agent_id: str) -> dict:
        async with self._lock:
            # P1 修复：未知 Agent 先查 SQLite，存在则重建到内存
            if agent_id not in self.agents:
                conn = self._db()
                c = conn.cursor()
                c.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
                row = c.fetchone()
                if row:
                    agent_dict = _row_dict(row)
                    self.agents[agent_id] = self._agent_from_db_row(agent_dict)
                    self.agents[agent_id]["status"] = "online"
                    self.agents[agent_id]["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
                    conn.close()
                    # P2B: 从 DB 恢复的 Agent 上线通知
                    await notifications.notify(agent_id, {
                        "type": "agent_online",
                        "agent_id": agent_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    await notifications.broadcast_dashboard({
                        "type": "agent_online",
                        "agent_id": agent_id,
                    })
                    return {"status": "ok", "restored_from_db": True}
                conn.close()
                return {"status": "unknown"}

            now = datetime.now(timezone.utc).isoformat()
            was_offline = self.agents[agent_id].get("status") == "offline"
            conn = self._db()
            c = conn.cursor()
            # XS-002（2026-09-08）：dict 命中分支也全量重载身份字段（DB 为单真相源，
            # 直改 DB 的 role/department/managed_agents/disclosure_policy 下一个心跳即生效，
            # dict 过期窗口 ≤ 心跳周期）；DB 行不存在时保持现状仅心跳，不踢出 dict 避免误伤。
            c.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
            row = c.fetchone()
            if row:
                self.agents[agent_id].update(self._agent_from_db_row(_row_dict(row)))
            self.agents[agent_id]["last_heartbeat"] = now
            self.agents[agent_id]["status"] = "online"
            # P2B: 从离线恢复的上线通知
            if was_offline:
                await notifications.notify(agent_id, {
                    "type": "agent_online",
                    "agent_id": agent_id,
                    "timestamp": now,
                })
                await notifications.broadcast_dashboard({
                    "type": "agent_online",
                    "agent_id": agent_id,
                })
            c.execute(
                "UPDATE agents SET last_heartbeat = ?, status = ? WHERE agent_id = ?",
                (now, "online", agent_id),
            )
            conn.commit()
            conn.close()
            return {"status": "ok"}


    async def _log_event(self, event_type: str, agent_id: str, payload: dict):
        # P1 O1：审计事件携带 trace_id（贯穿请求→规则判定→审计链路）
        from logfmt import get_trace_id
        tid = get_trace_id()
        if tid:
            payload = dict(payload)
            payload.setdefault("trace_id", tid)
        conn = self._db()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO events (event_type, agent_id, payload, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (
                event_type,
                agent_id,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        _event_rowid = c.lastrowid
        conn.commit()
        conn.close()
        # S2：事件双写 audit_log 主链（不可抵赖；失败静默不阻塞业务）
        try:
            from audit_chain import AuditChain
            AuditChain(CONFIG.DB_PATH).append(
                "event", "events", str(_event_rowid),
                {"event_type": event_type, "agent_id": agent_id, "payload": payload, "trace_id": tid})
        except Exception as _exc:
            logger.warning("hub_core silent-except @651: %s", _exc)


    async def _log_disclosure(self, **kwargs):
        """披露审计日志 — 委托给 DisclosureEngine"""
        await self.disclosure._log_disclosure(**kwargs)

    # ============ 通知系统（P7） ============


    def track_dispatch(self, session_id: str, envelope: dict):
        self._pending_dispatches.setdefault(session_id, []).append(envelope)


    def ack_dispatch(self, session_id: str, dispatch_id: str):
        if session_id in self._pending_dispatches:
            self._pending_dispatches[session_id] = [
                d for d in self._pending_dispatches[session_id] if d['id'] != dispatch_id
            ]


    def get_pending_dispatches(self, session_id: str, since_checkpoint_id: str = '') -> list:
        pending = self._pending_dispatches.get(session_id, [])
        return list(pending)


    def check_in_flight(self, agent_id: str) -> bool:
        return self._in_flight.get(agent_id, 0) < self._MAX_IN_FLIGHT


    def inc_in_flight(self, agent_id: str):
        self._in_flight[agent_id] = self._in_flight.get(agent_id, 0) + 1


    def dec_in_flight(self, agent_id: str):
        n = self._in_flight.get(agent_id, 0)
        if n > 0: self._in_flight[agent_id] = n - 1


    def record_pong(self, agent_id: str):
        import time as _t
        now_ts = _t.time()
        self._last_pong[agent_id] = now_ts
        # P0-FIX: 同步更新 last_heartbeat，防止后台任务误判离线
        if agent_id in self.agents:
            now = datetime.now(timezone.utc).isoformat()
            self.agents[agent_id]["last_heartbeat"] = now
            self.agents[agent_id]["status"] = "online"
            # 同步写 DB
            try:
                conn = self._db()
                c = conn.cursor()
                c.execute("UPDATE agents SET last_heartbeat = ?, status = ? WHERE agent_id = ?", (now, "online", agent_id))
                conn.commit()
                conn.close()
            except Exception as _exc:
                logger.debug("hub_core silent-except @707: %s", _exc)


    def is_agent_timed_out(self, agent_id: str) -> bool:
        import time as _t
        last = self._last_pong.get(agent_id, _t.time())
        return (_t.time() - last) > self._PONG_TIMEOUT


    def _persist_trace_batch(self, batch: list):
        """批量 INSERT buffer_log（单事务）— 线程池执行"""
        try:
            conn = self._db()
            c = conn.cursor()
            c.execute("BEGIN")
            for action, agent_id, title, entry_id, queued_at in batch:
                c.execute(
                    "INSERT INTO buffer_log (action, agent_id, title, entry_id, queued_at) VALUES (?,?,?,?,?)",
                    (action, agent_id, title, entry_id, queued_at))
            conn.commit()
            conn.close()
        except Exception:
            pass  # 持久化失败不阻塞入队


    async def archive_session(self, agent_id: str, local_session_id: int,
                              title: str = "", summary: str = "",
                              key_facts: list = None, msg_count: int = 0) -> dict:
        """归档 Agent 会话摘要到 Hub。
        使用 UPSERT：同 agent+session 组合只保留最新版本。
        """
        if key_facts is None:
            key_facts = []
        now = datetime.now(timezone.utc).isoformat()
        facts_json = json.dumps(key_facts, ensure_ascii=False)

        conn = self._db()
        c = conn.cursor()
        c.execute(
            """INSERT INTO session_archives (agent_id, local_session_id, title, summary,
               key_facts, msg_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_id, local_session_id)
               DO UPDATE SET title=excluded.title, summary=excluded.summary,
                  key_facts=excluded.key_facts, msg_count=excluded.msg_count,
                  updated_at=excluded.updated_at""",
            (agent_id, local_session_id, title, summary, facts_json, msg_count, now, now),
        )
        conn.commit()
        conn.close()
        return {"status": "ok", "archived": True}


    async def get_recent_sessions(self, agent_id: str, limit: int = 5) -> list:
        """获取 Agent 的近期会话摘要列表"""
        conn = self._db()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            """SELECT agent_id, local_session_id, title, summary, key_facts, msg_count,
               created_at, updated_at
               FROM session_archives WHERE agent_id = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (agent_id, limit),
        )
        rows = c.fetchall()
        conn.close()
        return [
            {
                "agent_id": r["agent_id"],
                "local_session_id": r["local_session_id"],
                "title": r["title"],
                "summary": r["summary"],
                "key_facts": json.loads(r["key_facts"] or "[]"),
                "msg_count": r["msg_count"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]


    # ============ 阶段4-B1 反哺归并人工队列（review_queue item_type='backfeed_merge'） ============
    # fail-closed 红线：以下方法不被任何现有流程调用（data_trunk.backfeed.enabled=false 语义），
    # 仅测试直连；真正的去重/合并执行器在 B2+ 批次接线（docs/phase4-backfeed-design.md §6）。
    # 复用 D6 review_queue 通用表与 N1 审批模式（role 门 manager/orchestrator、409 幂等、审计落链）。

    async def queue_backfeed_merge(self, doc_id: str, name: str, detail: dict,
                                   level: str = "summary") -> dict:
        """反哺归并候选入人工队列（item_type='backfeed_merge'，source='backfeed'）。

        doc_id: 候选 canonical_id（或 pair:<idA>:<idB> 未建档案时）；
        detail: {pair: [{branch,id,path,excerpt}], cos, suggested, reasons}（设计 §4）。
        """
        detail = detail or {}
        conn = self._db()
        c = conn.cursor()
        c.execute(
            """INSERT INTO review_queue
               (item_type, doc_id, name, detail, level, status, source)
               VALUES ('backfeed_merge', ?, ?, ?, ?, 'pending', 'backfeed')""",
            (doc_id or "", (name or "")[:200],
             json.dumps(detail, ensure_ascii=False)[:2000],
             level if level in ("full", "summary", "none") else "summary"),
        )
        conn.commit()
        qid = c.lastrowid
        conn.close()
        await self._log_event("backfeed_merge_queued", "", {
            "queue_id": qid, "doc_id": doc_id, "cos": detail.get("cos"),
        })
        return {"status": "queued", "queue_id": qid}

    async def _decide_backfeed_merge(self, queue_id: int, decision: str,
                                     reviewer: str = "") -> dict:
        """审批状态机（approved/rejected）。role 门 fail-closed：reviewer 不在
        self.agents 或非 manager/orchestrator 一律拒绝。409 幂等：已处理的
        重复审批不改状态，返回已有结果（code=409 + existing_status）。"""
        info = self.agents.get(reviewer) or {}
        if info.get("role") not in ("manager", "orchestrator"):
            return {"status": "error", "code": 403,
                    "detail": "仅 manager/orchestrator 可审批反哺归并"}
        conn = self._db()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM review_queue WHERE id = ? AND item_type = 'backfeed_merge'",
            (queue_id,),
        ).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "code": 404, "detail": "审批项不存在"}
        if row["status"] != "pending":
            # 409 幂等：重复审批同 id 返回已有结果，不翻转状态、不改 reviewed_by
            result = {"status": "already_processed", "code": 409,
                      "detail": f"已处理（{row['status']}）",
                      "queue_id": queue_id, "existing_status": row["status"],
                      "reviewed_by": row["reviewed_by"] or ""}
            conn.close()
            return result
        conn.execute(
            "UPDATE review_queue SET status=?, reviewed_at=?, reviewed_by=? WHERE id=?",
            (decision, datetime.now(timezone.utc).isoformat(), reviewer, queue_id),
        )
        conn.commit()
        try:
            detail = json.loads(row["detail"] or "{}")
        except Exception:
            detail = {}
        conn.close()
        # 审批动作审计落链（events + audit_log 双写，同 review_entity 模式）
        await self._log_event("backfeed_merge_reviewed", reviewer, {
            "queue_id": queue_id, "decision": decision,
            "canonical_id": row["doc_id"], "cos": detail.get("cos"),
        })
        # B3 接线：approved → 真正执行合并。条件化——detail 含执行材料(kind+pair)
        # 且环境就绪(_shadow 已挂 + backfeed enabled)才执行；骨架环境(B1 测试/直连
        # 无 data_trunk)保持原行为只翻状态+审计, 返回结构不变。
        # 执行失败不翻转审批状态(审批决定不可逆)，记审计事件 + execute 字段供处置。
        execute = None
        kind = (detail or {}).get("kind") or ""
        pair = (detail or {}).get("pair") or []
        if (decision == "approved" and kind and pair
                and self._backfeed_ready()
                and getattr(self, "_shadow", None) is not None):
            try:
                ids = [p.get("id") for p in pair if p.get("id")]
                srcs = self._shadow.resolve_sources(kind, ids) if ids else []
                if len(srcs) >= 2:
                    execute = await self.backfeed_execute_merge(
                        kind, [dict(s) for s in srcs], srcs[0].get("body", ""),
                        action="manual_merge", actor=reviewer,
                        queue_id=queue_id, cos=float(detail.get("cos") or 0.0))
                    if execute.get("status") != "merged":
                        await self._log_event("backfeed_merge_exec_failed", reviewer, {
                            "queue_id": queue_id, "reason": execute.get("detail", ""),
                            "status": execute.get("status", "")})
                else:
                    execute = {"status": "error", "detail": "来源解析不足(可能已合并/缺失)"}
                    await self._log_event("backfeed_merge_exec_failed", reviewer, {
                        "queue_id": queue_id, "reason": execute["detail"]})
            except Exception as e:
                execute = {"status": "error", "detail": str(e)}
                try:
                    await self._log_event("backfeed_merge_exec_failed", reviewer,
                                          {"queue_id": queue_id, "reason": str(e)})
                except Exception:
                    pass
        result = {"status": decision, "queue_id": queue_id}
        if execute is not None:
            result["execute"] = execute
        return result

    async def approve_backfeed_merge(self, queue_id: int, reviewer: str = "") -> dict:
        """批准归并。B1 骨架只翻转队列状态 + 审计落链；合并执行器 B2 接线。"""
        return await self._decide_backfeed_merge(queue_id, "approved", reviewer)

    async def reject_backfeed_merge(self, queue_id: int, reviewer: str = "") -> dict:
        """拒绝归并（保守 = 不合，各来源保留独立档案）。"""
        return await self._decide_backfeed_merge(queue_id, "rejected", reviewer)

    # ============ 阶段4-B2 反哺精确去重合并（chunk_hash 档） ============
    # 执行器在 hub_mixins/shadow.py（ShadowWriter.execute_merge / undo_merge /
    # scan_and_merge）；本域负责 fail-closed 判定 + audit 双写
    # （_log_event → events + audit_log，hub_core.py:538-569 既有双写模式）。
    # fail-closed 红线：data_trunk.enabled=false 或 data_trunk.backfeed.enabled
    # 缺省 → 全部 no-op 返回 {"enabled": False}（与 DATA_TRUNK_ENABLED 语义一致）。

    def _backfeed_ready(self) -> bool:
        dt = getattr(self, "data_trunk", None)
        sh = getattr(self, "_shadow", None)
        if not dt or not getattr(dt, "enabled", False) or sh is None:
            return False
        return bool((getattr(dt, "backfeed", None) or {}).get("enabled", False))

    async def backfeed_execute_merge(self, kind: str, sources: list, content: str,
                                     action: str = "manual_merge", actor: str = "system",
                                     queue_id=None, cos: float = 1.0) -> dict:
        """合并执行入口（人工批准/测试直连用；自动档走 backfeed_scan_and_merge）。
        审计落链：合并成功 → _log_event("backfeed_merge", ...) 双写 events + audit_log。"""
        if not self._backfeed_ready():
            return {"enabled": False}
        # identity 指纹：取首个在册来源属主的 api_key → key_fingerprint（绝不落明文 key）
        fp = ""
        try:
            from data_trunk import key_fingerprint
            for s in sources:
                info = self.agents.get(s.get("owner") or "") or {}
                if info.get("api_key"):
                    fp = key_fingerprint(info["api_key"])
                    break
        except Exception:
            fp = ""
        r = self._shadow.execute_merge(kind, sources, content, action=action,
                                       actor=actor, queue_id=queue_id, cos=cos,
                                       owner_key_fp=fp)
        if r.get("status") == "merged":
            try:
                await self._log_event("backfeed_merge", actor,
                                      r.get("audit") or {"canonical_id": r.get("canonical_id")})
            except Exception:
                pass  # 审计失败不阻塞合并结果（D4）
        return r

    async def backfeed_undo_merge(self, canonical_id: str, actor: str = "system") -> dict:
        """回滚入口：归档移回原路径 + index 追加 unmerged 修正行，审计落链。"""
        if not self._backfeed_ready():
            return {"enabled": False}
        r = self._shadow.undo_merge(canonical_id, actor=actor)
        if r.get("status") == "unmerged":
            try:
                await self._log_event("backfeed_unmerge", actor,
                                      r.get("audit") or {"canonical_id": canonical_id})
            except Exception as _exc:
                logger.warning("hub_core silent-except @960: %s", _exc)
        return r

    async def backfeed_scan_and_merge(self, kinds=None, dry_run: bool = False,
                                      min_age_sec=None) -> dict:
        """★ 自动合并扫描入口（e2e 装置对接此方法）★

        判定：主干 index/<kind>.jsonl 条目对，内容取分干 vault md 正文，
        chunk_hash 相等 → 自动合并（ShadowWriter.execute_merge，§2.3 动作序列）。
        - kinds: None = 全部（memory/knowledge/wiki/shared）
        - dry_run: True 只报告不写
        - min_age_sec: 不可合并窗口秒数，None = 默认 60s（§2.3-3）
        返回 {"enabled": True, "scanned": n, "merged": k, "merges": [...], ...}；
        fail-closed 未启用 → {"enabled": False}。
        """
        if not self._backfeed_ready():
            return {"enabled": False}
        r = self._shadow.scan_and_merge(kinds=kinds, dry_run=dry_run,
                                        min_age_sec=min_age_sec, actor="system")
        for m in r.get("merges") or []:
            if m.get("status") == "merged":
                try:
                    await self._log_event("backfeed_merge", "system",
                                          m.get("audit") or {})
                except Exception:
                    pass  # 审计失败不阻塞扫描结果（D4）
        return r

    async def backfeed_scan_cos_and_merge(self, kinds=None, dry_run: bool = False,
                                          min_age_sec: float = None,
                                          cos_auto_merge: float = None,
                                          cos_review_floor: float = None,
                                          embed_fn=None,
                                          provider_name: str = "") -> dict:
        """B3 入口:cos 三档扫描(自动合并 + 人工候选入队)。

        自动档 merges 审计落链(同 backfeed_scan_and_merge)；review 档候选
        入 review_queue(item_type='backfeed_merge', B1 消费器接线点)——查重:
        同 pair(两 id)已有 pending 不重复入队(幂等)。
        """
        if not self._backfeed_ready():
            return {"enabled": False}
        r = self._shadow.scan_cos_merge(
            kinds=kinds, dry_run=dry_run, min_age_sec=min_age_sec,
            cos_auto_merge=cos_auto_merge, cos_review_floor=cos_review_floor,
            embed_fn=embed_fn, provider_name=provider_name)
        for m in r.get("merges") or []:
            if m.get("status") == "merged":
                try:
                    await self._log_event("backfeed_merge", "system",
                                          m.get("audit") or {})
                except Exception as _exc:
                    logger.warning("hub_core silent-except @1012: %s", _exc)
        if not dry_run:
            for cand in r.get("review_candidates") or []:
                pair = cand.get("pair") or []
                ids = sorted(p.get("id", "") for p in pair)
                if len(ids) < 2 or self._pending_backfeed_pair(ids):
                    continue
                detail = {"kind": cand.get("kind", ""), "pair": pair,
                          "cos": cand.get("cos"), "suggested": "review"}
                await self.queue_backfeed_merge(
                    f"{cand.get('kind')}:{ids[0]}:{ids[1]}",
                    "相似归并(cos 档)", detail)
        return r

    def _pending_backfeed_pair(self, ids: list) -> bool:
        """review_queue 是否已有同 pair 的 pending backfeed_merge(防重复入队)。"""
        try:
            with self._db() as conn:
                c = conn.cursor()
                rows = c.execute(
                    "SELECT detail FROM review_queue"
                    " WHERE item_type='backfeed_merge' AND status='pending'"
                ).fetchall()
            for (d,) in rows:
                if all(f'"id": "{i}"' in (d or "") for i in ids):
                    return True
        except Exception as _exc:
            logger.warning("hub_core silent-except @1039: %s", _exc)
        return False


class _DbCtx:
    """sqlite3.Connection 的上下文管理器代理。
    兼容现有直接调用模式（conn.execute / conn.close），同时支持 `with`。"""

    def __init__(self, conn):
        super().__setattr__('_conn', conn)
        super().__setattr__('_closed', False)

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self._closed:
            self._conn.close()
            self._closed = True
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in ('_conn', '_closed'):
            super().__setattr__(name, value)
        else:
            setattr(self._conn, name, value)

    def close(self):
        if not self._closed:
            self._conn.close()
            self._closed = True


hub = SyncHub()
hub.hub_agent = HubAgent(CONFIG.DB_PATH)
hub_agent = hub.hub_agent
