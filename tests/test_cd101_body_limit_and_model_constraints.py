"""CD-101：请求体大小上限（413）+ Pydantic 请求模型约束边界。

中间件口径（routes.py TokenAuthMiddleware）：
- 快路径按 Content-Length 预检，超限 413；无头/非法头不拦（下游解析兜底）。
- 默认档 CONFIG.MAX_BODY_BYTES（2MB）；大文本端点（hub-agent chat / wiki import）
  按路径放行到 CONFIG.MAX_BODY_BYTES_LARGE（8MB）。
- 资源护栏与认证无关，NO_AUTH 测试态同样生效（本文件据此直测，无需起真实 Hub）。

约束取值（models.py）：以既有测试合法用例为下界核对，仅封顶不设枚举
（kind/role 等历史取值比注释宽，收窄会破坏兼容，见修复报告）。
"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import models
import routes
from models import (
    AgentRegistration,
    HubAgentConfig,
    MemoryBatchOp,
    MemoryEntry,
    SemanticSearchRequest,
    SessionHandoffRequest,
)


@pytest.fixture()
def client():
    return TestClient(routes.app)


# ============ 413 请求体上限 ============

def test_oversize_body_default_tier_413(client):
    """默认档：Content-Length > 2MB → 413（在认证/NO_AUTH 短路之前生效）。"""
    r = client.post("/api/v1/memory/search",
                    content=b"x" * (models.CONFIG.MAX_BODY_BYTES + 1),
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert "Payload Too Large" in r.json()["detail"]


def test_normal_body_passes_gate(client):
    """小请求体不受影响（不被 413 拦下；业务码不限定）。"""
    r = client.post("/api/v1/memory/search", json={"query": "q", "agent_id": "a"})
    assert r.status_code != 413


def test_large_tier_path_allows_above_default(client):
    """>2MB 但 <8MB：普通路径 413，大文本端点（hub-agent chat）放行。"""
    big = "x" * (models.CONFIG.MAX_BODY_BYTES + 1024)  # 2MB+1KB
    r_big = client.post("/api/v1/hub-agent/chat", json={"message": big})
    assert r_big.status_code != 413


def test_large_tier_path_over_8mb_413(client):
    """大文本端点超过 8MB 档同样 413。"""
    big = "x" * (models.CONFIG.MAX_BODY_BYTES_LARGE + 1024)
    r = client.post("/api/v1/wiki/import", json={"pages": {"a.md": big}})
    assert r.status_code == 413


def test_metrics_in_auth_allowlist():
    """CD-101 移交项：/metrics 免认证（Prometheus 抓取），与 /healthz 同组。"""
    assert "/metrics" in routes.AUTH_ALLOWLIST_PATHS


def test_body_limit_config_default():
    cfg = models.Config()
    assert cfg.MAX_BODY_BYTES == 2 * 1024 * 1024
    assert cfg.MAX_BODY_BYTES_LARGE == 8 * 1024 * 1024


def test_body_limit_config_yaml_override(tmp_path, monkeypatch):
    """config.yaml server.max_body_bytes / max_body_bytes_large 可调。"""
    (tmp_path / "config.yaml").write_text(
        "server:\n  max_body_bytes: 5242880\n  max_body_bytes_large: 12582912\n",
        encoding="utf-8")
    monkeypatch.setenv("SYNC_HUB_CONFIG_DIR", str(tmp_path))
    overrides = models._load_config_from_yaml()
    assert overrides["MAX_BODY_BYTES"] == 5 * 1024 * 1024
    assert overrides["MAX_BODY_BYTES_LARGE"] == 12 * 1024 * 1024


# ============ Pydantic 约束边界 ============

def test_memory_entry_content_bound():
    MemoryEntry(memory_key="k", content="x" * 100_000)  # 边界值放行
    with pytest.raises(ValidationError):
        MemoryEntry(memory_key="k", content="x" * 100_001)


def test_memory_entry_key_bound():
    MemoryEntry(memory_key="k" * 200, content="c")
    with pytest.raises(ValidationError):
        MemoryEntry(memory_key="k" * 201, content="c")


def test_semantic_search_n_results_bound():
    SemanticSearchRequest(query="q", requester_agent_id="a", n_results=1)
    SemanticSearchRequest(query="q", requester_agent_id="a", n_results=200)
    for bad in (0, -1, 201):
        with pytest.raises(ValidationError):
            SemanticSearchRequest(query="q", requester_agent_id="a", n_results=bad)


def test_handoff_messages_bound():
    base = dict(from_agent_id="a", to_agent_id="b", local_session_id=1)
    SessionHandoffRequest(messages=[{"role": "user", "content": "hi"}] * 200, **base)
    with pytest.raises(ValidationError):
        SessionHandoffRequest(messages=[{"role": "user"}] * 201, **base)


def test_batch_op_limit_bound():
    MemoryBatchOp(action="search", query="q", limit=200)
    for bad in (0, 201):
        with pytest.raises(ValidationError):
            MemoryBatchOp(action="search", query="q", limit=bad)


def test_hub_agent_config_bounds():
    HubAgentConfig(temperature=0.0)
    HubAgentConfig(temperature=2.0)
    for bad in (-0.1, 2.1):
        with pytest.raises(ValidationError):
            HubAgentConfig(temperature=bad)
    with pytest.raises(ValidationError):
        HubAgentConfig(api_key="k" * 501)


def test_agent_registration_id_bound():
    AgentRegistration(agent_id="a" * 200, agent_name="n")
    with pytest.raises(ValidationError):
        AgentRegistration(agent_id="a" * 201, agent_name="n")
