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

from logfmt import rotate_request_id
from models import CONFIG
from hub_core import hub, hub_agent
from notifications import notifications
from routes_common import (
    NO_AUTH, AUTH_WHITELIST, _authenticate, _auth_provider, _valid_credential,
    _scope_client_ip, get_current_agent, get_current_agent_optional,
    require_ops_privilege, log_ops_trigger,
)
from routes_common import require_role  # CD-074（hub_token 放行的 canonical 角色门）

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
    _audit_dir = CONFIG.AUDIT_DIR or "audit"   # CD-070b：与 audit/memory_audit 同口径
    jsonl_files = {
        "memory_pool.jsonl": os.path.join(_audit_dir, "memory_pool.jsonl"),
        "transport.jsonl": os.path.join(_audit_dir, "transport.jsonl"),
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
        logger.warning("routes_audit silent-except(api_audit_verify): %s", _exc)
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
        logger.warning("routes_audit silent-except(api_disclosure_replay): %s", _exc)
    return result


# ── U3：审计中心（检索 / 上次校验 / 导出） ──
def _require_manager(current_agent: str):
    """CD-074：委托 canonical `routes_common.require_role`（hub_token 放行）。

    原实现只看 `hub.agents[current_agent].role` → hub_token 登录时该组端点整组 403。
    """
    require_role(current_agent, agents=hub.agents, no_auth=NO_AUTH, detail="仅主管/店长可查审计")



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
        # CD-073：已声明缺口（anchor_gap）随卡片一并出参——否则首屏卡片只能说"完整"，
        # 缺口要等到点过校验才看得见（等于把缺口藏进绿灯）
        try:
            gap_rows = conn.execute(
                "SELECT ref_table, ref_id, created_at, payload FROM audit_log"
                " WHERE entry_type='anchor_gap' ORDER BY log_id").fetchall()
        except Exception:
            gap_rows = []
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
    declared_gaps = []
    for _g in gap_rows:
        try:
            _pl = json.loads(_g["payload"] or "{}")
        except Exception:
            _pl = {}
        declared_gaps.append({"ref_table": _g["ref_table"], "ref_id": _g["ref_id"],
                              "created_at": _g["created_at"],
                              "declared_by": _pl.get("declared_by", ""),
                              "reason": _pl.get("reason", "")})
    return {"status": "ok", "declared_gaps": declared_gaps, "last_verify": last_verify,
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
        logger.warning("routes_audit silent-except(api_audit_export): %s", _exc)
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
        logger.warning("routes_audit silent-except(api_disclosure_simulate): %s", _exc)

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


# ── CD-034 R3：链头外部时间戳（RFC3161 TSA） ──
# 为什么放在 Hub 侧：盖章与回拉比对都要读主链，且告警要落 events + 通知 dashboard。
# 语义：被盖章的链头节点必须仍存在于链中；不在 = 链被整段重写/截断 → 告警（见
# audit_chain.verify_tsa 与 tests/test_audit_tsa.py）。


async def _anchor_alarm(detail: Dict) -> None:
    """锚不一致告警：events 审计 + dashboard 安全通知（失败只告警，不阻塞响应，D4）。"""
    try:
        await hub._log_event("anchor_mismatch", "__audit__", detail)
    except Exception as e:
        logger.warning("anchor_mismatch 审计落行失败: %s", e)
    try:
        n = len(detail.get("mismatches") or [])
        await hub.create_notification(
            "__dashboard__", "security", "审计链外部锚不一致",
            f"外部时间戳锚点与当前链不符（{n} 条）——可能被整段重写/截断，"
            f"请立即核查 audit/tsa 与数据库备份。",
            source="audit_anchor")
    except Exception as e:
        logger.warning("anchor_mismatch 通知失败: %s", e)


@router.post("/api/audit/anchor/stamp")
async def api_audit_anchor_stamp(request: Request,
                                 current_agent: str = Depends(get_current_agent)):
    """CD-034 R3：手动触发链头 RFC3161 盖章（重运维端点：hub_token/manager/orchestrator）。

    TSA 地址取 config audit.tsa.url，未配则用默认公共 TSA。盖章后立即回拉比对；
    不一致 → events anchor_mismatch + dashboard 安全通知。"""
    await require_ops_privilege(request, "POST /api/audit/anchor/stamp", current_agent)
    from audit_chain import tsa_stamp, verify_tsa, DEFAULT_TSA_URL
    url = getattr(CONFIG, "AUDIT_TSA_URL", "") or DEFAULT_TSA_URL
    res = await asyncio.to_thread(tsa_stamp, CONFIG.DB_PATH, url)
    if res.get("status") != "ok":
        raise HTTPException(status_code=502,
                            detail=f"TSA 盖章失败: {res.get('error')}")
    v = await asyncio.to_thread(verify_tsa, CONFIG.DB_PATH)
    await log_ops_trigger("POST /api/audit/anchor/stamp", current_agent,
                          {"anchor": (res.get("anchor") or "")[:16],
                           "bytes": res.get("bytes", 0),
                           "checked": v.get("checked", 0)})
    if not v.get("valid"):
        await _anchor_alarm({"source": "manual_stamp",
                             "mismatches": (v.get("mismatches") or [])[:5]})
    return {"status": "ok", "anchor": res["anchor"], "imprint": res["imprint"],
            "tsq": res["tsq"], "tsr": res["tsr"], "tsa_url": res["tsa_url"],
            "bytes": res.get("bytes", 0), "token_has_imprint": res.get("token_has_imprint"),
            "verify": v}


@router.get("/api/audit/anchor/status")
async def api_audit_anchor_status(current_agent: str = Depends(get_current_agent)):
    """本机锚 + 外部 TSA 锚的回拉比对状态（CD-034 R1/R3 统一查询入口）。"""
    _require_manager(current_agent)
    from audit_chain import verify_anchor, verify_tsa, DEFAULT_TSA_URL
    local = await asyncio.to_thread(verify_anchor, CONFIG.DB_PATH)
    tsa = await asyncio.to_thread(verify_tsa, CONFIG.DB_PATH)
    if not tsa.get("valid"):
        await _anchor_alarm({"source": "status_query",
                             "mismatches": (tsa.get("mismatches") or [])[:5]})
    return {"status": "ok", "local_anchor": local, "tsa": tsa,
            "tsa_enabled": bool(getattr(CONFIG, "AUDIT_TSA_ENABLED", False)),
            "tsa_url": getattr(CONFIG, "AUDIT_TSA_URL", "") or DEFAULT_TSA_URL}


def _collect_token_verifications(tsr: str, tsa_dir: str) -> List[Dict]:
    """TSA 令牌离线验签集合（同步；由 asyncio.to_thread 调用，不占事件循环）。

    - 显式 `tsr`：单令牌验签，判据同 `scripts/verify_tsa_anchor.py token`
      （chain_head 取当前主链链头，pins 走配置）
    - 否则遍历 tsa 目录 `index.jsonl` 的 ok 记录逐个验签，判据同脚本 `tsa-dir`；
      `index.jsonl` 不存在 → 空列表（不报错）
    单条异常/缺文件不拖垮整批：记 `unverified` + reason（读不到不是篡改证据），
    与脚本退出码语义一致（unverified ≠ failed）。
    """
    from audit_chain import (
        verify_tsa_token, current_chain_head, tsa_out_dir, _load_trusted_fingerprints,
    )
    if tsr:
        head = current_chain_head(CONFIG.DB_PATH) or ""
        try:
            r = verify_tsa_token(tsr, head, None)
        except Exception as e:
            r = {"status": "unverified",
                 "reason": f"tsr_unreadable: {type(e).__name__}: {e}"[:200]}
        return [{"tsr": tsr, "anchor": (head or "")[:16], **r}]
    out_dir = tsa_out_dir(tsa_dir)
    idx = os.path.join(out_dir, "index.jsonl")
    if not os.path.isfile(idx):
        return []
    try:
        with open(idx, "r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        logger.warning("tsa index 读取失败（tokens 记空，不阻塞响应）: %s", e)
        return []
    pins = _load_trusted_fingerprints()
    details: List[Dict] = []
    for rec in rows:
        if rec.get("status") != "ok" or not rec.get("tsr"):
            continue
        tsr_path = rec["tsr"]
        if not os.path.isfile(tsr_path):
            details.append({"tsr": tsr_path, "status": "unverified",
                            "reason": "tsr_file_missing"})
            continue
        try:
            r = verify_tsa_token(tsr_path, rec.get("anchor") or "", pins)
        except Exception as e:
            r = {"status": "unverified",
                 "reason": f"tsr_unreadable: {type(e).__name__}: {e}"[:200]}
        details.append({"tsr": tsr_path,
                        "anchor": (rec.get("anchor") or "")[:16], **r})
    return details


def _anchors_mismatches(anchors: Dict) -> List[Dict]:
    """从 fetch_and_verify_anchors 结果提取不一致项（最多 5 条）。

    只收真 mismatch（成功取回但锚不在链中）；`unverified`/`rejected` 取不回/被拒，
    不是篡改证据，**不混进 mismatches**（口径见 audit_chain.fetch_and_verify_anchors
    docstring 与本任务 3.2b）。
    """
    mm = anchors.get("mismatches")
    if isinstance(mm, list):
        return mm[:5]
    return [r for r in (anchors.get("results") or [])
            if r.get("status") == "mismatch"][:5]


@router.post("/api/audit/anchor/verify")
async def api_audit_anchor_verify(request: Request,
                                  current_agent: str = Depends(get_current_agent)):
    """CD-106：TSA 验签 HTTP 出口（重运维端点：hub_token/manager/orchestrator）。

    把离线脚本 `scripts/verify_tsa_anchor.py` 的 `token` + `tsa-dir` + `anchors`
    判据搬到端点；验签 `failed` 自动落 `anchor_mismatch` 审计 + dashboard 安全通知
    （复用既有 `_anchor_alarm`）。

    **本端点不主动联网**：不发起盖章，也不取远端锚——要取远端锚需显式传 `urls`
    （不传时 `fetch_and_verify_anchors` 收到空列表 = 休眠态）。

    请求体（JSON，全部可选）：
      - `tsr`：.tsr 令牌文件路径 → 只验这一个令牌（不传则遍历 tsa index.jsonl）
      - `urls`：远端锚 URL 列表 → 显式取回比对（不传 = 不联网）
      - `tsa_dir`：tsa 产物目录（缺省 env SYNC_HUB_TSA_DIR / audit/tsa）

    返回体字段来源：
      - `chain.local_anchor`：`audit_chain.verify_anchor`（本地锚 vs 链头）
      - `chain.tsa_stamped_head`：`audit_chain.verify_tsa`（被盖章链头是否仍在链中）
      - `anchors`：`audit_chain.fetch_and_verify_anchors`（远端锚取回比对）
      - `tokens`：`audit_chain.verify_tsa_token` 批量结果（显式 tsr 或 index.jsonl 遍历）
      - `trusted_fingerprints_configured`：`audit_chain._load_trusted_fingerprints()` 非空
        （false 时 tokens 多为 unverified = 未钉扎，fail-closed 不假绿）

    告警口径（unverified ≠ failed，不触发 anchor_mismatch）：
      1. 任一 token `status == "failed"` → `_anchor_alarm({"source": "http_verify", "tokens": [...]})`
      2. `anchors` 出现**真 mismatch**（成功取回但锚不在链中，status=='mismatch'）→ 告警；
         `db_unavailable` / `unverified` / `rejected` 不是篡改证据，**不落**
         `anchor_mismatch`（验收裁决 2026-09-24，见下方实现注释）
      3. `chain.tsa_stamped_head.valid is False` → `_anchor_alarm({"source": "http_verify", "mismatches": [...]})`
    """
    await require_ops_privilege(request, "POST /api/audit/anchor/verify", current_agent)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    tsr = str(body.get("tsr") or "").strip()
    urls = [str(u).strip() for u in (body.get("urls") or [])
            if isinstance(u, str) and u.strip()]
    tsa_dir = str(body.get("tsa_dir") or "").strip()
    from audit_chain import (verify_anchor, verify_tsa, fetch_and_verify_anchors,
                             _load_trusted_fingerprints)
    local, tsa = await asyncio.gather(
        asyncio.to_thread(verify_anchor, CONFIG.DB_PATH),
        asyncio.to_thread(verify_tsa, CONFIG.DB_PATH, tsa_dir),
    )
    # 联网策略：urls 显式传才取远端锚；空列表 = fetch_and_verify_anchors 休眠态
    anchors = await asyncio.to_thread(fetch_and_verify_anchors, CONFIG.DB_PATH, urls)
    tokens = await asyncio.to_thread(_collect_token_verifications, tsr, tsa_dir)
    pins_configured = bool(await asyncio.to_thread(_load_trusted_fingerprints))

    # (b) failed 自动落 anchor_mismatch + dashboard 通知（复用既有 _anchor_alarm；
    #     unverified 不算 failed、不告警，但如实出现在 tokens 里）
    failed_tokens = [t for t in tokens if t.get("status") == "failed"]
    if failed_tokens:
        await _anchor_alarm({"source": "http_verify",
                             "tokens": [{"tsr": t.get("tsr"),
                                         "status": t.get("status"),
                                         "reason": t.get("reason")}
                                        for t in failed_tokens[:5]]})
    # CD-106 验收裁决（2026-09-24，Hermes）：`valid is False` 有两个来源——真 mismatch
    # （锚成功取回但不在链中）与 db_unavailable（取不回、results 为空、带 error）。
    # 后者不是篡改证据，落 anchor_mismatch 会把「库打不开」误报成「链被整段重写/截断」
    # → 只在**真 mismatch 非空**时告警（unverified / rejected 同样不落）。
    # 返回体仍照原样带 anchors.error，可见性不丢。
    anchors_mismatches = _anchors_mismatches(anchors)
    if anchors_mismatches:
        await _anchor_alarm({"source": "http_verify",
                             "mismatches": anchors_mismatches})
    if tsa.get("valid") is False:
        await _anchor_alarm({"source": "http_verify",
                             "mismatches": (tsa.get("mismatches") or [])[:5]})

    await log_ops_trigger("POST /api/audit/anchor/verify", current_agent, {
        "tokens_checked": len(tokens),
        "tokens_failed": len(failed_tokens),
        "tokens_unverified": sum(1 for t in tokens if t.get("status") == "unverified"),
        "anchors_checked": anchors.get("checked", 0),
        "anchors_unverified": anchors.get("unverified", 0),
        "chain_local_valid": local.get("valid"),
        "chain_tsa_valid": tsa.get("valid"),
    })
    return {"status": "ok",
            "chain": {"local_anchor": local, "tsa_stamped_head": tsa},
            "anchors": anchors,
            "tokens": tokens,
            "trusted_fingerprints_configured": pins_configured}


async def anchor_export_loop():
    """XS-004 + CD-034 R3：审计锚定周期外发（本机快照 / webhook）+ 链头外部盖章。

    启动即跑一轮；interval<=0 跑完即退；整圈 try/except 兜底防 scheduler 炸；
    to_thread 防阻塞事件循环。TSA 盖章按 audit.tsa.interval 另行计频（默认 86400s）。"""
    from audit_chain import export_anchor, tsa_stamp, verify_tsa, DEFAULT_TSA_URL
    from models import CONFIG
    _last_tsa = 0.0
    while True:
        rotate_request_id("anchor-export")  # CD-108：每轮 tick 轮换 request id
        try:
            await asyncio.to_thread(export_anchor, CONFIG.DB_PATH)
        except Exception:
            pass  # 外发失败静默（export_anchor 内部已逐 url 容错，此处兜底）
        # CD-034 R3：链头外部时间戳（默认关；开启后按自身周期盖章 + 回拉比对 + 告警）
        try:
            if getattr(CONFIG, "AUDIT_TSA_ENABLED", False):
                tsa_interval = max(60, int(getattr(CONFIG, "AUDIT_TSA_INTERVAL", 86400) or 86400))
                now = time.time()
                if now - _last_tsa >= tsa_interval:
                    url = getattr(CONFIG, "AUDIT_TSA_URL", "") or DEFAULT_TSA_URL
                    r = await asyncio.to_thread(tsa_stamp, CONFIG.DB_PATH, url)
                    v = await asyncio.to_thread(verify_tsa, CONFIG.DB_PATH)
                    if not v.get("valid"):
                        await _anchor_alarm({"source": "tsa_loop",
                                             "mismatches": (v.get("mismatches") or [])[:5]})
                    # 空链是启动期瞬态（新库 audit_log 为 0 行）——此时不消耗计频，
                    # 否则第一次跑完就要等满一个周期才重试（实测踩过：24h 内再不盖章）。
                    if not ("主链为空" in str(r.get("error", ""))):
                        _last_tsa = now
        except Exception as e:
            logger.warning("tsa 盖章循环异常（不阻塞）: %s", e)
        interval = getattr(CONFIG, "AUDIT_ANCHOR_INTERVAL", 3600)
        if interval <= 0:
            return
        await asyncio.sleep(interval)
