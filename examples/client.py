#!/usr/bin/env python3
"""
Hermes Sync Client SDK - 渐进式披露架构
案例场景：企业 6 人客服团队

使用方式：
  1. 启动 Hub：python main.py
  2. 启动本客户端（3 个终端分别启动不同角色）：
     python client.py --role worker --id cs-wang
     python client.py --role manager --id tl-li
     python client.py --role orchestrator --id store-manager

业务场景说明（面向小公司老板）：
  企业有 6 个客服（小王、小李、小张...）、2 个主管、1 个店长。
  - 客服接待客户，写工单到自己的记忆池（其他人看不见）
  - 主管可以查看自己团队客服的工单摘要（不能看完整内容）
  - 店长可以全局调度、分配任务（涉及隐私的部分受策略限制）
  - 遇到纠纷需要完整记录时，可以申请"提升披露级别"
"""

import asyncio
import json
import time
import os
import sqlite3
import argparse
from typing import Optional, Callable, List, Dict, Any
from dataclasses import dataclass, field

import aiohttp


@dataclass
class SyncConfig:
    hub_url: str = "http://localhost:3060"
    agent_id: str = ""
    agent_name: str = ""
    department: str = ""                    # 部门名（如"客服部"、"售后部"）
    capabilities: List[str] = field(default_factory=lambda: ["customer_service"])
    role: str = "worker"                    # worker | manager | orchestrator
    managed_agents: List[str] = field(default_factory=list)
    endpoint: str = ""
    heartbeat_interval: int = 25
    auto_reconnect: bool = True
    api_key: str = ""                       # P4: API 认证密钥


# ============ 本地缓存（P3：离线写入+补传） ============

class LocalCache:
    """本地 SQLite 缓存，用于离线写入和自动补传"""

    def __init__(self, agent_id: str):
        db_path = f"./{agent_id}_local_cache.db"
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS local_memory (
                local_id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_key TEXT,
                content TEXT,
                summary TEXT,
                importance REAL DEFAULT 1.0,
                tags TEXT,
                disclosure_level TEXT DEFAULT 'summary',
                disclosure_scope TEXT DEFAULT 'manager',
                allowed_viewers TEXT,
                created_at TEXT,
                synced INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()

    def store(self, memory_data: dict) -> int:
        """写入本地缓存，synced=0，返回 local_id"""
        import datetime
        now = datetime.datetime.utcnow().isoformat()
        c = self.conn.execute("""
            INSERT INTO local_memory
            (memory_key, content, summary, importance, tags, disclosure_level,
             disclosure_scope, allowed_viewers, created_at, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            memory_data.get("memory_key", ""),
            memory_data.get("content", ""),
            memory_data.get("summary") or (memory_data.get("content", "")[:200] + "..."),
            memory_data.get("importance", 1.0),
            json.dumps(memory_data.get("tags") or []),
            memory_data.get("disclosure_level", "summary"),
            memory_data.get("disclosure_scope", "manager"),
            json.dumps(memory_data.get("allowed_viewers") or []),
            now,
        ))
        self.conn.commit()
        return c.lastrowid

    def mark_synced(self, local_id: int):
        """标记已同步"""
        self.conn.execute(
            "UPDATE local_memory SET synced = 1 WHERE local_id = ?",
            (local_id,)
        )
        self.conn.commit()

    def get_unsynced(self) -> list:
        """获取所有未同步的记忆"""
        rows = self.conn.execute(
            "SELECT * FROM local_memory WHERE synced = 0 ORDER BY local_id"
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def get_all_unsynced_payloads(self) -> list:
        """获取未同步记忆的请求体列表"""
        unsynced = self.get_unsynced()
        payloads = []
        for row in unsynced:
            payloads.append({
                "local_id": row["local_id"],
                "payload": {
                    "memory_key": row["memory_key"],
                    "content": row["content"],
                    "summary": row["summary"],
                    "importance": row["importance"],
                    "tags": json.loads(row["tags"] or "[]"),
                    "disclosure_level": row["disclosure_level"],
                    "disclosure_scope": row["disclosure_scope"],
                    "allowed_viewers": json.loads(row["allowed_viewers"] or "[]"),
                }
            })
        return payloads

    def cleanup_old(self, days: int = 7):
        """清理 7 天前已同步的记忆"""
        self.conn.execute(
            "DELETE FROM local_memory WHERE synced = 1 AND created_at < datetime('now', ?)",
            (f'-{days} days',)
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


class HermesSyncClient:
    """
    Hermes Agent 同步客户端

    核心行为：
      - 写入隔离：写入自己的记忆池，零广播零通知
      - 按需查询：通过 Hub 查其他 Agent 的记忆，受披露策略控制
      - 任务协作：接收调度通知，可申请提升披露级别

    企业使用示例：
      客服小王 → write_memory("客户李姐对柜门颜色有疑问")
      主管小李 → query_memory("小王", "柜门")  → 看到摘要
      店长 → create_task("处理李姐投诉")  → 调度给小王
    """

    def __init__(self, config: SyncConfig):
        self.config = config
        if not self.config.endpoint:
            self.config.endpoint = f"http://{self.config.agent_id}.internal"

        self.session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._cache = LocalCache(config.agent_id)  # P3: 本地缓存
        self._sync_backoff = 30  # P3: 初始同步间隔 30s
        self._sync_failures = 0  # P3: 连续失败计数

    async def _get_headers(self):
        """构建请求头，含认证 token"""
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    # ============ 连接管理 ============

    async def connect(self):
        self.session = aiohttp.ClientSession()
        await self._register()
        self._running = True
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._sync_loop())  # P3: 启动离线补传
        # P3: 连接后立即触发一次补传
        asyncio.create_task(self._sync_once())
        print(f"[{self.config.agent_name}] ✅ 已连接 Hub ({self.config.hub_url})")
        self._print_role_summary()

    async def disconnect(self):
        self._running = False
        if self.session:
            await self.session.close()
        self._cache.cleanup_old()  # P3: 退出前清理过期缓存
        self._cache.close()

    def _print_role_summary(self):
        role_names = {"worker": "客服/执行者", "manager": "主管", "orchestrator": "店长/调度者"}
        print(f"   角色：{role_names.get(self.config.role, self.config.role)}")
        if self.config.managed_agents:
            print(f"   管理团队：{', '.join(self.config.managed_agents)}")
        print()

    async def _register(self):
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/agents/register",
            json={
                "agent_id": self.config.agent_id,
                "agent_name": self.config.agent_name,
                "department": self.config.department,
                "capabilities": self.config.capabilities,
                "role": self.config.role,
                "managed_agents": self.config.managed_agents,
                "endpoint": self.config.endpoint,
            },
        ) as resp:
            result = await resp.json()
            # P4: 保存返回的 api_key（仅首次注册返回明文）
            if result.get("api_key") and not self.config.api_key:
                self.config.api_key = result["api_key"]
                print(f"[{self.config.agent_name}] 注册成功，已获取 API Key")
            else:
                print(f"[{self.config.agent_name}] 注册成功")

    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(self.config.heartbeat_interval)
            async with self.session.post(
                f"{self.config.hub_url}/api/v1/agents/{self.config.agent_id}/heartbeat",
                headers=await self._get_headers(),
            ) as resp:
                if resp.status != 200:
                    print(f"[{self.config.agent_name}] ⚠️ 心跳失败")

    # ============ 核心 API ============

    async def write_memory(
        self,
        key: str,
        content: str,
        importance: float = 1.0,
        tags: List[str] = None,
        disclosure_level: str = "summary",
        disclosure_scope: str = "manager",
    ) -> dict:
        """
        写入自己的记忆池。
        【不广播，不通知任何人】

        P3: 先写本地缓存（永远成功），再尝试同步到 Hub。
        Hub 不可用时保持本地，恢复后自动补传。

        案例场景：
          客服小王接待完客户后，把工单记下来，
          只有小王自己能看见完整内容。
          主管可以看摘要（如果 scope 设为 manager）。
        """
        payload = {
            "memory_key": key,
            "content": content,
            "importance": importance,
            "tags": tags or [],
            "disclosure_level": disclosure_level,
            "disclosure_scope": disclosure_scope,
            "allowed_viewers": [],
        }

        # P3: 1. 先写本地缓存（永远成功）
        local_id = self._cache.store(payload)

        # P3: 2. 尝试同步到 Hub
        try:
            headers = await self._get_headers()
            async with self.session.post(
                f"{self.config.hub_url}/api/v1/memory/store",
                params={"agent_id": self.config.agent_id},
                json=payload,
                headers=headers,
            ) as resp:
                result = await resp.json()
                # 精准标记：用 store() 返回的 local_id
                self._cache.mark_synced(local_id)
        except Exception:
            pass  # Hub 不可用，本地已缓存，补传循环会处理

        level_names = {
            "none": "不披露",
            "metadata": "仅元数据",
            "summary": "摘要可见",
            "full": "完整可见",
        }
        print(
            f"  📝 记忆已写入 [{key}] "
            f"(级别: {level_names.get(disclosure_level, disclosure_level)})"
        )
        return {"status": "stored", "memory_key": key}

    async def _sync_once(self):
        """执行一次补传"""
        payloads = self._cache.get_all_unsynced_payloads()
        if not payloads:
            return
        headers = await self._get_headers()
        synced_count = 0
        for p in payloads:
            try:
                async with self.session.post(
                    f"{self.config.hub_url}/api/v1/memory/store",
                    params={"agent_id": self.config.agent_id},
                    json=p["payload"],
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        self._cache.mark_synced(p["local_id"])
                        synced_count += 1
            except Exception:
                break  # 失败则停止，等下一轮
        if synced_count:
            self._sync_failures = 0
            self._sync_backoff = 30
            print(f"[{self.config.agent_name}] 📡 补传 {synced_count} 条离线记忆到 Hub")

    async def _sync_loop(self):
        """后台补传循环（P3）"""
        await asyncio.sleep(self._sync_backoff)
        while self._running:
            try:
                await self._sync_once()
            except Exception:
                self._sync_failures += 1
                # 连续失败 3 次后降频到 2 分钟
                if self._sync_failures >= 3:
                    self._sync_backoff = 120
            await asyncio.sleep(self._sync_backoff)

    async def query_peer_memory(
        self,
        target_agent: str,
        query: str = "",
        task_id: str = "default",
        required_level: str = "summary",
    ) -> List[dict]:
        """
        通过 Hub 查询其他 Agent 的记忆。
        【受披露策略控制，可能只返回摘要或元数据】

        案例场景：
          主管小李想看看小王今天接待了哪些客户，
          如果查到的只是摘要 → "客户李姐对柜门颜色有疑问"
          如果 info 不够 → 可以申请提升级别看完整内容
        """
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/memory/disclose",
            json={
                "task_id": task_id,
                "requester_agent_id": self.config.agent_id,
                "target_agent_id": target_agent,
                "query": query,
                "required_level": required_level,
            },
            headers=await self._get_headers(),
        ) as resp:
            result = await resp.json()
            memories = result.get("memories", [])
            print(
                f"  🔍 查询 [{target_agent}] → "
                f"返回 {result.get('disclosed_count', 0)} 条 "
                f"(申请级别: {required_level})"
            )
            return memories

    async def create_task(
        self,
        task_id: str,
        description: str,
        required_capabilities: List[str] = None,
        required_memories: List[str] = None,
        priority: int = 1,
    ) -> dict:
        """
        创建任务，等待 Hub 调度。

        案例场景：
          店长：安排小王处理客户李姐的橱柜投诉
          → 创建任务，Hub 匹配到小王（能力匹配）→ 推送给小王
          → 第一阶段只给任务描述
          → 小王需要更多信息时再申请升级
        """
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/tasks/create",
            json={
                "task_id": task_id,
                "description": description,
                "creator_agent_id": self.config.agent_id,
                "required_capabilities": required_capabilities or [],
                "required_memories": required_memories or [],
                "priority": priority,
            },
            headers=await self._get_headers(),
        ) as resp:
            result = await resp.json()
            print(f"  📋 任务创建 [{task_id}]: {description[:50]}...")
            return result

    async def request_more_info(
        self, task_id: str, reason: str
    ) -> dict:
        """
        申请提升披露级别。

        场景：
          小王接到"处理李姐投诉"的任务，
          目前只知道是"柜门颜色问题"（摘要级别），
          但客户情绪激动需要完整的沟通历史 → 申请升级
        """
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/tasks/{task_id}/advance",
            params={
                "agent_id": self.config.agent_id,
                "reason": reason,
            },
            headers=await self._get_headers(),
        ) as resp:
            result = await resp.json()
            print(
                f"  ⬆️ 披露升级 [{task_id}]: "
                f"阶段 {result.get('new_phase', '?')} "
                f"({reason})"
            )
            return result

    async def schedule_task(self, task_id: str) -> dict:
        """触发任务调度（Orchestrator/Manager 使用）"""
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/tasks/{task_id}/schedule",
            headers=await self._get_headers(),
        ) as resp:
            result = await resp.json()
            print(
                f"  🚀 任务调度 [{task_id}] → "
                f"分配给 {result.get('assigned_to', '无')}, "
                f"披露阶段 {result.get('disclosure_phase', 1)}"
            )
            return result

    async def get_dashboard(self) -> dict:
        """获取监控面板数据"""
        async with self.session.get(
            f"{self.config.hub_url}/api/v1/dashboard",
            headers=await self._get_headers(),
        ) as resp:
            return await resp.json()

    async def semantic_search(
        self, query: str, n_results: int = 10, filter_owner: str = None
    ) -> list:
        """语义搜索：基于 ChromaDB 在记忆池中查找语义相似的内容"""
        body = {
            "query": query,
            "requester_agent_id": self.config.agent_id,
            "n_results": n_results,
        }
        if filter_owner:
            body["filter_owner"] = filter_owner
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/memory/semantic_search",
            json=body,
            headers=await self._get_headers(),
        ) as resp:
            return await resp.json()

    # ============ 任务生命周期方法（P6） ============

    async def start_task(self, task_id: str) -> dict:
        """开始执行任务: assigned → in_progress"""
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/tasks/{task_id}/start",
            params={"agent_id": self.config.agent_id},
            headers=await self._get_headers(),
        ) as resp:
            result = await resp.json()
            print(f"  ▶️ 任务开始 [{task_id}]: {result.get('new_status', result.get('status', ''))}")
            return result

    async def complete_task(self, task_id: str, result: str = "") -> dict:
        """完成任务: in_progress → completed"""
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/tasks/{task_id}/complete",
            params={"agent_id": self.config.agent_id, "result": result},
            headers=await self._get_headers(),
        ) as resp:
            data = await resp.json()
            print(f"  ✅ 任务完成 [{task_id}]: {data.get('status', '')}")
            return data

    async def fail_task(self, task_id: str, reason: str = "") -> dict:
        """任务失败: in_progress → failed"""
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/tasks/{task_id}/fail",
            params={"agent_id": self.config.agent_id, "reason": reason},
            headers=await self._get_headers(),
        ) as resp:
            result = await resp.json()
            print(f"  ❌ 任务失败 [{task_id}]: {result.get('reason', '')}")
            return result

    async def cancel_task(self, task_id: str) -> dict:
        """取消任务: 任意非终态 → cancelled"""
        async with self.session.post(
            f"{self.config.hub_url}/api/v1/tasks/{task_id}/cancel",
            params={"agent_id": self.config.agent_id},
            headers=await self._get_headers(),
        ) as resp:
            result = await resp.json()
            print(f"  🚫 任务取消 [{task_id}]")
            return result

    # ============ 通知系统（P2B） ============

    async def start_notification_listener(self, callback: Callable = None):
        """
        连接通知 WebSocket，接收实时推送。

        通知类型：
          - disclosure_request: 披露升级请求
          - task_assigned: 新任务分配
          - task_completed: 你创建的任务已完成
          - task_failed: 任务失败
          - task_cancelled: 任务取消
          - agent_offline: 你管理的 Agent 掉线
          - agent_online: 你管理的 Agent 上线
        """
        api_key = self.config.api_key
        ws_url = f"{self.config.hub_url.replace('http', 'ws')}/ws/{self.config.agent_id}"
        if api_key:
            ws_url += f"?api_key={api_key}"

        async with self.session.ws_connect(ws_url) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type") or data.get("msg_type")
                        if msg_type and msg_type != "pong":
                            self._handle_notification(msg_type, data, callback)
                    except json.JSONDecodeError:
                        pass

    def _handle_notification(self, msg_type: str, data: dict, callback: Callable = None):
        """处理收到的通知"""
        handlers = {
            "disclosure_request": lambda: print(
                f"  🔔 [通知] 披露升级请求: {data.get('agent_id')} → 任务 {data.get('task_id')} ({data.get('reason', '')})"
            ),
            "task_assigned": lambda: print(
                f"  🔔 [通知] 新任务: {data.get('task_id')} — {data.get('description', '')}"
            ),
            "task_completed": lambda: print(
                f"  🔔 [通知] 任务完成: {data.get('task_id')} by {data.get('agent_id')}"
            ),
            "task_failed": lambda: print(
                f"  🔔 [通知] 任务失败: {data.get('task_id')} — {data.get('reason', '')}"
            ),
            "agent_offline": lambda: print(
                f"  🔔 [通知] Agent 掉线: {data.get('agent_id')}"
            ),
            "agent_online": lambda: print(
                f"  🔔 [通知] Agent 上线: {data.get('agent_id')}"
            ),
            "task_cancelled": lambda: print(
                f"  🔔 [通知] 任务取消: {data.get('task_id')}"
            ),
        }

        if msg_type in handlers:
            handlers[msg_type]()

        if callback:
            callback(msg_type, data)


# ============ 演示场景：企业客服系统 ============

async def demo_customer_service_scenario():
    """
    演示场景：企业 3 个角色的协同工作流

    角色：
      1. 小王 (cs-wang) — 客服 Worker
      2. 主管小李 (tl-li) — Manager
      3. 店长 (store-manager) — Orchestrator

    流程：
      Step 1: 小王接待客户 → 写入记忆（摘要级别，仅主管可见）
      Step 2: 主管查看团队工作 → 看到摘要
      Step 3: 店长创建投诉处理任务 → 调度给小王
      Step 4: 小王接受任务，但信息不足 → 申请升级
      Step 5: 店长授权升级 → 小王获得完整历史
    """

    # ====== Step 1: 启动三个角色 ======
    print("=" * 60)
    print("  企业 · 多 Agent 协同演示")
    print("  案例场景：客户李姐橱柜门板色差投诉")
    print("=" * 60)
    print()

    # 客服小王
    wang = HermesSyncClient(SyncConfig(
        hub_url="http://localhost:3060",
        agent_id="cs-wang",
        agent_name="小王",
        department="客服一部",
        role="worker",
        capabilities=["customer_service", "after_sales", "橱柜"],
    ))

    # 主管小李
    li = HermesSyncClient(SyncConfig(
        hub_url="http://localhost:3060",
        agent_id="tl-li",
        agent_name="小李",
        department="客服一部",
        role="manager",
        capabilities=["team_manage", "customer_service"],
        managed_agents=["cs-wang", "cs-zhang"],
    ))

    # 店长
    boss = HermesSyncClient(SyncConfig(
        hub_url="http://localhost:3060",
        agent_id="store-manager",
        agent_name="店长老陈",
        department="总经办",
        role="orchestrator",
        capabilities=["orchestration", "task_dispatch"],
        managed_agents=["tl-li", "cs-wang", "cs-zhang"],
    ))

    await wang.connect()
    await li.connect()
    await boss.connect()

    # ====== Step 2: 小王写入记忆（写入隔离） ======
    print("\n" + "-" * 50)
    print("[Day 1] 小王接待客户后写工单")
    print("-" * 50)

    await wang.write_memory(
        key="customer-李姐-20260301",
        content="""
客户姓名：李姐
联系方式：138****5678
订单号：ORD-2026-0089
产品：企业·法式系列 橱柜门板（型号 H-338）
问题描述：客户反映安装完成后，右侧门板与左侧门板存在明显色差。
右侧偏黄，左侧偏白。客户情绪较为激动，要求更换整组门板。
处理进展：已安排安装师傅小王下周三上门复尺，确认色差程度。
如果确实是批次问题，按公司政策走退换货流程。
客户要求补偿：客户提出因等待更换导致装修延误，要求 500 元补偿。
备注：客户是某装修公司的设计师介绍的，态度较强硬但讲道理。
        """.strip(),
        importance=0.9,
        tags=["客户投诉", "橱柜", "色差", "售后工单"],
        disclosure_level="summary",       # 默认：摘要级别
        disclosure_scope="manager",       # 范围：仅主管可见摘要
    )
    print("  （写入完成，其他 Agent 未收到任何通知 ✅）")

    # ====== Step 3: 小王又写一条（只有标签可见） ======
    await wang.write_memory(
        key="customer-李姐-跟进-20260303",
        content="""
跟进记录 - 2026年3月3日
与李姐电话沟通，同意先上门确认色差再决定方案。
客户情绪好转，表示如果只是轻微色差可以接受局部调整。
安装师傅已预约下周三（3月8日）上午上门。
已通知仓库准备备件（门板 H-338 右侧 x1 + 左侧 x1）。
        """.strip(),
        importance=0.7,
        tags=["客户跟进", "橱柜", "色差"],
        disclosure_level="metadata",      # 只允许看标签和重要性
        disclosure_scope="manager",
    )
    print("  （第二条写入完成，仅标签可见 ✅）")

    # ====== Step 4: 主管查看团队工作 ======
    print("\n" + "-" * 50)
    print("[Day 2] 主管小李查看小王今天的工作记录")
    print("-" * 50)

    memories = await li.query_peer_memory(
        target_agent="cs-wang",
        query="",
        required_level="summary",
    )

    print(f"\n  主管小李看到 {len(memories)} 条记录：")
    for m in memories:
        level_icon = {
            "metadata": "🔖",
            "summary": "📄",
            "full": "📃",
            "none": "🚫",
        }
        icon = level_icon.get(m["disclosure_level"], "❓")
        content_preview = json.loads(m["content"]) if m["disclosure_level"] == "metadata" else m["content"]
        if isinstance(content_preview, dict):
            tags = ", ".join(content_preview.get("tags", []))
            print(f"  {icon} [importance={content_preview.get('importance','?')}] 标签: {tags}")
        else:
            print(f"  {icon} {content_preview[:100]}...")

    # ====== Step 5: 店长看到投诉需要处理 ======
    print("\n" + "-" * 50)
    print("[Day 2] 店长老陈：创建投诉处理任务")
    print("-" * 50)

    task_id = f"task-customer-li-{int(time.time())}"
    await boss.create_task(
        task_id=task_id,
        description="跟进处理客户李姐的橱柜门板色差投诉，确认色差原因，给出处理方案",
        required_capabilities=["customer_service", "橱柜", "after_sales"],
    )

    # 调度
    result = await boss.schedule_task(task_id)
    print(f"\n  任务状态: {result['status']}")
    print(f"  分配给: {result.get('assigned_to', 'N/A')}")
    print(f"  披露阶段: {result.get('disclosure_phase', 1)}")

    # ====== Step 6: 小王觉得信息不够 ======
    print("\n" + "-" * 50)
    print("[Day 2] 小王：信息不够，申请提升披露级别")
    print("-" * 50)

    print("  当前只知道：跟进处理客户李姐的橱柜门板色差投诉")
    print("  需要知道：完整的客户沟通历史、补偿谈判底线")
    print()

    await wang.request_more_info(
        task_id=task_id,
        reason="需要查看完整客户历史记录和补偿授权",
    )

    print("\n  （店长可以在控制面板上看到披露升级请求并授权）")

    # ====== Step 7: 语义搜索演示 ======
    print("\n" + "-" * 50)
    print("[Day 3] 店长老陈：用语义搜索查找相关历史案例")
    print("-" * 50)

    search_result = await boss.semantic_search("橱柜色差", n_results=3)
    print(f"\n  搜索「橱柜色差」返回 {search_result.get('total', 0)} 条：")
    for m in search_result.get("memories", []):
        sim = m.get("similarity", 0)
        tags = ", ".join(m.get("tags", []))
        content = str(m.get("content", ""))[:80]
        print(f"  [{sim:.3f}] [{tags}] {content}")

    # ====== 清理 ======
    print("\n" + "=" * 60)
    print("  演示结束，断开连接")
    print("=" * 60)
    await wang.disconnect()
    await li.disconnect()
    await boss.disconnect()


# ============ 命令行入口 ============

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Hermes Sync Hub Agent SDK - 企业案例"
    )
    parser.add_argument("--role", default="worker",
                        choices=["worker", "manager", "orchestrator"])
    parser.add_argument("--id", dest="agent_id", default="cs-wang")
    parser.add_argument("--name", default="客服小王")
    parser.add_argument("--dept", default="客服一部")
    parser.add_argument("--hub", default="http://localhost:3060")
    parser.add_argument("--manage", nargs="*", default=[],
                        help="管理的 Agent ID 列表（仅 manager/orchestrator 使用）")
    parser.add_argument("--demo", action="store_true",
                        help="运行完整演示场景")

    args = parser.parse_args()

    if args.demo:
        asyncio.run(demo_customer_service_scenario())
    else:
        config = SyncConfig(
            hub_url=args.hub,
            agent_id=args.agent_id,
            agent_name=args.name,
            department=args.dept,
            role=args.role,
            managed_agents=args.manage,
        )

        client = HermesSyncClient(config)

        async def run_client():
            await client.connect()
            print(f"输入命令：")
            print(f"  write <key> <content>     — 写入记忆")
            print(f"  query <agent_id> <query>   — 查询他人记忆")
            print(f"  task <task_id> <desc>      — 创建任务")
            print(f"  schedule <task_id>         — 调度任务")
            print(f"  advance <task_id> <reason> — 申请披露升级")
            print(f"  search <query>             — 语义搜索")
            print(f"  dash                      — 查看面板")
            print(f"  quit                      — 退出")

            try:
                while True:
                    cmd = await asyncio.get_event_loop().run_in_executor(
                        None, input, "> "
                    )
                    parts = cmd.strip().split(maxsplit=2)
                    if not parts:
                        continue
                    action = parts[0].lower()

                    if action == "quit":
                        break
                    elif action == "write" and len(parts) >= 3:
                        await client.write_memory(parts[1], parts[2])
                    elif action == "query" and len(parts) >= 3:
                        await client.query_peer_memory(
                            parts[1], parts[2]
                        )
                    elif action == "task" and len(parts) >= 3:
                        await client.create_task(
                            parts[1], parts[2]
                        )
                    elif action == "schedule" and len(parts) >= 2:
                        await client.schedule_task(parts[1])
                    elif action == "advance" and len(parts) >= 3:
                        await client.request_more_info(
                            parts[1], parts[2]
                        )
                    elif action == "search" and len(parts) >= 2:
                        result = await client.semantic_search(parts[1])
                        print(f"\n  语义搜索「{parts[1]}」结果：")
                        for m in result.get("memories", []):
                            sim = m.get("similarity", 0)
                            tags = ", ".join(m.get("tags", []))
                            content = str(m.get("content", ""))[:80]
                            print(f"  [{sim:.3f}] [{tags}] {content}")
                    elif action == "dash":
                        data = await client.get_dashboard()
                        d = data
                        print(
                            f"  Agent在线: {d['agents']['online']}/{d['agents']['total']}, "
                            f"记忆: {d['memories']['total']}, "
                            f"待调度任务: {d['tasks']['pending']}, "
                            f"披露记录: {d['disclosures']['total']}"
                        )
            except KeyboardInterrupt:
                pass
            finally:
                await client.disconnect()

        asyncio.run(run_client())
