#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星枢 Sync Hub — 种子数据脚本
插入测试数据：8 个任务 + 3 个定时任务 + 1 个团队成员
用法: python seed_data.py [db_path]
"""
import sqlite3
import json
import sys
import os
from datetime import datetime, timezone, timedelta

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "./sync_hub.db"

SEED_AGENT_ID = "alice"
SEED_AGENT_NAME = "Alice"


def seed_tasks(c):
    """8 个任务: pending 3 / in_progress 2 / completed 2 / cancelled 1"""
    now = datetime.now(timezone.utc).isoformat()
    tasks = [
        # pending x3
        ("T-101", "pending",     "整理客户反馈邮件并分类归档",     2, now, now),
        ("T-102", "pending",     "更新产品 FAQ 文档",              3, now, now),
        ("T-103", "pending",     "准备周报数据汇总",               2, now, now),
        # in_progress x2
        ("T-201", "in_progress", "处理订单 #20260728-001 退款",    1, now, now),
        ("T-202", "in_progress", "联系供应商确认交货时间",         2, now, now),
        # completed x2
        ("T-301", "completed",   "修复登录页面样式问题",           2, now, now),
        ("T-302", "completed",   "导出本月销售报表",               3, now, now),
        # cancelled x1
        ("T-401", "cancelled",   "旧系统数据迁移（已搁置）",       3, now, now),
    ]
    inserted = 0
    for tid, status, desc, prio, created, updated in tasks:
        existing = c.execute("SELECT task_id FROM tasks WHERE task_id = ?", (tid,)).fetchone()
        if existing:
            continue
        c.execute("""
            INSERT INTO tasks (task_id, status, creator_agent_id, assigned_agent_id,
                               description, priority, current_phase, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """, (tid, status, SEED_AGENT_ID, SEED_AGENT_ID, desc, prio, created, updated))
        inserted += 1
    return inserted


def seed_cron_jobs(c):
    """3 个定时任务: 日报生成 / 数据备份 / 过期清理"""
    jobs = [
        ("日报生成", "0 18 * * *",  "report"),
        ("数据备份", "0 2 * * 0",   "backup"),
        ("过期清理", "0 4 * * *",   "cleanup"),
    ]
    inserted = 0
    for name, schedule, action in jobs:
        existing = c.execute(
            "SELECT id FROM cron_jobs WHERE name = ? AND created_by = ?",
            (name, SEED_AGENT_ID)
        ).fetchone()
        if existing:
            continue
        c.execute("""
            INSERT INTO cron_jobs (name, schedule, action, action_params, enabled, created_by)
            VALUES (?, ?, ?, '{}', 1, ?)
        """, (name, schedule, action, SEED_AGENT_ID))
        inserted += 1
    return inserted


def seed_team_member(c):
    """1 个团队成员: Alice@DESKTOP-ALICE"""
    existing = c.execute(
        "SELECT id FROM team_members WHERE user_name = ? AND hostname = ?",
        ("Alice", "DESKTOP-ALICE")
    ).fetchone()
    if existing:
        return 0
    paired_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    c.execute("""
        INSERT INTO team_members
        (local_agent_id, remote_hub_id, remote_hub_url, remote_agent_id, remote_api_key,
         hostname, user_name, role, department, paired_at, key_expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'worker', '', ?, ?)
    """, (
        SEED_AGENT_ID, "hub-local", "http://127.0.0.1:3060",
        SEED_AGENT_ID, "seed-key-placeholder",
        "DESKTOP-ALICE", "Alice",
        paired_at, expires,
    ))
    return 1


def main():
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] 数据库不存在: {DB_PATH}")
        print("请先启动 Hub 服务创建数据库，或指定正确路径。")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    t_count = seed_tasks(c)
    cr_count = seed_cron_jobs(c)
    tm_count = seed_team_member(c)

    conn.commit()

    # 汇总
    task_total = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    cron_total = c.execute("SELECT COUNT(*) FROM cron_jobs").fetchone()[0]
    team_total = c.execute("SELECT COUNT(*) FROM team_members WHERE revoked_at IS NULL").fetchone()[0]

    conn.close()

    print(f"种子数据插入完成 (DB: {DB_PATH})")
    print(f"  任务:   新增 {t_count} 条, 总计 {task_total} 条")
    print(f"  定时:   新增 {cr_count} 条, 总计 {cron_total} 条")
    print(f"  团队:   新增 {tm_count} 条, 总计 {team_total} 条")


if __name__ == "__main__":
    main()
