"""D-5 3-5a: 向量栈可选化 — chromadb/langchain 缺失时的降级启动与降级响应。

全部用例在子进程中执行（同进程屏蔽 sys.modules/meta_path 会污染其他测试）。
阻断方式：meta path finder 对目标包抛 ModuleNotFoundError（模拟包未安装）。
"""
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BLOCKER_TEMPLATE = '''
import sys, importlib.abc

class _Blocker(importlib.abc.MetaPathFinder):
    PREFIXES = {prefixes!r}

    def find_spec(self, fullname, path, target=None):
        if fullname in self.PREFIXES or any(
                fullname.startswith(p + ".") for p in self.PREFIXES):
            raise ModuleNotFoundError(
                "No module named '" + fullname + "' (blocked by test)")
        return None

sys.meta_path.insert(0, _Blocker())
'''


def _blocker(*prefixes):
    return _BLOCKER_TEMPLATE.format(prefixes=prefixes)


def _run(script, timeout=240):
    env = os.environ.copy()
    env.setdefault("SYNC_HUB_NO_AUTH", "1")
    env.setdefault("SYNC_HUB_DATA_TRUNK", "0")
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, env=env,
    )


def test_import_hub_core_without_chromadb():
    """L2 红线：阻断 chromadb 后 import hub_core 必须成功（exit 0）。"""
    r = _run(_blocker("chromadb") + "\nimport hub_core\nprint('IMPORT_OK')\n")
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "IMPORT_OK" in r.stdout
    # 降级 warning 必须出现（logging 走 stderr）
    assert "chromadb" in (r.stdout + r.stderr)


def test_import_routes_app_without_chromadb():
    """阻断 chromadb 后 from routes import app 必须成功（沿用 T0-1 import 红线口径）。"""
    r = _run(_blocker("chromadb") + "\nfrom routes import app\nprint('IMPORT_OK')\n")
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "IMPORT_OK" in r.stdout


def test_synchub_degraded_without_chromadb():
    """阻断 chromadb 下构造 SyncHub：_chroma_collection 为 None 且出现降级 warning。"""
    script = _blocker("chromadb") + '''
import logging

records = []

class _H(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())

_logger = logging.getLogger("xingshu.hub_core")
_logger.addHandler(_H())
_logger.setLevel(logging.WARNING)

from hub_core import SyncHub

hub = SyncHub()
assert hub._chroma_collection is None, "chromadb 阻断下 _chroma_collection 必须为 None"
assert hub._chroma_client is None, "chromadb 阻断下 _chroma_client 必须为 None"
assert any("chromadb" in m for m in records), f"缺少降级 warning: {records}"
print("DEGRADE_OK")
'''
    r = _run(script)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "DEGRADE_OK" in r.stdout


def test_full_stack_unchanged():
    """不阻断（正常环境）：import hub_core 正常、ChromaDB 集合可建 — 分层未破坏正常路径。"""
    script = '''
import hub_core

assert hub_core.chromadb is not None, "正常环境 chromadb 应可导入"
assert hub_core.hub._chroma_collection is not None, "正常环境 ChromaDB 集合应可建"
print("FULL_OK")
'''
    r = _run(script)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "FULL_OK" in r.stdout


def test_hub_agent_endpoints_degraded_without_langchain():
    """阻断 langchain 后调 hub-agent 端点 → 明确降级响应（status=degraded）而非崩溃。"""
    script = _blocker("langchain", "langchain_core", "langchain_openai") + '''
import asyncio

from routes_hubagent import (
    api_hub_agent_chat, api_hub_agent_chat_history, ChatRequest,
)

r = asyncio.run(api_hub_agent_chat(ChatRequest(message="hello")))
assert isinstance(r, dict) and r.get("status") == "degraded", r
assert "langchain" in r.get("error", ""), r

r2 = asyncio.run(api_hub_agent_chat_history("default"))
assert isinstance(r2, dict) and r2.get("status") == "degraded", r2

print("HUBAGENT_DEGRADED_OK")
'''
    r = _run(script)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "HUBAGENT_DEGRADED_OK" in r.stdout
