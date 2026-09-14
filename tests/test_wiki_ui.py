"""P1: Wiki 面板 EXPECTED_ROUTES 反向断言 — wiki 端点组 + /wiki 页面路由"""
import re
import pytest
from routes import app


EXPECTED_WIKI_ENDPOINTS = [
    ("GET", "/api/v1/wiki/pages"),
    ("GET", "/api/v1/wiki/export"),
    ("POST", "/api/v1/wiki/import"),
    ("GET", "/api/v1/wiki/page/{page_path:path}"),
    ("GET", "/api/v1/wiki/search/hybrid"),
    ("GET", "/api/v1/wiki/search"),
    ("GET", "/api/v1/wiki/graph"),
    ("GET", "/api/v1/wiki/sync"),
    ("GET", "/api/v1/wiki/inbox"),
    ("POST", "/api/v1/wiki/inbox/{inbox_id}/approve"),
    ("POST", "/api/v1/wiki/inbox/{inbox_id}/reject"),
]

EXPECTED_PAGES = ["/wiki"]


def _registered_routes():
    """收集 FastAPI app 全部注册路由"""
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods:
            if m in ("GET", "POST", "PUT", "DELETE", "WEBSOCKET"):
                out.add((m, r.path))
    return out


def test_wiki_endpoints_all_registered():
    """wiki 端点组全部注册（防该有的路由没注册）"""
    registered = _registered_routes()
    missing = [f"{m} {p}" for m, p in EXPECTED_WIKI_ENDPOINTS if (m, p) not in registered]
    assert not missing, f"以下 wiki 端点未注册: {missing}"


def test_wiki_page_route_registered():
    """/wiki dashboard 页面路由已注册"""
    registered = _registered_routes()
    missing = [p for p in EXPECTED_PAGES if not any(r[1] == p for r in registered)]
    assert not missing, f"以下页面路由未注册: {missing}"


def test_wiki_html_exists_and_has_approve_reject():
    """wiki.html 文件存在且含审查按钮"""
    import os
    p = os.path.join(os.path.dirname(__file__), "..", "dashboard", "wiki.html")
    assert os.path.exists(p), f"{p} 不存在"
    with open(p, encoding="utf-8") as f:
        html = f.read()
    assert "approve" in html and "reject" in html, "缺少 approve/reject 审查按钮"
    assert "localStorage" in html, "缺少 API Key 存储机制"


def test_wiki_html_xss_safe():
    """XSS 防护：动态内容必须经 esc() 转义，禁止直接 innerHTML 拼接外部内容"""
    import os, re
    p = os.path.join(os.path.dirname(__file__), "..", "dashboard", "wiki.html")
    with open(p, encoding="utf-8") as f:
        html = f.read()
    # esc() 转义函数存在
    assert "function esc" in html, "缺少 esc() 转义函数"
    # 所有 innerHTML 赋值：含数据拼接（+ 或模板串或 .map 链）必须经 esc()；纯静态字面量放行
    for m in re.finditer(r"\.innerHTML\s*=\s*([^;]+);", html):
        expr = m.group(1)
        has_data = ("+" in expr) or ("${" in expr) or (".map(" in expr) or (
            "'" in expr and re.search(r"\b(it|p|d|r|list|content|path|title|e)\.", expr)
        )
        if has_data and "esc(" not in expr:
            raise AssertionError(f"innerHTML 拼接未转义: {expr.strip()[:80]}")
    # marked 本地化使用必须伴随 DOMPurify（预览渲染 sanitize）
    assert "DOMPurify.sanitize(marked.parse" in html, "marked 渲染必须过 DOMPurify"


def test_wiki_editor_local_libs():
    """编辑功能：EasyMDE/marked/DOMPurify 必须本地化（lib/ 目录），禁止 CDN 外链"""
    import os
    base = os.path.join(os.path.dirname(__file__), "..", "dashboard")
    for lib in ["easymde.min.js", "easymde.min.css", "marked.min.js", "purify.min.js"]:
        p = os.path.join(base, "lib", lib)
        assert os.path.exists(p), f"本地 lib 缺失: {lib}"
        assert os.path.getsize(p) > 1000, f"lib 文件异常小: {lib}"
    with open(os.path.join(base, "wiki.html"), encoding="utf-8") as f:
        html = f.read()
    # 引用本地路径，无 CDN
    assert html.count("cdn.") == 0, "禁止 CDN 外链"
    assert "/static/lib/easymde.min.js" in html
    assert "/static/lib/marked.min.js" in html
    assert "/static/lib/purify.min.js" in html
    # 编辑按钮 + 保存函数 + XSS 防线（previewRender 过 DOMPurify）
    assert "editPage" in html and "savePage" in html and "btn-new-page" in html
    assert "DOMPurify.sanitize(marked.parse" in html, "预览渲染必须过 DOMPurify"
