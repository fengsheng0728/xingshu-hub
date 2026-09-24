"""自动化工作 API (R1-R4) — 统一调度引擎

v2: 1s tick + croniter + heartbeat + 3种调度(kind: at/every/cron)
"""
import logging
logger = logging.getLogger("xingshu.routes_automation")


import json as _json, datetime as _dt, asyncio
from db import record_dead_letter
from logfmt import rotate_request_id
from envelope import envelope_dispatch, serialize
from fastapi.responses import JSONResponse
from fastapi import Depends
from notifications import notifications

# croniter 可选依赖
try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

TICK_INTERVAL = 1.0  # 1秒精度


def _compute_next_run(schedule_kind: str, schedule_spec: str,
                      interval_sec: int = 60) -> str | None:
    """计算下次运行时间。返回 ISO 字符串或 None。"""
    now = _dt.datetime.now()
    if schedule_kind == "at":
        return schedule_spec
    elif schedule_kind == "every":
        sec = int(schedule_spec) if schedule_spec.isdigit() else interval_sec
        return (now + _dt.timedelta(seconds=sec)).isoformat()
    elif schedule_kind == "cron" and HAS_CRONITER and schedule_spec:
        try:
            cron = croniter(schedule_spec, now)
            return cron.get_next(_dt.datetime).isoformat()
        except Exception:
            return (now + _dt.timedelta(seconds=interval_sec)).isoformat()
    return None


def register(app, hub, get_current_agent):

    @app.get("/api/v1/automation/jobs")
    async def api_automation_list(current_agent: str = Depends(get_current_agent)):
        conn = hub._db(); c = conn.cursor()
        c.execute("SELECT * FROM automation_jobs WHERE owner_agent_id=? ORDER BY created_at DESC",
                  (current_agent,))
        jobs = [dict(zip([col[0] for col in c.description], row)) for row in c.fetchall()]
        conn.close()
        return {"jobs": jobs}

    @app.post("/api/v1/automation/jobs")
    async def api_automation_create(job: dict, current_agent: str = Depends(get_current_agent)):
        if not job.get("instruction"):
            return JSONResponse({"error": "instruction required"}, status_code=400)

        trigger_type = job.get("trigger_type", "schedule")
        # 归一化：daily/weekly/cron 都是 cron 语义（spec 即 cron 表达式）
        # interval 是秒数间隔；event 保留原样（由事件触发，不走定时调度器）
        if trigger_type in ("daily", "weekly", "cron"):
            trigger_type = "schedule"
            schedule_kind = "cron"
        elif trigger_type == "interval":
            trigger_type = "schedule"
            schedule_kind = "every"
        else:
            schedule_kind = job.get("schedule_kind", "every")
        trigger_spec = str(job.get("trigger_spec", ""))
        payload_type = job.get("payload_type", "instruction")
        heartbeat_file = job.get("heartbeat_file", "")

        delivery = _json.dumps(job.get("delivery", ["notification"]))
        guardrail = _json.dumps(job.get("guardrail", {
            "max_iterations": 10, "max_tokens": 50000,
            "permission": "read_memory+write_memory"
        }))

        next_run = _compute_next_run(schedule_kind, trigger_spec) if trigger_type == "schedule" else None

        conn = hub._db(); c = conn.cursor()
        allow_auto = 1 if job.get("guardrail", {}).get("allow_auto_source") else 0
        delete_after = 1 if job.get("delete_after_run") else 0
        c.execute(
            """INSERT INTO automation_jobs
               (name, trigger_type, trigger_spec, schedule_kind, payload_type, heartbeat_file,
                instruction, delivery, guardrail, owner_agent_id, next_run_at,
                allow_auto_source, delete_after_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.get("name", ""), trigger_type, trigger_spec,
             schedule_kind, payload_type, heartbeat_file,
             job["instruction"], delivery, guardrail, current_agent, next_run,
             allow_auto, delete_after)
        )
        conn.commit(); jid = c.lastrowid; conn.close()
        return {"ok": True, "job_id": jid}

    @app.post("/api/v1/automation/jobs/{job_id}/toggle")
    async def api_automation_toggle(job_id: int, req: dict,
                                     current_agent: str = Depends(get_current_agent)):
        conn = hub._db(); c = conn.cursor()
        enabled = 1 if req.get("enabled", True) else 0
        if req.get("_reset_fail_count") or enabled:
            c.execute(
                "UPDATE automation_jobs SET enabled=?, consecutive_failures=0, "
                "updated_at=datetime('now') WHERE id=? AND owner_agent_id=?",
                (enabled, job_id, current_agent))
        else:
            c.execute(
                "UPDATE automation_jobs SET enabled=?, updated_at=datetime('now') "
                "WHERE id=? AND owner_agent_id=?",
                (enabled, job_id, current_agent))
        conn.commit(); conn.close()
        return {"ok": True, "enabled": bool(enabled)}

    @app.delete("/api/v1/automation/jobs/{job_id}")
    async def api_automation_delete(job_id: int,
                                     current_agent: str = Depends(get_current_agent)):
        from routes_n1 import _n1_gate
        _gate = await _n1_gate(current_agent, "automation_jobs",
                               {"job_id": job_id, "owner": current_agent})
        if _gate:
            return _gate
        conn = hub._db(); c = conn.cursor()
        c.execute("DELETE FROM automation_jobs WHERE id=? AND owner_agent_id=?",
                  (job_id, current_agent))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.get("/api/v1/automation/jobs/{job_id}/runs")
    async def api_automation_runs(job_id: int,
                                   current_agent: str = Depends(get_current_agent)):
        conn = hub._db(); c = conn.cursor()
        # 归属先验（修复轮）：与 delete/toggle/retry 同口径 owner_agent_id=current_agent；
        # 「不存在」与「非本人」合并为同一 404（docs/api-error-policy.md 404 合并模式）
        c.execute("SELECT id FROM automation_jobs WHERE id=? AND owner_agent_id=?",
                  (job_id, current_agent))
        if not c.fetchone():
            conn.close(); return JSONResponse({"error": "job not found"}, status_code=404)
        c.execute("SELECT * FROM automation_runs WHERE job_id=? ORDER BY started_at DESC LIMIT 20",
                  (job_id,))
        runs = [dict(zip([col[0] for col in c.description], row)) for row in c.fetchall()]
        conn.close()
        return {"runs": runs}

    @app.post("/api/v1/automation/runs")
    async def api_automation_result(run: dict,
                                     current_agent: str = Depends(get_current_agent)):
        job_id = run.get("job_id"); status = run.get("status", "success")
        result = run.get("result_summary", ""); full = run.get("full_result", "")
        duration = run.get("duration_ms", 0); iterations = run.get("iterations", 0)
        tokens = run.get("tokens_used", 0)
        artifact_path = run.get("artifact_path", "")
        if not artifact_path and isinstance(full, str):
            import re as _re
            m = _re.search(r'"path"\s*:\s*"([^"]+\.(?:docx|xlsx|html))"', full, _re.I)
            if m:
                artifact_path = m.group(1)
        conn = hub._db(); c = conn.cursor()
        # 归属先验（修复轮）：job_id 来自请求体但必须属于调用者；
        # 「不存在」与「非本人」合并为同一 404（防存在性枚举，同 retry/run 口径）
        c.execute("SELECT id FROM automation_jobs WHERE id=? AND owner_agent_id=?",
                  (job_id, current_agent))
        if not c.fetchone():
            conn.close(); return JSONResponse({"error": "job not found"}, status_code=404)
        c.execute(
            "INSERT INTO automation_runs (job_id,agent_id,status,started_at,finished_at,"
            "duration_ms,result_summary,full_result,iterations,tokens_used) "
            "VALUES (?,?,?,datetime('now','-1 seconds'),datetime('now'),?,?,?,?,?)",
            (job_id, current_agent, status, duration, result, full, iterations, tokens))
        _disabled_job = None
        if status == 'failed':
            c.execute(
                "UPDATE automation_jobs SET last_status=?, last_run_duration_ms=?, "
                "last_result_summary=?, consecutive_failures=consecutive_failures+1, "
                "run_count=run_count+1 WHERE id=?",
                (status, duration, (result or "")[:200], job_id))
            c.execute("SELECT consecutive_failures, owner_agent_id, name FROM automation_jobs WHERE id=?",
                      (job_id,))
            row = c.fetchone()
            if row and row[0] >= 5:
                c.execute("UPDATE automation_jobs SET enabled=0 WHERE id=?", (job_id,))
                job_owner = row[1]
                # CD-084：连失败停用落死信账本（retry 端点对 automation_job:<id> 源
                # 有「重置连失败计数+重新启用」语义）。落库挪到 conn.close() 之后，
                # 避免本连接持写事务期间第二连接写入被锁。
                _disabled_job = {"job_id": job_id, "title": row[2],
                                 "consecutive_failures": row[0]}
                if hasattr(hub, 'create_notification'):
                    # 修复轮：旧调用 kind= 不是 create_notification 的真实参数
                    # （签名 (agent_id, type, title, ..., source=...)），命中 ≥5 连失败
                    # 必 TypeError → 500 且停用 UPDATE 随未提交连接回滚；改回真实签名。
                    asyncio.create_task(hub.create_notification(
                        job_owner, "automation_alert",
                        "自动化任务已暂停：连续 5 次失败",
                        source=f"job:{job_id}"
                    ))
        else:
            c.execute(
                "UPDATE automation_jobs SET last_status=?, last_run_duration_ms=?, "
                "last_result_summary=?, consecutive_failures=0, run_count=run_count+1 WHERE id=?",
                (status, duration, (result or "")[:200], job_id))
        conn.commit(); conn.close()
        if _disabled_job:
            # 死信落库不吞原语义：任务已停用+已通知 owner，账本供运维面板/重试闭环
            await asyncio.to_thread(
                record_dead_letter,
                f"automation_job:{_disabled_job['job_id']}", "automation_alert",
                _disabled_job,
                f"连续 {_disabled_job['consecutive_failures']} 次失败，任务已自动停用")
        if hasattr(hub, 'create_notification'):
            await hub.create_notification(current_agent, "automation",
                f"自动化: {run.get('name','')} = {status}",
                body=(result or "")[:200],
                source="automation",
                artifact_path=artifact_path)
        return {"ok": True}

    @app.get("/api/v1/automation/missed")
    async def api_automation_missed(current_agent: str = Depends(get_current_agent)):
        conn = hub._db(); c = conn.cursor()
        c.execute(
            "SELECT SUM(missed_runs) as total, COUNT(*) as job_count "
            "FROM automation_jobs WHERE owner_agent_id=? AND missed_runs > 0",
            (current_agent,))
        row = c.fetchone()
        total = row[0] if row and row[0] else 0
        job_count = row[1] if row and row[1] else 0
        c.execute(
            "SELECT id, name, missed_runs FROM automation_jobs "
            "WHERE owner_agent_id=? AND missed_runs > 0", (current_agent,))
        jobs = [dict(zip([col[0] for col in c.description], row2)) for row2 in c.fetchall()]
        conn.close()
        return {"total_missed": total, "job_count": job_count, "jobs": jobs}

    @app.post("/api/v1/automation/missed/retry")
    async def api_automation_retry(req: dict, current_agent: str = Depends(get_current_agent)):
        job_id = req.get("job_id")
        if not job_id:
            return JSONResponse({"error": "job_id required"}, status_code=400)
        conn = hub._db(); c = conn.cursor()
        c.execute("SELECT * FROM automation_jobs WHERE id=? AND owner_agent_id=?",
                  (job_id, current_agent))
        job = c.fetchone()
        if not job:
            conn.close(); return JSONResponse({"error": "job not found"}, status_code=404)
        job_dict = dict(zip([col[0] for col in c.description], job))
        ws = hub.active_ws.get(current_agent)
        if not ws:
            conn.close(); return {"ok": False, "error": "agent offline"}
        try:
            gr = (_json.loads(job_dict.get('guardrail', '{}'))
                  if isinstance(job_dict.get('guardrail'), str)
                  else job_dict.get('guardrail', {}))
            dl = (_json.loads(job_dict.get('delivery', '["notification"]'))
                  if isinstance(job_dict.get('delivery'), str)
                  else job_dict.get('delivery', ["notification"]))
            # CD-098：直发改走 per-agent 发送锁（判空保留，发送持锁防帧交错）
            delivered = await notifications.safe_send(current_agent, {
                "type": "automation.run",
                "job_id": job_dict["id"],
                "name": job_dict.get("name", ""),
                "instruction": job_dict.get("instruction", ""),
                "payload_type": job_dict.get("payload_type", "instruction"),
                "guardrail": gr,
                "delivery": dl,
                "_retry": True,
            })
            if delivered == 0:
                # 死连接/零投递：safe_send 已清理死连接并落死信，不吞失败信号
                conn.close()
                return {"ok": False, "error": "deliver failed: no live connection"}
            c.execute(
                "UPDATE automation_jobs SET missed_runs=0, run_count=run_count+1, "
                "last_run_at=datetime('now') WHERE id=?", (job_id,))
            conn.commit(); conn.close()
            return {"ok": True}
        except Exception as e:
            conn.close(); return {"ok": False, "error": str(e)}

    @app.post("/api/v1/automation/missed/skip")
    async def api_automation_skip(req: dict, current_agent: str = Depends(get_current_agent)):
        job_id = req.get("job_id")
        if not job_id:
            return JSONResponse({"error": "job_id required"}, status_code=400)
        conn = hub._db(); c = conn.cursor()
        c.execute("UPDATE automation_jobs SET missed_runs=0 WHERE id=? AND owner_agent_id=?",
                  (job_id, current_agent))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/v1/automation/jobs/{job_id}/run")
    async def api_automation_manual_run(job_id: int,
                                         current_agent: str = Depends(get_current_agent)):
        """手动触发一次自动化任务。"""
        conn = hub._db(); c = conn.cursor()
        c.execute("SELECT * FROM automation_jobs WHERE id=? AND owner_agent_id=?",
                  (job_id, current_agent))
        job = c.fetchone()
        if not job:
            conn.close(); return JSONResponse({"error": "job not found"}, status_code=404)
        job_dict = dict(zip([col[0] for col in c.description], job))
        ws = hub.active_ws.get(current_agent)
        if not ws:
            conn.close(); return {"ok": False, "error": "agent offline"}
        try:
            gr = (_json.loads(job_dict.get('guardrail', '{}'))
                  if isinstance(job_dict.get('guardrail'), str)
                  else job_dict.get('guardrail', {}))
            dl = (_json.loads(job_dict.get('delivery', '["notification"]'))
                  if isinstance(job_dict.get('delivery'), str)
                  else job_dict.get('delivery', ["notification"]))
            # CD-098：直发改走 per-agent 发送锁（判空保留，发送持锁防帧交错）
            delivered = await notifications.safe_send(current_agent, {
                "type": "automation.run",
                "job_id": job_dict["id"],
                "name": job_dict.get("name", ""),
                "instruction": job_dict.get("instruction", ""),
                "payload_type": job_dict.get("payload_type", "instruction"),
                "guardrail": gr,
                "delivery": dl,
                "_manual": True,
            })
            if delivered == 0:
                # 死连接/零投递：safe_send 已清理死连接并落死信，不吞失败信号
                conn.close()
                return {"ok": False, "error": "deliver failed: no live connection"}
            c.execute(
                "UPDATE automation_jobs SET run_count=run_count+1, "
                "last_run_at=datetime('now'), last_status='dispatched' WHERE id=?",
                (job_id,))
            conn.commit(); conn.close()
            return {"ok": True}
        except Exception as e:
            conn.close(); return {"ok": False, "error": str(e)}


# ============ 统一调度器 ============

async def automation_scheduler(hub):
    """统一调度引擎：1s tick，支持 at/every/cron/event/heartbeat。"""
    await asyncio.sleep(5)

    while True:
        rotate_request_id("automation-tick")  # CD-108：每轮 tick 轮换 request id
        try:
            conn = hub._db(); c = conn.cursor()
            now = _dt.datetime.now()
            now_iso = now.isoformat()

            c.execute(
                """SELECT * FROM automation_jobs
                   WHERE enabled=1
                     AND trigger_type IN ('schedule','cron','interval','daily','weekly')
                     AND consecutive_failures < 5
                     AND (next_run_at IS NULL OR next_run_at <= ?)
                   ORDER BY id""",
                (now_iso,))
            jobs = [dict(zip([col[0] for col in c.description], row)) for row in c.fetchall()]
            conn.close()

            for job in jobs:
                agent_id = job.get("owner_agent_id", "")
                ws = hub.active_ws.get(agent_id)
                if not ws:
                    try:
                        conn3 = hub._db(); c3 = conn3.cursor()
                        c3.execute("UPDATE automation_jobs SET missed_runs=missed_runs+1 WHERE id=?",
                                   (job["id"],))
                        conn3.commit(); conn3.close()
                    except Exception as _exc:
                        logger.warning("routes_automation silent-except(automation_scheduler): %s", _exc)
                    continue

                payload_type = job.get("payload_type", "instruction")
                dispatch_event = "automation.run"
                dispatch_data = {
                    "event": dispatch_event,
                    "job_id": job["id"],
                    "name": job.get("name", ""),
                    "instruction": job.get("instruction", ""),
                    "payload_type": payload_type,
                }

                if payload_type == "heartbeat":
                    dispatch_data["heartbeat_file"] = job.get("heartbeat_file", "TASK.md")
                    dispatch_data["instruction"] = ""

                try:
                    gr = (_json.loads(job.get("guardrail", "{}"))
                          if isinstance(job.get("guardrail"), str)
                          else job.get("guardrail", {}))
                    dl = (_json.loads(job.get("delivery", '["notification"]'))
                          if isinstance(job.get("delivery"), str)
                          else job.get("delivery", ["notification"]))
                    dispatch_data["guardrail"] = gr
                    dispatch_data["delivery"] = dl

                    env = envelope_dispatch(dispatch_data, via="automation")
                    # CD-098：文本帧也走同一把 per-agent 锁（与 JSON 帧防交错）
                    delivered = await notifications.safe_send_text(agent_id, serialize(env))
                    if delivered == 0:
                        # 死连接/零投递：不吞失败信号——不推进 next_run（下个 tick
                        # 重投本周期），last_status 记失败，不再静默跳过整个周期
                        conn2 = hub._db(); c2 = conn2.cursor()
                        c2.execute(
                            "UPDATE automation_jobs SET last_status='dispatch_failed' "
                            "WHERE id=?", (job["id"],))
                        conn2.commit(); conn2.close()
                        continue
                    hub.track_dispatch("", env)

                    # 调度时按 trigger_type 推导 kind（兼容历史 daily/weekly 任务）
                    _jt = job.get("trigger_type", "schedule")
                    if _jt in ("daily", "weekly", "cron"):
                        schedule_kind = "cron"
                    elif _jt == "interval":
                        schedule_kind = "every"
                    else:
                        schedule_kind = job.get("schedule_kind", "every")
                    trigger_spec = job.get("trigger_spec", "")
                    next_run = _compute_next_run(schedule_kind, trigger_spec) if schedule_kind != "at" else None

                    delete_after = job.get("delete_after_run", 0)
                    conn2 = hub._db(); c2 = conn2.cursor()
                    if delete_after:
                        c2.execute("UPDATE automation_jobs SET enabled=0 WHERE id=?",
                                   (job["id"],))
                    else:
                        c2.execute(
                            "UPDATE automation_jobs SET run_count=run_count+1, "
                            "last_run_at=datetime('now'), last_status='dispatched', "
                            "next_run_at=? WHERE id=?",
                            (next_run, job["id"]))
                    conn2.commit(); conn2.close()
                except Exception as _exc:
                    logger.warning("routes_automation silent-except(automation_scheduler): %s", _exc)

        except Exception as _exc:
            logger.debug("routes_automation silent-except(automation_scheduler): %s", _exc)

        await asyncio.sleep(TICK_INTERVAL)
