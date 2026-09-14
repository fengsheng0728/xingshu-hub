"""星枢 Sync Hub — 审计链 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_audit")

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

@router.post("/api/audit/verify")
async def api_audit_verify(request: Request,
                             current_agent: str = Depends(get_current_agent)):
    """S2：审计 hash chain 完整性校验（主链 + 披露链 + jsonl 滚动链）。
    入参：{start_id?, end_id?}（0=全链）。输出 {valid, chains, checked_total}。
    """
    try:
        from audit_chain import verify_all
        body = await request.json()
        start_id = int(body.get("start_id", 0) or 0)
        end_id = int(body.get("end_id", 0) or 0)
    except Exception:
        start_id, end_id = 0, 0
    jsonl_files = {
        "memory_pool.jsonl": os.path.join("audit", "memory_pool.jsonl"),
        "transport.jsonl": os.path.join("audit", "transport.jsonl"),
    }
    result = verify_all(CONFIG.DB_PATH, jsonl_files, start_id, end_id)
    # 校验动作本身入审计（谁在何时校验）
    try:
        from audit_chain import AuditChain
        AuditChain(CONFIG.DB_PATH).append(
            "verify", "audit_log", "",
            {"actor": current_agent, "valid": result["valid"],
             "checked_total": result.get("checked_total", 0)})
    except Exception as _exc:
        logger.warning("routes_audit silent-except @48: %s", _exc)
    return result


@router.get("/api/audit/disclosure/rules")
async def api_disclosure_rules(current_agent: str = Depends(get_current_agent)):
    """A3：披露规则表（可枚举，供审计/文档）。"""
    from disclosure_rules import rule_table
    return {"rules": rule_table()}


@router.post("/api/audit/disclosure/replay")
async def api_disclosure_replay(request: Request,
                                   current_agent: str = Depends(get_current_agent)):
    """A3：历史披露审计重放 — 模拟器判定 vs 历史判定，一致性报告。
    100% 一致 = 规则表化未改变线上行为。"""
    from disclosure_rules import replay_disclosure_log
    result = replay_disclosure_log(CONFIG.DB_PATH, hub.agents, hub._disclosure_policy)
    try:
        from audit_chain import AuditChain
        AuditChain(CONFIG.DB_PATH).append(
            "disclosure_replay", "audit_log", "",
            {"actor": current_agent, "total": result["total"],
             "matched": result["matched"], "mismatched": result["mismatched"]})
    except Exception as _exc:
        logger.warning("routes_audit silent-except @73: %s", _exc)
    return result


# ── U3：审计中心（检索 / 上次校验 / 导出） ──
def _require_manager(current_agent: str):
    """与 /api/v1/keys 一致的角色门：仅 manager/orchestrator 可查审计"""
    if not NO_AUTH:
        info = hub.agents.get(current_agent, {})
        if info.get("role") not in ("manager", "orchestrator"):
            raise HTTPException(status_code=403, detail="仅主管/店长可查审计")


def _query_audit_events(time_from: str, time_to: str, entry_type: str,
                        ref_table: str, q: str, limit: int, offset: int):
    """audit_log 检索：时间范围 + 动作类型 + 来源表 + payload 关键词（principal 等）"""
    import sqlite3 as _sq
    conn = _sq.connect(CONFIG.DB_PATH)
    conn.row_factory = _sq.Row
    try:
        conn.execute("SELECT 1 FROM audit_log LIMIT 1")
    except Exception:
        # 老库无 audit_log 表（未迁移 S2）→ 空结果而非 500
        conn.close()
        return 0, [], [], []
    try:
        where, params = [], []
        if time_from:
            where.append("created_at >= ?")
            params.append(time_from)
        if time_to:
            where.append("created_at <= ?")
            params.append(time_to)
        if entry_type:
            where.append("entry_type = ?")
            params.append(entry_type)
        if ref_table:
            where.append("ref_table = ?")
            params.append(ref_table)
        if q:
            where.append("payload LIKE ?")
            params.append(f"%{q}%")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM audit_log {clause}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT log_id, entry_type, ref_table, ref_id, payload, entry_hash, created_at"
            f" FROM audit_log {clause} ORDER BY log_id DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        types = [r[0] for r in conn.execute(
            "SELECT DISTINCT entry_type FROM audit_log ORDER BY entry_type").fetchall()]
        tables = [r[0] for r in conn.execute(
            "SELECT DISTINCT ref_table FROM audit_log ORDER BY ref_table").fetchall()]
        return total, [dict(r) for r in rows], types, tables
    finally:
        conn.close()


@router.get("/api/audit/events")
async def api_audit_events(time_from: str = "", time_to: str = "",
                           entry_type: str = "", ref_table: str = "",
                           q: str = "", limit: int = 50, offset: int = 0,
                           current_agent: str = Depends(get_current_agent)):
    """U3：审计事件检索（时间范围/principal 关键词/动作类型/结果表）"""
    _require_manager(current_agent)
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    total, rows, types, tables = _query_audit_events(
        time_from, time_to, entry_type, ref_table, q, limit, offset)
    return {"status": "ok", "total": total, "rows": rows,
            "facets": {"entry_types": types, "ref_tables": tables},
            "limit": limit, "offset": offset}


@router.get("/api/audit/reads")
async def api_audit_reads(requester: str = "", kind: str = "", limit: int = 50, offset: int = 0,
                          current_agent: str = Depends(get_current_agent)):
    """阶段2/03: 读审计检索（网关读取记录 gateway_read_log：谁/哪把 key/看了什么/给到哪级/剥离多少）"""
    _require_manager(current_agent)
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    conn = sqlite3.connect(CONFIG.DB_PATH)
    conn.row_factory = sqlite3.Row
    where, args = [], []
    if requester:
        where.append("requester LIKE ?")
        args.append(f"%{requester}%")
    if kind:
        where.append("kind = ?")
        args.append(kind)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM gateway_read_log {w}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM gateway_read_log {w} ORDER BY log_id DESC LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    conn.close()
    return {"status": "ok", "total": total, "rows": [dict(r) for r in rows],
            "limit": limit, "offset": offset}


@router.get("/api/audit/last-verify")
async def api_audit_last_verify(current_agent: str = Depends(get_current_agent)):
    """U3：链完整性卡片 — 上次 verify 结果 + 各链覆盖条数"""
    _require_manager(current_agent)
    import sqlite3 as _sq
    conn = _sq.connect(CONFIG.DB_PATH)
    conn.row_factory = _sq.Row
    try:
        conn.execute("SELECT 1 FROM audit_log LIMIT 1")
    except Exception:
        conn.close()
        return {"status": "ok", "last_verify": None,
                "coverage": {"audit_log": 0, "disclosure_log": 0, "jsonl_anchors": 0}}
    try:
        last = conn.execute(
            "SELECT log_id, payload, created_at FROM audit_log"
            " WHERE entry_type='verify' ORDER BY log_id DESC LIMIT 1").fetchone()
        audit_count = conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"]
        try:
            disc_count = conn.execute(
                "SELECT COUNT(*) AS c FROM disclosure_log WHERE entry_hash != ''").fetchone()["c"]
        except Exception:
            disc_count = 0
        anchor_count = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE entry_type='jsonl_anchor'").fetchone()["c"]
    finally:
        conn.close()
    last_verify = None
    if last:
        try:
            payload = json.loads(last["payload"])
        except Exception:
            payload = {}
        last_verify = {"log_id": last["log_id"], "created_at": last["created_at"],
                       "valid": payload.get("valid"), "actor": payload.get("actor", ""),
                       "checked_total": payload.get("checked_total", 0)}
    return {"status": "ok", "last_verify": last_verify,
            "coverage": {"audit_log": audit_count, "disclosure_log": disc_count,
                         "jsonl_anchors": anchor_count}}


@router.get("/api/audit/export")
async def api_audit_export(format: str = "json", time_from: str = "", time_to: str = "",
                           entry_type: str = "", ref_table: str = "", q: str = "",
                           limit: int = 1000,
                           current_agent: str = Depends(get_current_agent)):
    """U3：审计导出 CSV/JSON（导出动作本身入审计链）"""
    _require_manager(current_agent)
    limit = min(max(1, limit), 10000)
    total, rows, _t, _tb = _query_audit_events(
        time_from, time_to, entry_type, ref_table, q, limit, 0)
    # 导出动作本身入审计（谁导出了什么范围）
    try:
        from audit_chain import AuditChain
        AuditChain(CONFIG.DB_PATH).append(
            "audit_export", "audit_log", "",
            {"actor": current_agent, "format": format, "rows": len(rows),
             "filters": {"from": time_from, "to": time_to, "entry_type": entry_type,
                         "ref_table": ref_table, "q": q}})
    except Exception as _exc:
        logger.warning("routes_audit silent-except @233: %s", _exc)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if format == "csv":
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["log_id", "entry_type", "ref_table", "ref_id",
                    "payload", "entry_hash", "created_at"])
        for r in rows:
            w.writerow([r["log_id"], r["entry_type"], r["ref_table"], r["ref_id"],
                        r["payload"], r["entry_hash"], r["created_at"]])
        return HTMLResponse(
            buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="audit-{ts}.csv"'})
    return JSONResponse(
        {"status": "ok", "exported": len(rows), "total_matched": total, "rows": rows},
        headers={"Content-Disposition": f'attachment; filename="audit-{ts}.json"'})




# ── U4：披露模拟器（单条判定 → 逐规则命中轨迹） ──

@router.post("/api/audit/disclosure/simulate")
async def api_disclosure_simulate(request: Request,
                                  current_agent: str = Depends(get_current_agent)):
    """U4：披露模拟器 — 输入 requester + target，返回判定级别 + 命中规则 + 逐规则轨迹。

    body: {
      requester_agent_id: str (必填),
      memory_id: str (可选，加载真实记忆快照),
      owner_agent_id: str (无 memory_id 时必填，合成记忆),
      memory_level: str (合成记忆的存储级别, 默认 summary),
      allowed_viewers: list (合成记忆白名单, 默认 []),
      required_level: str (默认 full),
      task_id: str (可选，r7 同级协作判定的任务上下文)
    }
    轨迹语义：规则链是短路求值 —— 命中规则之前 = 已评估未通过（pass），
    命中 = hit，之后 = 未到达（skip）。r9 为写入时打标的说明性规则，永不返回。
    """
    _require_manager(current_agent)
    from disclosure_rules import simulate, rule_table, DisclosureLevel
    body = await request.json()
    requester = (body.get("requester_agent_id") or "").strip()
    memory_id = (body.get("memory_id") or "").strip()
    owner = (body.get("owner_agent_id") or "").strip()
    if not requester:
        raise HTTPException(status_code=400, detail="requester_agent_id 必填")

    # ── 记忆快照：真实加载 or 合成 ──
    memory_source = "synthetic"
    if memory_id:
        import sqlite3 as _sq
        conn = _sq.connect(CONFIG.DB_PATH)
        conn.row_factory = _sq.Row
        try:
            row = conn.execute("SELECT * FROM memory_pool WHERE memory_id = ?",
                               (memory_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail=f"memory 不存在: {memory_id}")
        memory = dict(row)
        owner = memory.get("owner_agent_id", owner)
        memory_source = "real"
    elif owner:
        memory = {
            "owner_agent_id": owner,
            "disclosure_level": body.get("memory_level", "summary"),
            "allowed_viewers": json.dumps(body.get("allowed_viewers") or []),
        }
    else:
        raise HTTPException(status_code=400, detail="memory_id 或 owner_agent_id 必填其一")

    # ── 任务上下文（r7 同级协作用） ──
    task = {}
    task_id = (body.get("task_id") or "").strip()
    if task_id:
        import sqlite3 as _sq
        conn = _sq.connect(CONFIG.DB_PATH)
        conn.row_factory = _sq.Row
        try:
            trow = conn.execute("SELECT * FROM tasks WHERE task_id = ?",
                                (task_id,)).fetchone()
            task = dict(trow) if trow else {}
        finally:
            conn.close()

    try:
        required = DisclosureLevel(body.get("required_level", "full"))
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"required_level 非法: {body.get('required_level')}")

    sim_level, hit_rule = simulate(
        memory=memory, requester=requester, task=task,
        required_level=required, agents=hub.agents,
        policy=hub._disclosure_policy, db_path=CONFIG.DB_PATH,
    )

    # ── 逐规则轨迹：短路链，命中前 pass / 命中 hit / 命中后 skip ──
    trace = []
    hit_seen = False
    for r in rule_table():
        if r["id"] == "r9_sensitivity_cap":
            stage = "info"      # 写入时打标的说明性规则，不参与读取短路
        elif hit_seen:
            stage = "skip"
        elif r["id"] == hit_rule:
            stage = "hit"
            hit_seen = True
        else:
            stage = "pass"
        trace.append({**r, "stage": stage})

    # 模拟动作入审计（谁模拟了什么）
    try:
        from audit_chain import AuditChain
        AuditChain(CONFIG.DB_PATH).append(
            "disclosure_simulate", "audit_log", "",
            {"actor": current_agent, "requester": requester, "owner": owner,
             "memory_id": memory_id, "level": sim_level.value, "hit_rule": hit_rule})
    except Exception as _exc:
        logger.warning("routes_audit silent-except @356: %s", _exc)

    return {
        "status": "ok",
        "level": sim_level.value,
        "hit_rule": hit_rule,
        "trace": trace,
        "inputs": {
            "requester_agent_id": requester,
            "requester_role": hub.agents.get(requester, {}).get("role", "worker(未注册)"),
            "owner_agent_id": owner,
            "owner_role": hub.agents.get(owner, {}).get("role", "worker(未注册)"),
            "memory_id": memory_id, "memory_source": memory_source,
            "memory_level": memory.get("disclosure_level", ""),
            "required_level": required.value, "task_id": task_id,
        },
    }


async def anchor_export_loop():
    """XS-004：审计锚定周期外发。启动即推一次；interval<=0 推完即退；
    整圈 try/except 兜底防 scheduler 炸；to_thread 防阻塞事件循环。"""
    from audit_chain import export_anchor
    from models import CONFIG
    while True:
        try:
            await asyncio.to_thread(export_anchor, CONFIG.DB_PATH)
        except Exception:
            pass  # 外发失败静默（export_anchor 内部已逐 url 容错，此处兜底）
        interval = getattr(CONFIG, "AUDIT_ANCHOR_INTERVAL", 3600)
        if interval <= 0:
            return
        await asyncio.sleep(interval)
