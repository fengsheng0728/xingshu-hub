"""
星枢 Sync Hub — 渐进式披露引擎（从 hub_core.py 拆分）
处理记忆可见性判定、披露请求、语义搜索、审批流程
"""
import json, asyncio, sqlite3, logging, time, threading
from datetime import datetime, timezone
from typing import Dict, List
import numpy as np
import json
from models import DisclosureLevel, DisclosureScope, SemanticSearchRequest, CONFIG, PUBLIC_DOMAIN
from db import row_to_dict
import db_facade
from functools import partial

logger = logging.getLogger("xingshu.disclosure")

# 向后兼容
_row_dict = row_to_dict


def _bump_memory_access(conn, accessed_at, memory_id):
    """request_disclosure 披露后的 access_count 自增事务体（D-11 门面迁移：
    run_in_conn 的同步 fn，SQL 与原内联写法逐字一致）。"""
    conn.execute(
        "UPDATE memory_pool SET access_count = access_count + 1, last_accessed = ? WHERE memory_id = ?",
        (accessed_at, memory_id),
    )


def _level_rank(level: "DisclosureLevel") -> int:
    """披露级别秩（用于 min 比较）：none=0, metadata=1, summary=2, full=3"""
    return {
        DisclosureLevel.NONE: 0,
        DisclosureLevel.METADATA: 1,
        DisclosureLevel.SUMMARY: 2,
        DisclosureLevel.FULL: 3,
    }.get(level, 0)


class DisclosureEngine:
    """渐进式披露引擎 — 封装披露决策逻辑，从 SyncHub 解耦"""

    def __init__(self, hub):
        self.hub = hub
        self._chunk_window = {}  # H3: (requester, parent_doc_id) -> [(ts, chunk_id)]
        self._chunk_window_lock = threading.Lock()  # H3 滑窗锁

    # ── 三档匹配 ──

    def _match_query(self, memory: dict, query: str) -> tuple:
        """三档匹配 + relevance_score。返回 (是否匹配, 相关度 0.0-1.0)"""
        score = 0.0
        q = query.lower()

        tags = json.loads(memory.get("tags") or "[]")
        if any(q == t.lower() for t in tags):
            score += 0.5
        elif any(q in t.lower() for t in tags):
            score += 0.3

        key = (memory.get("memory_key") or "").lower()
        if q in key:
            score += 0.3

        content = (memory.get("content") or "").lower()
        summary = (memory.get("summary") or "").lower()
        if q in content or q in summary:
            score += 0.2

        return score > 0, min(score, 1.0)

    def _is_known_employee(self, requester: str) -> bool:
        """1e：requester 是否已登记员工(active)。无表/异常 → False(零影响)。"""
        try:
            # T2-2: 复用 hub._db 连接工厂(busy_timeout 5000), 热路径不再每次新建连接
            with self.hub._db() as conn:
                c = conn.cursor()
                c.execute("PRAGMA table_info(employee_accounts)")
                cols = {r[1] for r in c.fetchall()}
                if "status" not in cols:
                    return False
                c.execute(
                    "SELECT 1 FROM employee_accounts WHERE employee_id = ? AND status = 'active'",
                    (requester,),
                )
                return c.fetchone() is not None
        except Exception as _e:
            logger.warning("disclosure employee lookup failed (fail-closed False): %s", _e)
            return False

    # ── 核心披露决策 ──

    def _principal_groups(self, agent_id: str) -> set:
        """查 principal_groups 表的组集合（S1）。无表/无数据 → 空集（零影响）。"""
        try:
            # T2-2: 复用 hub._db 连接工厂(busy_timeout 5000)
            with self.hub._db() as conn:
                c = conn.cursor()
                c.execute("SELECT group_dn FROM principal_groups WHERE principal_id = ?", (agent_id,))
                groups = {r[0] for r in c.fetchall()}
                return groups
        except Exception as _e:
            logger.warning("disclosure principal_groups lookup failed (fail-closed empty): %s", _e)
            return set()

    def _calculate_disclosure_level(
        self,
        memory: dict,
        requester: str,
        task: dict,
        required_level: DisclosureLevel,
        requester_info: dict = None,
        owner_info: dict = None,
    ) -> DisclosureLevel:
        """核心披露决策函数。8 条优先级规则链。
        requester_info / owner_info：判定上下文显式 override（XS-012，2026-09-08），
        供跨 Hub 虚拟身份等外部上下文直接传入；为 None 时回退查 hub.agents dict，
        行为与原实现完全一致。"""
        owner = memory["owner_agent_id"]
        policy = self.hub._disclosure_policy

        # 规则 1：自己查自己 → FULL
        if requester == owner:
            return DisclosureLevel.FULL

        # 规则 2：白名单检查
        allowed = json.loads(memory.get("allowed_viewers") or "[]")
        if requester in allowed:
            return DisclosureLevel.FULL

        # 规则 2b（S1 身份接入）：组交集可见性 — requester 组 ∩ owner 组非空 → SUMMARY
        # 组关系来自 principal_groups 表（LDAP/OIDC 同步）；无组数据时跳过（零影响）
        _rg = self._principal_groups(requester)
        _og = self._principal_groups(owner)
        if _rg and _og and (_rg & _og):
            mem_level_2b = DisclosureLevel(memory.get("disclosure_level", "summary"))
            if mem_level_2b != DisclosureLevel.NONE:
                return DisclosureLevel.SUMMARY

        # 规则 3：记忆自身的 NONE 阻断
        mem_level = DisclosureLevel(memory.get("disclosure_level", "summary"))
        if mem_level == DisclosureLevel.NONE:
            return DisclosureLevel.NONE

        # 规则 4：角色关系获取（override 优先，None 时回退 hub.agents dict——XS-012）
        requester_info = requester_info or self.hub.agents.get(requester, {})
        owner_info = owner_info or self.hub.agents.get(owner, {})
        requester_role = (requester_info.get("role") or "").strip()
        owner_role = (owner_info.get("role") or "").strip()
        # 规则 4.5：主体 fail-closed（阶段1/2b+1e）
        # 完全未知主体(非 agent 非员工) → METADATA 封顶, 不再默认 worker 越权
        # 已登记员工(employee_accounts active) → 基础可见性 SUMMARY(FULL 走阶段2 网关+审批)
        if not requester_role:
            if self._is_known_employee(requester):
                if _level_rank(required_level) > _level_rank(DisclosureLevel.SUMMARY):
                    return DisclosureLevel.SUMMARY
                return required_level
            if _level_rank(required_level) > _level_rank(DisclosureLevel.METADATA):
                return DisclosureLevel.METADATA
            return required_level

        # 规则 5：Manager/主管 查看下属记忆
        if requester_role in ["manager", "orchestrator"]:
            managed = requester_info.get("managed_agents", [])
            if owner in managed:
                default_level = policy.get("default_manager_level", "summary")
                if required_level == DisclosureLevel.FULL:
                    return DisclosureLevel.FULL
                return DisclosureLevel(default_level)

        # 规则 6：Orchestrator/店长 全局查看
        if requester_role == "orchestrator":
            agent_policy = requester_info.get("disclosure_policy", {})
            max_level_str = agent_policy.get("max_disclosure") or policy.get("orchestrator_max_level", "full")
            max_level = DisclosureLevel(max_level_str)
            if required_level.value in ("full",):
                return max_level if max_level.value == "full" else DisclosureLevel.SUMMARY
            return required_level

        # 规则 7：同级 Worker，同一任务协作
        if requester_role == "worker" and owner_role == "worker":
            if policy.get("department_peer_visibility", False):
                req_dept = requester_info.get("department", "")
                own_dept = owner_info.get("department", "")
                if req_dept and own_dept and req_dept == own_dept:
                    return DisclosureLevel.SUMMARY
            if policy.get("allow_peer_disclosure", True):
                assigned = task.get("assigned_agent_id", "")
                creator = task.get("creator_agent_id", "")
                if requester in (assigned, creator) or owner in (assigned, creator):
                    return DisclosureLevel.SUMMARY
            return DisclosureLevel.NONE

        # 规则 8：记忆自身的级别上限
        level_map = {
            DisclosureLevel.METADATA: 1,
            DisclosureLevel.SUMMARY: 2,
            DisclosureLevel.FULL: 3,
        }
        if level_map.get(required_level, 0) > level_map.get(mem_level, 0):
            return mem_level

        # 默认：不披露
        return DisclosureLevel.NONE

    def _resolve_memory_domain(self, memory: dict) -> str:
        """XS-001（2026-09-08）读时派生记忆数据域：显式 department 键 → owner 部门
        （hub.agents → DB 兜底 agents / employee_accounts）→ 查不到返回 ""（归公共区）。
        全程 fail-safe：hub.agents 为 None、表不存在/缺列等任何异常静默返回 ""。"""
        dept = (memory.get("department") or "").strip()
        if dept:
            return dept
        owner = memory.get("owner_agent_id") or ""
        if not owner:
            return ""
        agents = getattr(self.hub, "agents", None) or {}
        info = agents.get(owner) or {}
        dept = (info.get("department") or "").strip()
        if dept:
            return dept
        try:
            with self.hub._db() as conn:
                c = conn.cursor()
                try:
                    c.execute("SELECT department FROM agents WHERE agent_id = ?", (owner,))
                    row = c.fetchone()
                    if row and (row[0] or "").strip():
                        return row[0].strip()
                except Exception as _exc:
                    logger.debug("disclosure silent-except @218: %s", _exc)
                try:
                    # employee_id 列名以 auth_provider._lookup_employee_by_key 为准
                    c.execute(
                        "SELECT department FROM employee_accounts WHERE employee_id = ?",
                        (owner,),
                    )
                    row = c.fetchone()
                    if row and (row[0] or "").strip():
                        return row[0].strip()
                except Exception:
                    pass  # employee_accounts 表不存在/缺列 → 静默落空，归公共区
        except Exception as _exc:
            logger.debug("disclosure silent-except @231: %s", _exc)
        return ""

    def disclose_for_principal(
        self,
        memory: dict,
        requester: str,
        task: dict,
        required_level: DisclosureLevel,
        scope: dict = None,
    ) -> DisclosureLevel:
        """S1K（2026-08-07）：带 scoped key 披露判定——原 8 规则链结果再叠 key scope。

        scope: {"endpoints": [...], "data_domain": [...], "level_cap": "summary"}
          level_cap   -> min(原判定, cap)（D5 三方取 min 的 key 侧）
          data_domain -> requester 被限定的数据域；记忆域 = 显式 department 键 → owner 部门
                         （agents/employee_accounts）→ 空 = 公共区（XS-001 读时派生）；
                         域外一律降级 METADATA（fail-closed）
        无 scope -> 行为与原 _calculate_disclosure_level 完全一致（向后兼容）。
        """
        lv = self._calculate_disclosure_level(memory, requester, task, required_level)
        if not scope:
            return lv
        cap = scope.get("level_cap") or ""
        if cap:
            cap_lv = DisclosureLevel(cap) if cap in DisclosureLevel._value2member_map_ else None
            if cap_lv is not None and _level_rank(lv) > _level_rank(cap_lv):
                lv = cap_lv
        domains = scope.get("data_domain") or []
        if domains:
            # XS-001（2026-09-08）fail-closed：空域记忆归公共区，域外一律降 METADATA
            mem_domain = self._resolve_memory_domain(memory)
            if not mem_domain:
                mem_domain = PUBLIC_DOMAIN
            if mem_domain not in domains:
                if _level_rank(lv) > _level_rank(DisclosureLevel.METADATA):
                    lv = DisclosureLevel.METADATA
        return lv

    def _extract_by_level(self, memory: dict, level: DisclosureLevel) -> str:
        """按披露级别提取内容"""
        if level == DisclosureLevel.METADATA:
            return json.dumps({
                "tags": json.loads(memory.get("tags") or "[]"),
                "importance": memory["importance"],
                "created_at": memory["created_at"],
                "access_count": memory.get("access_count", 0),
            }, ensure_ascii=False)
        elif level == DisclosureLevel.SUMMARY:
            return memory.get("summary", memory["content"][:200] + "...")
        elif level == DisclosureLevel.FULL:
            return memory["content"]
        else:
            return ""

    # ── 按需披露请求 ──

    async def request_disclosure(self, req, scope: dict = None) -> dict:
        """按需披露核心入口。查询 → 逐条决策 → 审计日志"""
        hub = self.hub
        async with hub._task_lock:
            task_row = await db_facade.query_one(
                "SELECT * FROM tasks WHERE task_id = ?", (req.task_id,))
            task = _row_dict(task_row) if task_row else {}

            if req.query:
                q_like = f"%{req.query}%"
                memories = await db_facade.query(
                    """SELECT * FROM memory_pool WHERE owner_agent_id = ?
                    AND (content LIKE ? OR summary LIKE ? OR tags LIKE ? OR memory_key LIKE ?)
                    ORDER BY importance DESC, created_at DESC LIMIT 50""",
                    (req.target_agent_id, q_like, q_like, q_like, q_like),
                )
            else:
                memories = await db_facade.query(
                    """SELECT * FROM memory_pool WHERE owner_agent_id = ?
                    ORDER BY importance DESC, created_at DESC LIMIT 20""",
                    (req.target_agent_id,),
                )

            disclosure_results = []
            for mem in memories:
                level = self.disclose_for_principal(
                    memory=_row_dict(mem), requester=req.requester_agent_id,
                    task=task, required_level=req.required_level, scope=scope,
                )
                # A3 shadow 灰度：真实判定后并行跑模拟器，不一致 → 审计告警
                try:
                    from disclosure_rules import shadow_check
                    _warn = shadow_check(
                        memory=_row_dict(mem), requester=req.requester_agent_id,
                        task=task, required_level=req.required_level,
                        actual_level=level, agents=self.hub.agents,
                        policy=self.hub._disclosure_policy,
                        db_path=CONFIG.DB_PATH,
                    )
                    if _warn:
                        await self.hub._log_event(
                            "disclosure_shadow_mismatch", req.requester_agent_id,
                            {"memory_id": mem["memory_id"], **_warn})
                except Exception as _e:
                    # T2-2: shadow 安全网故障必须可观测(静默=灰度保护失效无告警)
                    logger.warning("disclosure shadow_check failed: %s", _e)
                if level == DisclosureLevel.NONE:
                    continue

                disclosed_content = self._extract_by_level(_row_dict(mem), level)

                if req.query:
                    matched, relevance = self._match_query(_row_dict(mem), req.query)
                    if not matched:
                        continue
                else:
                    relevance = 0.0

                disclosure_results.append({
                    "memory_id": mem["memory_id"], "owner": mem["owner_agent_id"],
                    "disclosure_level": level.value, "content": disclosed_content,
                    "tags": json.loads(mem["tags"] or "[]"),
                    "importance": mem["importance"],
                    "relevance_score": round(relevance, 2),
                })

                await self._log_disclosure(
                    task_id=req.task_id, from_agent=req.target_agent_id,
                    to_agent=req.requester_agent_id, memory_id=mem["memory_id"],
                    level=level, content=disclosed_content[:100], reason="task_scheduling",
                )

                await db_facade.run_in_conn(
                    partial(_bump_memory_access,
                            accessed_at=datetime.now(timezone.utc).isoformat(),
                            memory_id=mem["memory_id"]),
                    write=True,
                )

            return {"task_id": req.task_id, "disclosed_count": len(disclosure_results), "memories": disclosure_results}

    # ── 语义搜索 ──

    async def semantic_search(self, req: SemanticSearchRequest, scope: dict = None) -> dict:
        """基于 ChromaDB 的语义搜索（CD-016: ChromaDB 故障时降级 SQLite 关键词 + degraded 标记）"""
        hub = self.hub
        degraded = False
        if hub._chroma_collection is None:
            # CD-016: ChromaDB 不可用（未初始化/初始化失败）→ 直接降级，不再返回 error
            degraded = True

        model = None
        query_emb = None
        if not degraded:
            model = await hub._ensure_embedding_model()
            if model is None:
                degraded = True

        if not degraded:
            try:
                loop = asyncio.get_event_loop()
                query_emb = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: model.encode(req.query).tolist()),
                    timeout=15,
                )
            except (asyncio.TimeoutError, RuntimeError, ValueError, TypeError) as e:
                logger.warning(f"[semantic_search embedding 失败] {type(e).__name__}: {e}")
                degraded = True

        memories = []
        if not degraded:
            try:
                memories = await self._chroma_search(req, query_emb, scope)
            except Exception as e:
                logger.warning(
                    f"[semantic_search ChromaDB 查询失败] {type(e).__name__}: {str(e)[:120]} — 降级 SQLite 关键词")
                degraded = True

        if degraded:
            # 降级：SQLite 关键词检索（content LIKE）+ 披露级别过滤
            memories = await self._sqlite_keyword_search(req, scope)

        result = {"query": req.query, "total": len(memories), "memories": memories}
        if degraded:
            result["degraded"] = True
        return result

    async def _chroma_search(self, req, query_emb, scope: dict = None) -> list:
        """ChromaDB 向量检索（含披露级别过滤）"""
        hub = self.hub
        where_filter = {}
        if req.filter_owner:
            where_filter["owner"] = req.filter_owner

        results = hub._chroma_collection.query(
            query_embeddings=[query_emb], n_results=req.n_results,
            where=where_filter if where_filter else None,
        )

        memories = []
        for i in range(len(results["ids"][0])):
            mem_id = results["ids"][0][i]
            metadata = results["metadatas"][0][i]
            distance = results["distances"][0][i] if results.get("distances") else 0

            row = await db_facade.query_one(
                "SELECT * FROM memory_pool WHERE memory_id = ?", (mem_id,))
            if not row:
                continue

            mem_dict = _row_dict(row)
            level = self.disclose_for_principal(
                memory=mem_dict, requester=req.requester_agent_id,
                task={}, required_level=DisclosureLevel.SUMMARY, scope=scope,
            )
            if level == DisclosureLevel.NONE:
                continue

            content = self._extract_by_level(mem_dict, level)
            memories.append({
                "memory_id": mem_id, "owner": metadata.get("owner", ""),
                "disclosure_level": level.value, "content": content,
                "tags": json.loads(mem_dict.get("tags") or "[]"),
                "importance": mem_dict.get("importance", 0),
                "similarity": round(1 - distance, 4),
            })
        return memories

    async def _sqlite_keyword_search(self, req, scope: dict = None) -> list:
        """降级：SQLite 关键词检索（content LIKE，disclosure_level != none）+ 披露级别过滤"""
        hub = self.hub

        # 构造 LIKE 条件（保留中文/字母数字 token，避免空查询）
        import re as _re
        tokens = _re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", req.query or "")[:10]
        like_clauses = []
        params = []
        for t in tokens:
            like_clauses.append("(content LIKE ? OR summary LIKE ?)")
            params.extend([f"%{t}%", f"%{t}%"])
        if not like_clauses:
            like_clauses = ["1=1"]

        where_owner = ""
        params_owner = []
        if req.filter_owner:
            where_owner = "AND owner_agent_id = ?"
            params_owner = [req.filter_owner]

        rows = await db_facade.query(
            f"""SELECT * FROM memory_pool
                WHERE disclosure_level != 'none'
                AND ({' OR '.join(like_clauses)})
                {where_owner}
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?""",
            params + params_owner + [req.n_results],
        )

        memories = []
        for row in rows:
            mem_dict = _row_dict(row)
            level = self.disclose_for_principal(
                memory=mem_dict, requester=req.requester_agent_id,
                task={}, required_level=DisclosureLevel.SUMMARY, scope=scope,
            )
            if level == DisclosureLevel.NONE:
                continue
            content = self._extract_by_level(mem_dict, level)
            memories.append({
                "memory_id": mem_dict.get("memory_id", ""),
                "owner": mem_dict.get("owner_agent_id", ""),
                "disclosure_level": level.value, "content": content,
                "tags": json.loads(mem_dict.get("tags") or "[]"),
                "importance": mem_dict.get("importance", 0),
                "similarity": 0.0,
            })
        return memories

    # ── 披露阶段执行 ──

    async def _execute_disclosure_phase(self, task_id: str, agent_id: str, phase: int) -> dict:
        """执行披露计划的某个阶段"""
        hub = self.hub
        row = await db_facade.query_one(
            "SELECT disclosure_plan FROM tasks WHERE task_id = ?", (task_id,))
        if not row:
            return {"count": 0, "memories": []}

        plan = json.loads(row["disclosure_plan"] or "{}")
        phase_config = next((p for p in plan.get("phases", []) if p["phase"] == phase), None)
        if not phase_config:
            return {"count": 0, "memories": []}

        level = DisclosureLevel(phase_config["level"])
        creator_row = await db_facade.query_one(
            "SELECT creator_agent_id FROM tasks WHERE task_id = ?", (task_id,))
        creator = creator_row["creator_agent_id"]
        memories = await db_facade.query(
            "SELECT * FROM memory_pool WHERE owner_agent_id = ? AND disclosure_level != 'none' ORDER BY importance DESC LIMIT 10",
            (creator,),
        )
        results = []
        for mem in memories:
            content = self._extract_by_level(_row_dict(mem), level)
            results.append({"memory_id": mem["memory_id"], "level": level.value, "content": content})
        return {"count": len(results), "memories": results}

    # ── 审计日志 ──

    async def _log_disclosure(self, **kwargs):
        """披露审计（XS-003，2026-09-08）：INSERT + 披露链 hash 回填合并为同一事务，
        写链失败整体回滚并抛错——披露成功 ⇔ 审计必落链（防无痕披露；与旧实现
        INSERT 裸写失败会 500 的行为一致，不静默吞错）。"""
        hub = self.hub
        try:
            from logfmt import get_trace_id
            trace_id = get_trace_id() or ""
        except Exception:
            trace_id = ""
        from audit_chain import DisclosureChain
        DisclosureChain(CONFIG.DB_PATH).append_row({
            "task_id": kwargs.get("task_id", "remote"),
            "from_agent_id": kwargs.get("from_agent", ""),
            "to_agent_id": kwargs.get("to_agent", ""),
            "memory_id": kwargs.get("memory_id", ""),
            "disclosed_level": kwargs.get("level", DisclosureLevel.NONE).value
                if hasattr(kwargs.get("level", DisclosureLevel.NONE), "value")
                else str(kwargs.get("level", "NONE")),
            "disclosed_content": (kwargs.get("content") or "")[:200],
            "disclosed_at": datetime.now(timezone.utc).isoformat(),
            "reason": kwargs.get("reason", ""),
            "trace_id": trace_id,
        })

    async def disclose_for_remote(self, virtual_agent: dict, target_agent_id: str,
                                   query: str, required_level: str) -> dict:
        """跨 Hub 披露——虚拟身份代入 8 条规则。
        virtual_agent = {agent_id, role, department, managed_agents} — 来自 team_members 表。
        XS-012（2026-09-08）：不再注入/弹出 hub.agents dict，判定纯函数化——
        虚拟身份通过 requester_info override 显式传入 _calculate_disclosure_level。
        方法保留 async 签名（调用方约定），内部不再需要 hub._lock。
        """
        hub = self.hub
        if query:
            q_like = f"%{query}%"
            memories = await db_facade.query(
                """SELECT * FROM memory_pool WHERE owner_agent_id = ?
                AND (content LIKE ? OR summary LIKE ? OR tags LIKE ? OR memory_key LIKE ?)
                ORDER BY importance DESC, created_at DESC LIMIT 50""",
                (target_agent_id, q_like, q_like, q_like, q_like),
            )
        else:
            memories = await db_facade.query(
                """SELECT * FROM memory_pool WHERE owner_agent_id = ?
                ORDER BY importance DESC, created_at DESC LIMIT 20""",
                (target_agent_id,),
            )

        # XS-012（2026-09-08）：判定纯函数化，虚拟身份经 requester_info override 传入，
        # 不再注入/弹出 hub.agents dict，也不需要 hub._lock
        results = []
        for mem in memories:
            mem_dict = _row_dict(mem)
            level = self._calculate_disclosure_level(
                memory=mem_dict,
                requester=virtual_agent["agent_id"],
                task={},
                required_level=DisclosureLevel(required_level),
                requester_info=virtual_agent,
            )
            if level == DisclosureLevel.NONE:
                continue
            content = self._extract_by_level(mem_dict, level)
            if query:
                matched, _ = self._match_query(mem_dict, query)
                if not matched:
                    continue
            results.append({
                "memory_id": mem_dict.get("memory_id", ""),
                "owner": mem_dict.get("owner_agent_id", ""),
                "disclosure_level": level.value,
                "content": content,
                "tags": json.loads(mem_dict.get("tags") or "[]"),
            })

        return {"disclosed_count": len(results), "memories": results}
    # ═══════════════════════════════════════════════════════════
    # H3 防拼接滑窗（附录 E v1.4，2026-08-06）
    # E.3：跨请求累计窗口 — (requester, parent_doc_id) 24h 滑窗
    # 累计披露 chunk 比例 > 50% → 该 requester 就此文档降级 + 审计 + 通知
    # parent_hint 只给布尔量（存在关联内容），不给 total_chunks（防结构泄露）
    # ═══════════════════════════════════════════════════════════
    _CHUNK_WINDOW_SEC = 24 * 3600      # 24h 滑窗
    _CHUNK_QUOTA_RATIO = 0.5           # 累计披露 >50% 触发降级

    def _chunk_quota_check(self, requester, doc_id, total_chunks):
        """滑窗累计检查：24h 内 requester 已披露 chunk 数 / 文档总块数 > 50% -> 降级"""
        if total_chunks <= 0:
            return False
        key = (requester, doc_id)
        now = time.time()
        with self._chunk_window_lock:
            window = self._chunk_window.get(key, [])
            window = [(ts, cid) for ts, cid in window if now - ts < self._CHUNK_WINDOW_SEC]
            disclosed = len(window)
            return disclosed / total_chunks > self._CHUNK_QUOTA_RATIO

    def _chunk_window_add(self, requester, doc_id, chunk_ids):
        """登记本次披露的 chunk（滑窗累计）"""
        if not chunk_ids:
            return
        key = (requester, doc_id)
        now = time.time()
        with self._chunk_window_lock:
            window = self._chunk_window.get(key, [])
            window = [(ts, cid) for ts, cid in window if now - ts < self._CHUNK_WINDOW_SEC]
            for cid in chunk_ids:
                window.append((now, cid))
            self._chunk_window[key] = window

    async def search_chunks(self, requester, query, doc_id="", limit=10):
        """H3 chunk 检索（含披露过滤 + 防拼接滑窗）。

        逐 chunk 跑 8 规则链，结果级别 = min(请求方判定, chunk 存储级别)（E.2 r8 语义）。
        滑窗累计 >50% -> 本次降级（只返回 summary 级）+ 审计。
        parent_hint 只给布尔量（E.3：total_chunks 移除防结构泄露）。
        """
        hub = self.hub

        where = []
        params = []
        if doc_id:
            where.append("parent_doc_id = ?")
            params.append(doc_id)
        import re as _re
        tokens = _re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", query or "")[:10]
        if tokens:
            like_parts = []
            for t in tokens:
                like_parts.append("(content LIKE ? OR summary LIKE ?)")
                params.extend([f"%{t}%", f"%{t}%"])
            where.append("(" + " OR ".join(like_parts) + ")")

        # 总块数（防拼接比例分母）——只用于内部滑窗判定，不返回给客户端
        total_row = await db_facade.query_one(
            "SELECT COUNT(*) FROM document_chunks WHERE parent_doc_id = ?", (doc_id,))
        total = total_row[0] if doc_id else 0

        sql = "SELECT * FROM document_chunks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY piece_index ASC LIMIT ?"
        params.append(limit)
        rows = await db_facade.query(sql, params)

        if not rows:
            return {"status": "ok", "results": [], "parent_hint": bool(doc_id)}

        degraded = False
        quota_hit = bool(doc_id) and self._chunk_quota_check(requester, doc_id, total)
        results = []
        disclosed_ids = []
        for row in rows:
            chunk = _row_dict(row)
            chunk["owner_agent_id"] = chunk.get("source_agent_id") or ""
            level = self._calculate_disclosure_level(
                memory=chunk, requester=requester, task={},
                required_level=DisclosureLevel.SUMMARY,
            )
            if level == DisclosureLevel.NONE:
                continue
            stored = DisclosureLevel(chunk.get("disclosure_level") or "summary")
            if stored == DisclosureLevel.NONE:
                continue
            if _level_rank(level) > _level_rank(stored):
                level = stored
            if quota_hit and level == DisclosureLevel.FULL:
                level = DisclosureLevel.SUMMARY
                degraded = True
            content = self._extract_by_level(chunk, level)
            results.append({
                "chunk_id": chunk.get("chunk_id", ""),
                "piece_index": chunk.get("piece_index", 0),
                "disclosure_level": level.value,
                "content": content,
            })
            disclosed_ids.append(chunk.get("chunk_id", ""))

        if doc_id and disclosed_ids:
            self._chunk_window_add(requester, doc_id, disclosed_ids)
            if quota_hit:
                await hub._log_event("chunk_disclosure_quota", requester, {
                    "parent_doc_id": doc_id, "degraded": True,
                    "disclosed_in_window": len(disclosed_ids), "total_chunks": total,
                })

        return {
            "status": "ok",
            "results": results,
            "parent_hint": bool(doc_id),  # E.3：只给布尔量，不给 total_chunks
            "degraded": degraded,
        }
