"""星枢 Sync Hub — WebSocket 通道端点（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_ws")

import asyncio, time
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from envelope import envelope_pong, parse_envelope, extract_payload, serialize
from transport_audit import log_transport_frame, log_dispatch_in, log_ping_pong
from models import CONFIG, DisclosureLevel
from hub_core import hub
from notifications import notifications
import routes_common
from routes_common import _version_ge, principal_is_privileged
# _ws 为 routes_shared 里的动态 getter（返回 shared_workspace.workspace 当前值），
# 生命周期里重新绑定的是 shared_workspace.workspace——import 此函数语义与搬前一致
from routes_shared import (
    _ws, _shared_watchers, _broadcast_shared_update, _doc_archived,
    _is_hub_token_principal, _shared_doc_ws_level,
)

router = APIRouter()


# ============ WS 首帧鉴权（P1 安全底线） ============
# D3: 连接后首帧必须 {type:"auth", token}，3 秒超时或校验失败 → close 4401。
# 不用 query param（会进日志）、不用 header（浏览器 WS 不支持）。
# 认证成功前连接不入任何管理器（NotificationManager/active_ws/YRoom）。
# P0 S4（2026-08-04）：鉴权失败按来源 IP 滑动窗口计数 + 自动熔断——
# 同 IP 窗口内失败 N 次封禁 T 秒（封禁状态在内存字典；熔断事件写 events 表审计，
# 启动时经 restore_ws_bans() 从 events 回填未过期封禁，防重启清零），
# 熔断事件入审计 + 通知管理员。超时值可配置但禁止 <2s（防重连风暴，见 2026-08-03 修复）。
WS_AUTH_TIMEOUT = CONFIG.WS_AUTH_TIMEOUT_SEC if CONFIG.WS_AUTH_TIMEOUT_SEC >= 2.0 else 3.0
_ws_fail_counts: Dict[str, list] = {}   # ip → [fail_ts, ...] 滑动窗口
_ws_banned_until: Dict[str, float] = {}  # ip → 封禁截止 ts
_WS_FAILS_LOCK = asyncio.Lock()


def _client_ip(websocket: WebSocket) -> str:
    """提取客户端 IP（WebSocket scope）。"""
    try:
        return websocket.client.host if websocket.client else "unknown"
    except Exception:
        return "unknown"


async def _ws_record_failure(ip: str) -> bool:
    """记录一次鉴权失败；返回 True 表示本次已触发封禁。"""
    global _ws_fail_counts, _ws_banned_until
    now = time.time()
    window = CONFIG.WS_AUTH_WINDOW_SEC
    max_fails = CONFIG.WS_AUTH_MAX_FAILS
    ban_sec = CONFIG.WS_AUTH_BAN_SEC
    async with _WS_FAILS_LOCK:
        if ip in _ws_banned_until and _ws_banned_until[ip] > now:
            return False  # 已在封禁中，不再叠加
        # 键数水位：两张表以 IP 为键、仅在键再次出现时清理——超水位整体清过期项
        if len(_ws_fail_counts) > 10000:
            for _k in [k for k, v in _ws_fail_counts.items() if not v or now - v[-1] >= window]:
                del _ws_fail_counts[_k]
        if len(_ws_banned_until) > 10000:
            for _k in [k for k, u in _ws_banned_until.items() if u <= now]:
                del _ws_banned_until[_k]
        # 滑动窗口裁剪
        fails = [t for t in _ws_fail_counts.get(ip, []) if now - t < window]
        fails.append(now)
        _ws_fail_counts[ip] = fails
        if len(fails) >= max_fails:
            _ws_banned_until[ip] = now + ban_sec
            _ws_fail_counts[ip] = []
            return True  # 触发封禁
        return False


async def _ws_banned(ip: str) -> bool:
    """当前 IP 是否处于封禁中。"""
    global _ws_banned_until
    now = time.time()
    async with _WS_FAILS_LOCK:
        until = _ws_banned_until.get(ip, 0)
        if until > now:
            return True
        if until > 0:
            del _ws_banned_until[ip]  # 过期清理
        return False


async def _audit_ws_ban(ip: str, reason: str = "auth_fail_ban"):
    """熔断事件入 events 表审计 + 通知管理员（dashboard 通道）。"""
    try:
        await hub._log_event(reason, "__ws_gate__", {"ip": ip, "action": "ban",
                              "window_sec": CONFIG.WS_AUTH_WINDOW_SEC,
                              "max_fails": CONFIG.WS_AUTH_MAX_FAILS,
                              "ban_sec": CONFIG.WS_AUTH_BAN_SEC})
    except Exception as _exc:
        logger.warning("routes silent-except(_audit_ws_ban): %s", _exc)
    try:
        await hub.create_notification(
            "__dashboard__", "security",
            f"WS 鉴权熔断: {ip}",
            f"IP {ip} 在 {CONFIG.WS_AUTH_WINDOW_SEC}s 内鉴权失败 {CONFIG.WS_AUTH_MAX_FAILS} 次，已封禁 {CONFIG.WS_AUTH_BAN_SEC}s",
            source="ws_gate")
    except Exception as _exc:
        logger.warning("routes silent-except(_audit_ws_ban): %s", _exc)


async def restore_ws_bans():
    """启动回填：从 events 表恢复未过期的 WS 熔断封禁（防重启清零）。

    熔断事件（auth_fail_ban）写入时 payload 带 ip；封禁截止 = 事件时间 +
    当前 CONFIG.WS_AUTH_BAN_SEC（配置变更以当前值为准）。events 表缺失或查询
    异常 → 静默跳过（退化为旧行为「重启即清零」，不阻塞启动）。
    """
    import json as _json
    from datetime import datetime, timezone, timedelta
    now = time.time()
    ban_sec = CONFIG.WS_AUTH_BAN_SEC
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ban_sec)).isoformat()
    try:
        conn = hub._db()
        try:
            rows = conn.execute(
                "SELECT payload, timestamp FROM events "
                "WHERE event_type = 'auth_fail_ban' AND timestamp >= ?",
                (cutoff,)).fetchall()
        finally:
            conn.close()
    except Exception as _exc:
        logger.warning("routes_ws silent-except(restore_ws_bans): %s", _exc)
        return
    for payload, ts in rows:
        try:
            ip = str(_json.loads(payload).get("ip", ""))
            if not ip:
                continue
            until = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() + ban_sec
            if until > now:
                _ws_banned_until[ip] = max(_ws_banned_until.get(ip, 0), until)
        except Exception:
            continue


async def _ws_auth_accept(websocket: WebSocket, agent_id_hint: str = "", strict_agent: bool = False,
                          require_privileged: bool = False):
    """accept + 首帧鉴权。成功返回 agent_id，失败返回 None。

    调用方必须在返回 None 时直接 return（连接已 close 4401）。
    首帧格式: {"type": "auth", "token": "..."}
    - hub_token：无身份语义，agent_id 由连接路径/声明提供（D1 不做 RBAC）
    - agents.api_key：strict_agent=True 时校验 key 归属该 agent_id（防冒充）
    - require_privileged=True（T15）：principal 须为 hub_token 或
      role ∈ (manager, orchestrator)，否则 close 4401——dashboard/buffer
      监控通道用；凭据有效但角色不足不算鉴权失败，不计入熔断。
    """
    agent_id, _principal = await _ws_auth_accept_full(
        websocket, agent_id_hint, strict_agent, require_privileged)
    return agent_id


async def _ws_auth_accept_full(websocket: WebSocket, agent_id_hint: str = "",
                               strict_agent: bool = False, require_privileged: bool = False):
    """_ws_auth_accept 的完整返回版：成功返回 (agent_id, principal)，失败返回 (None, None)。

    CD-094：WS 共享文档通道的披露级别门需要 principal.scope / auth_mode，
    原 str 返回拿不到——拆出本函数；鉴权/熔断/4401 口径与 _ws_auth_accept 完全一致
    （本函数即原实现整体下移）。NO_AUTH 开发态 principal=None（无身份语义）。
    调用方必须在 agent_id 为 None 时直接 return（连接已 close 4401）。
    """
    await websocket.accept()
    if routes_common.NO_AUTH:
        return agent_id_hint or websocket.query_params.get("agent_id", ""), None

    # P0 S4：封禁中的 IP 直接拒绝（连鉴权都不给）
    ip = _client_ip(websocket)
    if await _ws_banned(ip):
        await websocket.close(code=4401, reason="IP banned")
        return None, None

    try:
        frame = await asyncio.wait_for(websocket.receive_json(), timeout=WS_AUTH_TIMEOUT)
    except asyncio.TimeoutError:
        if await _ws_record_failure(ip):
            await _audit_ws_ban(ip, "auth_fail_ban")
        await websocket.close(code=4401, reason="Auth timeout")
        return None, None
    except Exception:
        await websocket.close(code=4401, reason="Auth required")
        return None, None

    if not isinstance(frame, dict) or frame.get("type") != "auth":
        await websocket.close(code=4401, reason="Auth frame required")
        return None, None

    token = str(frame.get("token", ""))
    if not token:
        await websocket.close(code=4401, reason="Missing token")
        return None, None

    # S1：auth_provider 统一认证（api_key 过期/白名单/轮换语义全覆盖）
    principal = routes_common._auth_provider().authenticate(token, ip)
    if principal is None:
        if await _ws_record_failure(ip):
            await _audit_ws_ban(ip, "auth_fail_ban")
        await websocket.close(code=4401, reason="Invalid token")
        return None, None
    # T15：特权监控通道角色门（/ws/dashboard、/ws/buffer）——
    # 凭据有效但非 hub_token/manager/orchestrator → 拒绝，不计入熔断（非鉴权失败）
    if require_privileged and not principal_is_privileged(principal):
        await websocket.close(code=4401, reason="Forbidden: privileged role required")
        return None, None
    # CD-069：scoped key（对外受限凭据）默认不得建任何 WS 连接——WS 是全双工通道，
    # 绕过 endpoints/methods 白名单（可在同一条连接上跑 memory/task 类命令）。
    # 需显式 scope.ws = true 才放行（fail-closed；未声明一律拒）。
    if principal.scoped_key_id and not (principal.scope or {}).get("ws"):
        await websocket.close(code=4401, reason="Forbidden: scoped key cannot open WS")
        return None, None
    # hub_token 路径：信任声明身份（D1）；api_key 路径：精确归属
    if principal.auth_mode == "hub_token":
        return agent_id_hint or websocket.query_params.get("agent_id", ""), principal
    if strict_agent and agent_id_hint and principal.subject_id != agent_id_hint:
        if await _ws_record_failure(ip):
            await _audit_ws_ban(ip, "auth_fail_ban")
        await websocket.close(code=4401, reason="Unauthorized")
        return None, None
    return principal.subject_id, principal



@router.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket):
    """Dashboard 实时推送端点 — P1 首帧鉴权（D3）"""
    agent_id = await _ws_auth_accept(websocket, "__dashboard__", require_privileged=True)
    if agent_id is None:
        return
    await notifications.connect("__dashboard__", websocket)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("msg_type")
            if msg_type == "ping":
                await websocket.send_json({"msg_type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        notifications.disconnect("__dashboard__", websocket)


# H2: WS 请求路由表
_WS_HANDLERS = {}

def _ws_handler(method: str):
    """装饰器：注册 WS 请求处理方法"""
    def decorator(fn):
        _WS_HANDLERS[method] = fn
        return fn
    return decorator

def _build_env(type_: str, payload: dict) -> dict:
    """构建 envelope（简化版，用于 WS 内部通信）"""
    return {"type": type_, "id": "", "session_id": "", "via": "ws", "ts": 0, "version": 2, "payload": payload}

async def _handle_ws_request(agent_id: str, method: str, params: dict):
    handler = _WS_HANDLERS.get(method)
    if not handler:
        # Fallback to HTTP-equivalent hub methods
        if method == "memory_search":
            # CD-091 修复（2026-09-22 实测恒 error）：旧代码调不存在的
            # hub.search_memory(...)，真实方法是 hub.memory_search(req)
            # （hub_mixins/memory.py），入参模型与 REST /memory/search 同款；
            # params 兼容旧口径 limit（映射 top_k）。
            from routes_memory import MemorySearchRequest
            req = MemorySearchRequest(
                query=params.get("query", ""),
                agent_id=agent_id,
                kind=params.get("kind") or ["fact", "todo"],
                top_k=params.get("top_k") or params.get("limit") or 5,
                min_confidence=params.get("min_confidence", 0.6),
            )
            return await hub.memory_search(req)
        elif method == "memory_store":
            from models import MemoryEntry
            entry = MemoryEntry(**params)
            return await hub.store_memory(agent_id, entry)
        elif method == "memory_list":
            return hub.get_memories(agent_id, params.get("kind", ""))
        raise ValueError(f"Unknown WS method: {method}")
    return await handler(agent_id, params)


@router.websocket("/ws/buffer")
async def ws_buffer_stats(websocket: WebSocket):
    """实时推送写入缓冲统计（每秒）— P1 首帧鉴权（D3）"""
    agent_id = await _ws_auth_accept(websocket, "buffer-monitor", require_privileged=True)
    if agent_id is None:
        return
    try:
        while True:
            await websocket.send_json(hub.buffer_stats())
            await asyncio.sleep(1)
    except (WebSocketDisconnect, Exception):
        pass


@router.websocket("/ws/{agent_id}")
async def ws_endpoint(websocket: WebSocket, agent_id: str):
    """L0: WebSocket 实时通信 — envelope 分层。P1 首帧鉴权（D3），strict_agent 防冒充"""
    authed = await _ws_auth_accept(websocket, agent_id, strict_agent=True)
    if authed is None:
        return
    # hub_token 连接：authed 为路径 agent_id；api_key 连接：authed 为 key 归属 agent
    # 统一使用路径 agent_id（与历史行为一致），hub_token 时信任路径声明
    hub.active_ws[agent_id] = websocket
    await notifications.connect(agent_id, websocket)  # 注册到通知推送池
    hub.record_pong(agent_id)  # L5: initial heartbeat

    try:
        while True:
            raw = await websocket.receive_json()
            env = parse_envelope(raw)
            if env:
                log_transport_frame('in', env)  # L7

            if env is None:
                # 平铺旧格式（version<2）兼容分支于 2026-09-21 下线（CD-032 收口：
                # 自研 Agent 端砍掉、只放通用 API，该分支已无未来消费方）——
                # 非 envelope 帧一律丢弃（原分支只兼容旧 Agent 的 heartbeat 平铺帧）
                continue
            else:
                etype = env["type"]
                session_id = env.get("session_id", "")
                
                if etype == "ping":
                    hub.record_pong(agent_id)
                    pong_env = envelope_pong()
                    async with notifications.send_lock(agent_id):
                        await websocket.send_text(serialize(pong_env))
                    log_ping_pong('out', pong_env)
                elif etype == "pong":
                    hub.record_pong(agent_id)
                elif etype == "hello":
                    hub.record_pong(agent_id)
                    # O3: 版本协商 — Agent 上报版本低于最低支持 → 426 拒连
                    _ver = extract_payload(env).get("agent_version", "")
                    if _ver and not _version_ge(_ver, CONFIG.AGENT_MIN_VERSION):
                        try:
                            await websocket.close(code=426, reason=f"agent_version {_ver} < min {CONFIG.AGENT_MIN_VERSION}")
                        except Exception as _exc:
                            logger.debug("routes silent-except(ws_endpoint): %s", _exc)
                        await hub._log_event(
                            "agent_version_rejected", agent_id,
                            {"agent_version": _ver, "min_version": CONFIG.AGENT_MIN_VERSION})
                        return
                    # L3: replay pending dispatches
                    ckpt = extract_payload(env).get("last_checkpoint_id", "")
                    pending = hub.get_pending_dispatches(session_id, ckpt)
                    for disp in pending:
                        hub.inc_in_flight(agent_id)
                        async with notifications.send_lock(agent_id):
                            await websocket.send_text(serialize(disp))
                        log_dispatch_in(disp)  # L7: replay dispatch
                elif etype == "ack":
                    hub.ack_dispatch(session_id, extract_payload(env).get("dispatch_id", ""))
                    hub.dec_in_flight(agent_id)  # L4
                elif etype == "request":
                    # H2: Agent→Hub WS 请求——路由到对应方法，返回 response
                    req_id = env.get("id", "")
                    method = extract_payload(env).get("method", "")
                    params = extract_payload(env).get("params", {})
                    try:
                        result = await _handle_ws_request(agent_id, method, params)
                        resp = _build_env("response", {"id": req_id, "result": result})
                    except Exception as e:
                        # CD-091②：错误帧之外必须落日志——只回 error 不留痕 = 静默失败
                        logger.warning(
                            "[ws request 失败] agent=%s method=%s %s: %s",
                            agent_id, method, type(e).__name__, e)
                        resp = _build_env("response", {"id": req_id, "error": str(e)})
                    async with notifications.send_lock(agent_id):
                        await websocket.send_text(serialize(resp))

    except WebSocketDisconnect:
        pass
    finally:
        notifications.disconnect(agent_id, websocket)  # 从通知推送池注销
        # 身份比对注销：同一 agent 快速重连时，旧连接的 finally 不得删掉新连接的注册
        if hub.active_ws.get(agent_id) is websocket:
            del hub.active_ws[agent_id]
        # NOTE: 不在此处标 offline——cleanup loop 根据心跳超时统一处理。
        # WS 断连可能是瞬态（ping timeout / 网络抖动），agent 会重连。


# ============ CD-094：WS 共享文档披露级别门（2026-09-23 终审补丁） ============
# 背景：CD-094「共享文档纳入披露判定」只封了 REST 读出口
# （routes_shared.api_shared_get 按 min(主体级别, trust_level 密级) 剥离），
# WS 侧是洞——进房只查 is_archived，任何有效 api_key 的 worker 连上 private
# 文档房间即可经 CRDT 同步拿全文；watch 通道同样无校验，preview 推给全部 watcher。
#
# 级别矩阵（判定复用 disclosure.DisclosureEngine.shared_doc_level，与 REST 同一口径）：
#   主体级别   /ws/shared/{doc_id}                /ws/shared/watch/{doc_id}
#              （CRDT 房 = 全文读写）             （轻量事件流，preview ≤ 100 字）
#   FULL       放行                               放行
#   SUMMARY    拒绝 4403（fail-closed：CRDT       放行（preview 100 字 ≤ summary 200 字口径）
#              是全文通道无法只给摘要；引导走 watch）
#   METADATA   拒绝 4403（协同 = 全文读写，        放行（CD-104：preview 剥离为空串
#              metadata 级不该进房）              + preview_stripped=true，不再送正文）
#   NONE       拒绝 4403                          拒绝 4403
# private 文档非 allowed_agents 成员：可见性硬门恒 NONE（与 REST 的 can_access
# 第一道门同源——注意规则链对未知主体有 4.5 METADATA 兜底，并不恒 NONE，
# 仅靠级别判定会让 private 文档 preview 经 watch 漏给未知主体）→ 两通道一律拒。
# hub_token 运维主体（D1 不做 RBAC）不判定直接放行（与 REST 读出口旁路同源）；
# NO_AUTH 开发态 principal=None → 主体侧不判定、文档密级仍生效（fail-closed 兜底）。
# 文档不存在（meta=None）→ 按 NONE 拒（fail-closed；hub_token 除外，既有
# 「不存在」语义不变，见 tests/test_ws_auth_matrix.py）。
# 拒绝码 4403 沿用仓内自定义 44xx 口径（4401 鉴权失败 / 4404 已归档）。


async def _shared_ws_gate(websocket: WebSocket, doc_id: str, agent_id: str,
                          principal, room: bool) -> bool:
    """CD-094 WS 级别门：True 放行；False = 已 close 4403，调用方直接 return。

    room=True（CRDT 全文读写房）仅 FULL 放行；room=False（watch，preview ≤ 100 字）
    NONE 拒、METADATA/SUMMARY/FULL 放行。矩阵见上方注释。
    """
    level = await _shared_doc_ws_level(doc_id, agent_id, principal)
    ok = level == DisclosureLevel.FULL if room else level != DisclosureLevel.NONE
    if ok:
        return True
    reason = "Forbidden: doc disclosure level below full" if room \
        else "Forbidden: no doc access"
    try:
        await websocket.close(code=4403, reason=reason)
    except Exception as _exc:
        logger.debug("routes_ws silent-except(_shared_ws_gate): %s", _exc)
    return False


@router.websocket("/ws/shared/watch/{doc_id}")
async def ws_shared_watch(websocket: WebSocket, doc_id: str):
    """轻量 JSON 事件监听 — 无需 pycrdt，纯文本推送。P1 首帧鉴权（D3）
    + CD-094 披露级别门（NONE 拒连，metadata/summary/full 放行）"""
    agent_id, principal = await _ws_auth_accept_full(websocket)
    if agent_id is None:
        return
    if not agent_id:
        agent_id = "__anon__"
    if not await _shared_ws_gate(websocket, doc_id, agent_id, principal, room=False):
        return
    # 2026-09-22：已归档的房不接纳新 watcher —— 归档后 REST 读写已 404，
    # 通道也要一起关（先送一帧 shared_archived 再按 4404 关闭）。
    if _doc_archived(doc_id):
        import json as _json
        try:
            await websocket.send_text(_json.dumps({
                "type": "shared_archived", "doc_id": doc_id, "reason": "doc archived"}))
        except Exception:
            pass
        try:
            await websocket.close(code=4404, reason="doc archived")
        except Exception:
            pass
        return
    watchers = _shared_watchers.setdefault(doc_id, {})
    watchers[agent_id] = (websocket, time.time(), principal)
    try:
        # 通知已有协作者：新成员加入
        import json as _json
        await _broadcast_shared_update(doc_id, {
            "type": "shared_presence", "doc_id": doc_id,
            "agent_id": agent_id, "joined": True,
        })
        while True:
            await websocket.receive_text()  # keepalive
    except Exception as _exc:
        logger.debug("routes silent-except(ws_shared_watch): %s", _exc)
    finally:
        # 身份比对注销：同一 agent 快速重连时，旧连接的 finally 不得误删新 watcher
        if watchers.get(agent_id, (None,))[0] is websocket:
            watchers.pop(agent_id, None)
        # 通知剩余协作者：成员离开
        try:
            await _broadcast_shared_update(doc_id, {
                "type": "shared_presence", "doc_id": doc_id,
                "agent_id": agent_id, "joined": False,
            })
        except Exception as _exc:
            logger.debug("routes silent-except(ws_shared_watch): %s", _exc)


@router.websocket("/ws/shared/{doc_id}")
async def ws_shared(websocket: WebSocket, doc_id: str):
    """实时协同编辑 — pycrdt YRoom。P1 首帧鉴权（D3）
    + CD-094 披露级别门（CRDT=全文读写，仅 FULL 主体进房；其余 4403 引导走 watch）"""
    agent_id, principal = await _ws_auth_accept_full(websocket)
    if agent_id is None:
        return
    ws_inst = _ws()
    if ws_inst is None:
        await websocket.close(code=4000, reason="workspace not ready")
        return
    if not await _shared_ws_gate(websocket, doc_id, agent_id, principal, room=True):
        return
    await ws_inst.serve_websocket(doc_id, websocket)

# D-8 验收修正（2026-09-14）：本模块不再 import routes——routes <-> routes_ws 循环依赖
# （子模块反向 import 装配层）已消除：NO_AUTH / _auth_provider 经 routes_common 命名空间
# 在调用时解析（与搬前经 routes 命名空间取值的语义一致；测试 patch routes_common.<name>
# 即生效），_version_ge 随之上移 routes_common（routes.py 保留 re-export）。
