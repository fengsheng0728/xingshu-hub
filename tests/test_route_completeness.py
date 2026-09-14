# -*- coding: utf-8 -*-
"""星枢断言机械化 batch3 · 任务1+2：全量路由存在性断言 + 路由模块挂载断言

防「幻影缺陷/幽灵交付」：存在性断言机械化，替代手写 EXPECTED_ROUTES 部分清单。

覆盖：
1. 正向断言：AST 提取 routes.py + 全部 routes_*.py 的每个路由装饰器路径，
   必须存在于 FastAPI app.routes 注册表（缺一个即 fail「幽灵交付」）。
2. 反向断言：app.routes 中业务端点（/api/ 前缀）必须能追溯到某个路由模块的
   提取清单（防「多注册了不该有的」）。
3. 统计断言：提取 (method, path) 对总数 == app.routes APIRoute 对总数，
   且 /api/ 业务路径数两侧一致。
4. 挂载断言（任务2）：每个 routes_*.py 模块必须在 routes.py 中被挂载——
   APIRouter 模块走 include_router；register(app,...) 模式模块走函数调用；
   无路由的 helper 模块（routes_common）必须被 import。

归一化规则（经原型实测验证，2026-09）：
- 全部 APIRouter() 无 prefix、include_router 不带 prefix，挂载前缀恒为空；
  本测试仍机械解析 APIRouter(prefix=)/include_router(prefix=) 并拼接，防未来漂移。
- 路径模板 {param} / {param:path}：装饰器字符串与 Starlette route.path 字面一致，
  无需改写；比对前仅做「补前导斜杠 / 去尾部斜杠（根路径除外）/ 折叠重复斜杠」。
- 忽略自动端点：/openapi.json /docs /docs/oauth2-redirect /redoc（starlette Route），
  以及 Mount（/static /assets /legacy /mcp）——它们无 methods 语义或非业务路由。
"""
import ast
import glob
import os

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}
ALL_METHODS = HTTP_METHODS | {"websocket"}

# FastAPI 自动端点（非业务、非装饰器提取来源）
AUTO_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _norm(path: str) -> str:
    """路径归一化：补前导斜杠、折叠重复斜杠、去尾部斜杠（根路径除外）。"""
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def _route_decorators(file_path: str):
    """AST 提取一个文件中的全部路由装饰器：(method, path, lineno)。

    识别 @app.<method>("...") 与 @router.<method>("...")，覆盖模块级函数与
    嵌套函数（routes_automation.register 内 @app.* 也能提取）。路径必须是
    字符串常量；非常量路径（f-string/变量）无法静态追溯，返回 ("<NON-CONST>")。
    """
    src = open(file_path, encoding="utf-8").read()
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            f = dec.func
            if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
                continue
            if f.value.id not in ("app", "router") or f.attr not in ALL_METHODS:
                continue
            if dec.args and isinstance(dec.args[0], ast.Constant) \
                    and isinstance(dec.args[0].value, str):
                out.append((f.attr.upper(), dec.args[0].value, node.lineno))
            else:
                out.append((f.attr.upper(), "<NON-CONST>", node.lineno))
    return out


def _module_router_prefix(file_path: str) -> str:
    """模块级 router = APIRouter(prefix=...) 的 prefix（无则空串）。"""
    tree = ast.parse(open(file_path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "router" \
                and isinstance(node.value, ast.Call):
            for kw in node.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    return kw.value.value or ""
    return ""


def _routes_py_wiring():
    """解析 routes.py 的挂载接线。

    返回 (imported, include_router_map, called_aliases)：
    - imported: {alias: (module, original_name)}  顶层 from routes_* import ...
    - include_router_map: {alias: prefix}  app.include_router(alias[, prefix=...])
    - called_aliases: {alias}  routes.py 中以别名直接调用的函数（register 模式）
    """
    tree = ast.parse(open(os.path.join(BASE, "routes.py"), encoding="utf-8").read())
    imported, include_map, called = {}, {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("routes"):
            for a in node.names:
                imported[a.asname or a.name] = (node.module, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("routes"):
                    imported[a.asname or a.name] = (a.name, a.name)
        elif isinstance(node, ast.Call):
            f = node.func
            # app.include_router(_xxx_router[, prefix="..."])
            if isinstance(f, ast.Attribute) and f.attr == "include_router" \
                    and node.args and isinstance(node.args[0], ast.Name):
                prefix = ""
                for kw in node.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        prefix = kw.value.value or ""
                include_map[node.args[0].id] = prefix
            # _register_xxx(app, ...) 直调（routes_automation 模式）
            elif isinstance(f, ast.Name):
                called.add(f.id)
    return imported, include_map, called


def _extract_all():
    """全量提取：routes.py（@app）+ routes_*.py（@router/@app），含 prefix 归一化。

    返回 {module_name: [(method, full_path, lineno), ...]}
    """
    imported, include_map, _ = _routes_py_wiring()
    # module → include_router prefix（经别名反查）
    mod_prefix = {}
    for alias, (mod, _orig) in imported.items():
        if alias in include_map:
            mod_prefix[mod] = include_map[alias]

    result = {}
    files = ["routes.py"] + sorted(
        os.path.basename(p) for p in glob.glob(os.path.join(BASE, "routes_*.py")))
    for fname in files:
        mod = fname[:-3]
        entries = []
        router_prefix = _module_router_prefix(os.path.join(BASE, fname))
        mount_prefix = mod_prefix.get(mod, "")
        for method, path, lineno in _route_decorators(os.path.join(BASE, fname)):
            if path == "<NON-CONST>":
                entries.append((method, path, lineno))
                continue
            # @app 装饰器（routes.py / routes_automation.register）不带 router 前缀
            full = path if fname in ("routes.py", "routes_automation.py") \
                else (mount_prefix or "") + router_prefix + path
            entries.append((method, _norm(full), lineno))
        result[mod] = entries
    return result


def _registered():
    """app.routes 注册表：(http_pairs, ws_paths, business_paths)"""
    from routes import app
    http_pairs, ws_paths = set(), set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if not path:
            continue
        cls = type(r).__name__
        if cls == "APIRoute":
            for m in (getattr(r, "methods", None) or set()):
                http_pairs.add((m, _norm(path)))
        elif "WebSocketRoute" in cls:
            ws_paths.add(_norm(path))
        # starlette Route（/docs 等自动端点）与 Mount（/static 等）忽略
    business = {p for _, p in http_pairs
                if p.startswith("/api/") and p not in AUTO_PATHS}
    return http_pairs, ws_paths, business


# ═══════════ 任务 1：全量路由存在性断言 ═══════════

def test_no_non_const_route_paths():
    """提取健全性：所有路由装饰器路径必须是字符串常量（否则静态断言失效）。"""
    bad = [(mod, m, ln) for mod, entries in _extract_all().items()
           for m, p, ln in entries if p == "<NON-CONST>"]
    assert not bad, f"存在非常量路径装饰器，无法机械化断言: {bad}"


def test_forward_extracted_routes_all_registered():
    """正向：提取的每个 (method, path) 必须存在于 app.routes —— 缺一个即幽灵交付。"""
    extracted = _extract_all()
    http_pairs, ws_paths, _ = _registered()

    missing_http, missing_ws = [], []
    for mod, entries in sorted(extracted.items()):
        for method, path, _ln in entries:
            if method == "WEBSOCKET":
                if path not in ws_paths:
                    missing_ws.append(f"{mod}: WS {path}")
            elif (method, path) not in http_pairs:
                missing_http.append(f"{mod}: {method} {path}")
    assert not missing_http, f"幽灵交付（提取到但未注册）: {missing_http}"
    assert not missing_ws, f"幽灵交付（WS 提取到但未注册）: {missing_ws}"


def test_reverse_business_routes_all_traceable():
    """反向：app.routes 中 /api/ 业务端点必须能追溯到某个路由模块的提取清单。"""
    extracted = _extract_all()
    ext_pairs = {(m, p) for entries in extracted.values()
                 for m, p, _ in entries if m != "WEBSOCKET"}
    http_pairs, _, business = _registered()

    untraceable = sorted(
        f"{m} {p}" for m, p in http_pairs
        if p in business and (m, p) not in ext_pairs)
    assert not untraceable, \
        f"多注册了不该有的端点（app.routes 有但提取清单无）: {untraceable}"


def test_route_counts_match():
    """统计断言：提取总数 == app.routes 注册数（HTTP 对、/api/ 业务路径两级）。"""
    extracted = _extract_all()
    ext_http = {(m, p) for entries in extracted.values()
                for m, p, _ in entries if m != "WEBSOCKET"}
    ext_ws = {p for entries in extracted.values()
              for m, p, _ in entries if m == "WEBSOCKET"}
    http_pairs, ws_paths, business = _registered()
    ext_business = {p for _, p in ext_http if p.startswith("/api/")}

    print(f"\n[路由统计] 提取 HTTP (method,path) 对: {len(ext_http)}"
          f" | app.routes APIRoute 对: {len(http_pairs)}")
    print(f"[路由统计] 提取 /api/ 业务路径: {len(ext_business)}"
          f" | app.routes /api/ 业务路径: {len(business)}")
    print(f"[路由统计] 提取 WS 路径: {len(ext_ws)} | 注册 WS 路径: {len(ws_paths)}")
    assert len(ext_http) == len(http_pairs), \
        f"HTTP 路由数不一致: 提取 {len(ext_http)} vs 注册 {len(http_pairs)}"
    assert len(ext_business) == len(business), \
        f"/api/ 业务路径数不一致: 提取 {len(ext_business)} vs 注册 {len(business)}"
    assert len(ext_ws) == len(ws_paths), \
        f"WS 路由数不一致: 提取 {len(ext_ws)} vs 注册 {len(ws_paths)}"


# ═══════════ 任务 2：路由模块挂载断言 ═══════════

def _module_has_apirouter(file_path: str) -> bool:
    tree = ast.parse(open(file_path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "router" \
                and isinstance(node.value, ast.Call):
            f = node.value.func
            if (isinstance(f, ast.Name) and f.id == "APIRouter") or \
                    (isinstance(f, ast.Attribute) and f.attr == "APIRouter"):
                return True
    return False


def test_all_route_modules_mounted():
    """每个 routes_*.py 模块必须在 routes.py 中被挂载/接入：

    - 定义 router = APIRouter() 的模块 → 必须有 from 模块 import router as X
      且 app.include_router(X)；
    - register(app, ...) 模式的模块（routes_automation）→ routes.py 必须 import
      并直调其函数；
    - 无路由的 helper 模块（routes_common）→ 必须被 routes.py import。
    发现未挂载模块即 fail（「模块定义了但没注册」的幽灵形态）。
    """
    extracted = _extract_all()
    imported, include_map, called = _routes_py_wiring()
    # module → 全部别名（一个模块可能被多次 import：router / registry 等）
    aliases_of = {}
    for alias, (mod, _o) in imported.items():
        aliases_of.setdefault(mod, []).append(alias)

    modules = sorted(mod for mod in extracted if mod != "routes")
    assert len(modules) == 27, f"routes_*.py 模块数变化: {len(modules)}: {modules}"

    table, unmounted = [], []
    for mod in modules:
        fpath = os.path.join(BASE, mod + ".py")
        n_routes = len(extracted[mod])
        if n_routes == 0:
            # helper 模块：只要求被 import
            ok = mod in {m for _a, (m, _o) in imported.items()}
            mode = "helper(import)"
        elif _module_has_apirouter(fpath):
            ok = any(a in include_map and imported.get(a, ("", ""))[1] == "router"
                     for a in aliases_of.get(mod, []))
            mode = "include_router"
        else:
            # register(app, ...) 模式：模块被 import 且其别名被直调
            aliases = [a for a, (m, _o) in imported.items() if m == mod]
            ok = any(a in called for a in aliases)
            mode = "register() 直调"
        table.append((mod, n_routes, mode, "OK" if ok else "未挂载"))
        if not ok:
            unmounted.append(mod)

    print("\n[模块挂载状态表]")
    for mod, n, mode, status in table:
        print(f"  {mod:24s} routes={n:3d}  模式={mode:18s} {status}")
    assert not unmounted, f"路由模块定义了但未挂载（幽灵形态）: {unmounted}"
