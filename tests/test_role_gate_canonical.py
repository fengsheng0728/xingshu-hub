# -*- coding: utf-8 -*-
"""CD-074：角色门 canonical 化（hub_token 不再被整组 403）——结构锁 + 行为矩阵。

背景（2026-09-21 生产实测）：审计页顶部弹「仅主管/店长可查审计」——因为 `routes_audit` 等 7 个模块
各自实现了 `_require_manager`，判定只读 `hub.agents[current_agent].role`；而 `get_current_agent`
对 hub_token 主体返回「请求声明的 agent_id」（通常空串）→ **控制台用 hub_token 登录时这些页面整组 403**
（审计检索/导出、知识编辑、集成管理、密钥管理、N1 审批队列、机密词库、实体审查、任务改派…）。
共 25 处，与 CD-071（`routes_access`）同型。

本测试两层锁：
1. **结构锁**：`routes_*.py` 里不得再出现「按 role 判定后 raise 403」的老写法（无豁免清单，全清）；
2. **行为矩阵**：canonical 门 `routes_common.require_role` 对 hub_token 一律放行（含 orchestrator-only
   档位），对 worker 一律 403，manager 按档位判定，DB（agents 表）是真相源。
"""
import os
import re
import sqlite3
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod  # noqa: E402
from models import CONFIG  # noqa: E402
from routes_common import PRIVILEGED_ROLES, require_role  # noqa: E402
from routes_gateway import set_mcp_principal  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 1. 结构锁：老写法必须为 0 ──

def test_no_legacy_role_gate_left_in_routes():
    """扫描 routes_*.py：`hub.agents.get(current_agent…)` 后紧跟 `raise HTTPException(403)` = 老门。

    这类写法在 hub_token 调用方会 403（current_agent 为空串）——CD-071/CD-074 的根因。
    新写法一律走 `require_role(...)`；若确需 role 值只作文案，不得紧随 403 raise。
    """
    offenders = []
    for fn in sorted(os.listdir(ROOT)):
        if not re.match(r"routes.*\.py$", fn):
            continue
        txt = open(os.path.join(ROOT, fn), encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"hub\.agents\.get\(current_agent", txt):
            window = txt[m.start():m.start() + 320]
            if "raise HTTPException(status_code=403" in window:
                line = txt[:m.start()].count("\n") + 1
                offenders.append(f"{fn}:{line}")
    assert offenders == [], f"仍有按 role 判定后 403 的老门（hub_token 会整组 403）: {offenders}"


def test_routes_use_canonical_helper():
    """七个曾中招的模块都必须真的调用 canonical 门（防"删了老门但没接新门"）。"""
    for fn in ("routes_audit.py", "routes_integrations.py", "routes_keys.py",
               "routes_knowledge.py", "routes_n1.py", "routes_pipeline.py", "routes_tasks.py"):
        txt = open(os.path.join(ROOT, fn), encoding="utf-8", errors="replace").read()
        assert "require_role(" in txt, f"{fn} 未接 canonical 角色门"


# ── 2. 行为矩阵 ──

@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rolegate.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO agents (agent_id, role) VALUES ('w1','worker')")
    conn.execute("INSERT INTO agents (agent_id, role) VALUES ('m1','manager')")
    conn.execute("INSERT INTO agents (agent_id, role) VALUES ('o1','orchestrator')")
    conn.commit(); conn.close()
    yield db_path
    set_mcp_principal(None)


def test_hub_token_always_allowed(env):
    """hub_token（部署级全权凭据，控制台登录态）→ 任意档位都放行，即使 current_agent 是空串。"""
    from auth_provider import Principal
    set_mcp_principal(Principal(auth_mode="hub_token", subject_id="__hub__"))
    for roles in (PRIVILEGED_ROLES, ("orchestrator",), ("manager", "orchestrator", "worker")):
        require_role("", roles=roles, detail="x", no_auth=False)   # 不抛即通过


def test_worker_denied_manager_by_tier(env):
    set_mcp_principal(None)
    with pytest.raises(HTTPException) as e:
        require_role("w1", detail="仅主管/店长可访问", no_auth=False)
    assert e.value.status_code == 403 and e.value.detail == "仅主管/店长可访问"
    require_role("m1", detail="x", no_auth=False)                     # manager 在档位内
    require_role("o1", detail="x", no_auth=False)                     # orchestrator 在档位内
    with pytest.raises(HTTPException) as e2:                          # orchestrator-only 档位：manager 不放行
        require_role("m1", roles=("orchestrator",), detail="仅店长", no_auth=False)
    assert e2.value.status_code == 403


def test_db_is_truth_source_and_caller_map_is_honoured(env):
    """角色来源：调用方传入的 agents 映射（各路由模块的 hub 符号，测试会 patch）与 DB 双查。"""
    set_mcp_principal(None)
    require_role("m1", detail="x", no_auth=False)                      # 仅靠 DB 即放行
    fake = {"x9": {"role": "manager"}}                                 # DB 无此人，但调用方映射说 manager
    require_role("x9", agents=fake, detail="x", no_auth=False)
    with pytest.raises(HTTPException):
        require_role("w1", agents=fake, detail="x", no_auth=False)


def test_no_auth_short_circuits(env):
    """NO_AUTH（测试/开发态）不拦——两侧都为真才跳过（调用方副本与 routes_common 取与）。"""
    set_mcp_principal(None)
    require_role("w1", no_auth=True)        # 跳过
    with pytest.raises(HTTPException):
        require_role("w1", no_auth=False)   # 明确要求判定时不跳
