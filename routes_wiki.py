"""星枢 Sync Hub — Wiki API + 收件箱审查 API（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import logging
logger = logging.getLogger("xingshu.routes_wiki")

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Request

import routes_common
from routes_common import (get_current_agent, get_current_principal,
                             principal_is_privileged)

router = APIRouter()

_wiki_sync_state: dict = {
    "running": False, "started_at": None, "last_finished_at": None,
    "last_result": None, "last_error": None,
}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _wiki_sync_bg(federate: bool) -> None:
    """CD-042 后台全量同步：单飞 + 结果/错误落状态；失败不抛给事件循环。"""
    try:
        from wiki_sync import sync
        res = await asyncio.to_thread(sync, False, False, federate)
        _wiki_sync_state["last_result"] = res
    except Exception as e:  # noqa: BLE001 — 后台任务异常必须落状态否则静默消失
        _wiki_sync_state["last_error"] = str(e)[:200]
        logger.warning("wiki sync (background) failed: %s", e)
    finally:
        _wiki_sync_state["running"] = False
        _wiki_sync_state["last_finished_at"] = _now_iso()



# ============ Wiki API ============

@router.get("/api/v1/wiki/pages")
async def api_wiki_pages(current_agent: str = Depends(get_current_agent)):
    """列出所有 Wiki 页面"""
    from wiki_engine import list_pages
    return {"status": "ok", "pages": list_pages()}


@router.get("/api/v1/wiki/export")
async def api_wiki_export(current_agent: str = Depends(get_current_agent)):
    """导出所有 Wiki 页面为 JSON（联邦同步用）"""
    from wiki_engine import WIKI_ROOT
    import os as _os
    pages = {}
    for subdir in ["entities", "concepts", "comparisons", "queries"]:
        d = _os.path.join(WIKI_ROOT, subdir)
        if not _os.path.isdir(d):
            continue
        for fname in sorted(_os.listdir(d)):
            if fname.endswith(".md") and not fname.startswith("_"):
                path = _os.path.join(d, fname)
                with open(path, encoding="utf-8") as f:
                    pages[f"{subdir}/{fname}"] = f.read()
    return {"status": "ok", "count": len(pages), "pages": pages}


@router.post("/api/v1/wiki/import")
async def api_wiki_import(
    request: Request,
    current_agent: str = Depends(get_current_agent),
):
    """从远程 Hub 导入 Wiki 页面（联邦同步）"""
    from wiki_engine import WIKI_ROOT, validate_path
    import os as _os
    data = await request.json()
    pages = data.get("pages", {})
    imported = 0
    errors = []
    for rel_path, content in pages.items():
        try:
            abs_path = validate_path(rel_path)
            _os.makedirs(_os.path.dirname(abs_path), exist_ok=True)
            if _os.path.exists(abs_path):
                if open(abs_path, encoding="utf-8").read() == content:
                    continue
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            imported += 1
        except ValueError as e:
            errors.append(f"{rel_path}: {e}")
    from wiki_engine import ensure_wiki
    ensure_wiki()
    from wiki_sync import _update_index
    _update_index(dry_run=False)
    return {"status": "ok", "imported": imported, "errors": errors}


@router.get("/api/v1/wiki/page/{page_path:path}")
async def api_wiki_page(
    page_path: str,
    format: str = "md",
    current_agent: str = Depends(get_current_agent),
):
    """获取单个 Wiki 页面

    Args:
        page_path: 页面路径，如 entities/产品a.md
        format: 返回格式，md=markdown原文, html=渲染后的HTML
    """
    from wiki_engine import WIKI_ROOT, validate_path, md_to_html
    try:
        abs_path = validate_path(page_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"页面不存在: {page_path}")

    with open(abs_path, encoding="utf-8") as f:
        content = f.read()

    if format == "html":
        content = md_to_html(content)

    # 提取 frontmatter
    meta = {}
    body = content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            fm_raw = content[3:end]
            body = content[end+3:].strip()
            for line in fm_raw.strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()

    return {
        "status": "ok",
        "path": page_path,
        "meta": meta,
        "content": content,
        "format": format,
    }


@router.get("/api/v1/wiki/search/hybrid")
async def api_wiki_search_hybrid(
    q: str,
    top_k: int = 10,
    keyword_weight: float = 0.5,
    current_agent: str = Depends(get_current_agent),
):
    """混合搜索：关键词 + 向量相似度"""
    from wiki_engine import search_hybrid
    results = search_hybrid(q, top_k, keyword_weight)
    return {"status": "ok", "q": q, "method": "hybrid", "count": len(results), "results": results}


@router.get("/api/v1/wiki/search")
async def api_wiki_search(
    q: str,
    field: str = "content",
    current_agent: str = Depends(get_current_agent),
):
    """搜索 Wiki 页面

    Args:
        q: 搜索关键词
        field: 搜索字段，content=全文搜索, title=标题搜索, tags=标签搜索
    """
    from wiki_engine import WIKI_ROOT, list_pages, validate_path
    import re

    if not q:
        return {"status": "ok", "results": []}

    results = []
    for page in list_pages():
        path = os.path.join(WIKI_ROOT, page["path"])
        if not os.path.isfile(path):
            continue

        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        match = False
        snippet = ""

        if field == "title":
            if q.lower() in page["title"].lower():
                match = True
                # 取第一段作为摘要
                body = content
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        body = content[end+3:].strip()
                snippet = body[:200]

        elif field == "tags":
            if q.lower() in page.get("tags", "").lower():
                match = True
                snippet = page.get("tags", "")

        else:  # content
            body = content
            if q.lower() in body.lower():
                match = True
                # 查找关键词上下文
                idx = body.lower().find(q.lower())
                start = max(0, idx - 40)
                end = min(len(body), idx + len(q) + 80)
                snippet = body[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(body):
                    snippet = snippet + "..."

        if match:
            results.append({
                "path": page["path"],
                "title": page["title"],
                "type": page["type"],
                "tags": page.get("tags", ""),
                "snippet": snippet,
            })

    return {"status": "ok", "q": q, "field": field, "count": len(results), "results": results}


@router.get("/api/v1/wiki/graph")
async def api_wiki_graph(current_agent: str = Depends(get_current_agent)):
    """获取 Wiki 知识图谱数据"""
    from wiki_engine import get_graph
    graph = get_graph()
    return {"status": "ok", **graph}


@router.get("/api/v1/wiki/sync")
async def api_wiki_sync(
    federate: bool = False,
    background: bool = False,
    current_agent: str = Depends(get_current_agent),
):
    """触发 DB → Wiki 同步。federate=true 时同时从已配对 Hub 拉取。

    CD-042（2026-09-14）：全量同步在 wiki 页数多时单次要 20s+（实测 6429 页 23-24s），
    旧实现直接同步调用 → 调用方必然超时。现：
      · 默认路径改为 `asyncio.to_thread`（不再占事件循环，行为与返回体不变）；
      · `background=1` 走后台任务（单飞，重复触发返回 already_running）+ 进度查
        `/api/v1/wiki/sync/status`，控制台按钮用这条路径。
    """
    if background:
        state = _wiki_sync_state
        if state["running"]:
            return {"status": "ok", "started": False, "already_running": True}
        state.update({"running": True, "started_at": _now_iso(), "last_error": None})
        asyncio.create_task(_wiki_sync_bg(federate))
        return {"status": "ok", "started": True, "already_running": False}
    from wiki_sync import sync
    result = await asyncio.to_thread(sync, False, False, federate)
    return {"status": "ok", **result}


@router.get("/api/v1/wiki/sync/status")
async def api_wiki_sync_status(current_agent: str = Depends(get_current_agent)):
    """CD-042：后台同步进度/最近结果（供控制台轮询）。"""
    return {"status": "ok", **_wiki_sync_state}



# ============ Wiki 收件箱审查 API ============

@router.get("/api/v1/wiki/inbox")
async def api_wiki_inbox(
    current_agent: str = Depends(get_current_agent),
):
    """获取待审核的 Wiki 页面列表"""
    import sqlite3
    from db import CONFIG
    conn = sqlite3.connect(CONFIG.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, page_path, title, status, source, created_at FROM wiki_inbox WHERE status='pending' ORDER BY created_at DESC")
    rows = [{"id": r[0], "page_path": r[1], "title": r[2], "status": r[3], "source": r[4], "created_at": r[5]} for r in c.fetchall()]
    conn.close()
    return {"inbox": rows}




def _wiki_reviewer_gate(principal, action: str) -> None:
    """CD-031（2026-09-14）：Wiki 收件箱审批人角色门。

    与同仓 n1 通道（manager/orchestrator）对齐；hub_token（控制台 wiki.html / hub_ui）
    视为特权，故控制台流程不受影响。原实现只有认证、零角色门——任意 worker 可把内容
    trust_level 由 external 升 internal（S3）。NO_AUTH 开发态仍全放行。
    """
    if routes_common.NO_AUTH:      # 运行期解析（测试 patch routes_common.NO_AUTH 生效）
        return None
    if principal_is_privileged(principal):
        return None
    raise HTTPException(status_code=403, detail="仅主管/店长可%s Wiki 收件箱" % action)


@router.post("/api/v1/wiki/inbox/{inbox_id}/approve")
async def api_wiki_approve(
    inbox_id: int,
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """批准 Wiki 页面（从 inbox 发布：标记 + 重新同步生成页面文件）"""
    _wiki_reviewer_gate(principal, "审批")   # CD-031
    import sqlite3
    from db import CONFIG
    conn = sqlite3.connect(CONFIG.DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE wiki_inbox SET status='approved', reviewed_at=datetime('now'), reviewed_by=?, trust_level='internal' WHERE id=? AND status='pending'", (current_agent, inbox_id))  # S3: 审查通过 → external 升 internal
    updated = c.rowcount
    conn.commit()
    conn.close()
    # 发布动作：重新同步生成页面（源数据在则重建文件，保证发布后 pages 可查）
    if updated:
        try:
            from wiki_sync import sync
            sync(force=True)
        except Exception as _exc:
            logger.debug("routes_wiki silent-except @261: %s", _exc)
    return {"ok": True, "approved": updated > 0}


@router.post("/api/v1/wiki/inbox/{inbox_id}/reject")
async def api_wiki_reject(
    inbox_id: int,
    current_agent: str = Depends(get_current_agent),
    principal=Depends(get_current_principal),
):
    """拒绝 Wiki 页面（从 inbox 移除）"""
    _wiki_reviewer_gate(principal, "驳回")   # CD-031
    import sqlite3
    from db import CONFIG
    conn = sqlite3.connect(CONFIG.DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE wiki_inbox SET status='rejected', reviewed_at=datetime('now'), reviewed_by=? WHERE id=? AND status='pending'", (current_agent, inbox_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return {"ok": True, "rejected": updated > 0}
