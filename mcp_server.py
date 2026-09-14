"""
星枢 Wiki MCP Server — SSE 模式，集成到 Hub FastAPI
提供 5 个 MCP 工具：wiki_search, wiki_get, wiki_list, wiki_graph, wiki_sync
"""
import os
import json
from mcp.server.fastmcp import FastMCP
from wiki_engine import WIKI_ROOT, list_pages, get_graph, validate_path, md_to_html

mcp = FastMCP("xingshu-wiki")


@mcp.tool()
def wiki_search(query: str, field: str = "hybrid", top_k: int = 10) -> dict:
    """搜索 Wiki 页面（默认混合搜索）
    
    Args:
        query: 搜索关键词
        field: 搜索模式 (hybrid/content/title/tags)
        top_k: 返回最大条数 (hybrid 模式)
    """
    if field == "hybrid":
        from wiki_engine import search_hybrid
        results = search_hybrid(query, top_k)
        return {"status": "ok", "q": query, "method": "hybrid", "count": len(results), "results": results}

    pages = list_pages()
    results = []
    for page in pages:
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
        body = content
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                body = content[end + 3 :].strip()

        if field == "title":
            if query.lower() in page["title"].lower():
                match = True
                snippet = body[:200]
        elif field == "tags":
            if query.lower() in page.get("tags", "").lower():
                match = True
                snippet = page.get("tags", "")
        else:
            if query.lower() in body.lower():
                match = True
                idx = body.lower().find(query.lower())
                start = max(0, idx - 40)
                end = min(len(body), idx + len(query) + 80)
                snippet = body[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(body):
                    snippet += "..."

        if match:
            results.append(
                {
                    "path": page["path"],
                    "title": page["title"],
                    "type": page["type"],
                    "tags": page.get("tags", ""),
                    "snippet": snippet,
                }
            )
    return {"status": "ok", "q": query, "field": field, "count": len(results), "results": results}


@mcp.tool()
def wiki_get(page_path: str, format: str = "md") -> dict:
    """获取单个 Wiki 页面内容
    
    Args:
        page_path: 页面路径 (如 entities/产品a.md)
        format: 返回格式 (md/html)
    """
    try:
        abs_path = validate_path(page_path)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if not os.path.isfile(abs_path):
        return {"status": "error", "error": f"页面不存在: {page_path}"}

    with open(abs_path, encoding="utf-8") as f:
        content = f.read()

    if format == "html":
        content = md_to_html(content)

    meta = {}
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            for line in content[3:end].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()

    return {
        "status": "ok",
        "path": page_path,
        "meta": meta,
        "content": content,
    }


@mcp.tool()
def wiki_list() -> dict:
    """列出所有 Wiki 页面"""
    pages = list_pages()
    by_type = {}
    for p in pages:
        t = p["type"]
        by_type.setdefault(t, []).append(
            {"title": p["title"], "path": p["path"], "tags": p.get("tags", "")}
        )
    return {"status": "ok", "total": len(pages), "by_type": by_type}


@mcp.tool()
def wiki_graph() -> dict:
    """获取 Wiki 知识图谱 (nodes + links)"""
    return get_graph()


@mcp.tool()
def wiki_sync() -> dict:
    """触发 DB → Wiki 同步"""
    from wiki_sync import sync

    result = sync()
    return result


@mcp.tool()
def buffer_stats() -> dict:
    """写入缓冲实时统计：队列深度、落库延迟、同步状态"""
    from hub_core import hub
    return hub.buffer_stats()


@mcp.tool()
def buffer_trace(limit: int = 20) -> dict:
    """最近写入跟踪：谁写了什么 → 何时入队 → 何时落库 → 何时同步
    
    Args:
        limit: 返回条数 (默认 20)
    """
    from hub_core import hub
    traces = hub.recent_traces(limit)
    return {
        "count": len(traces),
        "buffer_stats": hub.buffer_stats(),
        "traces": traces,
    }


def create_sse_app():
    """返回 FastAPI SSE 应用 (挂载到 Hub)"""
    return mcp.sse_app()
