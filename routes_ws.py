"""星枢 Sync Hub — WebSocket 通道端点（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_ws")

import asyncio, time
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from envelope import envelope_pong, parse_envelope, is_legacy_flat, extract_payload, serialize
from transport_audit import log_transport_frame, log_dispatch_in, log_ping_pong
from models import CONFIG
from hub_core import hub
from notifications import notifications
import routes_common
from routes_common import _version_ge, principal_is_privileged
# _ws 为 routes_shared 里的动态 getter（返回 shared_workspace.workspace 当前值），
# 生命周期里重新绑定的是 shared_workspace.workspace——import 此函数语义与搬前一致
from routes_shared import _ws, _shared_watchers, _broadcast_shared_update

router = APIRouter()


# ============ WS 首帧鉴权（P1 安全底线） ============
# D3: 连接后首帧必须 {type:"auth", token}，3 秒超时或校验失败 → close 4401。
# 不用 query param（会进日志）、不用 header（浏览器 WS 不支持）。
# 认证成功前连接不入任何管理器（NotificationManager/active_ws/YRoom）。
# P0 S4（2026-08-04）：鉴权失败按来源 IP 滑动窗口计数 + 自动熔断——
# 同 IP 窗口内失败 N 次封禁 T 秒（内存 + events 表持久化防重启清零），
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
        logger.warning("routes silent-except @446: %s", _exc)
    try:
        await hub.create_notification(
            "__dashboard__", "security",
            f"WS 鉴权熔断: {ip}",
            f"IP {ip} 在 {CONFIG.WS_AUTH_WINDOW_SEC}s 内鉴权失败 {CONFIG.WS_AUTH_MAX_FAILS} 次，已封禁 {CONFIG.WS_AUTH_BAN_SEC}s",
            source="ws_gate")
    except Exception as _exc:
        logger.warning("routes silent-except @454: %s", _exc)


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
    await websocket.accept()
    if routes_common.NO_AUTH:
        return agent_id_hint or websocket.query_params.get("agent_id", "")

    # P0 S4：封禁中的 IP 直接拒绝（连鉴权都不给）
    ip = _client_ip(websocket)
    if await _ws_banned(ip):
        await websocket.close(code=4401, reason="IP banned")
        return None

    try:
        frame = await asyncio.wait_for(websocket.receive_json(), timeout=WS_AUTH_TIMEOUT)
    except asyncio.TimeoutError:
        if await _ws_record_failure(ip):
            await _audit_ws_ban(ip, "auth_fail_ban")
        await websocket.close(code=4401, reason="Auth timeout")
        return None
    except Exception:
        await websocket.close(code=4401, reason="Auth required")
        return None

    if not isinstance(frame, dict) or frame.get("type") != "auth":
        await websocket.close(code=4401, reason="Auth frame required")
        return None

    token = str(frame.get("token", ""))
    if not token:
        await websocket.close(code=4401, reason="Missing token")
        return None

    # S1：auth_provider 统一认证（api_key 过期/白名单/轮换语义全覆盖）
    principal = routes_common._auth_provider().authenticate(token, ip)
    if principal is None:
        if await _ws_record_failure(ip):
            await _audit_ws_ban(ip, "auth_fail_ban")
        await websocket.close(code=4401, reason="Invalid token")
        return None
    # T15：特权监控通道角色门（/ws/dashboard、/ws/buffer）——
    # 凭据有效但非 hub_token/manager/orchestrator → 拒绝，不计入熔断（非鉴权失败）
    if require_privileged and not principal_is_privileged(principal):
        await websocket.close(code=4401, reason="Forbidden: privileged role required")
        return None
    # hub_token 路径：信任声明身份（D1）；api_key 路径：精确归属
    if principal.auth_mode == "hub_token":
        return agent_id_hint or websocket.query_params.get("agent_id", "")
    if strict_agent and agent_id_hint and principal.subject_id != agent_id_hint:
        if await _ws_record_failure(ip):
            await _audit_ws_ban(ip, "auth_fail_ban")
        await websocket.close(code=4401, reason="Unauthorized")
        return None
    return principal.subject_id



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
            return await hub.search_memory(agent_id, params.get("query", ""), params.get("limit", 10))
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
                if is_legacy_flat(raw):
                    msg_type = raw.get("msg_type")
                    if msg_type == "heartbeat":
                        await hub.heartbeat(agent_id)
                        hub.record_pong(agent_id)  # L5
            else:
                etype = env["type"]
                session_id = env.get("session_id", "")
                
                if etype == "ping":
                    hub.record_pong(agent_id)
                    pong_env = envelope_pong(); await websocket.send_text(serialize(pong_env)); log_ping_pong('out', pong_env)
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
                            logger.debug("routes silent-except @664: %s", _exc)
                        await hub._log_event(
                            "agent_version_rejected", agent_id,
                            {"agent_version": _ver, "min_version": CONFIG.AGENT_MIN_VERSION})
                        return
                    # L3: replay pending dispatches
                    ckpt = extract_payload(env).get("last_checkpoint_id", "")
                    pending = hub.get_pending_dispatches(session_id, ckpt)
                    for disp in pending:
                        hub.inc_in_flight(agent_id)
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
                        resp = _build_env("response", {"id": req_id, "error": str(e)})
                    await websocket.send_text(serialize(resp))

    except WebSocketDisconnect:
        pass
    finally:
        notifications.disconnect(agent_id, websocket)  # 从通知推送池注销
        if agent_id in hub.active_ws:
            del hub.active_ws[agent_id]
        # NOTE: 不在此处标 offline——cleanup loop 根据心跳超时统一处理。
        # WS 断连可能是瞬态（ping timeout / 网络抖动），agent 会重连。


@router.websocket("/ws/shared/watch/{doc_id}")
async def ws_shared_watch(websocket: WebSocket, doc_id: str):
    """轻量 JSON 事件监听 — 无需 pycrdt，纯文本推送。P1 首帧鉴权（D3）"""
    agent_id = await _ws_auth_accept(websocket)
    if agent_id is None:
        return
    if not agent_id:
        agent_id = "__anon__"
    watchers = _shared_watchers.setdefault(doc_id, {})
    watchers[agent_id] = (websocket, time.time())
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
        logger.debug("routes silent-except @1057: %s", _exc)
    finally:
        watchers.pop(agent_id, None)
        # 通知剩余协作者：成员离开
        try:
            await _broadcast_shared_update(doc_id, {
                "type": "shared_presence", "doc_id": doc_id,
                "agent_id": agent_id, "joined": False,
            })
        except Exception as _exc:
            logger.debug("routes silent-except @1067: %s", _exc)


@router.websocket("/ws/shared/{doc_id}")
async def ws_shared(websocket: WebSocket, doc_id: str):
    """实时协同编辑 — pycrdt YRoom。P1 首帧鉴权（D3）"""
    agent_id = await _ws_auth_accept(websocket)
    if agent_id is None:
        return
    ws_inst = _ws()
    if ws_inst is None:
        await websocket.close(code=4000, reason="workspace not ready")
        return
    await ws_inst.serve_websocket(doc_id, websocket)

# D-8 验收修正（2026-09-14）：本模块不再 import routes——routes <-> routes_ws 循环依赖
# （子模块反向 import 装配层）已消除：NO_AUTH / _auth_provider 经 routes_common 命名空间
# 在调用时解析（与搬前经 routes 命名空间取值的语义一致；测试 patch routes_common.<name>
# 即生效），_version_ge 随之上移 routes_common（routes.py 保留 re-export）。
