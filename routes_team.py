"""星枢 Sync Hub — 团队联邦 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_team")

import asyncio, json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import BaseModel as PydanticBase, Field as PydanticField

from models import CONFIG
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
)

router = APIRouter()


def _fetch_url_sync(request, timeout: float) -> bytes:
    """同步 HTTP 请求并读取响应体——仅经 asyncio.to_thread 调用，避免阻塞事件循环"""
    import urllib.request as _ur
    with _ur.urlopen(request, timeout=timeout) as resp:
        return resp.read()

@router.get("/api/v1/team/stats")
async def api_team_stats(current_agent: str = Depends(get_current_agent)):
    """P1: 团队仪表盘聚合统计 — agent 在线/角色/心跳 + 任务 by-status/by-agent + 记忆 by-kind + 自动化状态"""
    agents = []
    for aid, a in hub.agents.items():
        agents.append({
            "agent_id": aid,
            "agent_name": a.get("agent_name", ""),
            "role": a.get("role", "worker"),
            "department": a.get("department", ""),
            "status": a.get("status", "offline"),
            "last_heartbeat": a.get("last_heartbeat", ""),
            "online": a.get("status") == "online",
        })
    agents.sort(key=lambda x: (not x["online"], x["agent_id"]))

    conn = hub._db()
    try:
        task_rows = conn.execute("SELECT status, assigned_agent_id FROM tasks").fetchall()
        mem_rows = conn.execute("SELECT kind FROM memory_pool").fetchall()
        auto_rows = conn.execute("SELECT enabled, last_status FROM automation_jobs").fetchall()
    finally:
        conn.close()

    by_status = {}
    by_agent = {}
    for status, assigned in task_rows:
        by_status[status] = by_status.get(status, 0) + 1
        if assigned:
            d = by_agent.setdefault(assigned, {})
            d[status] = d.get(status, 0) + 1

    by_kind = {}
    for (kind,) in mem_rows:
        k = kind or "other"
        by_kind[k] = by_kind.get(k, 0) + 1

    auto = {"total": len(auto_rows), "enabled": 0, "disabled": 0, "by_last_status": {}}
    for enabled, last_status in auto_rows:
        if enabled:
            auto["enabled"] += 1
        else:
            auto["disabled"] += 1
        if last_status:
            auto["by_last_status"][last_status] = auto["by_last_status"].get(last_status, 0) + 1

    return {"status": "ok",
            "agents": agents,
            "tasks": {"by_status": by_status, "by_agent": by_agent},
            "memory": {"total": len(mem_rows), "by_kind": by_kind},
            "automation": auto}


@router.get("/api/v1/team/discover")
async def api_team_discover(current_agent: str = Depends(get_current_agent)):
    """UDP 广播/多播发现的设备列表"""
    peers = []
    try:
        discovery = getattr(hub, "discovery", None)
        if discovery is not None:
            peers = discovery.peers
    except Exception:
        peers = []
    return {"peers": peers}


@router.get("/api/v1/team/ping")
async def api_team_ping():
    """手工输入 IP 时验证对方 Hub 可达性"""
    return {"status": "ok", "hub_id": hub.hub_id if hasattr(hub, 'hub_id') else "sync-hub"}


@router.get("/api/v1/team/members")
async def api_team_members(current_agent: str = Depends(get_current_agent)):
    """列出当前 Agent 的团队成员"""
    result = await hub.list_team_members(current_agent)
    return result


@router.post("/api/v1/team/pair/request")
async def api_team_pair_request(current_agent: str = Depends(get_current_agent)):
    """R3-FIX: 发起配对 - 返回 6 位配对码"""
    result = await hub.request_pairing(current_agent)
    return result


# ═══ T11：pair/exchange 每 IP 失败计数 + 指数退避（防 6 位码爆破） ═══
# 码校验失败（无效/过期/已用等 error 响应）按来源 IP 滑动窗口计数：
# 窗口内失败 ≥ 阈值 → 该 IP 退避（60s 起、每次触发翻倍），退避期内直接 429 不查库；
# 校验成功清零该 IP 全部计数。内存态，重启清零（与 WS 熔断同级的轻量防线）。
import threading

_PAIR_FAIL_WINDOW_SEC = 300   # 失败计数窗口（5 分钟）
_PAIR_FAIL_MAX = 5            # 窗口内失败阈值
_PAIR_BACKOFF_BASE_SEC = 60   # 首次退避时长（此后指数翻倍）
_pair_fails: Dict[str, list] = {}        # ip → [fail_ts, ...] 滑动窗口
_pair_ban_until: Dict[str, float] = {}   # ip → 退避截止 ts
_pair_backoff_sec: Dict[str, float] = {} # ip → 当前退避时长（指数递增）
_PAIR_LOCK = threading.Lock()


def _pair_ip_banned(ip: str) -> bool:
    """当前 IP 是否处于退避期。"""
    now = time.time()
    with _PAIR_LOCK:
        until = _pair_ban_until.get(ip, 0)
        if until > now:
            return True
        if until > 0:
            del _pair_ban_until[ip]  # 过期清理
        return False


def _pair_record_failure(ip: str) -> bool:
    """记录一次码校验失败；返回 True 表示本次触发了（新一轮）退避。"""
    now = time.time()
    with _PAIR_LOCK:
        if _pair_ban_until.get(ip, 0) > now:
            return False  # 已在退避中，不叠加
        fails = [t for t in _pair_fails.get(ip, []) if now - t < _PAIR_FAIL_WINDOW_SEC]
        fails.append(now)
        _pair_fails[ip] = fails
        if len(fails) >= _PAIR_FAIL_MAX:
            backoff = _pair_backoff_sec.get(ip, 0)
            backoff = backoff * 2 if backoff else _PAIR_BACKOFF_BASE_SEC
            _pair_backoff_sec[ip] = backoff
            _pair_ban_until[ip] = now + backoff
            _pair_fails[ip] = []
            return True
        return False


def _pair_record_success(ip: str) -> None:
    """校验成功 → 清零该 IP 的失败计数与退避状态。"""
    with _PAIR_LOCK:
        _pair_fails.pop(ip, None)
        _pair_backoff_sec.pop(ip, None)
        _pair_ban_until.pop(ip, None)


@router.post("/api/v1/team/pair/exchange")
async def api_team_pair_exchange(req: dict, request: Request):
    """配对握手：对方 Hub 携带 DH 公钥 + 配对码来交换 api_key（R3-FIX 注册，防孤儿）

    T11：退避期内的 IP 直接 429（不查库）；码校验失败按 IP 计数，成功清零。"""
    code = (req.get("code") or "").strip()
    if not code:
        return {"error": "code 必填"}
    ip = request.client.host if request.client else "unknown"
    if _pair_ip_banned(ip):
        return JSONResponse({"error": "失败次数过多，请稍后重试"}, status_code=429)
    result = await hub._handle_pair_exchange(code, req)
    if isinstance(result, dict) and result.get("error"):
        _pair_record_failure(ip)
    else:
        _pair_record_success(ip)
    return result


@router.post("/api/v1/team/pair/accept")
async def api_team_pair_accept(req: dict, current_agent: str = Depends(get_current_agent)):
    """R3-FIX: 接受配对 - 校验 6 位码 + 交换密钥"""
    result = await hub.accept_pairing(current_agent, req)
    return result


@router.delete("/api/v1/team/members/{member_id}")
async def api_team_remove(member_id: int, current_agent: str = Depends(get_current_agent)):
    from routes_n1 import _n1_gate
    _gate = await _n1_gate(current_agent, "team_members",
                           {"member_id": member_id, "owner": current_agent})
    if _gate:
        return _gate
    """移除团队成员"""
    result = await hub.remove_team_member(member_id, current_agent)
    return result


async def _locate_shared_secret(request: Request) -> bytes:
    """P2 加密信道密钥定位：requester_agent_id 走 query param（密钥路由用，不泄露披露内容），
    从 team_members 取配对时落库的 shared_secret。未配对/无密钥 → 抛异常（→403）。
    注意：不能读 request.body()——外层已消费，Starlette body 只可读一次。"""
    requester = request.query_params.get("requester_agent_id", "")
    conn = hub._db()
    c = conn.cursor()
    row = c.execute(
        "SELECT shared_secret FROM team_members "
        "WHERE remote_agent_id = ? AND shared_secret IS NOT NULL AND revoked_at IS NULL",
        (requester,),
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        raise ValueError("no shared secret for requester")
    return bytes.fromhex(row[0])


@router.post("/api/v1/team/proxy/disclose")
async def api_team_proxy_disclose(request: Request):
    """跨 Hub 代理披露 — remote_api_key 认证 + 本地 role 裁决。P2 升级：加密信道。

    P2 双模式：
    - 带 X-Hub-Crypto: v1 头：请求体为 AES-GCM 密文（shared_secret 解密），
      未配对/错密钥/重放 → 403；解密失败不泄露任何明文信息
    - 无头（旧客户端兼容）：明文 JSON + Bearer remote_api_key
    """
    import json as _json
    body_bytes = await request.body()
    auth = request.headers.get("Authorization", "")
    crypto_hdr = request.headers.get("X-Hub-Crypto", "")

    if crypto_hdr == "v1":
        # P2 加密信道：AES-GCM 解密（shared_secret 来自配对握手）
        from fed_crypto import decrypt_payload
        try:
            enc = _json.loads(body_bytes.decode("utf-8"))
            secret = await _locate_shared_secret(request)  # async，必须 await
            # T17: peer 隔离防重放——peer = 密钥路由用的 requester（query param）
            req_peer = request.query_params.get("requester_agent_id", "")
            plain = decrypt_payload(secret, enc, peer=req_peer)
            body = _json.loads(plain)
        except Exception:
            # T2-3/T2-4: 重放/伪造/解密失败 → 403 + 审计记录（可追踪攻击尝试）
            try:
                await hub._log_event(
                    "proxy_disclose_rejected",
                    "hub:unknown",
                    {"reason": "decrypt failed or replay",
                     "requester": request.query_params.get("requester_agent_id", ""),
                     "crypto": crypto_hdr},
                )
            except Exception:
                pass
            return JSONResponse({"error": "decrypt failed or replay"}, status_code=403)
        # 加密信道信任锚点 = 配对关系（shared_secret 只在配对双方间存在）：
        # requester_agent_id 来自解密后的明文，虚拟身份 role 用本地 team_members 记录
        requester = body.get("requester_agent_id", "")
        conn = hub._db()
        c = conn.cursor()
        c.execute(
            "SELECT remote_agent_id, role, department, hostname FROM team_members "
            "WHERE remote_agent_id = ? AND shared_secret IS NOT NULL AND revoked_at IS NULL",
            (requester,),
        )
        row = c.fetchone()
        conn.close()
        if not row:
            return JSONResponse({"error": "unpaired requester"}, status_code=403)
        remote_agent_id, local_role, department, hostname = row
        # 防伪造：解密出的 requester 必须与密钥路由用的 requester 一致
        if requester != request.query_params.get("requester_agent_id", ""):
            return JSONResponse({"error": "requester mismatch"}, status_code=403)
    else:
        # 旧模式：明文 + Bearer remote_api_key
        api_key = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
        if not api_key:
            return JSONResponse({"error": "missing api key"}, status_code=401)
        conn = hub._db()
        c = conn.cursor()
        c.execute(
            "SELECT remote_agent_id, role, department, hostname, shared_secret FROM team_members "
            "WHERE remote_api_key = ? AND revoked_at IS NULL",
            (api_key,),
        )
        row = c.fetchone()
        conn.close()
        if not row:
            # T2-4: 未配对/假 key → 403 + 审计
            try:
                await hub._log_event(
                    "proxy_disclose_rejected",
                    "hub:unknown",
                    {"reason": "invalid or revoked key", "crypto": "plain"},
                )
            except Exception as _exc:
                logger.warning("routes_team silent-except @303: %s", _exc)
            return JSONResponse({"error": "invalid or revoked key"}, status_code=403)
        remote_agent_id, local_role, department, hostname, _sec = row
        body = _json.loads(body_bytes.decode("utf-8"))
    
    # 构造虚拟身份（信任锚点在本地 team_members.role）
    virtual_agent = {
        "agent_id": remote_agent_id,
        "role": local_role,
        "department": department,
    }

    target_agent_id = body.get("target_agent_id", "")
    query = body.get("query", "")
    level = body.get("required_level", "summary")

    result = await hub.disclosure.disclose_for_remote(
        virtual_agent, target_agent_id, query, level
    )

    # 双主体审计
    await hub._log_event(
        "proxy_disclose",
        f"hub:{remote_agent_id}",
        {
            "target": target_agent_id,
            "role_used": local_role,
            "hostname": hostname,
        },
    )

    return result


@router.post("/api/v1/team/disclose/remote")
async def api_team_remote_disclose(
    agent_id: str,
    remote_agent_id: str,
    query: str,
    required_level: str = "summary",
    current_agent: str = Depends(get_current_agent),
):
    """跨 Hub 披露调用端：查配对表拿对方 Hub 地址+交换密钥 → 直调对方 /proxy/disclose"""
    if not NO_AUTH and current_agent != agent_id:
        raise HTTPException(status_code=403,
            detail=f"Forbidden: 不能以 {current_agent} 身份操作 {agent_id}")
    conn = hub._db()
    c = conn.cursor()
    row = c.execute(
        "SELECT remote_hub_url, remote_api_key, shared_secret FROM team_members "
        "WHERE remote_agent_id=? AND revoked_at IS NULL",
        (remote_agent_id,),
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return {"ok": False, "error": f"未找到 {remote_agent_id} 的配对信息（remote_hub_url）"}
    remote_url = row[0].rstrip("/")
    remote_key = row[1]
    shared_secret_hex = row[2] or ""
    import urllib.request as _ur
    import json as _json
    payload = {
        "requester_agent_id": agent_id,
        "target_agent_id": remote_agent_id,
        "query": query,
        "required_level": required_level,
    }
    headers = {"Content-Type": "application/json"}
    # P2: 有共享密钥 → AES-GCM 加密信道；无（旧配对）→ 降级明文 Bearer
    if shared_secret_hex:
        from fed_crypto import encrypt_payload
        import urllib.parse as _uparse
        enc = encrypt_payload(bytes.fromhex(shared_secret_hex),
                              _json.dumps(payload).encode(), with_ts=True)  # T17: ts+aad
        body = _json.dumps(enc).encode()
        headers["X-Hub-Crypto"] = "v1"
        headers["Authorization"] = f"Bearer {remote_key}"  # 冗余校验（被调端可验）
        url = f"{remote_url}/api/v1/team/proxy/disclose?requester_agent_id={_uparse.quote(agent_id)}"
    else:
        body = _json.dumps(payload).encode()
        headers["Authorization"] = f"Bearer {remote_key}"
        url = f"{remote_url}/api/v1/team/proxy/disclose"
    hr = _ur.Request(url, data=body, method="POST", headers=headers)
    try:
        raw = await asyncio.to_thread(_fetch_url_sync, hr, 12)
        return _json.loads(raw.decode())
    except Exception as e:
        return {"ok": False, "error": f"远程披露失败: {str(e)[:120]}"}


@router.post("/api/v1/team/members/{member_id}/revoke")
async def api_team_revoke(member_id: int, current_agent: str = Depends(get_current_agent)):
    """撤销配对 — 通知对方 Hub 删除本地 key"""
    result = await hub.remove_team_member(member_id, current_agent)
    return result


