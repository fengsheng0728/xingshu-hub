"""T8 CD-030：shared_docs 内联 DDL 与 alembic 0002 一致性（2026-09-19）

背景：SHARED_DOCS_DDL 原只有 9 列，而 create_doc() 的 INSERT 直写
trust_level / source_agent_id / tainted_at（alembic 0002 增量列）。
未跑 `alembic upgrade head` 的全新库上 create_doc 直接
OperationalError: table shared_docs has no column named trust_level。
本文件固化：内联 DDL 与 0002 逐字对齐 / 全新库开箱可用 / _init_db 幂等。
不跑真实 Hub、不绑端口，全程 tmp_path 独立 sqlite 库。
"""
import asyncio
import importlib.util
import os
import sqlite3

from shared_workspace import SharedWorkspace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_0002 = os.path.join(
    ROOT, "migrations", "alembic", "versions", "0002_freeze_incremental_alters.py")

# 内联 DDL 原有 9 列（0002 增量 5 列中 visibility/allowed_agents 已内联在原 DDL）
DDL_ORIGINAL_9 = [
    "doc_id", "title", "created_by", "created_at", "updated_at",
    "archived", "block_count", "visibility", "allowed_agents",
]


def _load_0002():
    """加载 alembic 0002 迁移模块（只读 ADD_COLUMNS，不执行 upgrade）"""
    spec = importlib.util.spec_from_file_location("mig_0002", MIGRATION_0002)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _table_info(db_path):
    """PRAGMA table_info → {列名: {type, notnull, dflt}}，dict 序 = cid 序"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("PRAGMA table_info(shared_docs)").fetchall()
    conn.close()
    return {r[1]: {"type": r[2], "notnull": r[3], "dflt": r[4]} for r in rows}


def _mk_ws(tmp_path):
    db_path = str(tmp_path / "shared_ddl.db")
    store_dir = str(tmp_path / "store")
    return SharedWorkspace(db_path, store_dir=store_dir), db_path


# DDL-1 列集合/类型/默认值与 alembic 0002 对齐
def test_ddl_columns_match_alembic_0002(tmp_path):
    ws, db = _mk_ws(tmp_path)
    asyncio.run(ws._init_db())
    info = _table_info(db)

    add_cols = _load_0002().ADD_COLUMNS["shared_docs"]  # [(name, decl), ...] 追加顺序
    # 列集合 = 原 9 列 ∪ 0002 增量 5 列
    assert set(info) == set(DDL_ORIGINAL_9) | {name for name, _ in add_cols}
    # 列序 = 原 9 列 + 0002 增量中尚未内联的列（保持 0002 追加顺序）
    expected_order = DDL_ORIGINAL_9 + [
        name for name, _ in add_cols if name not in DDL_ORIGINAL_9]
    assert list(info) == expected_order
    # 关键列定义逐字对齐 0002（type / notnull / dflt_value）
    assert info["trust_level"] == {"type": "TEXT", "notnull": 1, "dflt": "'internal'"}
    assert info["source_agent_id"] == {"type": "TEXT", "notnull": 0, "dflt": "''"}
    assert info["tainted_at"] == {"type": "TEXT", "notnull": 0, "dflt": "''"}
    # 原有列不受新增列影响（抽查主键与非空约束）
    assert info["doc_id"]["type"] == "TEXT"
    assert info["title"]["notnull"] == 1
    assert info["allowed_agents"]["dflt"] == "'[]'"


# DDL-2 全新库（只走 _init_db，不跑 alembic）create_doc 开箱可用
def test_create_doc_on_fresh_db_without_alembic(tmp_path):
    ws, db = _mk_ws(tmp_path)

    async def main():
        await ws.start()  # start() 内部即 _init_db()，不跑任何 alembic 迁移
        try:
            return await ws.create_doc(title="验收文档", created_by="agent-t8")
        finally:
            await ws.stop()

    result = asyncio.run(main())
    assert result["doc_id"].startswith("doc-")
    assert result["title"] == "验收文档"
    # create_doc 返回 dict 仅 doc_id/title/created_by（白名单禁改类实现），
    # trust 三列改为直接查库验证真实落库值
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM shared_docs WHERE doc_id = ?", (result["doc_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["trust_level"] == "internal"
    assert row["source_agent_id"] == "agent-t8"
    assert row["tainted_at"]


# DDL-3 幂等：_init_db 连跑两次不抛错、列集合不变
def test_init_db_idempotent(tmp_path):
    ws, db = _mk_ws(tmp_path)
    asyncio.run(ws._init_db())
    first = _table_info(db)
    asyncio.run(ws._init_db())
    second = _table_info(db)
    assert first == second
    assert len(first) == 12  # 原 9 列 + trust_level/source_agent_id/tainted_at
