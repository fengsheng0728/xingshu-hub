"""星枢 Sync Hub — 静态页面端点（Phase 2 拆分自 routes.py，端点路径与行为不变）"""
import os, sys, yaml
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


def _ui_new_enabled() -> bool:
    """U1 灰度开关：config.yaml ui.new=true → / 直出新控制台；默认 false 回退旧页"""
    config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
    try:
        with open(os.path.join(config_dir, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return bool(cfg.get("ui", {}).get("new", False))
    except Exception:
        return False


# ============ 静态页读缓存（事件循环 C 类优化） ============
# 静态页路径集合固定（dashboard/examples/showcase 等 <20 条），无增长风险，无需 LRU。
_STATIC_HTML_CACHE: Dict[str, Dict[str, Any]] = {}  # abs_path → {"content": str, "mtime": float}


def _read_static_html(path: str) -> str:
    """读静态 HTML，按 mtime 失效的内存缓存，消除每请求磁盘 I/O。

    - key=绝对路径；读前 os.path.getmtime 比对，变了重读（开发期改页面即时生效）
    - 文件不存在/读取失败时不缓存，异常语义与直接 open() 一致（FileNotFoundError 等原样抛出）
    - PyInstaller 打包态兜底（2026-09-06）：cwd 无 dashboard/ 等目录时回退
      sys._MEIPASS（datas 解包目录）——所有 './xxx/index.html' 页面路由一处修复全收
    """
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        _mei = getattr(sys, "_MEIPASS", "")
        if _mei and path.startswith("./"):
            _alt = os.path.abspath(os.path.join(_mei, path[2:]))
            if os.path.exists(_alt):
                abs_path = _alt
    mtime = os.path.getmtime(abs_path)  # 不存在时在此抛 FileNotFoundError，不缓存
    cached = _STATIC_HTML_CACHE.get(abs_path)
    if cached is not None and cached["mtime"] == mtime:
        return cached["content"]
    with open(abs_path, "r", encoding="utf-8") as f:
        content = f.read()
    _STATIC_HTML_CACHE[abs_path] = {"content": content, "mtime": mtime}
    return content


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    # U1 灰度：ui.new=true 且产物存在 → 新控制台；否则旧首页（一个版本周期后可移除）
    if _ui_new_enabled() and os.path.isfile("./dashboard_dist/index.html"):
        return _read_static_html("./dashboard_dist/index.html")
    return _read_static_html("./dashboard/index.html")


@router.get("/showcase", response_class=HTMLResponse)
async def showcase():
    return _read_static_html("./examples/showcase/index.html")


@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page():
    """知识图谱页面"""
    return _read_static_html("./dashboard/knowledge.html")


@router.get("/chat", response_class=HTMLResponse)
async def chat_page():
    """Hub Agent 对话页面"""
    return _read_static_html("./dashboard/chat.html")


@router.get("/report", response_class=HTMLResponse)
async def report_page():
    """日报页面"""
    return _read_static_html("./dashboard/report.html")


@router.get("/wiki", response_class=HTMLResponse)
async def wiki_page():
    """Wiki 知识库面板 — 浏览/搜索/收件箱审查（P1）"""
    return _read_static_html("./dashboard/wiki.html")


@router.get("/team", response_class=HTMLResponse)
async def team_page():
    """P1: 团队仪表盘 — 老板视图(在线/任务分布/记忆/自动化)"""
    return _read_static_html("./dashboard/team.html")
