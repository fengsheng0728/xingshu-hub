"""pytest 全局配置
routes.NO_AUTH 是模块级常量（import 时求值）——不统一设置时，收集顺序
决定谁先 import routes，导致 test_shared_workspace 在全量跑时 401 失败。
这里保证所有测试文件 import routes 前 NO_AUTH=1 已生效。
依赖真实 Hub 的集成测试（guard_identity/team_integration）不 import routes，
不受影响；l6_ws_auth_regression / module5 用 monkeypatch 显式覆盖。
"""
import os
import tempfile

os.environ.setdefault("SYNC_HUB_NO_AUTH", "1")
# 阶段3: 单元测试强制关影子数据底座（防污染真实 data-trunk）
os.environ.setdefault("SYNC_HUB_DATA_TRUNK", "0")
# config.yaml 是 git-ignored 本地 dev 配置（可含 auth.registration: guarded / OGA
# hub_token / notify_channels 等）——测试不得依赖其内容（否则机器相关、pre-push 门禁
# 随机红）。指向空目录 → models 回落代码默认（registration=open, 2026-09-08 实测:
# config.yaml=guarded 时 test_register_role_preserve/test_dm 等 13 failed）。
# 需 guarded 语义的专项测试(test_register_guard/test_key_issue_e2e)自起子进程并显式
# 覆盖 SYNC_HUB_CONFIG_DIR, 不受此影响。
os.environ.setdefault("SYNC_HUB_CONFIG_DIR", tempfile.mkdtemp(prefix="sync-hub-testcfg-"))


def pytest_configure(config):
    """注册自定义 marker（requires_sentence_model：需 bge 模型文件就位）"""
    config.addinivalue_line(
        "markers",
        "requires_sentence_model: 需要 sentence provider（bge 模型文件）就位，hasher 模式自动 skip",
    )
