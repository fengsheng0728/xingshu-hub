"""pytest 全局配置
routes.NO_AUTH 是模块级常量（import 时求值）——不统一设置时，收集顺序
决定谁先 import routes，导致 test_shared_workspace 在全量跑时 401 失败。
这里保证所有测试文件 import routes 前 NO_AUTH=1 已生效。
依赖真实 Hub 的集成测试（guard_identity/team_integration）不 import routes，
不受影响；l6_ws_auth_regression / module5 用 monkeypatch 显式覆盖。

CD-070（2026-09-20）：测试库路径硬门 —— 生产库被测试顶掉过一次。
事故链：CONFIG_DIR 指向空目录 → config.yaml 不存在 → models 回落默认
DB_PATH "./sync_hub.db"（cwd=仓库根）→ 全仓 52 个测试文件在模块级 import
db/models → 按 config 默认路径在仓库根建库，把生产库顶成空壳（2026-09-20 实测
agents/memory_pool/knowledge_base/shared_docs 全 0，29 个 Agent 消失）。

对策（刻意**不设** SYNC_HUB_DB env：那会经 dict(os.environ) 漏进各测试自己
spawn 的 Hub 子进程，覆盖它们临时 config 里的 database.path）：
  ① 这里写一个只含 database.path 的最小 config.yaml 指向临时库；
  ② SYNC_HUB_DB_GUARD=1 打开 models.assert_db_path_safe 硬门（解析到仓库根即 fail）。
"""
import os
import tempfile

import pytest

os.environ.setdefault("SYNC_HUB_NO_AUTH", "1")
# 阶段3: 单元测试强制关影子数据底座（防污染真实 data-trunk）
os.environ.setdefault("SYNC_HUB_DATA_TRUNK", "0")
# config.yaml 是 git-ignored 本地 dev 配置（可含 auth.registration: guarded / OGA
# hub_token / notify_channels 等）——测试不得依赖其内容（否则机器相关、pre-push 门禁
# 随机红）。指向空目录 → models 回落代码默认（registration=open, 2026-09-08 实测:
# config.yaml=guarded 时 test_register_role_preserve/test_dm 等 13 failed）。
# 需 guarded 语义的专项测试(test_register_guard/test_key_issue_e2e)自起子进程并显式
# 覆盖 SYNC_HUB_CONFIG_DIR, 不受此影响。
#
# CD-070：临时 config 目录**不是空的**——里面只有一行 database.path 指向临时库，
# 其余键一律不写（保持「不依赖本机 config.yaml」的原意，只把 DB 挪出仓库根）。
_TEST_CFG_DIR = tempfile.mkdtemp(prefix="sync-hub-testcfg-")
_TEST_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="sync-hub-testdb-"), "test_hub.db")
with open(os.path.join(_TEST_CFG_DIR, "config.yaml"), "w", encoding="utf-8") as _f:
    _f.write("database:\n  path: %s\n" % _TEST_DB_PATH.replace("\\", "/"))
os.environ.setdefault("SYNC_HUB_CONFIG_DIR", _TEST_CFG_DIR)
# CD-070：硬门标记（models._in_test_context 据此生效）
os.environ["SYNC_HUB_DB_GUARD"] = "1"

# CD-070b（2026-09-20）：派生产物隔离 —— chroma / wiki / audit 一律落到 tmp。
# 同类事故面（PROBE 实测，DB 已堵后仍存在）：跑一轮测试会写仓库根 chroma_db
# （212992→217088）、重写生产 wiki/ 页（index.md 940→1003）、追加生产
# audit/anchor.txt 与 audit/memory_pool.jsonl。
# 这三项走 **env 覆盖**（models 已接 SYNC_HUB_CHROMA_PATH / SYNC_HUB_WIKI_ROOT /
# SYNC_HUB_AUDIT_DIR），因此也覆盖测试自 spawn 的 Hub 子进程 —— 子进程 env 由
# dict(os.environ) 构造，20+ 处显式设 SYNC_HUB_CHROMA_PATH 的由自身值覆盖（行为不变），
# 其余继承本篇指向 tmp（比原来写仓库根更安全）。
_TEST_ARTIFACTS_DIR = tempfile.mkdtemp(prefix="sync-hub-testartifacts-")
os.environ["SYNC_HUB_CHROMA_PATH"] = os.path.join(_TEST_ARTIFACTS_DIR, "chroma_db")
os.environ["SYNC_HUB_WIKI_ROOT"] = os.path.join(_TEST_ARTIFACTS_DIR, "wiki")
os.environ["SYNC_HUB_AUDIT_DIR"] = os.path.join(_TEST_ARTIFACTS_DIR, "audit")
os.environ["SYNC_HUB_TSA_DIR"] = os.path.join(_TEST_ARTIFACTS_DIR, "audit", "tsa")
os.environ["SYNC_HUB_YSTORE_PATH"] = os.path.join(_TEST_ARTIFACTS_DIR, "ystore.db")
_ARTIFACT_ENV = {
    "SYNC_HUB_CHROMA_PATH": os.environ["SYNC_HUB_CHROMA_PATH"],
    "SYNC_HUB_WIKI_ROOT": os.environ["SYNC_HUB_WIKI_ROOT"],
    "SYNC_HUB_AUDIT_DIR": os.environ["SYNC_HUB_AUDIT_DIR"],
    "SYNC_HUB_TSA_DIR": os.environ["SYNC_HUB_TSA_DIR"],
    "SYNC_HUB_YSTORE_PATH": os.environ["SYNC_HUB_YSTORE_PATH"],
}


@pytest.fixture(autouse=True)
def _reset_principal_contextvar():
    """CD-074：每个测试前清空 principal ContextVar。

    中间件每请求会 set 它（`routes_gateway`），而 TestClient 类测试执行后可能残留到
    同进程的后续直调测试 → 角色门会误把匿名直调当成 hub_token 主体（实测：
    auto-complete worker 403 用例因此变"不抛异常"）。
    """
    try:
        from routes_gateway import _mcp_principal_var
        _tok = _mcp_principal_var.set(None)
        yield
        try:
            _mcp_principal_var.reset(_tok)
        except Exception:
            pass
    except Exception:
        yield


@pytest.fixture(autouse=True)
def _reassert_artifact_env():
    """CD-070b：每个用例前重新断言隔离 env。

    事故模式（2026-09-20 实测点名）：test_semantic_degrade 的 teardown 做
    `os.environ.pop("SYNC_HUB_CHROMA_PATH")`（意图是「还原无覆盖」），但 pytest 共享
    同一个 os.environ —— 于是**此后所有用例**里 `dict(os.environ)` 构造的 Hub 子进程
    都丢了 chroma 覆盖，回落 models 默认 `./chroma_db` = 仓库根；
    实测 test_ws_auth_matrix / test_session_handoff 的常驻 Hub 每 ~5.6s 往生产
    chroma 写一次、持续 100s+。本 fixture 让任何 pop 都无法跨用例外溢。
    """
    for _k, _v in _ARTIFACT_ENV.items():
        if os.environ.get(_k) != _v:
            os.environ[_k] = _v
    yield


def _resolve_test_db_path() -> str:
    """CD-070：解析测试进程实际会用的 DB 路径（与 models 同口径：env > config.yaml）。"""
    explicit = os.environ.get("SYNC_HUB_DB", "").strip()
    if explicit:
        return os.path.abspath(explicit)
    cfg_path = os.path.join(os.environ.get("SYNC_HUB_CONFIG_DIR", ""), "config.yaml")
    db = ""
    if os.path.exists(cfg_path):
        try:
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as f:
                db = str(((yaml.safe_load(f) or {}).get("database") or {}).get("path", "") or "")
        except Exception:
            db = ""
    return os.path.abspath(db) if db else ""


def pytest_configure(config):
    """注册自定义 marker（requires_sentence_model：需 bge 模型文件就位）"""
    config.addinivalue_line(
        "markers",
        "requires_sentence_model: 需要 sentence provider（bge 模型文件）就位，hasher 模式自动 skip",
    )
    # CD-070：收集阶段再核一次 DB 路径（防 conftest 被绕过 / env 被外层覆盖成生产库）
    _root_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_hub.db")
    _resolved = _resolve_test_db_path()
    # CD-070b：派生产物根同样不得落在仓库内（chroma / wiki / audit）
    _repo = os.path.dirname(os.path.abspath(__file__))
    for _key, _sub in (("SYNC_HUB_CHROMA_PATH", "chroma_db"),
                       ("SYNC_HUB_WIKI_ROOT", "wiki"),
                       ("SYNC_HUB_AUDIT_DIR", "audit"),
                       ("SYNC_HUB_YSTORE_PATH", "ystore.db")):
        _v = os.path.abspath(os.environ.get(_key, "") or _sub)
        if _v == os.path.join(_repo, _sub):
            raise RuntimeError(
                f"[产物路径硬门] {_key} 命中仓库内生产路径: {_v!r}；conftest 应指向 tmp")
    if not _resolved or _resolved == _root_db:
        raise RuntimeError(
            "[DB 路径硬门] 测试库路径不合法或命中仓库根生产库: "
            f"{_resolved!r}（禁止 {_root_db!r}）；请检查 SYNC_HUB_DB / SYNC_HUB_CONFIG_DIR")
