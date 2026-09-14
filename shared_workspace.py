"""
星枢 Shared Workspace — 多 Agent 实时协同沙箱
基于 pycrdt + YRoom，块级 CRDT 无冲突合并
每个文档一个 YRoom，SQLite 持久化
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid

import logging
logger = logging.getLogger("xingshu.shared_workspace")

from logging import getLogger

from anyio import create_task_group
from pycrdt import Doc, Text
from pycrdt.store import SQLiteYStore
from pycrdt.websocket import YRoom

from models import CONFIG  # 3-7(2026-09-10): 容量护栏配置（TTL/水位阈值）

LOG = getLogger("shared_workspace")

SHARED_DOCS_DDL = """
CREATE TABLE IF NOT EXISTS shared_docs (
    doc_id         TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    archived       INTEGER DEFAULT 0,
    block_count    INTEGER DEFAULT 0,
    visibility     TEXT DEFAULT 'team',
    allowed_agents TEXT DEFAULT '[]'
)
"""


class SharedWorkspace:
    """共享工作区引擎"""

    def __init__(self, db_path: str, store_dir: str = "./shared_store", shadow=None):
        self.db_path = db_path
        self.store_dir = store_dir
        self._shadow = shadow  # 阶段3-P1: 影子双写器（可 None）
        self._rooms: dict[str, YRoom] = {}
        self._docs: dict[str, Doc] = {}
        # 3-7(2026-09-10): 容量护栏 — 房间空闲卸载 TTL + 水位告警
        self._last_active: dict[str, float] = {}   # doc_id → time.monotonic() 最后活动时间
        self._active_conns: dict[str, int] = {}    # doc_id → 活跃 WS 连接数
        self._stopping: bool = False               # stop() 置位，清扫循环据此退出
        self._cap_warned: bool = False             # 水位告警只打一次（回落到阈值以下复位）
        self._task_group = None
        self._lock = asyncio.Lock()

    async def _init_db(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        # visibility / allowed_agents 列已冻结进 alembic 0002（T2-3），
        # 老库缺列由 `alembic upgrade head` 补齐，不再做运行时 ALTER；
        # 新库由下方 DDL 内联建出两列。
        conn.execute(SHARED_DOCS_DDL)
        conn.commit()
        conn.close()

    async def start(self):
        await self._init_db()
        os.makedirs(self.store_dir, exist_ok=True)

        # 创建持久 task group 管理所有 room
        self._task_group = create_task_group()
        await self._task_group.__aenter__()

        # 恢复已有文档
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT doc_id, title FROM shared_docs WHERE archived = 0"
        ).fetchall()
        conn.close()

        for row in rows:
            await self._load_room(row["doc_id"])

        # 3-7(2026-09-10): 空闲卸载清扫循环（ttl<=0 关闭 = kill switch）
        if getattr(CONFIG, "WORKSPACE_ROOM_IDLE_TTL_SEC", 0) > 0:
            self._task_group.start_soon(self._sweeper_loop)

        LOG.info("SharedWorkspace started, %d docs", len(self._rooms))

    async def stop(self):
        self._stopping = True  # 3-7: 通知清扫循环退出（下一轮检查即 return，不留悬挂 task）
        for doc_id in list(self._rooms.keys()):
            await self._unload_room(doc_id)
        if self._task_group:
            # 3-7: 先 cancel 再 __aexit__ — pycrdt room 的内部子任务（awareness/ystore）
            # 在 room.stop() 后不一定自行终结，直接 __aexit__ 会悬挂等待它们
            self._task_group.cancel_scope.cancel()
            await self._task_group.__aexit__(None, None, None)
        LOG.info("SharedWorkspace stopped")

    async def _load_room(self, doc_id: str):
        """加载文档的 YRoom"""
        store_path = os.path.join(self.store_dir, f"{doc_id}.db")
        ystore = SQLiteYStore(path=store_path)
        ydoc = Doc()

        room = YRoom(ydoc=ydoc, ystore=ystore, log=LOG)

        # YRoom.start() 是长期运行任务，放到我们的 task group 里
        async def _run_room():
            await room.start()

        self._task_group.start_soon(_run_room)
        await room.started.wait()

        # 确保有 content Text
        if "content" not in ydoc:
            ydoc["content"] = Text()

        self._rooms[doc_id] = room
        self._docs[doc_id] = ydoc
        self._last_active[doc_id] = time.monotonic()  # 3-7: 加载即活动
        # 3-7(2026-09-10): 常驻 room 数水位告警 — 仅跨越阈值那一刻打一次，回落复位
        max_rooms = getattr(CONFIG, "WORKSPACE_MAX_ROOMS", 200)
        if len(self._rooms) > max_rooms:
            if not self._cap_warned:
                self._cap_warned = True
                logger.warning(
                    "SharedWorkspace 常驻 room 数 %d 超过容量水位阈值 %d"
                    "（库内未归档文档共 %d 个），请评估扩容或调低 room_idle_ttl_sec",
                    len(self._rooms), max_rooms, self._count_active_docs())
        else:
            self._cap_warned = False
        LOG.debug("Room loaded: %s", doc_id)

    async def _unload_room(self, doc_id: str):
        room = self._rooms.pop(doc_id, None)
        self._docs.pop(doc_id, None)
        self._last_active.pop(doc_id, None)   # 3-7: 同步清护栏状态，防这两个 dict 无上限增长
        self._active_conns.pop(doc_id, None)
        if len(self._rooms) <= getattr(CONFIG, "WORKSPACE_MAX_ROOMS", 200):
            self._cap_warned = False          # 3-7: 计数回落到阈值以下，复位允许下次跨越再告警
        if room:
            try:
                await room.stop()
            except RuntimeError:
                pass  # awareness may not have started yet

    async def sweep_once(self) -> int:
        """扫描一次：卸载「空闲超 TTL 且无活跃连接」的 room，返回卸载数量。"""
        ttl = getattr(CONFIG, "WORKSPACE_ROOM_IDLE_TTL_SEC", 0)
        if ttl <= 0:
            return 0
        now = time.monotonic()
        unloaded = 0
        for doc_id in list(self._rooms.keys()):
            idle = now - self._last_active.get(doc_id, 0)
            if idle > ttl and self._active_conns.get(doc_id, 0) == 0:
                LOG.info("Sweep idle room: %s (idle %.0fs > ttl %ds)", doc_id, idle, ttl)
                await self._unload_room(doc_id)
                unloaded += 1
        return unloaded

    async def _sweeper_loop(self):
        """3-7(2026-09-10): 周期性清扫。stop() 置 _stopping 后 ≤1s 内退出，不留悬挂 task。"""
        interval = max(1, getattr(CONFIG, "WORKSPACE_SWEEP_INTERVAL_SEC", 60))
        while not self._stopping:
            # 分片睡眠（每片 1s）：保证 stop() 后清扫循环能及时检查 _stopping 退出
            for _ in range(interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)
            if self._stopping:
                return
            try:
                await self.sweep_once()
            except Exception as _exc:
                logger.warning("shared_workspace sweeper error: %s", _exc)

    def _count_active_docs(self) -> int:
        """3-7: 库内未归档文档总数（水位告警上下文用；查询失败返回 -1，不阻断加载）"""
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            n = conn.execute(
                "SELECT COUNT(*) FROM shared_docs WHERE archived = 0").fetchone()[0]
            conn.close()
            return n
        except Exception:
            return -1

    async def create_doc(self, title: str, created_by: str,
                          visibility: str = "team",
                          allowed_agents: list | None = None,
                          trust_level: str = "internal") -> dict:
        async with self._lock:
            doc_id = f"doc-{uuid.uuid4().hex[:12]}"
            now = time.time()

            import sqlite3, json as _json
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO shared_docs (doc_id, title, created_by, created_at, updated_at, visibility, allowed_agents, trust_level, source_agent_id, tainted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, title, created_by, now, now,
                 visibility if visibility in ("team", "private") else "team",
                 _json.dumps(allowed_agents or [], ensure_ascii=False),
                 trust_level, created_by, now),
            )
            conn.commit()
            conn.close()

            # 阶段3-P1: 影子双写（落库后镜像 git 仓库群，零阻塞入队）
            try:
                if self._shadow is not None:
                    self._shadow.submit("shared", {
                        "doc_id": doc_id,
                        "title": title,
                        "created_by": created_by,
                        "visibility": visibility,
                        "allowed_agents": allowed_agents or [],
                        "trust": trust_level,
                        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
                    })
            except Exception:
                pass  # 影子失败不阻塞主链路（D4）

            await self._load_room(doc_id)

            # 写入初始内容
            ydoc = self._docs[doc_id]
            content = ydoc["content"]
            content += f"# {title}\n\n> 创建者: {created_by}\n\n"

            LOG.info("Created shared doc: %s (%s)", doc_id, title)
            self._last_active[doc_id] = time.monotonic()  # 3-7: 创建即活动
            return {"doc_id": doc_id, "title": title, "created_by": created_by}

    async def list_docs(self, agent_id: str | None = None) -> list[dict]:
        """列出共享文档。agent_id 提供时按可见性过滤：
        team 全员可见；private 仅创建者 + allowed_agents 可见。"""
        import sqlite3, json as _json
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT doc_id, title, created_by, created_at, updated_at, block_count, visibility, allowed_agents "
            "FROM shared_docs WHERE archived = 0 ORDER BY updated_at DESC"
        ).fetchall()
        conn.close()
        out = []
        for row in rows:
            d = {k: row[k] for k in row.keys()}
            if agent_id:
                vis = d.get("visibility", "team")
                if vis == "private":
                    allowed = d.get("allowed_agents") or "[]"
                    try:
                        allowed = _json.loads(allowed) if isinstance(allowed, str) else (allowed or [])
                    except Exception:
                        allowed = []
                    if d["created_by"] != agent_id and agent_id not in allowed:
                        continue
            out.append(d)
        return out

    async def get_doc_content(self, doc_id: str) -> str | None:
        ydoc = self._docs.get(doc_id)
        if ydoc is None:
            return None
        if "content" in ydoc:
            return str(ydoc["content"])
        return ""

    async def append_block(self, doc_id: str, text: str, agent_id: str) -> bool:
        ydoc = self._docs.get(doc_id)
        if ydoc is None:
            return False

        content = ydoc["content"]
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        block = f"\n\n<!-- BLOCK agent={agent_id} ts={timestamp} -->\n{text}\n"
        content += block

        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE shared_docs SET updated_at = ?, block_count = block_count + 1 WHERE doc_id = ?",
            (time.time(), doc_id),
        )
        conn.commit()
        conn.close()
        self._last_active[doc_id] = time.monotonic()  # 3-7: 写入即活动
        return True

    async def can_access(self, doc_id: str, agent_id: str) -> bool:
        """可见性校验：team 全员 / private 仅创建者 + 白名单。不存在返回 False。"""
        import sqlite3, json as _json
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT created_by, visibility, allowed_agents FROM shared_docs WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return False
        if row["created_by"] == agent_id:
            return True
        vis = row["visibility"] or "team"
        if vis != "private":
            return True
        allowed = row["allowed_agents"] or "[]"
        try:
            allowed = _json.loads(allowed) if isinstance(allowed, str) else (allowed or [])  # T5(3-7): 原误用 json（本作用域只有 _json），private+白名单路径 NameError
        except Exception:
            allowed = []
        return agent_id in allowed

    async def delete_doc(self, doc_id: str) -> bool:
        async with self._lock:
            if doc_id not in self._docs:
                return False

            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE shared_docs SET archived = 1 WHERE doc_id = ?", (doc_id,)
            )
            conn.commit()
            conn.close()

            await self._unload_room(doc_id)
            LOG.info("Archived shared doc: %s", doc_id)
            return True

    async def serve_websocket(self, doc_id: str, ws) -> None:
        """为 WebSocket 客户端提供实时协同服务"""
        if doc_id not in self._rooms:
            await self._load_room(doc_id)
        # 3-7: 连接进入即活动（含上面的兜底加载路径）
        self._last_active[doc_id] = time.monotonic()

        room = self._rooms[doc_id]
        channel = _FastAPIWSChannel(ws, f"/ws/shared/{doc_id}")

        self._active_conns[doc_id] = self._active_conns.get(doc_id, 0) + 1  # 3-7: 进入 +1
        try:
            try:
                await room.serve(channel)
            except Exception as e:
                LOG.debug("Client left %s: %s", doc_id, e)
        finally:
            # 3-7: finally 里 -1，异常路径也必须减
            self._active_conns[doc_id] = self._active_conns.get(doc_id, 1) - 1


class _FastAPIWSChannel:
    """FastAPI WebSocket → pycrdt Channel 适配"""

    def __init__(self, ws, path: str):
        self._ws = ws
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            data = await self._ws.receive()
            if "text" in data:
                return data["text"].encode()
            elif "bytes" in data:
                return data["bytes"]
            raise StopAsyncIteration
        except Exception:
            raise StopAsyncIteration

    async def send(self, message: bytes):
        try:
            await self._ws.send_bytes(message)
        except Exception as _exc:
            logger.warning("shared_workspace silent-except @304: %s", _exc)

    async def recv(self) -> bytes:
        try:
            data = await self._ws.receive()
            if "text" in data:
                return data["text"].encode()
            elif "bytes" in data:
                return data["bytes"]
            return b""
        except Exception:
            return b""


workspace: SharedWorkspace | None = None
