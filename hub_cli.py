"""
hub-cli — 星枢 Sync Hub 运维 CLI（O2 备份与恢复闭环，2026-08-05）
====================================================================

命令：
  python hub_cli.py backup  --out <目录> [--db <sqlite路径>] [--chroma <目录>]
  python hub_cli.py restore --from <备份目录> [--db <sqlite路径>] [--chroma <目录>] [--verify]
  python hub_cli.py verify  --from <备份目录>
  python hub_cli.py agent create --id <agent_id> --name <名称> [--role worker] [--db <sqlite路径>]
      （OGA guarded 受管注册：管理员预签发建号，幂等不覆盖，api_key 仅此一次可见）

设计（契约 O2 冻结）：
- backup ：
  1. SQLite 用 VACUUM INTO 在线热备（一致性快照，不停机）
  2. 备份前在 SQLite 写入 backup_marker（id/ts），ChromaDB 拷贝完成后
     在 chroma 备份目录写同一 marker 的 .backup_marker 文件
  3. ChromaDB 目录整体拷贝（flush 依赖 PersistentClient 的 WAL/索引文件一致性，
     marker 对齐用于恢复时检测"向量索引与记忆池对不上"）
- restore --verify：
  1. 恢复前自动备份当前库（防误操作，冷备兜底）
  2. 恢复 SQLite + ChromaDB（若备份含）
  3. 校验：marker 两侧一致 / 表计数 / 审计 hash chain（复用 audit_chain.verify_all）
     / ChromaDB degraded 检测（缺目录或 marker 不一致 → 标记 degraded，
     由 Hub 启动时复用 CD-016 降级机制触发索引重建）
- verify ：只读校验备份目录完整性，不落盘

依赖：仅标准库（sqlite3 / shutil / json / argparse）+ audit_chain（verify 用）。
VACUUM INTO 要求 sqlite >= 3.27（本机 3.50.4 OK）。
"""

from __future__ import annotations

import logging
logger = logging.getLogger("xingshu.hub_cli")

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

MARKER_TABLE = "backup_markers"
MARKER_FILE = ".backup_marker"
# 业务表（备份完整性校验用）——排除 FTS 影子表与内部表
EXCLUDE_TABLES = ("memory_pool_fts", "memory_pool_fts_config", "memory_pool_fts_data",
                  "memory_pool_fts_docsize", "memory_pool_fts_idx",
                  "alembic_version", "sqlite_sequence", "backup_markers")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def _ensure_marker_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} ("
        "marker_id TEXT PRIMARY KEY, created_at TEXT)"
    )
    conn.commit()


def _business_tables(db_path: str) -> list:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return [r["name"] for r in rows
                if r["name"] not in EXCLUDE_TABLES
                and not r["name"].startswith("memory_pool_fts")]
    finally:
        conn.close()


# ================= backup =================


def cmd_backup(out_dir: str, db_path: str, chroma_path: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    ts = _now()
    marker_id = f"bk-{ts}"
    result = {"marker": marker_id, "ts": ts}

    # 1) SQLite 在线热备（VACUUM INTO 一致性快照）
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"数据库不存在: {db_path}")
    db_backup = os.path.join(out_dir, f"sync_hub.{ts}.db")
    # VACUUM INTO 生成一致性快照文件
    conn = _connect(db_path)
    try:
        # 先写 marker（备份内容的一部分，恢复后可校验）
        _ensure_marker_table(conn)
        conn.execute(
            f"INSERT OR REPLACE INTO {MARKER_TABLE} (marker_id, created_at)"
            " VALUES (?, ?)", (marker_id, _utc_iso()))
        conn.commit()
        # VACUUM INTO 到目标文件
        conn.execute(
            f"VACUUM INTO '{db_backup.replace(chr(39), chr(39)*2)}'"
        )
    finally:
        conn.close()
    result["sqlite"] = db_backup
    result["sqlite_bytes"] = os.path.getsize(db_backup)

    # 2) ChromaDB 目录拷贝（若存在）+ marker 文件
    chroma_backup = ""
    if chroma_path and os.path.isdir(chroma_path):
        chroma_backup = os.path.join(out_dir, "chroma_db")
        # 先拷目录（可能带 .backup_marker 旧文件，拷完覆盖）
        if os.path.exists(chroma_backup):
            shutil.rmtree(chroma_backup, ignore_errors=True)
        shutil.copytree(chroma_path, chroma_backup)
        # marker 文件写入 chroma 备份目录
        with open(os.path.join(chroma_backup, MARKER_FILE), "w", encoding="utf-8") as f:
            f.write(json.dumps({"marker_id": marker_id, "ts": ts}))
        result["chroma"] = chroma_backup
    else:
        result["chroma"] = ""

    # 3) manifest
    manifest = {
        "marker_id": marker_id,
        "created_at": _utc_iso(),
        "sqlite": os.path.basename(db_backup),
        "chroma": os.path.basename(chroma_backup) if chroma_backup else "",
        "tables": _business_tables(db_backup),
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    result["manifest"] = manifest_path
    result["tables"] = len(manifest["tables"])
    return result


# ================= restore =================


def _load_manifest(backup_dir: str) -> dict:
    mp = os.path.join(backup_dir, "manifest.json")
    if not os.path.exists(mp):
        raise FileNotFoundError(f"备份目录缺少 manifest.json: {backup_dir}")
    with open(mp, "r", encoding="utf-8") as f:
        return json.load(f)


def _verify_backup(backup_dir: str) -> dict:
    """校验备份完整性（只读）：marker 对齐 / 表计数 / SQLite 可打开 / hash chain。"""
    manifest = _load_manifest(backup_dir)
    issues = []
    checks = {}

    db_backup = os.path.join(backup_dir, manifest.get("sqlite", ""))
    if not os.path.exists(db_backup):
        issues.append(f"SQLite 备份缺失: {db_backup}")
    else:
        # SQLite 可打开 + 表计数
        try:
            conn = _connect(db_backup)
            try:
                tables = _business_tables(db_backup)
                checks["tables"] = len(tables)
                if manifest.get("tables") and isinstance(manifest["tables"], list) \
                        and len(tables) != len(manifest["tables"]):
                    issues.append(
                        f"表计数不符: manifest={len(manifest['tables'])} 实际={len(tables)}")
                # marker 一致性：SQLite 内 marker
                row = conn.execute(
                    f"SELECT marker_id FROM {MARKER_TABLE} ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                sqlite_marker = row["marker_id"] if row else ""
                checks["sqlite_marker"] = sqlite_marker
                if sqlite_marker != manifest.get("marker_id"):
                    issues.append(
                        f"SQLite marker 与 manifest 不符: {sqlite_marker} != {manifest.get('marker_id')}")
            finally:
                conn.close()
        except Exception as e:
            issues.append(f"SQLite 备份损坏: {e}")

    # ChromaDB marker 对齐
    chroma_backup = os.path.join(backup_dir, "chroma_db")
    if manifest.get("chroma"):
        if not os.path.isdir(chroma_backup):
            issues.append("manifest 声明 chroma_db 但目录缺失")
        else:
            mf = os.path.join(chroma_backup, MARKER_FILE)
            if os.path.exists(mf):
                with open(mf, "r", encoding="utf-8") as f:
                    cm = json.load(f)
                checks["chroma_marker"] = cm.get("marker_id", "")
                if cm.get("marker_id") != manifest.get("marker_id"):
                    issues.append("ChromaDB marker 与 manifest 不一致（向量索引可能过期）")
            else:
                issues.append("ChromaDB 备份缺少 .backup_marker（无法对齐校验）")
    else:
        checks["chroma_marker"] = ""

    # 审计 hash chain 校验（S2 复用）
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from audit_chain import verify_all
        jsonl_dir = os.path.join(backup_dir, "audit")
        jsonl_files = {}
        for name in ("memory_pool.jsonl", "transport.jsonl"):
            p = os.path.join(jsonl_dir, name)
            if os.path.exists(p):
                jsonl_files[name] = p
        res = verify_all(db_backup, jsonl_files)
        checks["hash_chain"] = res
        if not res["valid"]:
            issues.append("审计 hash chain 校验失败")
    except Exception as e:
        checks["hash_chain"] = {"valid": False, "error": str(e)}
        issues.append(f"hash chain 校验异常: {e}")

    return {"valid": len(issues) == 0, "issues": issues, "checks": checks,
            "manifest": manifest}


def cmd_restore(backup_dir: str, db_path: str, chroma_path: str,
                do_verify: bool = True) -> dict:
    manifest = _load_manifest(backup_dir)

    # 0) 恢复前自动备份当前库（防误操作）
    safety_backup = os.path.join(backup_dir, "pre-restore-safety.db")
    if os.path.exists(db_path):
        try:
            conn = _connect(db_path)
            conn.execute(f"VACUUM INTO '{safety_backup.replace(chr(39), chr(39)*2)}'")
            conn.close()
        except Exception as e:
            print(f"[warn] 恢复前安全备份失败（继续）: {e}")

    # 1) 恢复 SQLite
    src_db = os.path.join(backup_dir, manifest.get("sqlite", ""))
    if not os.path.exists(src_db):
        raise FileNotFoundError(f"备份 SQLite 缺失: {src_db}")
    # 安全：目标文件先备份为 .pre-restore
    if os.path.exists(db_path):
        pre = db_path + ".pre-restore"
        try:
            shutil.copy2(db_path, pre)
        except OSError:
            pass
    shutil.copy2(src_db, db_path)
    result = {"restored_sqlite": db_path, "from": src_db}

    # 2) 恢复 ChromaDB（若备份含）
    if manifest.get("chroma"):
        src_chroma = os.path.join(backup_dir, "chroma_db")
        if os.path.isdir(src_chroma):
            if os.path.exists(chroma_path):
                pre_c = chroma_path + ".pre-restore"
                try:
                    shutil.copytree(chroma_path, pre_c, dirs_exist_ok=True)
                except OSError:
                    pass
                shutil.rmtree(chroma_path, ignore_errors=True)
            shutil.copytree(src_chroma, chroma_path)
            result["restored_chroma"] = chroma_path
        else:
            result["chroma_warning"] = "manifest 声明 chroma_db 但目录缺失，跳过"

    # 3) verify
    if do_verify:
        result["verify"] = _verify_backup(backup_dir)
    return result


# ================= agent(OGA 受管注册预签发) =================


def _api_key_expiry(now_iso: str) -> str:
    """对齐 hub_core._api_key_expiry：now + API_KEY_ROTATION_DAYS（0=不轮换则永不过期）。"""
    days = 90
    try:
        from models import CONFIG
        days = getattr(CONFIG, "API_KEY_ROTATION_DAYS", 90)
    except Exception as _exc:
        logger.debug("hub_cli silent-except @293: %s", _exc)
    if not days or days <= 0:
        return ""
    try:
        dt = datetime.fromisoformat(now_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt + timedelta(days=days)).isoformat()
    except Exception:
        return ""


def cmd_agent_create(agent_id: str, agent_name: str, role: str,
                     department: str, db_path: str) -> dict:
    """OGA: guarded 受管注册的管理员预签发建号。幂等——已存在返回 409,不 INSERT、不动任何行。
    api_key 仅此一次可见(stdout 打印),角色以本命令预置值为准(请求声明 role 无效)。
    T1-2（2026-09-09）：已迁移库（api_key_hash 列存在）只落 SHA256 hash,
    api_key 明文列写空串；未迁移老库保持旧明文写入（兼容窗口）。"""
    conn = _connect(db_path)
    try:
        try:
            row = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?",
                               (agent_id,)).fetchone()
        except sqlite3.OperationalError as e:
            return {"status": "error", "code": 500,
                    "detail": f"agents 表不可读(库未初始化?先启动一次 Hub 建 schema): {e}"}
        if row:
            return {"status": "error", "code": 409,
                    "detail": f"agent '{agent_id}' 已存在, 不覆盖(重置 key 属另一操作)"}
        now = _utc_iso()
        api_key = secrets.token_urlsafe(32)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        if "api_key_hash" in cols:
            # T1-2: 库内只存 hash, 明文仅此一次随返回值 stdout 回显
            import hashlib
            conn.execute(
                """
                INSERT INTO agents
                (agent_id, agent_name, department, capabilities, role,
                 managed_agents, disclosure_policy, endpoint, registered_at,
                 last_heartbeat, status, api_key, api_key_hash,
                 api_key_created_at, api_key_expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, agent_name, department, "[]", role,
                 "[]", "{}", "", now, "", "offline", "",
                 hashlib.sha256(api_key.encode("utf-8")).hexdigest(), now,
                 _api_key_expiry(now)))
        else:
            # 列清单对齐 hub_core.register 的 INSERT
            conn.execute(
                """
                INSERT INTO agents
                (agent_id, agent_name, department, capabilities, role,
                 managed_agents, disclosure_policy, endpoint, registered_at,
                 last_heartbeat, status, api_key, api_key_created_at, api_key_expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, agent_name, department, "[]", role,
                 "[]", "{}", "", now, "", "offline", api_key, now,
                 _api_key_expiry(now)))
        conn.commit()
        return {"status": "created", "agent_id": agent_id,
                "api_key": api_key, "role": role}
    finally:
        conn.close()


# ================= key(B1: scoped key 签发, 2026-09-06) =================


def _key_scope_from_flags(endpoints: str, data_domain: str, level_cap: str) -> dict:
    """CLI scope 参数 → scope dict(空值省略; 与 _DEFAULT_SCOPE 的合并在 key_scopes.create 内)"""
    scope = {}
    eps = [e.strip() for e in (endpoints or "").split(",") if e.strip()]
    if eps:
        scope["endpoints"] = eps
    dds = [d.strip() for d in (data_domain or "").split(",") if d.strip()]
    if dds:
        scope["data_domain"] = dds
    if level_cap:
        scope["level_cap"] = level_cap
    return scope


def cmd_key_create(agent_id: str, endpoints: str, data_domain: str,
                   level_cap: str, expires: str, db_path: str) -> dict:
    """B1: 签发 scoped key(S1K)。agent 必须已预建(agent create)——key 绑定其身份,
    全权 api_key 仅管理员持有, 交付外部协作者的只有这张受限 key。明文仅此一次可见。
    创建者记 created_by='hub-cli'(与 REST /api/v1/keys 的 manager 门等价——
    CLI 操作者持有 DB 全权, 与 agent create 同构, 不构成越权)。"""
    conn = _connect(db_path)
    agent_name = ""
    try:
        try:
            row = conn.execute(
                "SELECT agent_id, agent_name FROM agents WHERE agent_id = ?",
                (agent_id,)).fetchone()
        except sqlite3.OperationalError as e:
            return {"status": "error", "code": 500,
                    "detail": f"agents 表不可读(库未初始化?先启动一次 Hub 建 schema): {e}"}
        if not row:
            return {"status": "error", "code": 404,
                    "detail": f"agent '{agent_id}' 不存在——先 `python hub_cli.py agent create "
                              f"--id {agent_id} --name <名>` 预建身份(B1: key 绑定预签发身份)"}
        agent_name = row["agent_name"] or ""
    finally:
        conn.close()
    from key_scopes import get_store
    try:
        r = get_store(db_path).create(
            agent_id, _key_scope_from_flags(endpoints, data_domain, level_cap),
            created_by="hub-cli", expires_at=expires)
    except sqlite3.OperationalError as e:
        return {"status": "error", "code": 500,
                "detail": f"agent_keys 表不可写(库未初始化?): {e}"}
    return {"status": "created", "key_id": r["key_id"], "key": r["key"],
            "agent_id": agent_id, "agent_name": agent_name, "scope": r["scope"]}


def cmd_key_list(agent_id: str, db_path: str) -> dict:
    """列 scoped key(调用画像 last_used_at/call_count, 不含 key_hash/明文)"""
    from key_scopes import get_store
    try:
        keys = get_store(db_path).list_keys(agent_id or "")
    except sqlite3.OperationalError as e:
        return {"status": "error", "code": 500,
                "detail": f"agent_keys 表不可读(库未初始化?): {e}"}
    return {"status": "ok", "keys": keys, "count": len(keys)}


def cmd_key_revoke(key_id: str, db_path: str) -> dict:
    """吊销 scoped key(状态置 revoked, 认证立即失效)"""
    from key_scopes import get_store
    try:
        ok = get_store(db_path).revoke(key_id)
    except sqlite3.OperationalError as e:
        return {"status": "error", "code": 500,
                "detail": f"agent_keys 表不可写(库未初始化?): {e}"}
    if not ok:
        return {"status": "error", "code": 404,
                "detail": f"key '{key_id}' 不存在或已吊销"}
    return {"status": "revoked", "key_id": key_id}


# ================= CLI =================


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hub-cli", description="星枢 Sync Hub 运维 CLI（O2 备份恢复）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bk = sub.add_parser("backup", help="在线备份（VACUUM INTO + ChromaDB 拷贝 + marker 对齐）")
    p_bk.add_argument("--out", required=True, help="备份输出目录")
    p_bk.add_argument("--db", default="", help="SQLite 路径（默认 CONFIG.DB_PATH）")
    p_bk.add_argument("--chroma", default="", help="ChromaDB 目录（默认 CONFIG.CHROMA_PATH）")

    p_rs = sub.add_parser("restore", help="恢复备份（恢复前自动安全备份当前库）")
    p_rs.add_argument("--from", dest="backup_dir", required=True, help="备份目录")
    p_rs.add_argument("--db", default="", help="SQLite 目标路径")
    p_rs.add_argument("--chroma", default="", help="ChromaDB 目标目录")
    p_rs.add_argument("--no-verify", action="store_true", help="跳过恢复后校验")

    p_vf = sub.add_parser("verify", help="只读校验备份完整性")
    p_vf.add_argument("--from", dest="backup_dir", required=True, help="备份目录")

    p_ag = sub.add_parser("agent", help="Agent 管理（OGA guarded 受管注册预签发）")
    ag_sub = p_ag.add_subparsers(dest="agent_cmd", required=True)
    p_ac = ag_sub.add_parser(
        "create", help="预签发建号（幂等，已存在返回 409 不覆盖）",
        epilog="示例: python hub_cli.py agent create --id oga-provisioned --name 预建 --role worker --db ./sync_hub.db")
    p_ac.add_argument("--id", dest="agent_id", required=True, help="agent_id（必填）")
    p_ac.add_argument("--name", dest="agent_name", required=True, help="显示名（必填）")
    p_ac.add_argument("--role", default="worker",
                      choices=("worker", "manager", "orchestrator"),
                      help="预置角色（默认 worker；guarded 下以此为准，请求声明 role 无效）")
    p_ac.add_argument("--department", default="", help="部门（可选）")
    p_ac.add_argument("--db", default="", help="SQLite 路径（默认 CONFIG.DB_PATH）")

    p_key = sub.add_parser("key", help="scoped key 管理（B1: 外部协作者最小权限凭据签发）")
    key_sub = p_key.add_subparsers(dest="key_cmd", required=True)
    p_kc = key_sub.add_parser(
        "create", help="签发 scoped key（明文仅此一次可见；agent 须已用 agent create 预建）",
        epilog="示例: python hub_cli.py key create --agent ext-collab --endpoints /memory/disclose,/gateway/read --data-domain proj-alpha --level-cap summary --db ./sync_hub.db")
    p_kc.add_argument("--agent", dest="agent_id", required=True,
                      help="绑定的 agent_id（须已预建）")
    p_kc.add_argument("--endpoints", default="",
                      help="端点前缀白名单, 逗号分隔（空=全部; 匹配时 /api/v1 前缀归一）")
    p_kc.add_argument("--data-domain", dest="data_domain", default="",
                      help="数据域(department 标签), 逗号分隔（空=不限）")
    p_kc.add_argument("--level-cap", dest="level_cap", default="",
                      help="最高披露级别: full|summary|metadata|none（空=不设上限）")
    p_kc.add_argument("--expires", default="",
                      help="过期时间 ISO 格式（可选, 默认不过期）")
    p_kc.add_argument("--db", default="", help="SQLite 路径（默认 CONFIG.DB_PATH）")
    p_kl = key_sub.add_parser(
        "list", help="列 scoped key（调用画像 last_used_at/call_count, 不含明文）")
    p_kl.add_argument("--agent", dest="agent_id", default="",
                      help="按 agent 过滤（空=全部）")
    p_kl.add_argument("--db", default="", help="SQLite 路径（默认 CONFIG.DB_PATH）")
    p_kr = key_sub.add_parser("revoke", help="吊销 scoped key（立即失效）")
    p_kr.add_argument("--key-id", dest="key_id", required=True)
    p_kr.add_argument("--db", default="", help="SQLite 路径（默认 CONFIG.DB_PATH）")

    args = parser.parse_args(argv)

    # 默认路径：从 models.CONFIG 取（项目根运行时）；verify/agent 子命令无全量路径参数，用 getattr 兜底
    db_path = getattr(args, "db", "") or ""
    chroma_path = getattr(args, "chroma", "") or ""
    if not db_path or not chroma_path:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from models import CONFIG
            if not db_path:
                db_path = CONFIG.DB_PATH
            if not chroma_path:
                chroma_path = CONFIG.CHROMA_PATH
        except Exception:
            if not db_path:
                db_path = "./sync_hub.db"
            if not chroma_path:
                chroma_path = "./chroma_db"

    if args.cmd == "backup":
        res = cmd_backup(args.out, db_path, chroma_path)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.cmd == "agent":
        if args.agent_cmd == "create":
            res = cmd_agent_create(args.agent_id, args.agent_name, args.role,
                                   args.department, db_path)
            # api_key 仅此一次可见(管理员预签发凭据),随结果打印到 stdout
            print(json.dumps(res, ensure_ascii=False, indent=2))
            sys.exit(0 if res.get("status") == "created" else 1)
    elif args.cmd == "key":
        if args.key_cmd == "create":
            if args.level_cap not in ("", "full", "summary", "metadata", "none"):
                print(json.dumps({"status": "error", "code": 400,
                                  "detail": f"level_cap 非法: {args.level_cap} "
                                            f"(可选 full|summary|metadata|none)"}))
                sys.exit(1)
            res = cmd_key_create(args.agent_id, args.endpoints, args.data_domain,
                                 args.level_cap, args.expires, db_path)
            # key 明文仅此一次可见(外部协作者凭据),随结果打印到 stdout
            print(json.dumps(res, ensure_ascii=False, indent=2))
            sys.exit(0 if res.get("status") == "created" else 1)
        elif args.key_cmd == "list":
            res = cmd_key_list(args.agent_id, db_path)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            sys.exit(0 if res.get("status") == "ok" else 1)
        elif args.key_cmd == "revoke":
            res = cmd_key_revoke(args.key_id, db_path)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            sys.exit(0 if res.get("status") == "revoked" else 1)
    elif args.cmd == "verify":
        res = _verify_backup(args.backup_dir)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(0 if res["valid"] else 2)
    elif args.cmd == "restore":
        res = cmd_restore(args.backup_dir, db_path, chroma_path,
                          do_verify=not args.no_verify)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        v = res.get("verify")
        if v and not v["valid"]:
            print("\n[WARN] 恢复后校验未通过（见 issues）", file=sys.stderr)
            sys.exit(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
