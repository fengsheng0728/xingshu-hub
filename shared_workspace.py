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

# CD-070b（2026-09-20）：pycrdt 的 SQLiteYStore 把所有 room 写进**类属性** db_path
# （默认 "ystore.db"，cwd 相对 → 仓库根），传入的 path 只作为 room 名记录。
# 故这里接 CONFIG（空 = 沿用默认 "ystore.db"，生产行为零变化）；
# 测试态由 conftest 指向 tmp，避免跑测试写生产 ystore.db。
class _IsolatedSQLiteYStore(SQLiteYStore):
    db_path = (CONFIG.WORKSPACE_YSTORE_PATH or "ystore.db")


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
    allowed_agents TEXT DEFAULT '[]',
    trust_level    TEXT NOT NULL DEFAULT 'internal',
    source_agent_id TEXT DEFAULT '',
    tainted_at     TEXT DEFAULT ''
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
        # 2026-09-22（归档收口）：doc_id → 该房活跃 WS 连接对象集合。
        # 归档是「讨论结束」语义——连接必须一起关，故需要可主动 close 的引用
        # （此前只有计数 _active_conns，归档无从踢连接）。
        self._doc_conns: dict[str, set] = {}
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
        ystore = _IsolatedSQLiteYStore(path=store_path)
        ydoc = Doc()

        room = YRoom(ydoc=ydoc, ystore=ystore, log=LOG)

        # YRoom.start() 是长期运行任务，放到我们的 task group 里
        async def _run_room():
            await room.start()

        self._task_group.start_soon(_run_room)
        await room.started.wait()

        # 2026-09-22（正文持久化收口）：把 ystore 里的历史 apply 回 ydoc。
        # pycrdt 的 YRoom 只写不读 —— `_broadcast_updates` 里仅有 ystore.write，
        # 全包（pycrdt / pycrdt.websocket）无一处调用 `apply_updates`。不补这一步，
        # ydoc 在每次进程启动/room 重载时都是空的：shared_docs 元数据（标题/块数）
        # 在、正文丢。取证：`_sync/archive_probe/restart_probe.py`
        # （写后 len=111 → 同库重启后 len=0，而 ystore.db 里正文标记仍在）。
        #
        # 顺序很关键：**先在本地注册 content Text，再 apply**。pycrdt 的顶层取值
        # 依赖本地类型注册 —— 先 apply 后建 Text 会得到 `"content" in ydoc == True`
        # 但 `ydoc["content"] is None`（实测），正文照样读不出来。
        if "content" not in ydoc:
            ydoc["content"] = Text()

        for _ in range(100):                       # 等 room 内部的 ystore 起来（最多 ~2s）
            _di = getattr(ystore, "db_initialized", None)
            if _di is not None and _di.is_set():
                break
            await asyncio.sleep(0.02)
        try:
            await ystore.apply_updates(ydoc)       # 历史按 item id 合并进本地 Text
            LOG.debug("Applied ystore history for room %s", doc_id)
        except Exception as _e:
            LOG.debug("No ystore history for room %s (%s)", doc_id, _e)

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
        # 2026-09-22：卸载前把当前状态落一次 ystore —— room 一走，内存里未落盘的
        # CRDT 变更就永久丢了（覆盖建档初始内容等不经 append_block 的写入路径）。
        await self._flush_ystore(doc_id)
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
        # 2026-09-22：主动把当前状态落一次 ystore。
        # 默认落盘路径是 CRDT observe → room 的 `_broadcast_updates` → `start_soon(ystore.write)`，
        # 属**异步任务**：写完立刻归档（room 被卸载）或进程被杀时，这一段可能还没落盘
        # → 正文丢（实测：TestClient 下「写→归档→恢复」读到空内容）。
        # 这里同步 flush 一次全量 state（Yjs update 可重复应用，幂等）。
        await self._flush_ystore(doc_id)
        return True

    async def _flush_ystore(self, doc_id: str) -> None:
        """把内存 ydoc 的当前状态同步写入 ystore（写失败不阻塞业务，D4 降级）。"""
        room = self._rooms.get(doc_id)
        ystore = getattr(room, "ystore", None) if room is not None else None
        ydoc = self._docs.get(doc_id)
        if ystore is None or ydoc is None:
            return
        try:
            await ystore.encode_state_as_update(ydoc)
        except Exception as _e:
            LOG.warning("flush ystore failed for %s: %s", doc_id, _e)

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

    async def is_archived(self, doc_id: str) -> bool:
        """文档是否处于归档状态。不存在 → False（不存在的 doc 由各调用方按自身语义处理，
        以免改动既有的「不存在」语义，见 tests/test_ws_auth_matrix.py）。"""
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT archived FROM shared_docs WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            conn.close()
        except Exception:
            return False
        return bool(row and row[0])

    async def restore_doc(self, doc_id: str) -> bool:
        """取消归档（archived 1 → 0）。仅对处于归档态的文档生效；否则返回 False。
        恢复后按需懒加载 room，无需主动载入。"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "UPDATE shared_docs SET archived = 0 WHERE doc_id = ? AND archived = 1", (doc_id,)
        )
        conn.commit()
        conn.close()
        if cur.rowcount <= 0:
            return False
        LOG.info("Restored shared doc: %s", doc_id)
        # 恢复即可读 —— get_doc_content / append_block 都依赖内存 room
        # （归档时 _unload_room 已把它卸掉），故这里补一次懒加载。
        try:
            if doc_id not in self._rooms:
                await self._load_room(doc_id)
        except Exception as _e:
            LOG.warning("restore: load room failed for %s: %s", doc_id, _e)
        return True

    async def close_doc_connections(self, doc_id: str, reason: str = "doc archived") -> int:
        """关闭该文档的全部实时连接（归档用）：先送一帧 shared_archived 再按 4404 关闭。
        返回被关闭的连接数。连接异常一律吞掉——归档必须完成，不能被单个坏连接拖住。"""
        import json as _json
        conns = list(self._doc_conns.pop(doc_id, set()) or ())
        msg = _json.dumps({"type": "shared_archived", "doc_id": doc_id, "reason": reason})
        for ws in conns:
            try:
                await ws.send_text(msg)
            except Exception:
                pass
            try:
                await ws.close(code=4404, reason=reason)
            except Exception:
                pass
        return len(conns)

    async def delete_doc(self, doc_id: str) -> bool:
        """归档文档：库内置 archived=1 + 关闭该房全部实时连接 + 卸载内存 room。

        2026-09-22 两处收口：
        ① 归档 = 讨论结束 → 连接要一起关（此前归档后 WS 仍可进房，实测证据见
           `_sync/archive_probe/`）；
        ② 不再要求文档已在内存（原实现 `doc_id not in self._docs → False` 使
           sweep 卸载过的冷文档「归档不了」，只能 404）。
        """
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "UPDATE shared_docs SET archived = 1 WHERE doc_id = ? AND archived = 0", (doc_id,)
        )
        conn.commit()
        conn.close()
        if cur.rowcount <= 0:
            return False

        await self.close_doc_connections(doc_id)
        async with self._lock:
            await self._unload_room(doc_id)
        LOG.info("Archived shared doc: %s", doc_id)
        return True

    async def serve_websocket(self, doc_id: str, ws) -> None:
        """为 WebSocket 客户端提供实时协同服务。

        2026-09-22：已归档文档不再接纳新连接——先回一帧 shared_archived 再按
        4404 关闭。理由：归档后 REST 读写已 404（内容封锁），若通道仍可进房，
        「归档」就只关了一半（实测见 `_sync/archive_probe/`）。
        """
        import json as _json
        if await self.is_archived(doc_id):
            try:
                await ws.send_text(_json.dumps({
                    "type": "shared_archived", "doc_id": doc_id, "reason": "doc archived"}))
            except Exception:
                pass
            try:
                await ws.close(code=4404, reason="doc archived")
            except Exception:
                pass
            return

        if doc_id not in self._rooms:
            await self._load_room(doc_id)
        # 3-7: 连接进入即活动（含上面的兜底加载路径）
        self._last_active[doc_id] = time.monotonic()

        room = self._rooms[doc_id]
        channel = _FastAPIWSChannel(ws, f"/ws/shared/{doc_id}")

        self._doc_conns.setdefault(doc_id, set()).add(ws)                  # 归档时按此踢连接
        self._active_conns[doc_id] = self._active_conns.get(doc_id, 0) + 1  # 3-7: 进入 +1
        try:
            try:
                await room.serve(channel)
            except Exception as e:
                LOG.debug("Client left %s: %s", doc_id, e)
        finally:
            # 3-7: finally 里 -1，异常路径也必须减
            self._active_conns[doc_id] = self._active_conns.get(doc_id, 1) - 1
            _c = self._doc_conns.get(doc_id)
            if _c is not None:
                _c.discard(ws)
                if not _c:
                    self._doc_conns.pop(doc_id, None)


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
            logger.warning("shared_workspace silent-except(send): %s", _exc)

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
