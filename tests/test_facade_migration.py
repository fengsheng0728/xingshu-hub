# -*- coding: utf-8 -*-
"""D-11 (3-1b) db 门面热点迁移 —— AST 反向断言门禁（防复发）。

三条机械证据：
1. 7 个热点文件的任何 AsyncFunctionDef **自身函数体**内不得再出现直接 SQLite 调用
   （.execute/.executemany/.executescript/.fetchone/.fetchall/.commit/.rollback 或 sqlite3.connect）；
   确有无解例外必须登记在 EXCEPTIONS。
   **口径说明（2026-09-14 Hermes 验收修正）**：嵌套同步函数若被
   `run_in_conn / run_sync / to_thread / run_in_executor` 接收执行，其函数体在别的线程里跑、
   不占事件循环（这正是本次迁移的目标形态），**不计入**；未被 offload 接收的嵌套函数仍计入。
   原口径不区分这两者，会把 `def _txn(conn): ... conn.execute(...)` 这类**正确写法**误报为违规。
2. 7 个文件都必须 import db_facade。
3. 7 个文件里 `await db_facade.<api>(...)` 的实测下限（只许调高不许调低）。
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

TARGETS = [
    "hub_mixins/memory.py",
    "hub_mixins/ingest.py",
    "hub_mixins/knowledge.py",
    "hub_mixins/notifications.py",
    "hub_mixins/tasks.py",
    "disclosure.py",
    "hub_mixins/disclosure_ops.py",
]

FORBIDDEN_ATTRS = {
    "execute",
    "executemany",
    "executescript",
    "fetchone",
    "fetchall",
    "commit",
    "rollback",
}

# 该 async 函数内经「门面 / 线程池」执行的嵌套函数名集合（其函数体不占事件循环）
OFFLOAD_CALLERS = {"run_in_conn", "run_sync", "to_thread", "run_in_executor"}

# 例外登记：{(文件, 函数名): 理由}。空 = 无例外。
EXCEPTIONS = {}

# `await db_facade.*(...)` 实测下限（2026-09-14 迁移完成后实测回填）
MIN_AWAIT_FACADE_CALLS = {
    "hub_mixins/memory.py": 9,
    "hub_mixins/ingest.py": 17,
    "hub_mixins/knowledge.py": 3,
    "hub_mixins/notifications.py": 4,
    "hub_mixins/tasks.py": 24,
    "disclosure.py": 13,
    "hub_mixins/disclosure_ops.py": 6,
}


def _is_facade_call(func_attr):
    """`db_facade.x(...)` / `_facade_x(...)` 是门面自身调用，不算直连。"""
    value = func_attr.value
    if isinstance(value, ast.Name) and (value.id == "db_facade" or value.id.startswith("_facade")):
        return True
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) \
            and value.value.id == "db_facade":
        return True
    return False


def _offloaded_names(async_node):
    names = set()
    for call in ast.walk(async_node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Attribute):
            fname = func.attr
        elif isinstance(func, ast.Name):
            fname = func.id
        else:
            fname = None
        if fname in OFFLOAD_CALLERS:
            for arg in call.args:
                if isinstance(arg, ast.Name):
                    names.add(arg.id)
    return names


def _direct_calls_in_async(async_node):
    """返回仍占事件循环的直接 SQLite 调用 [(lineno, 描述)]。"""
    offloaded = _offloaded_names(async_node)
    skipped = []
    for node in ast.walk(async_node):
        if node is async_node:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in offloaded:
            skipped.append((node.lineno, getattr(node, "end_lineno", node.lineno)))

    def _in_skipped(lineno):
        return any(lo <= lineno <= hi for lo, hi in skipped)

    hits = []
    for call in ast.walk(async_node):
        if not isinstance(call, ast.Call) or _in_skipped(call.lineno):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr in FORBIDDEN_ATTRS and not _is_facade_call(func):
            hits.append((call.lineno, "." + func.attr))
        elif (func.attr == "connect" and isinstance(func.value, ast.Name)
              and func.value.id == "sqlite3"):
            hits.append((call.lineno, "sqlite3.connect"))
    return hits


def _await_facade_count(tree):
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id == "db_facade":
            n += 1
    return n


def test_no_direct_sqlite_in_async_functions():
    """7 个热点文件的 async 函数体内零直接 SQLite 调用（已 offload 的嵌套函数除外）。"""
    offenders = []
    for rel in TARGETS:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if (rel, node.name) in EXCEPTIONS:
                continue
            hits = _direct_calls_in_async(node)
            if hits:
                offenders.append("%s::%s %s" % (rel, node.name,
                                                 " ".join("%d%s" % h for h in hits)))
    assert not offenders, "热点文件 async 函数内仍有直接 SQLite 调用:\n" + "\n".join(offenders)


def test_targets_import_db_facade():
    missing = [rel for rel in TARGETS
               if "import db_facade" not in (ROOT / rel).read_text(encoding="utf-8")]
    assert not missing, f"未 import db_facade: {missing}"


def test_await_facade_call_counts_meet_floor():
    """机械证据：迁移确实发生（`await db_facade.*` 计数不低于实测下限）。"""
    low = {}
    for rel, floor in MIN_AWAIT_FACADE_CALLS.items():
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        got = _await_facade_count(tree)
        if got < floor:
            low[rel] = (got, floor)
    assert not low, f"await db_facade.* 计数低于下限（file: got, floor）: {low}"
