"""D-7 / 3-2a 回归断言：全仓禁用通配 import + 统一依赖出口 deps.py。

- 用 AST 扫描（非字符串 grep），避免误报字符串/注释里的 ``import *``。
- 仓根用 ``Path(__file__).resolve().parents[1]`` 定位，不依赖 cwd。
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 不纳入扫描的目录（依赖目录 / 构建产物 / 出仓示例资产）
EXCLUDED_DIRS = {"node_modules", "build", "dist", "__pycache__", ".venv"}

DEAD_FILES = ["split_hub_core.py", "split_routes.py", "split_routes2.py"]


def _iter_py_files():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        parts = set(rel.parts[:-1])
        if parts & EXCLUDED_DIRS:
            continue
        if "examples" in rel.parts and "arch-site" in rel.parts:
            continue
        yield path


def _star_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    hits.append((node.lineno, node.module))
    return hits


def test_no_wildcard_imports_anywhere():
    offenders = []
    for path in _iter_py_files():
        for lineno, module in _star_imports(path):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: from {module} import *")
    assert not offenders, "仍存在通配 import:\n" + "\n".join(offenders)


def test_deps_py_is_models_db_only_facade():
    deps = REPO_ROOT / "deps.py"
    assert deps.exists(), "deps.py 不存在（统一依赖出口未建立）"
    tree = ast.parse(deps.read_text(encoding="utf-8"), filename=str(deps))
    import_from_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert import_from_modules, "deps.py 没有任何 from ... import ... 语句"
    assert import_from_modules <= {"models", "db"}, (
        f"deps.py 只允许 re-export models/db 两个叶子模块，实际: {import_from_modules}"
    )


def test_dead_files_removed():
    for name in DEAD_FILES:
        assert not (REPO_ROOT / name).exists(), f"死文件 {name} 仍存在"
