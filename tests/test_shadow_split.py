# -*- coding: utf-8 -*-
"""D-9 / 3-2c：hub_mixins/shadow.py → hub_mixins/shadow/ 包二次拆分的结构断言。

只验结构与兼容面，不验行为（行为由 test_shadow*.py / test_backfeed*.py 等存量回归覆盖）。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub_mixins.shadow as shadow_mod
from hub_mixins.shadow import (  # noqa: F401  兼容面：外部引用一个不能少
    ShadowWriter, collect_origins, _index_rows, _PENDING_MAX_ATTEMPTS,
)
from hub_mixins.shadow.lifecycle import LifecycleMixin
from hub_mixins.shadow.merge import MergeMixin
from hub_mixins.shadow.pending import PendingMixin
from hub_mixins.shadow.scan import ScanMixin
from hub_mixins.shadow.write import WriteMixin

MIXINS = (LifecycleMixin, PendingMixin, WriteMixin, MergeMixin, ScanMixin)
PKG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "hub_mixins", "shadow")

# 任务书第一节实测的外部引用名（含下划线名）+ 模块级符号全清单
EXTERNAL_NAMES = [
    # 外部 import 实测清单
    "ShadowWriter", "collect_origins", "_index_rows", "_PENDING_MAX_ATTEMPTS",
    "_today", "_MERGE_MIN_AGE_SEC",
    # 其余模块级符号（原模块公开面，兼容出口一个不能少）
    "_BATCH_SIZE", "_BATCH_INTERVAL", "_WATCHDOG_INTERVAL", "_PEND_DDL",
    "_INDEX_KINDS", "_safe_name", "_ts_epoch", "_strip_front_matter",
    "_with_front_matter_field", "_drop_front_matter_field", "_index_state",
    "append_index_correction", "_front_matter", "_read_worktree",
    "_origins_from_trunk",
]

# 任务书 T3-5 要求每个 mixin 文件 ≤450 行；但 T2 分组表把 7 个反哺合并方法
# （execute_merge/undo_merge/scan_and_merge/_backfeed_threshold/scan_cos_merge/
# _mk_source/resolve_sources，实测内容 497 行）全部划入 merge 组，
# 497 行逐字搬运内容本身即超 450 —— T2 与 T3-5 冲突，merge.py 按实测放宽，
# 其余 mixin 严格 ≤450。
MAX_LINES = {"lifecycle.py": 450, "pending.py": 450, "write.py": 450,
             "merge.py": 450, "scan.py": 450, "writer.py": 450}


def _fn_names(cls):
    return {n for n, o in vars(cls).items() if inspect.isfunction(o)}


def test_compat_import_surface():
    """兼容出口：原模块被外部引用的名字全部仍在。"""
    for name in EXTERNAL_NAMES:
        assert hasattr(shadow_mod, name), f"hub_mixins.shadow 缺少兼容名 {name}"


def test_mro_composition_fixed_order():
    """组合正确：四个 mixin 以固定顺序进入 MRO。"""
    mro = ShadowWriter.__mro__
    for m in MIXINS:
        assert m in mro, f"{m.__name__} 不在 ShadowWriter.__mro__"
    assert mro[1:6] == MIXINS, f"MRO 顺序不符: {[c.__name__ for c in mro]}"


def test_no_duplicate_method_names():
    """四个 mixin 的方法名两两交集为空（防 MRO 静默遮蔽）。"""
    seen = {}
    for m in MIXINS:
        for name in _fn_names(m):
            assert name not in seen, f"{name} 同时定义于 {seen[name]} 与 {m.__name__}"
            seen[name] = m.__name__


def test_init_only_in_writer():
    """__init__ 只在组合类 ShadowWriter 里，四个 mixin 都不得定义。"""
    for m in MIXINS:
        assert "__init__" not in vars(m), f"{m.__name__} 不应定义 __init__"
    assert "__init__" in vars(ShadowWriter)


def test_package_layout_and_sizes():
    """shadow.py 已不存在；shadow/ 是目录；各 mixin 文件行数有界。"""
    assert not os.path.exists(PKG_DIR + ".py"), "hub_mixins/shadow.py 应已删除"
    assert os.path.isdir(PKG_DIR), "hub_mixins/shadow/ 应为包目录"
    for fname, limit in MAX_LINES.items():
        path = os.path.join(PKG_DIR, fname)
        assert os.path.isfile(path), f"缺少 {fname}"
        with open(path, "r", encoding="utf-8") as f:
            n = sum(1 for _ in f)
        assert n <= limit, f"{fname} {n} 行 > 上限 {limit}"
