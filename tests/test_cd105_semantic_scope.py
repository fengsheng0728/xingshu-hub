# -*- coding: utf-8 -*-
"""T1 · CD-105：semantic_search 的 scope 缺失 fail-closed 收口 验收测试（2026-09-24）

CD-100 已在 search_chunks 落地「哨兵默认值 _SCOPE_UNSET + 未声明主体上下文 →
fail-closed 封顶 metadata」；本篇验收同型缺口 semantic_search。

四种 scope 取值必须可区分（缺一不可）：
  1. **未传**（默认 _SCOPE_UNSET）→ fail-closed：封顶 metadata，正文绝不出门，
     且必须与「显式 None」形成对照（否则断言没打中判定）；
  2. **显式 scope=None** → 行为与修复前一致（普通 api_key 主体、无 cap，正文可返回）；
  3. **internal=True** → 显式声明「进程内全信主体」，与 scope=None 等价（逃生门）；
  4. **显式 dict**（{"level_cap": "metadata"}）→ cap 生效，与默认 fail-closed 同口径。

配方（照本仓既有做法，不发明新的——对齐 test_chunk_stitch CD-100 篇 +
test_cd052 _MiniHub）：
  - 不绑端口、不起真实 Hub、不依赖真实 ChromaDB：
    `hub._chroma_collection = None` 走 SQLite 关键词降级链（_sqlite_keyword_search），
    直接实例化 DisclosureEngine 并直调协程（asyncio.run）；
  - 临时库：monkeypatch.setattr(CONFIG, "DB_PATH", str(tmp_path / "x.db")) → db.init_db()；
  - conftest 全局 SYNC_HUB_NO_AUTH=1 —— 本篇不测鉴权，级别差异断言全部打在
    「scope 语义」（owner 自查规则 1 本判 FULL，只有 scope 能把它压到 metadata），
    与 NO_AUTH 无关。
"""
import asyncio
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_mod  # noqa: E402
from disclosure import DisclosureEngine  # noqa: E402
from models import CONFIG, SemanticSearchRequest  # noqa: E402

# 唯一哨兵串：正文级结果必须含它，metadata 级结果必须不含它
SENTINEL = "CD105哨兵串-明文不得出metadata级结果-7f3a9cE1"

# 与 hub_core._load_disclosure_policy 默认值对齐（同 test_cd052._policy_defaults）
_POLICY_DEFAULTS = {
    "department_peer_visibility": False,
    "default_manager_level": "summary",
    "orchestrator_max_level": "full",
    "allow_peer_disclosure": True,
}


class _MiniHub:
    """DisclosureEngine + _sqlite_keyword_search 所需的最小 hub 面。

    _chroma_collection = None → semantic_search 走 SQLite 关键词降级链，
    不触碰真实 ChromaDB。
    """

    def __init__(self, db_path):
        self._db_path = str(db_path)
        self._chroma_collection = None
        self._embedding_model = None
        self.agents = {}
        self._disclosure_policy = dict(_POLICY_DEFAULTS)
        self._index_rebuilding = False

    def _db(self):
        return sqlite3.connect(self._db_path)

    async def _ensure_embedding_model(self):
        return None


def _insert_memory(db_path, memory_id, owner, content, disclosure_level="full"):
    """往 memory_pool 灌一条（口径对齐 tests/test_kb_unified_retrieval._insert_memory）"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO memory_pool
           (memory_id, owner_agent_id, memory_key, content, summary, importance,
            tags, kind, confidence, source_type, disclosure_level, disclosure_scope,
            allowed_viewers, created_at, updated_at, trust_level, source_agent_id)
           VALUES (?, ?, ?, ?, ?, 1.0, '[]', 'fact', 1.0, 'user', ?, 'manager',
                   '[]', '2026-09-24T00:00:00+00:00', '2026-09-24T00:00:00+00:00',
                   'internal', ?)""",
        (memory_id, owner, memory_id[:8], content, content[:200],
         disclosure_level, owner),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立 sqlite 临时库 + 最小 hub + DisclosureEngine（无 ChromaDB、无端口）"""
    db_path = str(tmp_path / "cd105.db")
    monkeypatch.setattr(CONFIG, "DB_PATH", db_path)
    db_mod.init_db()
    hub = _MiniHub(db_path)
    engine = DisclosureEngine(hub)
    # owner 自查（规则 1 → FULL）——只有 scope 能把 FULL 压到 metadata，
    # 断言打的是 scope 语义，与 conftest 的 SYNC_HUB_NO_AUTH 无关
    hub.agents["owner-a"] = {"role": "worker", "managed_agents": [],
                             "disclosure_policy": {}, "department": ""}
    content = f"关于项目进展的机密正文，含哨兵 {SENTINEL}，不得被 metadata 级泄露。"
    _insert_memory(db_path, "m-cd105-0001", "owner-a", content, disclosure_level="full")
    req = SemanticSearchRequest(query="项目进展", requester_agent_id="owner-a",
                                n_results=5)
    return SimpleNamespace(hub=hub, engine=engine, req=req, db_path=db_path,
                           content=content)


def _run(coro):
    return asyncio.run(coro)


# ── 1. 未传 scope → fail-closed metadata 封顶；必须与「显式 None」形成对照 ──

def test_scope_default_fail_closed_contrast_with_none(env):
    """CD-105：直调 semantic_search 不声明主体上下文 → metadata 封顶（无正文）；
    与显式 scope=None 的结果必须可区分（对照组证明断言打中判定）"""
    res_default = _run(env.engine.semantic_search(env.req))
    res_none = _run(env.engine.semantic_search(env.req, scope=None))
    res_cap = _run(env.engine.semantic_search(
        env.req, scope={"level_cap": "metadata"}))

    # （1）未传 scope：fail-closed —— 仍列出命中，但只给 metadata 级，正文绝不出门
    assert res_default["memories"], "metadata 级仍应列出命中（同 CD-100 search_chunks 口径）"
    for hit in res_default["memories"]:
        assert hit["disclosure_level"] == "metadata", \
            f"未传 scope 必须封顶 metadata: {hit}"
        assert SENTINEL not in hit["content"], \
            f"fail-closed：未声明主体不得给正文: {hit['content']!r}"
        assert env.content not in hit["content"], \
            f"fail-closed：不得返回整段正文: {hit['content']!r}"

    # （2）对照组：显式 None（普通 api_key 主体）→ 正文可返回（行为同修复前）
    assert res_none["memories"], "显式 None 应命中"
    assert any(SENTINEL in h["content"] for h in res_none["memories"]), \
        f"显式 None 必须返回正文（对照组）: {res_none['memories']}"
    assert any(h["disclosure_level"] == "full" for h in res_none["memories"]), \
        f"owner 自查（规则1）显式 None 应判 full: {res_none['memories']}"

    # （3）对照成立：默认 ≠ 显式 None（否则断言没打中判定）
    assert res_default["memories"] != res_none["memories"], \
        "默认（未传）与显式 None 必须可区分"
    assert [h["content"] for h in res_default["memories"]] != \
           [h["content"] for h in res_none["memories"]], \
        "默认与显式 None 的 content 必须不同"

    # （4）默认与显式 {"level_cap": "metadata"} 同口径（fail-closed 即 metadata cap）
    assert [h["disclosure_level"] for h in res_default["memories"]] == \
           [h["disclosure_level"] for h in res_cap["memories"]], \
        "默认 fail-closed 应与显式 metadata cap 同口径"
    assert [h["content"] for h in res_default["memories"]] == \
           [h["content"] for h in res_cap["memories"]], \
        "默认 fail-closed 应与显式 metadata cap 同 content"


# ── 2. 显式 scope=None → 行为与修复前一致（正文可返回）──

def test_scope_explicit_none_keeps_pre_fix_behavior(env):
    res = _run(env.engine.semantic_search(env.req, scope=None))
    assert res["memories"], "显式 None 应命中"
    hit = res["memories"][0]
    assert hit["disclosure_level"] == "full", \
        f"owner 自查（规则1）显式 None 应判 full（同修复前）: {hit}"
    assert SENTINEL in hit["content"], \
        f"显式 None 必须返回正文（向后兼容关键点）: {hit['content']!r}"


# ── 3. internal=True → 与 scope=None 等价（进程内全信主体逃生门）──

def test_internal_true_equivalent_to_none(env):
    res_int = _run(env.engine.semantic_search(env.req, internal=True))
    res_none = _run(env.engine.semantic_search(env.req, scope=None))
    assert res_int["memories"], "internal=True 应命中"
    assert any(SENTINEL in h["content"] for h in res_int["memories"]), \
        f"internal=True 必须返回正文（进程内全信主体）: {res_int['memories']}"
    assert any(h["disclosure_level"] == "full" for h in res_int["memories"]), \
        f"internal=True 应与 scope=None 同判 full: {res_int['memories']}"
    assert [h["disclosure_level"] for h in res_int["memories"]] == \
           [h["disclosure_level"] for h in res_none["memories"]], \
        "internal=True 与显式 None 的级别序列必须一致"
    assert [h["content"] for h in res_int["memories"]] == \
           [h["content"] for h in res_none["memories"]], \
        "internal=True 与显式 None 的 content 必须一致"
    # 且与默认（未传）可区分
    res_default = _run(env.engine.semantic_search(env.req))
    assert res_int["memories"] != res_default["memories"], \
        "internal=True 与默认（未传）必须可区分"


# ── 4. 显式 {"level_cap": "metadata"} → cap 生效 ──

def test_scope_explicit_dict_cap_applies(env):
    res = _run(env.engine.semantic_search(env.req, scope={"level_cap": "metadata"}))
    assert res["memories"], "cap 生效后仍应列出命中"
    for hit in res["memories"]:
        assert hit["disclosure_level"] == "metadata", \
            f"level_cap=metadata 必须封顶（owner 自查规则1 本判 full）: {hit}"
        assert SENTINEL not in hit["content"], \
            f"cap 后不得给正文: {hit['content']!r}"


# ── 5. 表格驱动：四种取值一次分清 ──

@pytest.mark.parametrize("mode,expect_level,expect_body", [
    ("default", "metadata", False),
    ("none", "full", True),
    ("internal", "full", True),
    ("cap_metadata", "metadata", False),
])
def test_scope_modes_matrix(env, mode, expect_level, expect_body):
    """四种 scope 取值 × (级别, 是否给正文) —— 判定矩阵"""
    if mode == "default":
        res = _run(env.engine.semantic_search(env.req))
    elif mode == "none":
        res = _run(env.engine.semantic_search(env.req, scope=None))
    elif mode == "internal":
        res = _run(env.engine.semantic_search(env.req, internal=True))
    else:
        res = _run(env.engine.semantic_search(
            env.req, scope={"level_cap": "metadata"}))
    assert res["memories"], f"{mode}: 应命中"
    for hit in res["memories"]:
        assert hit["disclosure_level"] == expect_level, f"{mode}: {hit}"
        has_body = SENTINEL in hit["content"]
        assert has_body is expect_body, \
            f"{mode}: 正文可见性应为 {expect_body}，实际 {has_body}: {hit['content']!r}"


# ── 6. 转发层（hub_mixins.memory.MemoryMixin.semantic_search）签名与透传 ──

def test_memory_mixin_forwarding_passes_internal(env):
    """转发层必须保留 internal 逃生门 + 哨兵默认值（默认 None 会吞掉 fail-closed）"""
    from hub_mixins.memory import MemoryMixin

    class _Stub:
        disclosure = env.engine

    res_default = _run(MemoryMixin.semantic_search(_Stub(), env.req))
    assert res_default["memories"], "转发层默认应命中"
    for hit in res_default["memories"]:
        assert hit["disclosure_level"] == "metadata", \
            f"转发层默认（未传）必须 fail-closed: {hit}"
        assert SENTINEL not in hit["content"], \
            f"转发层默认不得给正文: {hit['content']!r}"

    res_int = _run(MemoryMixin.semantic_search(_Stub(), env.req, internal=True))
    assert any(SENTINEL in h["content"] for h in res_int["memories"]), \
        f"转发层 internal=True 必须透传并返回正文: {res_int['memories']}"

    res_none = _run(MemoryMixin.semantic_search(_Stub(), env.req, scope=None))
    assert any(SENTINEL in h["content"] for h in res_none["memories"]), \
        f"转发层显式 None 必须透传并返回正文: {res_none['memories']}"
