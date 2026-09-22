# -*- coding: utf-8 -*-
"""CD-079 后半 · 黄金线形状夹具的**生成器**（dev 工具，不参与 CI）

用途：从**真实调用**里抓取契约文档承诺的响应形状，冻结到
`tests/data/contract/wire_shapes.json`。契约测试再拿它跟实际响应比对——
方向是「夹具里的键**必须仍在**实际响应里（可新增、不可删改）」，即 §8 的向后兼容承诺。

为什么要生成器而不是手编夹具：手编会把「我以为返回什么」写成契约，
生成器给出的才是「实际返回什么」。产品改了响应形状 → 重跑本脚本会看到 diff，
**要么**夹具升级（兼容，新增键）、**要么**这是破坏性变更（删/改键）→ 必须走版本登记。

用法：python scripts/contract_capture.py          # 打印 + 覆盖夹具
      python scripts/contract_capture.py --check  # 只比对，不写（CI 不用，给人看）
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "data", "contract", "wire_shapes.json")

# ── 隔离：临时 config / 临时库 / 打开 DB 硬门（绝不碰仓库根的生产库）──
_tmp = tempfile.mkdtemp(prefix="contract-capture-")
_cfg = os.path.join(_tmp, "config")
os.makedirs(_cfg, exist_ok=True)
with open(os.path.join(_cfg, "config.yaml"), "w", encoding="utf-8") as f:
    f.write("database:\n  path: %s\n" % os.path.join(_tmp, "cap.db").replace("\\", "/"))
os.environ.setdefault("SYNC_HUB_CONFIG_DIR", _cfg)
os.environ["SYNC_HUB_DB_GUARD"] = "1"
os.environ.setdefault("SYNC_HUB_NO_AUTH", "1")
os.environ.setdefault("SYNC_HUB_DATA_TRUNK", "0")
os.environ.setdefault("SYNC_HUB_CHROMA_PATH", os.path.join(_tmp, "chroma"))
os.environ.setdefault("SYNC_HUB_WIKI_ROOT", os.path.join(_tmp, "wiki"))
os.environ.setdefault("SYNC_HUB_AUDIT_DIR", os.path.join(_tmp, "audit"))
os.environ.setdefault("SYNC_HUB_YSTORE_PATH", os.path.join(_tmp, "ystore.db"))

sys.path.insert(0, ROOT)

import asyncio  # noqa: E402

import db  # noqa: E402
import envelope  # noqa: E402
import models  # noqa: E402
from models import AgentRegistration, MemoryEntry, TaskCreate  # noqa: E402
from routes_memory import MemorySearchRequest  # noqa: E402  （该请求模型定义在路由模块里，不在 models.py）


def shape(obj):
    """把 JSON 值压成「键 → 类型名」的形状描述（列表取首元素代表）。"""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return ["<list>", shape(obj[0]) if obj else None]
    if obj is None:
        return "null"
    return type(obj).__name__


async def capture():
    db.init_db()
    import hub_core
    hub = hub_core.hub

    out = {}

    # 1) 注册响应
    reg = await hub.register(AgentRegistration(agent_id="contract-a", agent_name="契约夹具", role="worker"))
    out["register"] = shape(reg)

    # 2) 记忆写入 / 列表
    stored = await hub.store_memory("contract-a", MemoryEntry(memory_key="contract-mem", content="夹具内容", kind="fact"))
    out["memory_store"] = shape(stored)
    out["memory_list"] = shape(hub.get_memories("contract-a", ""))

    # 3) 记忆版本历史 / 回滚（CD-056 owner-only 语义的返回形状）
    out["memory_versions"] = shape(await hub.get_memory_versions("contract-mem", "contract-a"))

    # 4) 任务创建
    task = await hub.create_task(TaskCreate(task_id="contract-task-1", description="夹具任务",
                                          creator_agent_id="contract-a"))
    out["task_create"] = shape(task)

    # 5) 任务取消（业务错误也是契约的一部分）+ 记忆检索（接入方最常用的读路径）
    out["task_cancel"] = shape(await hub.cancel_task("contract-task-1", "contract-a"))
    out["memory_search"] = shape(await hub.memory_search(
        MemorySearchRequest(agent_id="contract-a", query="夹具", limit=5)))

    # 6) 信封（wire 硬契约，§3.1）
    out["envelope_dispatch"] = shape(envelope.envelope_dispatch({"method": "demo"}, session_id="s1"))
    out["envelope_hello"] = shape(envelope.envelope_hello("contract-a", "ckpt-1"))

    return out


def main():
    data = asyncio.run(capture())
    if "--check" in sys.argv:
        old = json.load(open(FIXTURE, encoding="utf-8")) if os.path.exists(FIXTURE) else {}
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("\n与夹具差异（空 = 一致）：", "一致" if old == data else "有差异，见上")
        return
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("已写出夹具：%s" % FIXTURE)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:1200])


if __name__ == "__main__":
    main()
