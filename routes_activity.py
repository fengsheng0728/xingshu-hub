"""星枢 Sync Hub — 活动流统一读面（CD-085(a)，**只读**）。

本模块是**读面**：把 `events` / `gateway_read_log` / `disclosure_log` / `wiki_inbox`
四路活动数据归一到一条可过滤、可分页、按主体收口的活动流（`GET /api/v1/activity`），
回答「最近发生了什么」。此前没有任何统一入口——`GET /api/v1/stats` 是容量观测
（行数/体积/p50·p95），不是活动流。

**写侧记账口径统一（三类入口收敛 + OTel→Langfuse LLM 调用级 trace）是 CD-085 的
另一半，尚未做**——本模块不写任何表、不改任何写侧记账口径（不动 `_log_event`
调用点、不动三张专用表写入路径），纯读。
"""
import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from logfmt import mask_sensitive
from models import CONFIG
import routes_common
from routes_common import get_current_agent

logger = logging.getLogger("xingshu.routes_activity")
router = APIRouter()

_KNOWN_SOURCES = ("events", "read", "disclosure", "wiki")
_SUMMARY_MAX = 200
_QUERY_SUMMARY_MAX = 80
_QUERY_PAYLOAD_MAX = 200
_MIN_TS = datetime.min.replace(tzinfo=timezone.utc)


# ── 时间列口径（先读实际列名再写，见报告来源映射表） ──
# events.timestamp / disclosure_log.disclosed_at：ISO8601（datetime.now(timezone.utc).isoformat()）
# gateway_read_log.created_at / wiki_inbox.created_at：SQLite datetime('now')（UTC，YYYY-MM-DD HH:MM:SS）


def _parse_iso(value: str, field: str) -> datetime:
    """ISO8601 → aware UTC datetime；解析失败 → 400（不静默放宽过滤条件）。"""
    raw = (value or "").strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"Invalid ISO8601 for {field}: {raw}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_at(raw) -> str:
    """任意来源时间值 → ISO8601 输出（活动流 at 字段统一形态）。"""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        if "T" in text:
            dt = _parse_iso(text, "at")
        else:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except HTTPException:
        return text
    except ValueError:
        return text


def _sort_key(raw):
    """排序键：解析出的 UTC datetime；解析不了排到最后（倒序时贴底）。"""
    text = (raw or "").strip()
    if not text:
        return _MIN_TS
    try:
        if "T" in text:
            dt = _parse_iso(text, "at")
        else:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return _MIN_TS


def _clip(text, max_len: int = _SUMMARY_MAX) -> str:
    return (text or "")[:max_len]


def _norm_event_type(source: str, raw_type: str, extra: str = "") -> str:
    """归一规则（依据见报告来源映射表）：
    events.ops_trigger → "ops.trigger"（其余原样透出）；read → "read.<kind>"；
    disclosure → "disclosure.<disclosed_level>"（表内无 action 列，取披露级别为动作后缀）；
    wiki → "wiki.<status>"。
    """
    if source == "events":
        return "ops.trigger" if raw_type == "ops_trigger" else (raw_type or "")
    prefix = {"read": "read", "disclosure": "disclosure", "wiki": "wiki"}.get(source, source)
    return f"{prefix}.{extra}" if extra else prefix


# ── 各来源同步查询（由 asyncio.to_thread 调用；SQL 全参数化，CD-060 纪律） ──

def _fetch_events(db_path: str, subject: str, since, until, event_type: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_id, event_type, agent_id, payload, timestamp FROM events"
            " WHERE agent_id = ?", (subject,)).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        raw_type = r["event_type"] or ""
        ntype = _norm_event_type("events", raw_type)
        if event_type and not ntype.startswith(event_type):
            continue
        ts = r["timestamp"] or ""
        sort_ts = _sort_key(ts)
        if since and sort_ts < since:
            continue
        if until and sort_ts > until:
            continue
        try:
            payload = json.loads(r["payload"] or "{}")
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except Exception:
            payload = {"raw": _clip(str(r["payload"] or ""), _QUERY_PAYLOAD_MAX)}
        if raw_type == "ops_trigger":
            summary = f"{payload.get('endpoint', '')} by {payload.get('requester', '')}"
        else:
            summary = f"{ntype} by {r['agent_id']}" if r["agent_id"] else ntype
        items.append({
            "at": _iso_at(ts),
            "source": "events",
            "event_type": ntype,
            "agent_id": r["agent_id"] or "",
            "summary": _clip(summary),
            "payload": mask_sensitive(payload),
            "_sort_ts": sort_ts,
        })
    return items


def _fetch_read(db_path: str, subject: str, since, until, event_type: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT log_id, requester, auth_mode, kind, query, target, granted_level,"
            " item_count, stripped_chunks, created_at FROM gateway_read_log"
            " WHERE requester = ?", (subject,)).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        kind = r["kind"] or ""
        ntype = _norm_event_type("read", "read", kind)
        if event_type and not ntype.startswith(event_type):
            continue
        ts = r["created_at"] or ""
        sort_ts = _sort_key(ts)
        if since and sort_ts < since:
            continue
        if until and sort_ts > until:
            continue
        query = r["query"] or ""
        summary = (f"{kind} {_clip(query, _QUERY_SUMMARY_MAX)} → "
                   f"{r['item_count'] or 0} 条（{r['granted_level'] or ''}）")
        items.append({
            "at": _iso_at(ts),
            "source": "read",
            "event_type": ntype,
            "agent_id": r["requester"] or "",
            "summary": _clip(summary),
            "payload": mask_sensitive({
                "kind": kind,
                "auth_mode": r["auth_mode"] or "",
                "query": _clip(query, _QUERY_PAYLOAD_MAX),
                "target": _clip(r["target"] or "", _QUERY_PAYLOAD_MAX),
                "granted_level": r["granted_level"] or "",
                "item_count": r["item_count"] or 0,
                "stripped_chunks": r["stripped_chunks"] or 0,
            }),
            "_sort_ts": sort_ts,
        })
    return items


def _fetch_disclosure(db_path: str, subject: str, since, until, event_type: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT log_id, task_id, from_agent_id, to_agent_id, memory_id,"
            " disclosed_level, disclosed_at, reason, trace_id FROM disclosure_log"
            " WHERE from_agent_id = ?", (subject,)).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        level = r["disclosed_level"] or ""
        ntype = _norm_event_type("disclosure", "disclosure", level)
        if event_type and not ntype.startswith(event_type):
            continue
        ts = r["disclosed_at"] or ""
        sort_ts = _sort_key(ts)
        if since and sort_ts < since:
            continue
        if until and sort_ts > until:
            continue
        summary = (f"{r['from_agent_id'] or ''} → {r['to_agent_id'] or ''} "
                   f"{r['memory_id'] or ''}（{level}）")
        items.append({
            "at": _iso_at(ts),
            "source": "disclosure",
            "event_type": ntype,
            "agent_id": r["from_agent_id"] or "",
            "summary": _clip(summary),
            "payload": mask_sensitive({
                "task_id": r["task_id"] or "",
                "from_agent_id": r["from_agent_id"] or "",
                "to_agent_id": r["to_agent_id"] or "",
                "memory_id": r["memory_id"] or "",
                "disclosed_level": level,
                "reason": r["reason"] or "",
                "trace_id": r["trace_id"] or "",
            }),
            "_sort_ts": sort_ts,
        })
    return items


def _fetch_wiki(db_path: str, subject: str, since, until, event_type: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, page_path, title, status, source, created_at, reviewed_at,"
            " reviewed_by FROM wiki_inbox WHERE reviewed_by = ?", (subject,)).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        status = r["status"] or ""
        ntype = _norm_event_type("wiki", "wiki", status)
        if event_type and not ntype.startswith(event_type):
            continue
        ts = r["created_at"] or ""
        sort_ts = _sort_key(ts)
        if since and sort_ts < since:
            continue
        if until and sort_ts > until:
            continue
        items.append({
            "at": _iso_at(ts),
            "source": "wiki",
            "event_type": ntype,
            "agent_id": r["reviewed_by"] or "",
            "summary": _clip(r["page_path"] or ""),
            "payload": mask_sensitive({
                "page_path": r["page_path"] or "",
                "title": r["title"] or "",
                "status": status,
                "source": r["source"] or "",
                "reviewed_at": r["reviewed_at"] or "",
                "reviewed_by": r["reviewed_by"] or "",
            }),
            "_sort_ts": sort_ts,
        })
    return items


_FETCHERS = {
    "events": _fetch_events,
    "read": _fetch_read,
    "disclosure": _fetch_disclosure,
    "wiki": _fetch_wiki,
}


def _collect_sync(db_path, sources, subject, since, until, event_type):
    """分来源查询 + 异常隔离（表缺失/查询异常 → 该来源空 + 记不可用，不整体 500）。"""
    items, unavailable = [], []
    for src in sources:
        try:
            items.extend(_FETCHERS[src](db_path, subject, since, until, event_type))
        except Exception as exc:
            unavailable.append(src)
            logger.warning("activity 源不可用 source=%s err=%s", src, type(exc).__name__)
    items.sort(key=lambda it: it["_sort_ts"], reverse=True)
    for it in items:
        it.pop("_sort_ts", None)
    return items, unavailable


def _resolve_subject(request: Request, current_agent: str, agent_id: str) -> str:
    """权限口径（定死）：默认本人；带他人 agent_id 仅 hub_token 放行，其余 403。
    NO_AUTH 开发态不拦（与同仓既有门一致）。"""
    if agent_id and agent_id != current_agent:
        if not routes_common.NO_AUTH:
            principal = request.scope.get("principal") if request is not None else None
            if routes_common._principal_auth_mode(principal) != "hub_token":
                raise HTTPException(
                    status_code=403,
                    detail="Forbidden: 不能查看其他主体的活动流"
                           "（仅 hub_token 控制台可跨主体查看；manager 看下属走披露审批流）")
        return agent_id
    return agent_id or current_agent


@router.get("/api/v1/activity")
async def api_activity_feed(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    source: str = "",
    event_type: str = "",
    agent_id: str = "",
    since: str = "",
    until: str = "",
    current_agent: str = Depends(get_current_agent),
):
    """活动流统一读面（CD-085(a)，只读）：events / read / disclosure / wiki 归一归并。

    - 默认只查 `events`（含 ops_trigger → ops.trigger 归一）；`source` 逗号分隔可多选。
    - `limit` 默认 50、上限 200（超限钳到 200 不报错）；`offset` 默认 0。
    - `event_type` 按归一后事件类型**前缀**匹配；`since`/`until` 为 ISO8601。
    - 权限：无 agent_id 只回请求者本人；带他人 agent_id 仅 hub_token（控制台）放行。
    - `payload` 一律过 `logfmt.mask_sensitive` 后返回。

    **本文档是读面**：写侧记账口径统一（三类入口收敛）是 CD-085 的另一半，未做。
    """
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    subject = _resolve_subject(request, current_agent, agent_id)
    since_dt = _parse_iso(since, "since")
    until_dt = _parse_iso(until, "until")
    event_prefix = (event_type or "").strip()

    raw_sources = [s.strip() for s in (source or "").split(",") if s.strip()]
    if raw_sources:
        sources = [s for s in dict.fromkeys(raw_sources) if s in _KNOWN_SOURCES]
    else:
        sources = ["events"]

    items, unavailable = await asyncio.to_thread(
        _collect_sync, CONFIG.DB_PATH, sources, subject, since_dt, until_dt, event_prefix)
    total = len(items)
    page = items[offset:offset + limit]
    return {
        "status": "ok",
        "items": page,
        "total": total,
        "returned": len(page),
        "has_more": offset + len(page) < total,
        "sources": sources,
        "filters": {
            "sources": sources,
            "event_type": event_prefix,
            "agent_id": subject,
            "since": since or "",
            "until": until or "",
            "limit": limit,
            "offset": offset,
            "unavailable_sources": unavailable,
        },
    }
