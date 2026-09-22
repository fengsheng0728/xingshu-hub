"""
星枢 Wiki MCP Server — SSE 模式，集成到 Hub FastAPI
提供 5 个 MCP 工具：wiki_search, wiki_get, wiki_list, wiki_graph, wiki_sync
"""
import os
import json
import logging
from mcp.server.fastmcp import FastMCP
from wiki_engine import WIKI_ROOT, list_pages, get_graph, validate_path, md_to_html

logger = logging.getLogger("xingshu.mcp_server")

mcp = FastMCP("xingshu-wiki")


def _mcp_caller():
    """CD-058（T20）：读 contextvar 主体并分级，返回 (principal, level, requester)。

    - 有主体且 principal_is_privileged（hub_token / manager / orchestrator）
      → level="full"；审计 requester 记真实主体 id（subject_id，缺省 "hub-token"）
    - 有主体非特权 → level="summary"（审计仍记真实主体）
    - 无主体（contextvar 未注入）→ (None, "summary", "mcp-tool")：CD-054
      fail-closed 现状逐字保留
    分级链路任何异常 → logger.warning（不含凭据）后按无主体处理（fail-closed）。
    """
    from routes_gateway import get_mcp_principal
    from routes_common import principal_is_privileged

    try:
        principal = get_mcp_principal()
        if principal is None:
            return None, "summary", "mcp-tool"
        level = "full" if principal_is_privileged(principal) else "summary"
        requester = getattr(principal, "subject_id", "") or "hub-token"
        return principal, level, requester
    except Exception as exc:
        logger.warning("mcp 主体分级失败，按无主体 fail-closed err=%s",
                       type(exc).__name__)
        return None, "summary", "mcp-tool"


@mcp.tool()
def wiki_search(query: str, field: str = "hybrid", top_k: int = 10) -> dict:
    """搜索 Wiki 页面（默认混合搜索，摘要级）

    CD-054（用户拍「甲」）：MCP 工具层无 requester/principal 传递通路 →
    无主体 fail-closed 到已发布摘要级；snippet 本已是 ≤200 字量级的摘要形态，
    内容形态不变，返回体标注 level="summary" 并落读审计（anonymous-tool）。
    CD-058（T20）：主体经 contextvar 注入后，level 按主体标注（特权=full，
    非特权/无主体=summary）；snippet 结果集口径不变（不放大成全文），
    有主体时审计记真实 requester/auth_mode。

    Args:
        query: 搜索关键词
        field: 搜索模式 (hybrid/content/title/tags)
        top_k: 返回最大条数 (hybrid 模式)
    """
    from routes_gateway import _log_read

    principal, level, requester = _mcp_caller()

    if field == "hybrid":
        from wiki_engine import search_hybrid
        results = search_hybrid(query, top_k)
        # 读审计（D4：审计失败不阻塞返回，_log_read 内部已处理）
        if principal is None:
            _log_read("mcp-tool", None, "wiki", query, "", "summary", len(results), 0,
                      auth_mode_override="anonymous-tool")
        else:
            _log_read(requester, principal, "wiki", query, "", level, len(results), 0)
        return {"status": "ok", "q": query, "method": "hybrid", "count": len(results),
                "results": results, "level": level}

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
                # CD-063（T28）：tags 档 snippet 按摘要级截断（前 200 字 + 省略号），
                # 与其余档形态一致；level 标记沿用 T20 按主体分级（返回体统一挂，
                # 只加键不删键，不新造取值）
                tags = page.get("tags", "")
                snippet = tags[:200] + "..." if len(tags) > 200 else tags
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
    # 读审计（D4：审计失败不阻塞返回，_log_read 内部已处理）
    if principal is None:
        _log_read("mcp-tool", None, "wiki", query, "", "summary", len(results), 0,
                  auth_mode_override="anonymous-tool")
    else:
        _log_read(requester, principal, "wiki", query, "", level, len(results), 0)
    return {"status": "ok", "q": query, "field": field, "count": len(results),
            "results": results, "level": level}


@mcp.tool()
def wiki_get(page_path: str, format: str = "md") -> dict:
    """获取单个 Wiki 页面内容（按主体分级：特权全文 / 其余摘要级）

    CD-054（用户拍「甲」）：MCP 工具层无 requester/principal 传递通路，
    分辨不出调用者身份 → 无主体一律 fail-closed 到已发布摘要级：
    保留 frontmatter（YAML 元数据不是正文），正文只给前 200 字。
    CD-058（T20，MCP 主体身份注入 contextvars）：有主体且
    principal_is_privileged（hub_token / manager / orchestrator）→ 返回全文
    （level="full"、truncated=False、frontmatter 仍保留）；非特权与无主体
    维持摘要级（无主体路径行为逐字不变）。

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
        raw = f.read()

    # 拆 frontmatter / 正文：frontmatter 保留，正文按主体分级（特权全文 / 摘要前 200 字）
    frontmatter = ""
    body = raw
    meta = {}
    if raw.startswith("---"):
        end = raw.find("---", 3)
        if end > 0:
            frontmatter = raw[: end + 3]
            body = raw[end + 3 :].strip()
            for line in raw[3:end].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()

    principal, level, requester = _mcp_caller()

    if level == "full":
        truncated = False
        content = (frontmatter + "\n" + body) if frontmatter else body
    else:
        truncated = len(body) > 200
        content = (frontmatter + "\n" + body[:200]) if frontmatter else body[:200]

    if format == "html":
        content = md_to_html(content)

    # 读审计（无主体记 anonymous-tool；D4：审计失败不阻塞返回，_log_read 内部已处理）
    from routes_gateway import _log_read
    if principal is None:
        _log_read("mcp-tool", None, "wiki", "", page_path, "summary", 1,
                  1 if truncated else 0, auth_mode_override="anonymous-tool")
    else:
        _log_read(requester, principal, "wiki", "", page_path, level, 1,
                  1 if truncated else 0)

    return {
        "status": "ok",
        "path": page_path,
        "meta": meta,
        "content": content,
        "level": level,
        "truncated": truncated,
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
