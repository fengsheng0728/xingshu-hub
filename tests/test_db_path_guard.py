"""CD-070：测试库路径硬门（生产库防误写）。

事故背景（2026-09-20 实测）：测试模块级 import db/models 时按 config 默认路径在
仓库根建库，把生产库 ./sync_hub.db 顶成空壳（agents/memory_pool/knowledge_base/
shared_docs 全 0，29 个 Agent 消失）。

口径：
  ① `SYNC_HUB_DB` env 覆盖 config.yaml 的 database.path（与 alembic env.py 同源）；
  ② 测试态下 DB 解析到「仓库根 ./sync_hub.db」= 直接 fail（models.assert_db_path_safe）；
  ③ 生产运行（无测试标记）不受影响 —— 生产库本来就该在仓库根。

本文件是**门禁**：第 5 条用例复刻事故配方，改动前必绿（不报错），改动后必须硬失败。
"""
import os
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DB = os.path.join(REPO_ROOT, "sync_hub.db")

sys.path.insert(0, REPO_ROOT)
import models  # noqa: E402


def _sub(env_overrides: dict, code: str):
    env = dict(os.environ)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


# ---------------- 1. 硬门本体 ----------------


def test_guard_rejects_repo_root_production_db():
    """解析到仓库根生产库 → 抛 RuntimeError（不被静默放行）。"""
    with pytest.raises(RuntimeError) as ei:
        models.assert_db_path_safe(ROOT_DB, "unit-test")
    assert "DB 路径硬门" in str(ei.value)
    # 相对写法同样命中（事故现场就是 './sync_hub.db' + cwd=仓库根）
    with pytest.raises(RuntimeError):
        models.assert_db_path_safe("./sync_hub.db", "unit-test-relative")


def test_guard_allows_non_root_path():
    """非仓库根路径正常通过并返回绝对路径。"""
    tmp = os.path.join(tempfile.mkdtemp(prefix="cd070-ok-"), "x.db")
    assert models.assert_db_path_safe(tmp) == os.path.abspath(tmp)


def test_guard_is_dormant_without_test_context(monkeypatch):
    """无测试标记（=生产运行）时守卫不生效：模型内部判定即为 False。"""
    for k in ("SYNC_HUB_DB_GUARD", "PYTEST_VERSION", "PYTEST_CURRENT_TEST", "SYNC_HUB_NO_AUTH"):
        monkeypatch.delenv(k, raising=False)
    assert models._in_test_context() is False


# ---------------- 2. 当前进程确实被隔离 ----------------


def test_current_test_process_db_is_not_repo_root():
    """pytest 进程内的 CONFIG.DB_PATH 必须落在临时库（不是仓库根生产库）。"""
    resolved = os.path.abspath(models.CONFIG.DB_PATH)
    assert resolved != ROOT_DB, f"测试进程仍指向生产库: {resolved}"
    assert "sync-hub-testdb-" in resolved, f"应落在 conftest 的临时库目录: {resolved}"


def test_env_override_wins_over_config_yaml():
    """SYNC_HUB_DB 优先于 config.yaml 的 database.path（子进程实证）。"""
    tmpdir = tempfile.mkdtemp(prefix="cd070-env-")
    db = os.path.join(tmpdir, "env_wins.db").replace("\\", "/")
    empty_cfg = tempfile.mkdtemp(prefix="cd070-cfg-")
    r = _sub({"SYNC_HUB_DB": db, "SYNC_HUB_DB_GUARD": "1", "SYNC_HUB_CONFIG_DIR": empty_cfg},
             "import os, models; print(os.path.abspath(models.CONFIG.DB_PATH))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == os.path.abspath(db)


# ---------------- 3. 门禁：事故配方必须硬失败 ----------------


def test_incident_recipe_fails_hard():
    """复刻事故配方：测试态 + config 目录为空（config.yaml 不存在，DB 回落仓库根）
    + cwd=仓库根 → `import models` 必须非零退出并报硬门。

    改动前（HEAD 1d418ce）此配方 exit=0 且 CONFIG.DB_PATH='./sync_hub.db'
    —— 这正是生产库被顶掉的路径。
    """
    empty_cfg = tempfile.mkdtemp(prefix="cd070-empty-cfg-")
    r = _sub({"SYNC_HUB_DB": None, "SYNC_HUB_DB_GUARD": "1", "SYNC_HUB_CONFIG_DIR": empty_cfg},
             "import models")
    assert r.returncode != 0, f"硬门未生效：事故配方仍然跑通。stdout={r.stdout!r}"
    assert "DB 路径硬门" in (r.stderr or ""), r.stderr


def test_production_startup_path_still_works():
    """生产路径（无任何测试标记）导入 models 必须正常，且 DB 仍是仓库根默认值。"""
    r = _sub({"SYNC_HUB_DB": None, "SYNC_HUB_DB_GUARD": None, "SYNC_HUB_NO_AUTH": None,
              "SYNC_HUB_CONFIG_DIR": tempfile.mkdtemp(prefix="cd070-prod-cfg-"),
              "PYTEST_VERSION": None, "PYTEST_CURRENT_TEST": None},
             "import os, models; print(models.CONFIG.DB_PATH)")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "./sync_hub.db", r.stdout


# ---------------- 4. CD-070b：派生产物（chroma / wiki / audit）路径 ----------------


def test_derived_artifacts_are_isolated_from_repo():
    """pytest 进程内：chroma / wiki / audit / tsa 四项 env 必须指向仓库外的 tmp。"""
    for key, sub in (("SYNC_HUB_CHROMA_PATH", "chroma_db"),
                     ("SYNC_HUB_WIKI_ROOT", "wiki"),
                     ("SYNC_HUB_AUDIT_DIR", "audit"),
                     ("SYNC_HUB_TSA_DIR", os.path.join("audit", "tsa")),
                     ("SYNC_HUB_YSTORE_PATH", "ystore.db")):
        v = os.environ.get(key, "")
        assert v, f"{key} 未设 → 产物会落仓库根"
        resolved = os.path.abspath(v)
        assert resolved != os.path.join(REPO_ROOT, sub), f"{key} 命中仓库内生产路径: {v}"
        assert "sync-hub-testartifacts-" in resolved, f"{key} 应在 conftest 的 tmp 产物目录: {v}"


def test_derived_modules_resolve_to_tmp_in_process():
    """内存对象的实际取值也要落在 tmp（模块级取值，不是只看 env）。"""
    import wiki_engine
    import transport_audit
    import audit_chain
    from audit import memory_audit as _ma

    for name, v in (("wiki_engine.WIKI_ROOT", wiki_engine.WIKI_ROOT),
                    ("memory_audit.AUDIT_DIR", _ma.AUDIT_DIR),
                    ("memory_audit.AUDIT_FILE", _ma.AUDIT_FILE),
                    ("transport_audit.AUDIT_DIR", transport_audit.AUDIT_DIR),
                    ("audit_chain._ANCHOR_FILE", audit_chain._ANCHOR_FILE),
                    ("audit_chain.TSA_DIR", audit_chain.TSA_DIR)):
        resolved = os.path.abspath(v)
        # 只断言「不落仓库内生产目录」——模块级属性可能被既有测试永久改写
        # （例：tests/test_l7_audit.py 在模块级直接给 `transport_audit.AUDIT_DIR` 赋值，不经 monkeypatch），
        # 那是测试自建的 tmp，同样不算污染生产。严格口径由上面的 env 用例守。
        assert resolved not in (os.path.join(REPO_ROOT, "wiki"), os.path.join(REPO_ROOT, "audit")), \
            f"{name} 仍指仓库内生产目录: {v}"
        _tmpbase = os.path.abspath(tempfile.gettempdir()).lower()
        assert resolved.lower().startswith(_tmpbase), f"{name} 未落到系统临时目录: {v}"


def test_models_resolves_derived_dirs_from_env():
    """models 真的接了这三个 env（子进程实证，非采信注释）。"""
    tmp = tempfile.mkdtemp(prefix="cd070b-env-")
    paths = [os.path.join(tmp, n) for n in ("c", "w", "a")]
    code = ("import models;"
            "print('|'.join([models.CONFIG.CHROMA_PATH, models.CONFIG.WIKI_ROOT, models.CONFIG.AUDIT_DIR]))")
    r = _sub({"SYNC_HUB_DB": os.path.join(tmp, "t.db"), "SYNC_HUB_DB_GUARD": "1",
              "SYNC_HUB_CONFIG_DIR": tempfile.mkdtemp(prefix="cd070b-cfg2-"),
              "SYNC_HUB_CHROMA_PATH": paths[0], "SYNC_HUB_WIKI_ROOT": paths[1],
              "SYNC_HUB_AUDIT_DIR": paths[2]}, code)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "|".join(paths), r.stdout


def test_derived_dir_defaults_stay_in_repo_without_env():
    """生产路径（无 env）：默认仍是仓库内/相对路径 —— 不误伤生产。"""
    tmp = tempfile.mkdtemp(prefix="cd070b-prod-")
    code = ("import models;"
            "print('|'.join([models.CONFIG.CHROMA_PATH, models.CONFIG.WIKI_ROOT, models.CONFIG.AUDIT_DIR]))")
    r = _sub({"SYNC_HUB_DB": None, "SYNC_HUB_DB_GUARD": None, "SYNC_HUB_NO_AUTH": None,
              "SYNC_HUB_CHROMA_PATH": None, "SYNC_HUB_WIKI_ROOT": None,
              "SYNC_HUB_AUDIT_DIR": None, "SYNC_HUB_TSA_DIR": None,
              "PYTEST_VERSION": None, "PYTEST_CURRENT_TEST": None,
              "SYNC_HUB_CONFIG_DIR": tmp}, code)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "./chroma_db||", r.stdout


# ---------------- 5. CD-070b：隔离 env 不得被单个用例「盲 pop」带出作用域 ----------------


def test_zz_zzz_leak_attempt_popps_isolation_env():
    """模拟事故：某用例把隔离 env 从共享 os.environ 摘掉。

    （真凶是 tests/test_semantic_degrade.py 的 teardown，它曾盲 pop
    SYNC_HUB_CHROMA_PATH，导致此后所有用例 spawn 的 Hub 回落仓库根 chroma。）
    """
    os.environ.pop("SYNC_HUB_CHROMA_PATH", None)
    assert "SYNC_HUB_CHROMA_PATH" not in os.environ


def test_zz_zzz_plus1_isolation_env_survives_next_test():
    """下一个用例必须仍看到隔离 env —— 由 conftest 的 autouse fixture 兜住。"""
    assert os.environ.get("SYNC_HUB_CHROMA_PATH"), \
        "隔离 env 被上一个用例 pop 后未恢复（conftest autouse fixture 失效）"
    assert os.environ.get("SYNC_HUB_AUDIT_DIR") and os.environ.get("SYNC_HUB_WIKI_ROOT")
