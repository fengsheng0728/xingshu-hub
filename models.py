"""
星枢 Sync Hub — 数据模型与配置
"""
import logging
logger = logging.getLogger("xingshu.models")

import os
import hashlib
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from pydantic import BaseModel, Field


# ============ 产品版本（单一来源）============
# CD-076（2026-09-22）：版本号原硬编码在三处——routes.py 的 FastAPI app、routes_server.py
# 的 /health 与 /healthz（三处同为旧版本号，而内容已到 2.1）。各写字符串必然漂移，
# 改为唯一定义在此，对外出口一律 import 本常量（行号引用会被 test_code_hygiene 的 H-3 拦下，
# 故此处只写符号名）。改版本只改这一行。
# 改版本只改这一行。
HUB_VERSION = "2.1.0"


# ============ 披露级别枚举 ============
class DisclosureLevel(str, Enum):
    """披露级别：从无到完整，逐级递增"""
    NONE = "none"           # 不披露
    METADATA = "metadata"   # 只披露标签/重要性/时间
    SUMMARY = "summary"     # 披露前 200 字符摘要
    FULL = "full"           # 完整内容
    # EMBEDDING 已废弃（v3.1）：向量 inversion 攻击可恢复原文语义，
    # 保留此注释仅作历史记录，不要取消注释。等价于 NONE。


class DisclosureScope(str, Enum):
    """披露范围定义"""
    SELF = "self"           # 仅自己可见
    MANAGER = "manager"     # 自己和上级可见（默认）
    PEERS = "peers"         # 同级任务协作可见
    ALL = "all"             # 所有人可见（谨慎使用）


class TaskStatus(str, Enum):
    """任务状态机（P6）"""
    PENDING = "pending"           # 待调度
    ASSIGNED = "assigned"         # 已分配
    IN_PROGRESS = "in_progress"   # 执行中
    COMPLETED = "completed"       # 已完成
    FAILED = "failed"             # 已失败
    CANCELLED = "cancelled"       # 已取消


# 合法状态转换
TASK_TRANSITIONS = {
    TaskStatus.PENDING:    [TaskStatus.ASSIGNED, TaskStatus.CANCELLED],
    TaskStatus.ASSIGNED:   [TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED],
    TaskStatus.IN_PROGRESS: [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED],
    TaskStatus.COMPLETED:  [],   # 终态
    TaskStatus.FAILED:     [],   # 终态
    TaskStatus.CANCELLED:  [],   # 终态
}


# ============ 配置 ============
@dataclass
class Config:
    """全局配置 — 从 config.yaml 读取，缺失时用默认值"""
    DB_PATH: str = "./sync_hub.db"
    SERVER_PORT: int = 3060
    # O3(2026-08-05): 版本协商兼容表 — Agent 最低支持版本
    AGENT_MIN_VERSION: str = "1.0.0"
    HEARTBEAT_TIMEOUT: int = 120
    DEFAULT_DISCLOSURE = DisclosureLevel.SUMMARY
    DISCLOSURE_SCOPE = DisclosureScope.MANAGER
    CHROMA_PATH: str = "./chroma_db"
    CHROMA_COLLECTION: str = "sync_hub"
    # CD-070b（2026-09-20）：测试态产物隔离 —— 空 = 沿用仓库内路径（生产默认）。
    # 事故背景同 CD-070：测试按默认路径写仓库根，跑一轮就改生产 chroma/wiki/audit。
    AUDIT_DIR: str = ""        # 审计产物根（空 = 仓库内 audit/）：anchor.txt / memory_pool.jsonl / transport.jsonl / tsa/
    WIKI_ROOT: str = ""        # wiki 页根（空 = 仓库内 wiki/）
    WORKSPACE_YSTORE_PATH: str = ""   # pycrdt 共享文档 ystore 文件（空 = cwd 相对 "ystore.db"）
    EMBEDDING_MODEL: str = "paraphrase-multilingual-MiniLM-L12-v2"
    # K1（2026-08-06，附录 F）：embedding provider 抽象 — hasher(默认词袋) | sentence(真语义)
    EMBEDDING_PROVIDER: str = "hasher"
    EMBEDDING_MODEL_PATH: str = ""  # sentence provider 的本地模型目录（离线包分发）
    RETENTION_MEMORY_DAYS: int = 180
    RETENTION_EVENTS_DAYS: int = 60
    RETENTION_TASKS_DAYS: int = 365
    RETENTION_READLOG_DAYS: int = 90  # CD-021: gateway_read_log 读审计保留天数(防御审计 2026-09-03)
    # XS-004(2026-09-08): 审计锚定外发 — 空列表=不外发(本地文件仅快照), 默认休眠
    AUDIT_ANCHOR_URLS: list = field(default_factory=list)  # 外部锚接收方 URL 列表(HTTP POST JSON)
    AUDIT_ANCHOR_INTERVAL: int = 3600  # 定时外发周期秒(0=仅启动时一次后停止循环)


    # CD-034 R3：链头外部时间戳（RFC3161 TSA）。默认关（不联网，与 anchor_urls 同哲学）；
    # 开启后按 AUDIT_TSA_INTERVAL 周期对链头盖章，校验时回拉比对，不一致告警。
    AUDIT_TSA_ENABLED: bool = False
    AUDIT_TSA_URL: str = ""            # 空 = 用 audit_chain.DEFAULT_TSA_URL（公共免费 TSA）
    AUDIT_TSA_INTERVAL: int = 86400    # 盖章周期秒（默认每日一次）
    HUB_TOKEN: str = ""  # 部署级单 token（config.yaml auth.hub_token）— 空=仅 api_key 认证
    # OGA: 注册准入模式 — guarded=受管注册(需 hub_token,防匿名注册) | open=自注册(仅可信内网)
    # CD-099（2026-09-23，H-4 默认值收口）：默认 open→guarded（「配置正确才安全」→ 默认安全）。
    # 旧内网部署/测试可在 config.yaml 显式写 open，或 env SYNC_HUB_REGISTRATION=open 回落
    # （env 仅在 config 未显式写 auth.registration 时生效，见 _load_config_from_yaml）。
    AUTH_REGISTRATION: str = "guarded"
    NOTIFY_CHANNELS: dict = None  # P2 通知多渠道（config.yaml notify_channels），惰性加载
    # P0 S4 暴露面收敛（2026-08-04）：WS 鉴权熔断 + REST 限速 + 联邦发现模式
    WS_AUTH_TIMEOUT_SEC: float = 3.0        # WS 首帧鉴权超时（可配置，禁止 <2s 防重连风暴）
    WS_AUTH_MAX_FAILS: int = 5              # 同 IP 滑动窗口内失败次数阈值
    WS_AUTH_WINDOW_SEC: int = 600           # 失败计数窗口（10 分钟）
    WS_AUTH_BAN_SEC: int = 1800             # 封禁时长（30 分钟）
    RATE_LIMIT_PER_IP: int = 1000           # REST 每 IP 每秒请求上限（默认远高于 200 并发压测基线）
    # CD-080（2026-09-23）：可信代理列表（CIDR/IP，config.yaml server.trusted_proxies）。
    # 默认空 = 不信任任何代理头（X-Forwarded-For 伪造不能绕过限速/熔断）；仅当直连
    # 对端命中本列表才采信 XFF（取链首=原始客户端）。仅在 Hub 位于反向代理之后时配置。
    TRUSTED_PROXIES: list = field(default_factory=list)
    FEDERATION_DISCOVERY: str = "multicast" # multicast | static
    FEDERATION_STATIC_PEERS: list = None    # static 模式节点清单 [{hub_id, host, port}]

    # S1 身份接入（2026-08-05）：auth_provider 抽象 + api_key 轮换 + LDAP/OIDC
    AUTH_MODE: str = "local"                # local | ldap | oidc | hybrid（缺依赖/配置自动降级 local）
    API_KEY_ROTATION_DAYS: int = 90         # api_key 自动轮换周期（天），0=不轮换
    API_KEY_ROTATION_GRACE_HOURS: int = 24  # 轮换后旧 key 宽限期（小时），期内新旧并存
    AUTH_LDAP_URL: str = ""                 # ldap://dc.corp.local 或 ldaps://
    AUTH_LDAP_BIND_DN: str = ""             # 服务账号 DN（组同步/查询用）
    AUTH_LDAP_BIND_PASSWORD: str = ""
    AUTH_LDAP_BASE_DN: str = ""             # 用户搜索基 DN
    AUTH_LDAP_GROUP_BASE_DN: str = ""       # 组搜索基 DN（缺省=用户基 DN）
    AUTH_LDAP_GROUP_SYNC_SEC: int = 900     # 组同步间隔（15 分钟）
    AUTH_OIDC_ISSUER: str = ""              # https://idp.corp.local/realms/x
    AUTH_OIDC_CLIENT_ID: str = ""
    AUTH_OIDC_JWKS_URL: str = ""            # 缺省从 issuer 发现

    # 阶段3 主干-分干数据底座（config.yaml data_trunk 段，影子模式）
    DATA_TRUNK_ENABLED: bool = False        # 默认关；生产 config 显式开（测试 env 强制关）
    DATA_TRUNK_ROOT: str = "./data-trunk"   # 企业主干仓库根
    DATA_TRUNK_BRANCH_DEFAULT: str = "default"  # 项目分干名（P0 单分干，映射表预留）
    DATA_TRUNK_SHADOW: dict = None          # {memory/knowledge/wiki/shared: bool} 影子双写开关
    DATA_TRUNK_BRANCHES: dict = None        # {agent_id: branch} scope↔分干映射（P2 交付1，预留；缺省全走 default）

    # 3-7(2026-09-10): SharedWorkspace 容量护栏（房间空闲卸载 TTL + 水位告警）
    WORKSPACE_ROOM_IDLE_TTL_SEC: int = 1800    # 房间空闲卸载 TTL 秒（0=关闭清扫）
    WORKSPACE_SWEEP_INTERVAL_SEC: int = 60     # 清扫周期秒
    WORKSPACE_MAX_ROOMS: int = 200             # 常驻 room 数水位阈值（跨阈值告警）
    CHROMA_MAX_VECTORS: int = 50000            # ChromaDB 集合向量数水位阈值
    # K-1（2026-09-16）：知识条目写入路径单条最大切片入向量数（写入延迟护栏；
    # sentence 档 ~10-30ms/chunk，200 封顶最坏个位数秒；hasher 档可忽略）
    KB_EMBED_MAX_CHUNKS: int = 200

    # D-10(2026-09-10): db 门面慢查询阈值毫秒（0 = 每次调用都记 WARNING，仅测试用）
    DB_SLOW_QUERY_MS: int = 200
    # CD-101（2026-09-23）：HTTP 请求体大小上限（Content-Length 快路径预检，超限 413）。
    # 默认档 2MB；hub-agent chat / wiki import 等大文本端点按路径放行到 LARGE 档。
    MAX_BODY_BYTES: int = 2 * 1024 * 1024
    MAX_BODY_BYTES_LARGE: int = 8 * 1024 * 1024


def _load_config_from_yaml() -> dict:
    """从 config.yaml 加载配置，返回覆盖字典"""
    overrides = {}
    registration_raw = None  # OGA: auth.registration 原值（校验放 try 外,防被宽 except 吞掉）
    try:
        import yaml
        config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
        config_path = os.path.join(config_dir, "config.yaml")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            db = cfg.get("database", {})
            if db.get("path"):
                overrides["DB_PATH"] = db["path"]
            server = cfg.get("server", {})
            if server.get("port"):
                overrides["SERVER_PORT"] = int(server["port"])
            # CD-080：可信代理列表（CIDR/IP，字符串按单元素列表收纳）
            if server.get("trusted_proxies") is not None:
                _tp = server["trusted_proxies"]
                if isinstance(_tp, str):
                    _tp = [_tp]
                overrides["TRUSTED_PROXIES"] = [str(_x) for _x in _tp]
            # CD-101：请求体上限可调（server.max_body_bytes / max_body_bytes_large）
            if server.get("max_body_bytes") is not None:
                overrides["MAX_BODY_BYTES"] = int(server["max_body_bytes"])
            if server.get("max_body_bytes_large") is not None:
                overrides["MAX_BODY_BYTES_LARGE"] = int(server["max_body_bytes_large"])
            auth = cfg.get("auth", {})
            if auth.get("hub_token"):
                overrides["HUB_TOKEN"] = str(auth["hub_token"])
            # OGA: 注册准入模式(open|guarded)
            if auth.get("registration") is not None:
                registration_raw = auth["registration"]
            # S1 身份接入：auth 段扩展（mode / 轮换 / LDAP / OIDC）
            if auth.get("mode"):
                overrides["AUTH_MODE"] = str(auth["mode"])
            if auth.get("api_key_rotation_days") is not None:
                overrides["API_KEY_ROTATION_DAYS"] = int(auth["api_key_rotation_days"])
            if auth.get("api_key_rotation_grace_hours") is not None:
                overrides["API_KEY_ROTATION_GRACE_HOURS"] = int(auth["api_key_rotation_grace_hours"])
            _ldap = auth.get("ldap", {}) or {}
            for _k, _v in (("url", "AUTH_LDAP_URL"), ("bind_dn", "AUTH_LDAP_BIND_DN"),
                           ("bind_password", "AUTH_LDAP_BIND_PASSWORD"),
                           ("base_dn", "AUTH_LDAP_BASE_DN"),
                           ("group_base_dn", "AUTH_LDAP_GROUP_BASE_DN"),
                           ("group_sync_interval_sec", "AUTH_LDAP_GROUP_SYNC_SEC")):
                if _ldap.get(_k):
                    overrides[_v] = _ldap[_k]
            _oidc = auth.get("oidc", {}) or {}
            for _k, _v in (("issuer", "AUTH_OIDC_ISSUER"), ("client_id", "AUTH_OIDC_CLIENT_ID"),
                           ("jwks_url", "AUTH_OIDC_JWKS_URL")):
                if _oidc.get(_k):
                    overrides[_v] = _oidc[_k]
            nc = cfg.get("notify_channels", {})
            if nc:
                overrides["NOTIFY_CHANNELS"] = nc
            db = cfg.get("database", {})
            if db.get("chroma_path"):
                overrides["CHROMA_PATH"] = db["chroma_path"]
            # K1（2026-08-06，附录 F）：embedding provider 抽象
            emb = cfg.get("embedding", {})
            if emb.get("provider"):
                overrides["EMBEDDING_PROVIDER"] = str(emb["provider"])
            if emb.get("model_path"):
                overrides["EMBEDDING_MODEL_PATH"] = str(emb["model_path"])
            if emb.get("model"):
                overrides["EMBEDDING_MODEL"] = str(emb["model"])
            # K-1: 知识条目写侧切片入向量上限（可选段，缺失走 Config 默认值 200）
            if emb.get("kb_embed_max_chunks") is not None:
                overrides["KB_EMBED_MAX_CHUNKS"] = int(emb["kb_embed_max_chunks"])
            # P0 S4：暴露面收敛配置（ws 熔断 / 限速 / 联邦发现）
            ws = cfg.get("ws", {})
            if ws.get("auth_timeout_sec"):
                overrides["WS_AUTH_TIMEOUT_SEC"] = float(ws["auth_timeout_sec"])
            if ws.get("max_fails"):
                overrides["WS_AUTH_MAX_FAILS"] = int(ws["max_fails"])
            if ws.get("fail_window_sec"):
                overrides["WS_AUTH_WINDOW_SEC"] = int(ws["fail_window_sec"])
            if ws.get("ban_sec"):
                overrides["WS_AUTH_BAN_SEC"] = int(ws["ban_sec"])
            rl = cfg.get("rate_limit", {})
            if rl.get("per_ip"):
                overrides["RATE_LIMIT_PER_IP"] = int(rl["per_ip"])
            fed = cfg.get("federation", {})
            if fed.get("discovery"):
                overrides["FEDERATION_DISCOVERY"] = str(fed["discovery"])
            if fed.get("static_peers"):
                overrides["FEDERATION_STATIC_PEERS"] = fed["static_peers"]
            # 阶段3 主干-分干数据底座（data_trunk 段）
            dt = cfg.get("data_trunk", {})
            if dt.get("enabled") is not None:
                overrides["DATA_TRUNK_ENABLED"] = bool(dt["enabled"])
            if dt.get("root"):
                overrides["DATA_TRUNK_ROOT"] = str(dt["root"])
            if dt.get("branch_default"):
                overrides["DATA_TRUNK_BRANCH_DEFAULT"] = str(dt["branch_default"])
            if dt.get("shadow"):
                overrides["DATA_TRUNK_SHADOW"] = dt["shadow"]
            if dt.get("branches") is not None:
                overrides["DATA_TRUNK_BRANCHES"] = dt["branches"]
            # XS-004（2026-09-08）：审计锚定外发（audit 段，默认休眠）
            audit_cfg = cfg.get("audit", {})
            if audit_cfg.get("anchor_urls"):
                urls = audit_cfg["anchor_urls"]
                if isinstance(urls, str):
                    urls = [urls]
                overrides["AUDIT_ANCHOR_URLS"] = [str(u) for u in urls]
            if audit_cfg.get("anchor_interval") is not None:
                overrides["AUDIT_ANCHOR_INTERVAL"] = int(audit_cfg["anchor_interval"])

            if audit_cfg.get("dir") is not None:
                overrides["AUDIT_DIR"] = str(audit_cfg["dir"])
            tsa_cfg = audit_cfg.get("tsa") or {}
            if tsa_cfg:
                if tsa_cfg.get("enabled") is not None:
                    overrides["AUDIT_TSA_ENABLED"] = bool(tsa_cfg["enabled"])
                if tsa_cfg.get("url"):
                    overrides["AUDIT_TSA_URL"] = str(tsa_cfg["url"])
                if tsa_cfg.get("interval") is not None:
                    overrides["AUDIT_TSA_INTERVAL"] = int(tsa_cfg["interval"])
            # 3-7(2026-09-10): workspace 容量护栏段 + database.chroma_max_vectors
            # 一律 is not None 判定：缺省不覆盖（保持 Config 默认值），显式写 0 也能生效
            wsp_y = cfg.get("workspace", {}) or {}
            if wsp_y.get("ystore_path") is not None:
                overrides["WORKSPACE_YSTORE_PATH"] = str(wsp_y["ystore_path"])
            _wiki = cfg.get("wiki") or {}
            if _wiki.get("root") is not None:
                overrides["WIKI_ROOT"] = str(_wiki["root"])
            wsp = cfg.get("workspace", {})
            if wsp.get("room_idle_ttl_sec") is not None:
                overrides["WORKSPACE_ROOM_IDLE_TTL_SEC"] = int(wsp["room_idle_ttl_sec"])
            if wsp.get("sweep_interval_sec") is not None:
                overrides["WORKSPACE_SWEEP_INTERVAL_SEC"] = int(wsp["sweep_interval_sec"])
            if wsp.get("max_rooms") is not None:
                overrides["WORKSPACE_MAX_ROOMS"] = int(wsp["max_rooms"])
            if db.get("chroma_max_vectors") is not None:
                overrides["CHROMA_MAX_VECTORS"] = int(db["chroma_max_vectors"])
            # D-10: db 门面慢查询阈值（同 3-7 风格：is not None 判定，显式写 0 也生效）
            if db.get("slow_query_ms") is not None:
                overrides["DB_SLOW_QUERY_MS"] = int(db["slow_query_ms"])
    except Exception as _exc:
        logger.debug("models silent-except(_load_config_from_yaml): %s", _exc)
    # OGA: auth.registration 值校验放 try 外 —— 函数体宽 except 会吞异常,
    # 拼错(如 "guraded")若静默回落 open = 受管语义失效,必须在此显式拒绝。
    if registration_raw is not None:
        reg = str(registration_raw).strip()
        if reg not in ("open", "guarded"):
            raise ValueError(
                f"config.yaml auth.registration 必须是 open 或 guarded, 当前值: {registration_raw!r}")
        overrides["AUTH_REGISTRATION"] = reg
    # CD-099：env 回落口（仅 config 未显式写 auth.registration 时生效）——测试/旧内网
    # 部署用；config 显式值优先（guarded 语义不被环境偷改）。
    if registration_raw is None:
        env_reg = os.environ.get("SYNC_HUB_REGISTRATION", "").strip()
        if env_reg in ("open", "guarded"):
            overrides["AUTH_REGISTRATION"] = env_reg
    # 环境变量覆盖（测试隔离用：独立测试 Hub 强制关影子，不碰生产 data-trunk）
    env_dt = os.environ.get("SYNC_HUB_DATA_TRUNK")
    if env_dt is not None:
        overrides["DATA_TRUNK_ENABLED"] = env_dt.strip().lower() in ("1", "true", "yes", "on")
    # 环境变量覆盖（测试隔离用：独立测试 Hub 指向独立 chroma 目录，不碰生产）
    env_chroma = os.environ.get("SYNC_HUB_CHROMA_PATH")
    if env_chroma:
        overrides["CHROMA_PATH"] = env_chroma
    # CD-070b：派生产物根（审计 / wiki）env 覆盖 —— 测试进程与测试自 spawn 的
    # Hub 子进程都据此把产物写到 tmp，绝不写仓库根（默认空 = 沿用生产路径）。
    env_audit = os.environ.get("SYNC_HUB_AUDIT_DIR")
    if env_audit:
        overrides["AUDIT_DIR"] = env_audit
    env_wiki = os.environ.get("SYNC_HUB_WIKI_ROOT")
    if env_wiki:
        overrides["WIKI_ROOT"] = env_wiki
    env_ystore = os.environ.get("SYNC_HUB_YSTORE_PATH")
    if env_ystore:
        overrides["WORKSPACE_YSTORE_PATH"] = env_ystore
    # CD-070（2026-09-20）：数据库路径 env 覆盖 —— 与 migrations/alembic/env.py 的
    # SYNC_HUB_DB 口径对齐。此前 models.py 不认这个变量，测试/迁移脚本只能落到
    # config 默认的仓库根 ./sync_hub.db（实测顶掉过生产库：29 agent/记忆/知识全空）。
    # env 优先级高于 config.yaml（与 SYNC_HUB_DATA_TRUNK / SYNC_HUB_CHROMA_PATH 同族）。
    env_db = os.environ.get("SYNC_HUB_DB")
    if env_db:
        overrides["DB_PATH"] = env_db
    return overrides


def _make_config() -> Config:
    """创建 Config 实例，config.yaml 值覆盖默认值"""
    cfg = Config()
    overrides = _load_config_from_yaml()
    for k, v in overrides.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


CONFIG = _make_config()

# ============ CD-070：生产库防误写硬门（2026-09-20）============
# 事故背景：测试模块级 import db/models 时按 config 默认路径在仓库根建库，把
# ./sync_hub.db（生产库）顶掉 —— 2026-09-20 实测 29 个 agent / memory_pool /
# knowledge_base / shared_docs 全被清空。生产库落在仓库根是「正常运行态」，
# 所以硬门只在测试态生效：测试态下 DB 若解析到仓库根生产库，直接 fail。
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_ROOT_DB_PATH = os.path.join(_REPO_ROOT, "sync_hub.db")


def _in_test_context() -> bool:
    """测试态判定：显式守卫 env / pytest 运行时标记 / 测试 NO_AUTH 标记。"""
    if os.environ.get("SYNC_HUB_DB_GUARD", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("PYTEST_VERSION") or os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return bool(os.environ.get("SYNC_HUB_NO_AUTH"))


def assert_db_path_safe(db_path: str, context: str = "") -> str:
    """测试态硬门：DB 路径不得解析到仓库根生产库；返回规范化绝对路径。

    生产运行（无测试标记）不受影响 —— 生产库本来就该是仓库根 ./sync_hub.db。
    """
    resolved = os.path.abspath(db_path)
    if resolved == _ROOT_DB_PATH:
        raise RuntimeError(
            "[DB 路径硬门] 测试态下解析到仓库根生产库, 拒绝继续: "
            f"{db_path!r} -> {resolved}"
            + (f" (来源: {context})" if context else "")
            + "; 测试请设 SYNC_HUB_DB 指向临时库（conftest 已强制）。")
    return resolved


if _in_test_context():
    assert_db_path_safe(CONFIG.DB_PATH, "models import")


PUBLIC_DOMAIN = "公共区"  # XS-001: 空域记忆归属域（员工模板 __public__ 占位对齐）

# ============ 数据模型 ============
class AgentRegistration(BaseModel):
    agent_id: str = Field(max_length=200)
    agent_name: str = Field(max_length=200)
    department: str = Field(default="", max_length=100)  # 部门（如"客服部"、"售后部"）
    capabilities: List[str] = Field(default_factory=list, max_length=200)
    role: str = Field(default="worker", max_length=50)  # worker | manager | orchestrator
    managed_agents: List[str] = Field(default_factory=list, max_length=200)
    disclosure_policy: dict = Field(default_factory=dict)
    endpoint: str = Field(default="", max_length=500)


class MemoryBatchOp(BaseModel):
    """H3: 批量记忆操作单条"""
    action: str = Field(max_length=20)  # "store" | "delete" | "search"
    memory_key: str = Field(default="", max_length=200)
    content: str = Field(default="", max_length=100_000)
    kind: str = Field(default="fact", max_length=50)
    source_type: str = Field(default="agent", max_length=50)
    query: str = Field(default="", max_length=4_000)
    limit: int = Field(default=10, ge=1, le=200)  # CD-101：单条检索分页上限收口


class MemoryEntry(BaseModel):
    memory_key: str = Field(max_length=200)
    content: str = Field(max_length=100_000)  # CD-101：单条记忆正文上限
    summary: Optional[str] = Field(default=None, max_length=50_000)
    embedding: Optional[List[float]] = None  # [可选] 配合 ChromaDB 使用
    importance: float = 1.0
    tags: List[str] = Field(default_factory=list, max_length=200)
    kind: str = Field(default="fact", max_length=50)  # fact | todo | profile | preference
    source_session_id: str = Field(default="", max_length=200)
    confidence: float = 1.0
    source_type: str = Field(default="user", max_length=50)  # user | tool | system
    trust_level: str = Field(default="internal", max_length=50)  # S3 taint: system | internal | federated | external
    disclosure_level: DisclosureLevel = DisclosureLevel.SUMMARY
    disclosure_scope: DisclosureScope = DisclosureScope.MANAGER
    allowed_viewers: List[str] = Field(default_factory=list, max_length=200)


# M2: 会话摘要归档
class SessionArchiveRequest(BaseModel):
    agent_id: str = Field(max_length=200)
    local_session_id: int = Field(ge=0)
    title: str = Field(default="", max_length=500)
    summary: str = Field(default="", max_length=50_000)
    key_facts: list = Field(default_factory=list, max_length=500)
    msg_count: int = Field(default=0, ge=0, le=10_000_000)


# P0 团队协作: 会话接力 handoff
class SessionHandoffRequest(BaseModel):
    """A 移交会话给 B: from_agent_id 必须 == 调用者(get_current_agent)"""
    from_agent_id: str = Field(max_length=200)
    to_agent_id: str = Field(max_length=200)
    local_session_id: int = Field(ge=0)
    title: str = Field(default="", max_length=500)
    summary: str = Field(default="", max_length=50_000)
    key_facts: list = Field(default_factory=list, max_length=500)
    # CD-101：messages 条数封顶（[{role, content}, ...] 最近 N 条全文，防单请求搬运整段会话史）
    messages: list = Field(default_factory=list, max_length=200)


class TaskCreate(BaseModel):
    task_id: str = Field(max_length=200)
    description: str = Field(max_length=50_000)
    creator_agent_id: str = Field(default="", max_length=200)
    required_capabilities: List[str] = Field(default_factory=list, max_length=200)
    required_memories: List[str] = Field(default_factory=list, max_length=200)
    priority: int = 1
    disclosure_plan: Optional[dict] = None
    depends_on: List[str] = Field(default_factory=list, max_length=200)  # P1 DAG: 前置任务 id 列表（向后兼容，默认空）
    parent_task_id: Optional[str] = Field(default=None, max_length=200)  # P2: 所属父任务 id（拆解并行）


class DisclosureRequest(BaseModel):
    task_id: str = Field(default="", max_length=200)
    requester_agent_id: str = Field(max_length=200)
    target_agent_id: str = Field(max_length=200)
    query: str = Field(default="", max_length=4_000)
    required_level: DisclosureLevel = DisclosureLevel.SUMMARY


class SemanticSearchRequest(BaseModel):
    query: str = Field(max_length=4_000)
    requester_agent_id: str = Field(max_length=200)
    n_results: int = Field(default=10, ge=1, le=200)  # CD-101：分页上限收口
    filter_owner: Optional[str] = Field(default=None, max_length=200)
    filter_tags: Optional[List[str]] = Field(default=None, max_length=200)
    # K-1（2026-09-16）：可选层过滤。"" = 不过滤（memory+knowledge 都查，向后兼容）；
    # "memory"/"knowledge" = 只查对应层。注意：无 layer 键的旧向量在显式 layer=memory 时不命中。
    layer: str = Field(default="", max_length=20)


class KnowledgeEntry(BaseModel):
    entry_id: Optional[str] = Field(default=None, max_length=200)
    title: str = Field(max_length=500)
    content: str = Field(default="", max_length=100_000)
    tags: List[str] = Field(default_factory=list, max_length=200)
    links: List[str] = Field(default_factory=list, max_length=200)
    category: str = Field(default="general", max_length=100)
    importance: float = 1.0
    created_by: str = Field(default="", max_length=200)

class HubAgentConfig(BaseModel):
    """Hub Agent 配置（CD-101：provider/api_key/api_base/model 长度封顶，temperature 限 0~2）"""
    provider: str = Field(default="openai", max_length=50)
    api_key: str = Field(default="", max_length=500)
    api_base: str = Field(default="", max_length=500)
    model: str = Field(default="gpt-4o-mini", max_length=200)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    enabled: bool = False
    auto_approve: bool = False

class DisclosureRules(BaseModel):
    """披露审计规则"""
    rules: str = Field(max_length=100_000)



# ============ 核心 Hub ============