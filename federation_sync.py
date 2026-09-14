# -*- coding: utf-8 -*-
"""N6a 轻量互备：Hub 间单向只读联邦同步（2026-08-05）

设计（D8 拍板：N6a = 迭代 3，单向只读无冲突解决）：
- 主 Hub 暴露快照端点（kind=agents|memory|knowledge|wiki），认证走 api_key
- 备 Hub 通过 team_members 配对（remote_hub_url + remote_api_key）拉取 → upsert 落库
- 单向：备只拉不推；无冲突解决：主为权威，按主键覆盖备
- 安全：agents 快照 **排除 api_key 相关列**（备 Hub 不该拿到主 Hub 的密钥）
"""
import json
import logging
import sqlite3
import urllib.request
import urllib.error

logger = logging.getLogger("federation")

# agents 快照排除敏感列（api_key/轮换/白名单——备 Hub 不需要也不该有主 Hub 的密钥）
AGENTS_SNAPSHOT_EXCLUDE = {
    "api_key", "api_key_created_at", "api_key_expires_at",
    "api_key_prev", "api_key_prev_expires_at", "api_key_ip_whitelist",
    "last_used_at",
}

SNAPSHOT_TABLES = {
    "agents": "agents",
    "memory": "memory_pool",
    "knowledge": "knowledge_base",
    "wiki": "wiki_inbox",
}


def export_snapshot(db_path: str, kind: str) -> dict:
    """从库导出某类数据快照（主 Hub 侧）。kind: agents|memory|knowledge|wiki"""
    table = SNAPSHOT_TABLES.get(kind)
    if not table:
        return {"status": "error", "error": f"unknown kind: {kind}"}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(f"SELECT * FROM {table}")
        rows = c.fetchall()
        conn.close()
    except Exception as e:
        return {"status": "error", "error": str(e)}

    data = []
    for r in rows:
        d = dict(r)
        if kind == "agents":
            for k in AGENTS_SNAPSHOT_EXCLUDE:
                d.pop(k, None)
        data.append(d)
    return {"status": "ok", "kind": kind, "count": len(data), "rows": data}


def import_snapshot(db_path: str, kind: str, rows: list) -> dict:
    """把快照 upsert 进库（备 Hub 侧）。主为权威，按主键覆盖。"""
    table = SNAPSHOT_TABLES.get(kind)
    if not table:
        return {"status": "error", "error": f"unknown kind: {kind}"}
    if not rows:
        return {"status": "ok", "imported": 0}
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        # 读目标表主键（PRAGMA 第 2 列是列名,第 6 列 pk=1 标记主键）
        c.execute(f"PRAGMA table_info({table})")
        info = c.fetchall()
        pk_cols = [row[1] for row in info if row[5] == 1]
        pk = pk_cols[0] if pk_cols else info[0][1]
        imported = 0
        for row in rows:
            if pk not in row:
                continue
            cols = list(row.keys())
            placeholders = ",".join("?" for _ in cols)
            colnames = ",".join(cols)
            updates = ",".join(f"{col}=excluded.{col}" for col in cols if col != pk)
            sql = (f"INSERT INTO {table} ({colnames}) VALUES ({placeholders}) "
                   f"ON CONFLICT({pk}) DO UPDATE SET {updates}")
            c.execute(sql, [row[col] for col in cols])
            imported += 1
        conn.commit()
        conn.close()
        return {"status": "ok", "imported": imported}
    except Exception as e:
        logger.exception("import_snapshot failed")
        return {"status": "error", "error": str(e)}


def pull_from_peer(db_path: str, peer: dict, kinds: list) -> dict:
    """备 Hub：从配对 Hub 拉取快照并落库（单向只读）。peer = team_members 行。"""
    hub_url = (peer.get("remote_hub_url") or "").rstrip("/")
    api_key = peer.get("remote_api_key") or ""
    if not hub_url:
        return {"status": "error", "error": "remote_hub_url 缺失"}
    results = {}
    for kind in kinds:
        try:
            req = urllib.request.Request(
                f"{hub_url}/api/v1/federation/snapshot/{kind}",
                headers={"Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                snap = json.loads(resp.read().decode())
            if snap.get("status") != "ok":
                results[kind] = {"status": "error", "error": snap.get("error")}
                continue
            imp = import_snapshot(db_path, kind, snap.get("rows", []))
            results[kind] = imp
        except urllib.error.HTTPError as e:
            results[kind] = {"status": "error", "error": f"HTTP {e.code}"}
        except Exception as e:
            results[kind] = {"status": "error", "error": str(e)}
    return {"status": "ok", "peer": hub_url, "kinds": results}
