"""星枢 Sync Hub — 控制台/工作台数据 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import os

from fastapi import APIRouter, Depends

from hub_core import hub
from routes_common import get_current_agent, get_current_agent_optional
import routes_wiki  # CD-043: /api/v1/stats 复用同步状态

router = APIRouter()

def _dir_bytes(path: str) -> int:
    """目录总字节数（os.scandir 轻量实现）。"""
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
            elif entry.is_dir(follow_symlinks=False):
                total += _dir_bytes(entry.path)
    except OSError:
        return total
    return total



@router.get("/api/v1/dashboard")
async def api_dashboard(current_agent: str = Depends(get_current_agent_optional)):
    """监控数据接口，按请求者角色过滤可见范围（可选认证）"""
    return await hub.get_dashboard_data(current_agent)


@router.get("/api/v1/agent/workspace")
async def api_agent_workspace(current_agent: str = Depends(get_current_agent)):
    """Agent 端工作台：返回当前 Agent 的任务、通知、团队摘要"""
    return await hub.get_agent_workspace(current_agent)


@router.get("/api/v1/stats")
async def api_stats(current_agent: str = Depends(get_current_agent)):
    """容量与延迟观测（CD-043）。

    把这轮「膨胀只能靠人工翻库才发现」的教训固化成可看数字：各表行数、DB/WAL 体积、
    wiki 页数、chroma 向量数、inbox 待审、检索次数/降级次数/p50·p95、最近一次 wiki 同步。
    只读 + 轻量（COUNT/stat），可被控制台或巡检脚本轮询。
    """
    import os
    import sqlite3

    from db import CONFIG
    from wiki_engine import WIKI_ROOT

    def _collect():
        out = {"db": {}, "rows": {}, "wiki": {}, "search": {}, "sync": {}}
        try:
            out["db"]["size_mb"] = round(os.path.getsize(CONFIG.DB_PATH) / 1048576.0, 2)
        except OSError:
            out["db"]["size_mb"] = None
        for suffix, key in (("-wal", "wal_mb"), ("-shm", "shm_mb")):
            try:
                out["db"][key] = round(os.path.getsize(CONFIG.DB_PATH + suffix) / 1048576.0, 2)
            except OSError:
                out["db"][key] = 0.0
        conn = sqlite3.connect(CONFIG.DB_PATH)
        try:
            for tbl in ("knowledge_base", "memory_pool", "document_chunks", "wiki_inbox",
                        "agents", "agent_quotas", "events", "buffer_log"):
                try:
                    out["rows"][tbl] = conn.execute("SELECT COUNT(*) FROM %s" % tbl).fetchone()[0]
                except sqlite3.Error:
                    out["rows"][tbl] = None
            try:
                out["rows"]["wiki_inbox_pending"] = conn.execute(
                    "SELECT COUNT(*) FROM wiki_inbox WHERE status='pending'").fetchone()[0]
            except sqlite3.Error:
                out["rows"]["wiki_inbox_pending"] = None
        finally:
            conn.close()
        pages = 0
        for sub in ("entities", "concepts", "comparisons", "queries"):
            d_ = os.path.join(WIKI_ROOT, sub)
            if os.path.isdir(d_):
                pages += len([f for f in os.listdir(d_) if f.endswith(".md")])
        out["wiki"] = {"pages": pages, "dir_bytes": _dir_bytes(WIKI_ROOT)}
        try:
            coll = getattr(hub, "_chroma_collection", None)
            out["wiki"]["chroma_vectors"] = int(coll.count()) if coll is not None else 0
        except Exception:  # noqa: BLE001 — 向量库不可用时只报 0，不影响其余指标
            out["wiki"]["chroma_vectors"] = None
        from disclosure import _SEARCH_STATS
        smp = sorted(_SEARCH_STATS["samples"])
        out["search"] = {
            "count": _SEARCH_STATS["count"],
            "degraded": _SEARCH_STATS["degraded"],
            "last_ms": _SEARCH_STATS["last_ms"],
            "p50_ms": round(smp[len(smp) // 2], 2) if smp else None,
            "p95_ms": round(smp[min(len(smp) - 1, int(len(smp) * 0.95))], 2) if smp else None,
            "samples": len(smp),
        }
        out["sync"] = dict(routes_wiki._wiki_sync_state)
        # CD-046: outbox 观测段（pending/done/failed/last_error/by_type）。
        # 观测面不许让 stats 500：无消费者实例给 None，异常兜底为 error 段
        try:
            consumer = getattr(hub, "_outbox_consumer", None)
            out["outbox"] = consumer.stats_snapshot() if consumer is not None else None
        except Exception as exc:  # noqa: BLE001 — 观测段失败不影响其余指标
            out["outbox"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    import asyncio as _aio
    data = await _aio.to_thread(_collect)
    return {"status": "ok", **data}

