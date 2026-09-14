# -*- coding: utf-8 -*-
"""data_trunk.py — 主干-分干数据底座（阶段3-P0）

企业主干仓库（真相源）+ 项目分干仓库（工作副本）的目录结构、BOUNDARY.md
边界规则单一事实源、identity 镜像、分干指针登记。

影子模式：现有 SQLite 写入路径不动，数据同步落 git 仓库群。本模块 P0 只建
结构与身份镜像，P1 挂写入钩子（hub_mixins/shadow.py）。

铁律：
- enabled=false（或 env SYNC_HUB_DATA_TRUNK=0，测试隔离）时全部 no-op
- 所有操作失败静默（D4：影子是增强不是依赖），返回 bool，绝不抛异常
- identity 只落 key fingerprint（sha256 前 16 hex），**绝不落明文 key**
- BOUNDARY.md / identity 生成幂等：内容不变不写盘不 commit（避免启动噪音）
"""
import hashlib
import json
import logging
import os
from types import SimpleNamespace

from gitrepo import GitRepo

logger = logging.getLogger("xingshu.data_trunk")


def key_fingerprint(api_key: str) -> str:
    """key 指纹：sha256 前 16 hex。明文 key 不进仓库。"""
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


# ── 阶段4-B2：customers/canonical 档案（设计 §3 冻结契约，逐字段对齐）──

def canonical_id_for(content: str) -> str:
    """canonical_id = cust:c-<8 hex>（chunk_hash 同口径规范化 sha256 截断，确定性）。"""
    from chunker import chunk_hash
    return "cust:c-" + chunk_hash(content or "")[:8]


def synthesize_tags(source_tags, base: dict = None) -> dict:
    """打标快照合成 = 各来源取最严（只降不升，设计 §2.2）。

    - level：min by _level_rank（disclosure.py:19 同口径秩 none<metadata<summary<full）
    - trust：TRUST_ORDER 数值小者胜（复用 hub_core.py:320 _merge_trust 语义，只降不升）
    - taint：任一来源 tainted → 记最早 tainted_at（ISO 字符串字典序 = 时序）
    - locked：任一来源硬锁 → locked=True
    base：既有 canonical 快照；新来源并入时连 base 一起重算 min——只可能降，不可升。
    """
    from hub_core import TRUST_ORDER  # 惰性 import 避循环（hub_core → hub_mixins.shadow）
    from disclosure import _level_rank
    from models import DisclosureLevel

    def _rank(lv):
        try:
            return _level_rank(DisclosureLevel(lv))
        except Exception:
            return _level_rank(DisclosureLevel.SUMMARY)  # 缺省/非法值按 summary 计

    items = [t for t in ([base] if base else []) + list(source_tags or []) if t]
    if not items:
        return {"trust": "internal", "level": "summary", "tainted_at": "", "locked": False}
    trust = min((t.get("trust") or "internal" for t in items),
                key=lambda x: TRUST_ORDER.get(x, 3))
    level = min((t.get("level") or "summary" for t in items), key=_rank)
    taints = [t.get("tainted_at") for t in items if t.get("tainted_at")]
    return {"trust": trust, "level": level,
            "tainted_at": min(taints) if taints else "",
            "locked": any(bool(t.get("locked")) for t in items)}


# ── BOUNDARY.md 生成（幂等：内容只依赖代码源与配置，不含时间戳）──

def _rule_rows() -> list:
    from disclosure_rules import rule_table
    return [dict(r) for r in rule_table()]


def _secret_words() -> list:
    from sensitivity import DEFAULT_SECRET_KEYWORDS
    return list(DEFAULT_SECRET_KEYWORDS)


def _boundary_md(cfg) -> str:
    """边界规则单一事实源 — 从披露规则/敏感度链/配置生成。修改代码源后重启即更新。"""
    rules = _rule_rows()
    words = _secret_words()
    L = []
    L.append("# BOUNDARY.md — 星枢数据底座边界规则（单一事实源）\n")
    L.append("> 本文件由 Hub 启动时从代码源自动生成（阶段3-P0），**请勿手工编辑**；\n")
    L.append("> 修改请改 disclosure_rules.py / sensitivity.py / config.yaml，重启后重新生成。\n")
    L.append("")
    L.append("## 1. 披露规则链（%d 条，优先级序）" % len(rules))
    L.append("")
    L.append("| 优先级 | id | 名称 | 规则 |")
    L.append("|---|---|---|---|")
    for r in sorted(rules, key=lambda x: x.get("priority", 99)):
        L.append("| %s | `%s` | %s | %s |" % (
            r.get("priority", "?"), r.get("id", "?"), r.get("name", ""),
            (r.get("desc") or "").replace("|", "\\|")))
    L.append("")
    L.append("## 2. 敏感度 6 维判定链（写入打标，fail-closed）")
    L.append("")
    L.append("| 维 | 规则 |")
    L.append("|---|---|")
    L.append("| 1 来源信任 | trust_level == UNTRUSTED → NONE + taint |")
    L.append("| 2 PII 正则 | 身份证/手机/银行卡/护照/密钥串 → NONE（预扫在切割前跑） |")
    L.append("| 3 机密词库 | 命中机密词 → SUMMARY 上限 |")
    L.append("| 4 写入者角色 | worker 含业务数据 → SUMMARY 上限 |")
    L.append("| 5 父文档级别 | 继承 parent 的 disclosure_level |")
    L.append("| 6 类型 | kind: secret 最高 / todo 最低 |")
    L.append("")
    L.append("## 3. 机密词库（内置默认 %d 词，可经词库目录扩展）" % len(words))
    L.append("")
    L.append("`%s`" % "、".join(words))
    L.append("")
    L.append("## 4. 角色分级")
    L.append("")
    L.append("- `worker`：仅可读自己内容（规则7 同级协作例外），写入门槛最高")
    L.append("- `manager`：可读下属 SUMMARY（default_manager_level）")
    L.append("- `orchestrator`：全局限 orchestrator_max_level")
    L.append("")
    L.append("## 5. 分干隔离声明")
    L.append("")
    L.append("- 分干之间**永不互读**；跨项目查询只走主干 index/")
    L.append("- 主干**不分发裸仓库**；index/ 仅元数据级，**内容不上行**")
    L.append("- 每个分干 = 独立 git 仓库（inbox → 清洗 → 打标 → vault）")
    L.append("- canonical 档案（customers/）是归并后内容级正本——「内容不上行」红线的**唯一受控例外**（阶段4 反哺归并，docs/phase4-backfeed-design.md §3）")
    L.append("- 被合并分干条目不删除、归档至 vault/_merged/；index 修正行（merged_into）把读取端重指向 canonical 档案")
    L.append("")
    L.append("## 6. 审计要求")
    L.append("")
    L.append("- 读写审计入哈希链（audit_chain.py）；每条数据只读经网关（读审计落链）")
    L.append("- 主干 audit/chain-head.jsonl 记录 git 链头 ↔ 哈希链互证")
    L.append("")
    return "\n".join(L) + "\n"


# ── 主干/分干操作 ──

class DataTrunk:
    """主干-分干数据底座句柄。enabled=false 时全部 no-op。"""

    def __init__(self, cfg=None):
        cfg = cfg or SimpleNamespace()
        self.enabled = bool(getattr(cfg, "DATA_TRUNK_ENABLED", False))
        self.root = os.path.abspath(getattr(cfg, "DATA_TRUNK_ROOT", "./data-trunk"))
        self.branch_default = getattr(cfg, "DATA_TRUNK_BRANCH_DEFAULT", "default")
        self.shadow = getattr(cfg, "DATA_TRUNK_SHADOW", None) or {}
        # P2 交付1：{agent_id: branch} 显式映射表（预留；缺省空 = 全走 default）
        self.branches_map = dict(getattr(cfg, "DATA_TRUNK_BRANCHES", None) or {})
        # 阶段4-B2：反哺归并开关（fail-closed 缺省 False；config data_trunk.backfeed.enabled）
        self.backfeed = dict(getattr(cfg, "DATA_TRUNK_BACKFEED", None) or {})
        self.trunk = GitRepo(self.root)
        self._branches = {}

    # ── scope ↔ 分干映射（P2 交付1）──
    def branch_for_agent(self, agent_id: str = "", scope: dict = None) -> str:
        """三层 scope 决定可写分干。解析序（命中即停）：
        1) config.data_trunk.branches 显式映射 {agent_id: branch}
        2) scope.data_domain 命中已登记分干名（projects/branches.jsonl）
        3) 默认分干（D1：默认全走 default，映射表预留）
        """
        if agent_id and agent_id in self.branches_map:
            return str(self.branches_map[agent_id])
        domains = (scope or {}).get("data_domain") or []
        if domains:
            known = self._known_branches()
            for dom in domains:
                if dom and str(dom) in known:
                    return str(dom)
        return self.branch_default

    def _known_branches(self) -> set:
        """已登记分干名集合（默认分干 + 映射值 + branches.jsonl 登记）。"""
        known = {self.branch_default} | {str(b) for b in self.branches_map.values()}
        try:
            path = os.path.join(self.root, "projects", "branches.jsonl")
            with open(path, "r", encoding="utf-8") as f:
                for ln in f:
                    if ln.strip():
                        b = json.loads(ln).get("branch")
                        if b:
                            known.add(str(b))
        except Exception as _exc:
            logger.debug("data_trunk silent-except @186: %s", _exc)
        return known

    # ── 分干 ──
    def branch_repo(self, branch: str = "") -> GitRepo:
        b = branch or self.branch_default
        if b not in self._branches:
            self._branches[b] = GitRepo(os.path.join(self.root, "branches", b))
        return self._branches[b]

    def branch_root(self, branch: str = "") -> str:
        b = branch or self.branch_default
        return os.path.join(self.root, "branches", b)

    # ── 初始化 ──
    def ensure(self, agents_rows=None) -> bool:
        """主干 + 分干结构初始化 + BOUNDARY + identity 镜像 + 首次 commit。"""
        if not self.enabled:
            return True
        try:
            trunk = self.trunk
            if not trunk.ensure():
                logger.warning("data-trunk 仓库初始化失败: %s", self.root)
                return False
            # 1) 目录结构（customers 预留给阶段4 反哺 canonical 档案）
            for d in ("identity", "customers", "index", "audit", "projects", "branches"):
                os.makedirs(os.path.join(self.root, d), exist_ok=True)
            # 1b) 主干 .gitignore 忽略 branches/（分干是独立 git 仓库——嵌套仓库会让
            #     主干 git add -A 报 "does not have a commit checked out" 全盘失败）
            trunk.write_file(".gitignore", "branches/\n")
            # 2) BOUNDARY.md（幂等写）
            trunk.write_file("BOUNDARY.md", _boundary_md(SimpleNamespace()))
            # 3) 分干指针登记
            self._ensure_branches_json()
            # 4) identity 镜像（agents 表）
            if agents_rows is not None:
                self.sync_identity(agents_rows)
            # 5) 首次/增量 commit（内容无变化时 git 自己 no-op）
            trunk.commit("阶段3-P0: 主干仓库结构")
            # 6) 分干仓库结构
            br = self.branch_repo()
            if br.ensure():
                for d in ("inbox", "vault", "_originals", "audit"):
                    os.makedirs(os.path.join(br.root, d), exist_ok=True)
                # README 保证分干有首个 commit（空目录 git 不跟踪，log 恒 0）
                br.write_file("README.md",
                              "# 分干 %s\n\ninbox/ 待处理 · vault/ 打标文件 · _originals/ 原件受控区 · audit/ 分干审计\n"
                              % self.branch_default)
                br.commit("阶段3-P0: 分干 %s 初始化" % self.branch_default)
            return True
        except Exception:
            logger.exception("DataTrunk.ensure 异常")
            return False

    def _ensure_branches_json(self) -> None:
        self._register_branch(self.branch_default)

    def _register_branch(self, branch: str, agent_id: str = "") -> bool:
        """分干指针登记（projects/branches.jsonl）：幂等，mapped_agents 累积去重。
        返回是否有写盘变化。"""
        path = os.path.join(self.root, "projects", "branches.jsonl")
        existing = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = [json.loads(ln) for ln in f if ln.strip()]
        except Exception:
            existing = []
        rec = next((e for e in existing if e.get("branch") == branch), None)
        if rec is None:
            existing.append({
                "branch": branch,
                "path": os.path.join("branches", branch),
                "status": "active",
                "created_at": _now_iso(),
                "mapped_agents": [agent_id] if agent_id else [],
            })
        elif agent_id and agent_id not in (rec.get("mapped_agents") or []):
            rec.setdefault("mapped_agents", []).append(agent_id)
        else:
            return False  # 已登记且无新映射 → 不写盘（幂等）
        with open(path, "w", encoding="utf-8") as f:
            for e in existing:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return True

    # ── 分干开通（P2 交付1：scope/映射命中非默认分干时惰性开通）──
    def ensure_branch(self, branch: str, agent_id: str = "") -> bool:
        """开通分干：仓库结构 + 指针登记 + mapped_agents 映射。幂等，失败静默。"""
        if not self.enabled:
            return True
        branch = str(branch or self.branch_default)
        try:
            br = self.branch_repo(branch)
            if not br.ensure():
                return False
            for d in ("inbox", "vault", "_originals", "audit"):
                os.makedirs(os.path.join(br.root, d), exist_ok=True)
            if not br.head_hash():
                # README 保证分干有首个 commit（空目录 git 不跟踪）
                br.write_file("README.md",
                              "# 分干 %s\n\ninbox/ 待处理 · vault/ 打标文件 · _originals/ 原件受控区 · audit/ 分干审计\n"
                              % branch)
                br.commit("阶段3-P2: 分干 %s 开通" % branch)
            if self._register_branch(branch, agent_id):
                self.trunk.commit("阶段3-P2: 分干 %s 指针登记" % branch)
            return True
        except Exception:
            logger.exception("ensure_branch 异常 %s", branch)
            return False

    # ── identity 镜像 ──
    def sync_identity(self, agents_rows) -> bool:
        """agents 表全量镜像 → identity/agents.jsonl + keys.jsonl（幂等，内容不变不写）。"""
        if not self.enabled:
            return True
        try:
            agents = []
            keys = []
            for row in agents_rows:
                aid = row.get("agent_id", "")
                fp = key_fingerprint(row.get("api_key", ""))
                agents.append({
                    "agent_id": aid,
                    "name": row.get("agent_name", ""),
                    "role": row.get("role", "worker"),
                    "department": row.get("department", ""),
                    "key_fp": fp,
                    "registered_at": row.get("registered_at", ""),
                })
                keys.append({
                    "agent_id": aid,
                    "key_fp": fp,
                    "scope": row.get("role", "worker"),
                    "status": "active",
                    "issued_at": row.get("registered_at", ""),
                })
            # 排序保证确定性
            agents.sort(key=lambda x: x["agent_id"])
            keys.sort(key=lambda x: x["agent_id"])
            a_text = "\n".join(json.dumps(a, ensure_ascii=False) for a in agents) + "\n"
            k_text = "\n".join(json.dumps(k, ensure_ascii=False) for k in keys) + "\n"
            changed = False
            for rel, text in (("identity/agents.jsonl", a_text),
                              ("identity/keys.jsonl", k_text)):
                old = self.trunk.read_at(rel) if self.trunk.head_hash() else None
                if old != text:
                    self.trunk.write_file(rel, text)
                    changed = True
            if changed:
                self.trunk.commit("阶段3-P0: identity 镜像同步")
            return True
        except Exception:
            logger.exception("sync_identity 异常")
            return False

    # ── customers/canonical 档案读写（阶段4-B2，设计 §3 冻结契约）──
    @staticmethod
    def canonical_path(canonical_id: str) -> str:
        """canonical 档案主干相对路径：customers/<canonical_id>.json。
        canonical_id 含冒号（cust:c-<8hex>），Windows/NTFS 下冒号是 ADS 分隔符
        （会写成名为 cust 的文件 + 备用数据流）——文件名层统一把冒号替换为
        下划线（同 shadow.py _safe_name 教训）；canonical_id 本身保持不变。"""
        safe = str(canonical_id).replace(":", "_")
        return f"customers/{safe}.json"

    def read_canonical(self, canonical_id: str):
        """读 canonical 档案（工作区）。不存在/失败返回 None（D4 静默）。"""
        try:
            full = os.path.join(self.root, self.canonical_path(canonical_id))
            if not os.path.isfile(full):
                return None
            with open(full, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("read_canonical 异常 %s", canonical_id)
            return None

    def write_canonical(self, record: dict) -> bool:
        """写 canonical 档案。幂等：内容不变不写盘（与既有「不变不写」语义一致）。
        不 commit——批提交由调用方（合并执行器）负责，commit message 含 canonical_id。"""
        if not self.enabled:
            return False
        try:
            cid = (record or {}).get("canonical_id")
            if not cid:
                return False
            text = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            old = self.read_canonical(cid)
            if old is not None and \
                    json.dumps(old, ensure_ascii=False, indent=2, sort_keys=True) + "\n" == text:
                return True  # 内容不变不写盘
            return self.trunk.write_file(self.canonical_path(cid), text)
        except Exception:
            logger.exception("write_canonical 异常")
            return False


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
