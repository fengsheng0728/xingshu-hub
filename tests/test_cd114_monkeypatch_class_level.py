# -*- coding: utf-8 -*-
"""CD-114/CD-114b 机器断言：禁止 monkeypatch 实例级打桩 hub 类方法。

背景：monkeypatch.setattr(hub, "<类方法>", fake) 会在 teardown 时给全局单例
永久留下实例属性（pytest 记录 oldval=getattr(hub,name) 得到的是绑定方法，
setattr 回实例后遮蔽此后一切对类属性的 monkeypatch）。
正确做法：monkeypatch.setattr(<所属类>, name, fake)，fake 补 self 形参。

CD-114b 扩面：原断言只扫 Name(id='hub') 形态，扫不到
Attribute(attr='hub')（routes_xxx.hub）与变量形态（h = routes_xxx.hub 后
setattr(h, ...)）——等于没防住。本版同时覆盖三形态。

仍用 AST（非字符串 grep）——只认「setattr 的第二参数是字符串字面量」。
实例属性（_chroma_collection / data_trunk / _shadow / _outbox_consumer 等）
天然不在方法名集合内 → 自然放行。

白名单外已知命中登记在 INSTANCE_STUB_EXCEPTIONS（显式例外清单，
照 test_code_hygiene.H3_EXCEPTIONS 风格：命中必须与登记完全一致）。
"""
import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# CD-114b 白名单外已知实例级打桩 hub 类方法（文件不在 T13 白名单内，禁止修改，登记为例外）。
# 键 = (相对文件路径, 被打桩的类方法名)；值 = 命中次数。
# 2026-09-24：test_memory_merge_branch.py 的 hub fixture 已改类级打桩
# （monkeypatch.setattr(SyncHub, "_ensure_embedding_model", ...)），例外已移除。
INSTANCE_STUB_EXCEPTIONS = {}


def _methods_in_classes(tree):
    """收集 tree 中所有 ClassDef 内的 def / async def 名。"""
    names = set()
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(item.name)
    return names


def _methods_in_class(tree, class_name):
    """收集指定类的 def / async def 名。"""
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == class_name:
            return {
                item.name
                for item in cls.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def _hub_class_method_names():
    """hub 类方法名集合：hub_core.SyncHub + hub_mixins/*.py 各类。"""
    names = set()
    core = ast.parse((REPO_ROOT / "hub_core.py").read_text(encoding="utf-8"))
    names |= _methods_in_class(core, "SyncHub")
    for py in sorted((REPO_ROOT / "hub_mixins").glob("*.py")):
        names |= _methods_in_classes(ast.parse(py.read_text(encoding="utf-8")))
    return names


def _str_literal(node):
    """取字符串字面量值；非字符串字面量返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if hasattr(ast, "Str") and isinstance(node, ast.Str):
        return node.s
    return None


def _dotted_name(node):
    """把 Name/Attribute 链还原成 'a.b.c' 形式；不是纯链返回 None。"""
    parts = []
    n = node
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts))
    return None


def _collect_hub_aliases(tree):
    """收集 hub 实例别名：name = <expr>.hub 或 name = <已知别名>。

    返回 {变量名: 来源描述}，来源描述如 'routes_memory.hub'。
    只认 RHS 是 *.hub 属性链或已登记别名的赋值——不会把 hub = _MiniHub()
    之类的测试替身误收进来（它们不是单例，不适用本根因）。
    """
    aliases = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        t = node.targets[0]
        if not isinstance(t, ast.Name):
            continue
        val = node.value
        if isinstance(val, ast.Attribute) and val.attr == "hub":
            src = _dotted_name(val)
            if src:
                aliases[t.id] = src
        elif isinstance(val, ast.Name) and val.id in aliases:
            aliases[t.id] = aliases[val.id]
    return aliases


def _describe_target(target, hub_aliases):
    """返回打桩对象形态描述；不是 hub 引用则返回 None。

    三形态：
      Name(hub)                      — monkeypatch.setattr(hub, ...)
      Attribute(routes_xxx.hub)      — monkeypatch.setattr(routes_xxx.hub, ...)
      变量(h = routes_xxx.hub)       — monkeypatch.setattr(h, ...)
    """
    if isinstance(target, ast.Attribute) and target.attr == "hub":
        src = _dotted_name(target)
        return f"Attribute({src})" if src else None
    if isinstance(target, ast.Name):
        if target.id == "hub":
            return "Name(hub)"
        if target.id in hub_aliases:
            return f"变量({target.id} = {hub_aliases[target.id]})"
    return None


def _find_instance_level_stubs(method_names):
    """扫 tests/**/*.py（排除自身），找实例级打桩 hub 类方法的调用。

    命中条件：monkeypatch.setattr(<hub 引用>, "<literal>", ...)
    且 <literal> 在 hub 类方法名集合内。
    三形态都认：Name(hub) / Attribute(*.hub) / 变量(h = *.hub)。
    """
    hits = []
    self_path = Path(__file__).resolve()
    for py in sorted((REPO_ROOT / "tests").rglob("*.py")):
        if py.resolve() == self_path:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        hub_aliases = _collect_hub_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "setattr"):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "monkeypatch"):
                continue
            target = node.args[0]
            name = _str_literal(node.args[1])
            if name is None or name not in method_names:
                continue
            form = _describe_target(target, hub_aliases)
            if form is None:
                continue
            rel = py.relative_to(REPO_ROOT).as_posix()
            hits.append((rel, node.lineno, form, name))
    return hits


def test_no_instance_level_stub_of_hub_class_methods():
    method_names = _hub_class_method_names()
    assert method_names, "hub 类方法名集合为空 —— AST 解析异常"
    hits = _find_instance_level_stubs(method_names)

    # 按 (文件, 类方法名) 计次，与例外清单对账（照 H-3 风格：必须完全一致）
    found = {}
    by_key = {}
    for rel, lineno, form, name in hits:
        key = (rel, name)
        found[key] = found.get(key, 0) + 1
        by_key.setdefault(key, []).append((lineno, form))

    stale = {k: v for k, v in INSTANCE_STUB_EXCEPTIONS.items() if k not in found}
    offenders = {k: v for k, v in found.items() if k not in INSTANCE_STUB_EXCEPTIONS}

    if stale or offenders:
        parts = []
        if offenders:
            detail = "\n".join(
                f"  {rel}:{ln} → {form} → 类方法名 `{name}`"
                for (rel, name), n in sorted(offenders.items())
                for ln, form in by_key[(rel, name)]
            )
            parts.append(
                "monkeypatch 实例级打桩了 hub 类方法"
                "（teardown 会给单例永久留下实例属性，遮蔽后续类级 monkeypatch）：\n"
                f"{detail}"
            )
        if stale:
            parts.append(
                "例外清单已过期（下列登记的命中已不再出现，须同步移除）：\n"
                + "\n".join(f"  {rel} / `{name}` × {n}"
                            for (rel, name), n in sorted(stale.items()))
            )
        parts.append(
            "修法：改 monkeypatch.setattr(<所属类>, name, fake)"
            "（如 SyncHub / MemoryMixin / DisclosureOpsMixin），并给 fake 补 self 形参。"
        )
        pytest.fail("\n".join(parts))
