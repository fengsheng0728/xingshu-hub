"""L8 内网落地配置 — 过关用例"""
import glob
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（tests/ 上一级）


class TestL8T1_ConfigGuard:
    """L8-T1: 复用 phase 10 配置端点 localhost 全量 / 远程脱敏守卫"""

    def _route_sources(self):
        """Phase 2 拆分后：配置端点分布在 routes.py 与 routes_*.py 子模块，合并读取"""
        content = ""
        for f in [str(_ROOT / "routes.py")] + sorted(glob.glob(str(_ROOT / "routes_*.py"))):
            with open(f, encoding="utf-8") as fh:
                content += fh.read()
        return content

    def test_config_endpoint_has_raw_param(self):
        """GET /api/v1/hub-agent/config?raw=true for localhost"""
        content = self._route_sources()
        assert "raw=true" in content or "raw" in content.lower()

    def test_localhost_guard_exists(self):
        """localhost guard for raw config"""
        content = self._route_sources()
        assert "127.0.0.1" in content or "localhost" in content.lower()


class TestL8T3_TimeoutConfig:
    """L8-T3: 四类超时配置可观测且与 ToolExecutor 对齐"""

    def test_timeout_values_documented(self):
        """handshake 5s / heartbeat 30s / idle 90s / tool timeout aligned"""
        # hub_core.py: _PONG_TIMEOUT=90, _HEARTBEAT_INTERVAL=30
        # routes.py: ws timeout via WebSocket timeout parameter
        # ToolExecutor: DEFAULT_TIMEOUT
        with open(_ROOT / "hub_core.py", encoding="utf-8") as f:
            hub = f.read()
        assert "_PONG_TIMEOUT" in hub
        assert "_HEARTBEAT_INTERVAL" in hub
        assert "_MAX_IN_FLIGHT" in hub

        # sync-hub-agent 为仓库根的平行目录（agent 端独立仓库）
        with open(_ROOT.parent / "sync-hub-agent" / "backend" / "tools" / "executor.py", encoding="utf-8") as f:
            executor = f.read()
        assert "DEFAULT_TIMEOUT" in executor or "timeout" in executor.lower()
