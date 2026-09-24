# -*- coding: utf-8 -*-
"""影子档案对账与归档（T31，2026-09-20）。

- 扫描所有分干的 vault/memory/*/*.md
- 主库无该 memory_id → 孤儿归档
- 同一 memory_id 多日期 → 留新归档旧
- 归档 = 移动到 vault/_trash/{今日}/{原相对路径}（禁止物理删除）
- 每次归档写审计事件 events.shadow_archive
"""
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone

from .common import _today

logger = logging.getLogger("xingshu.shadow")


def _list_branches(data_trunk):
    """返回待扫描分干列表。"""
    try:
        return list(data_trunk._known_branches())
    except Exception:
        return [data_trunk.branch_default]


def _scan_memory_files(data_trunk):
    """扫所有分干的 vault/memory/*/*.md。

    返回 [(branch, rel_path, memory_id, date_str), ...]。
    """
    results = []
    for branch in _list_branches(data_trunk):
        br = data_trunk.branch_repo(branch)
        vault_dir = os.path.join(br.root, "vault", "memory")
        if not os.path.isdir(vault_dir):
            continue
        for date_dir in os.listdir(vault_dir):
            date_path = os.path.join(vault_dir, date_dir)
            if not os.path.isdir(date_path):
                continue
            for fname in os.listdir(date_path):
                if not fname.endswith(".md"):
                    continue
                memory_id = fname[:-3]
                rel_path = f"vault/memory/{date_dir}/{fname}"
                results.append((branch, rel_path, memory_id, date_dir))
    return results


def _archive_file(data_trunk, branch, rel_path, memory_id, reason, db_path=None):
    """单文件归档。返回 (archived_ok: bool, skipped: bool)。"""
    br = data_trunk.branch_repo(branch)
    src = os.path.join(br.root, rel_path)
    if not os.path.exists(src):
        logger.warning(
            "shadow_archive 源文件不存在 memory_id=%s path=%s reason=%s",
            memory_id, rel_path, reason)
        return False, False

    today = _today()
    trash_rel = f"vault/_trash/{today}/{rel_path}"
    dst = os.path.join(br.root, trash_rel)

    if os.path.exists(dst):
        logger.warning(
            "shadow_archive 目标已存在（跳过）memory_id=%s to=%s",
            memory_id, trash_rel)
        return False, True

    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
    except Exception as e:
        logger.warning(
            "shadow_archive 移动失败 memory_id=%s path=%s %s: %s",
            memory_id, rel_path, type(e).__name__, str(e)[:200])
        return False, False

    # 审计事件
    if db_path:
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout = 5000")  # 对齐 outbox 口径，防瞬时 locked
            conn.execute(
                "INSERT INTO events (event_type, agent_id, payload, timestamp)"
                " VALUES (?, ?, ?, ?)",
                ("shadow_archive", "",
                 json.dumps({
                     "memory_id": memory_id,
                     "from": rel_path,
                     "to": trash_rel,
                     "reason": reason,
                     "branch": branch,
                 }, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(
                "shadow_archive 审计落行失败 memory_id=%s %s: %s",
                memory_id, type(e).__name__, str(e)[:200])
    return True, False


def reconcile_shadow_archives(data_trunk, db_path=None):
    """对账兜底：归档孤儿与重复旧档。

    返回 {"scanned", "orphan_archived", "duplicate_archived", "skipped", "errors"}。
    """
    stats = {
        "scanned": 0,
        "orphan_archived": 0,
        "duplicate_archived": 0,
        "skipped": 0,
        "errors": 0,
    }

    files = _scan_memory_files(data_trunk)
    stats["scanned"] = len(files)

    by_id = {}
    for branch, rel_path, memory_id, date_str in files:
        by_id.setdefault(memory_id, []).append((branch, rel_path, date_str))

    conn = None
    if not db_path:
        # 防御（Hermes 验收补）：无库可比对时**拒绝执行**——否则 exists_in_db 恒 False，
        # 会把全部档案误判为孤儿批量归档。
        logger.warning("reconcile 未提供 db_path → 拒绝执行（避免把全部档案误判为孤儿）")
        stats["errors"] += 1
        return stats
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout = 5000")  # 对齐 outbox 口径，防瞬时 locked
    except Exception as e:
        logger.warning("reconcile 连库失败 %s: %s → 拒绝执行",
                       type(e).__name__, str(e)[:200])
        stats["errors"] += 1
        return stats

    try:
        for memory_id, entries in by_id.items():
            exists_in_db = False
            if conn:
                try:
                    row = conn.execute(
                        "SELECT 1 FROM memory_pool WHERE memory_id = ?",
                        (memory_id,)).fetchone()
                    exists_in_db = row is not None
                except Exception as e:
                    logger.warning(
                        "reconcile 查库失败 memory_id=%s %s: %s",
                        memory_id, type(e).__name__, str(e)[:200])
                    stats["errors"] += 1
                    continue

            if not exists_in_db:
                for branch, rel_path, date_str in entries:
                    ok, skipped = _archive_file(
                        data_trunk, branch, rel_path, memory_id, "orphan", db_path)
                    if ok:
                        stats["orphan_archived"] += 1
                    elif skipped:
                        stats["skipped"] += 1
                    else:
                        stats["errors"] += 1
            elif len(entries) > 1:
                sorted_entries = sorted(entries, key=lambda x: x[2], reverse=True)
                for branch, rel_path, date_str in sorted_entries[1:]:
                    ok, skipped = _archive_file(
                        data_trunk, branch, rel_path, memory_id, "superseded", db_path)
                    if ok:
                        stats["duplicate_archived"] += 1
                    elif skipped:
                        stats["skipped"] += 1
                    else:
                        stats["errors"] += 1
    finally:
        if conn:
            conn.close()

    return stats


def archive_by_memory_id(data_trunk, memory_id, db_path=None):
    """按 memory_id 归档所有分干中的对应档案（shadow_delete 事件消费侧用）。"""
    archived = 0
    skipped = 0
    errors = 0
    for branch, rel_path, mid, date_str in _scan_memory_files(data_trunk):
        if mid == memory_id:
            ok, sk = _archive_file(data_trunk, branch, rel_path, memory_id, "delete", db_path)
            if ok:
                archived += 1
            elif sk:
                skipped += 1
            else:
                errors += 1
    return {"archived": archived, "skipped": skipped, "errors": errors}


def archive_by_path(data_trunk, old_path, memory_id, db_path=None):
    """按相对路径归档指定档案（shadow_archive 事件消费侧用）。

    old_path 不含分干信息，因此遍历所有分干查找。
    幂等：源已不存在且目标已存在 → skipped，不报错。
    """
    for branch in _list_branches(data_trunk):
        br = data_trunk.branch_repo(branch)
        src = os.path.join(br.root, old_path)
        if os.path.exists(src):
            ok, sk = _archive_file(data_trunk, branch, old_path, memory_id, "superseded", db_path)
            return {"archived": 1 if ok else 0, "skipped": 1 if sk else 0, "errors": 0 if ok or sk else 1}
        # 幂等：源已不存在，检查是否已归档到今日 _trash
        today = _today()
        trash_rel = f"vault/_trash/{today}/{old_path}"
        dst = os.path.join(br.root, trash_rel)
        if os.path.exists(dst):
            return {"archived": 0, "skipped": 1, "errors": 0}
    logger.warning(
        "archive_by_path 未找到源文件 memory_id=%s path=%s",
        memory_id, old_path)
    return {"archived": 0, "skipped": 0, "errors": 1}
