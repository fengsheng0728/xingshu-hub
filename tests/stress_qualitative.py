"""P2: SQLite 峰值并发 + ChromaDB 多进程/降级 —— 稳定性定性（只测量不修，D4）

T2-1: SQLite 阶梯并发（25/50/100/200 × 30s）经 HTTP 真实路径写记忆
     记录: 入队延迟峰值 / database locked 次数 / 降级直写次数 / HTTP 超时数 / 数据丢失数
T2-2: ChromaDB 双进程打开同一 db 目录（5 轮 × 2 进程）
T2-3: 降级路径故障注入（kill Hub -> rename chroma_db -> 占位文件 -> 启动）
     验证 memory/search 仍返回结果 + embedding_unavailable 降级标记
     验证 semantic_search（ChromaDB 依赖端点）行为

用法: python tests/stress_qualitative.py
依赖: 外部已启动生产 Hub（T2-3 会 kill 并自管重启），p2-stress-agent 已注册
"""
import asyncio
import json
import os
import pathlib
import sys
import time
import sqlite3
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（tests/ 上一级）
HUB = os.environ.get("P2_HUB", "http://127.0.0.1:3060")
TOKEN = os.environ.get("P2_TOKEN", "")
AGENT_ID = "p2-stress-agent"
DB_PATH = str(_ROOT / "sync_hub.db")
CHROMA_PATH = str(_ROOT / "chroma_db")
BACKUP_PATH = CHROMA_PATH + ".bak-stability"
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results = {}


def req(method, path, token=None, data=None, timeout=35):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(HUB + path, data=body, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace")), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, {}, time.time() - t0
    except Exception as e:
        return 0, {"error": str(e)[:80]}, time.time() - t0


def setup_agent():
    """注册压测 agent（幂等：已存在则复用）+ role=manager（knowledge 写入需 manager 角色）"""
    s, d, _ = req("POST", "/api/v1/agents/register", TOKEN,
                  {"agent_id": AGENT_ID, "agent_name": "p2-stress", "role": "manager"})
    if isinstance(d, dict) and d.get("api_key"):
        return d["api_key"]
    return ""


def count_memories():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    n = c.execute("SELECT COUNT(*) FROM memory_pool WHERE owner_agent_id=?", (AGENT_ID,)).fetchone()[0]
    conn.close()
    return n


def stress_level(concurrency, duration, key):
    """单档压测：concurrency 并发写记忆 duration 秒（内容唯一化绕过三段去重）"""
    import uuid as _uuid
    n_written = 0
    n_http_timeout = 0
    n_locked = 0
    delays = []
    lock = threading.Lock()

    def worker(i):
        nonlocal n_written, n_http_timeout, n_locked
        deadline = time.time() + duration
        while time.time() < deadline:
            payload = {
                "memory_key": "p2s-%s" % _uuid.uuid4().hex[:16],
                "kind": "fact",
                "content": "压测内容 %s 并发测试数据" % _uuid.uuid4().hex[:12],
                "tags": ["stress"],
            }
            s, d, dt = req("POST", "/api/v1/memory/store?agent_id=%s" % AGENT_ID, key, payload)
            with lock:
                if s == 200:
                    n_written += 1
                    delays.append(dt)
                elif s == 0:
                    n_http_timeout += 1
                elif s == 503 or "locked" in json.dumps(d):
                    n_locked += 1
            time.sleep(0.01)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    # 数据核对：写入总数 vs 落库总数（等缓冲 worker 落盘）
    time.sleep(5)
    total_db = count_memories()
    results[concurrency] = {
        "concurrency": concurrency,
        "duration_s": round(elapsed, 1),
        "requests_ok": n_written,
        "http_timeout": n_http_timeout,
        "db_locked": n_locked,
        "peak_latency_s": round(max(delays), 3) if delays else 0,
        "avg_latency_s": round(sum(delays) / len(delays), 3) if delays else 0,
        "db_total_after": total_db,
    }
    print("  [%d并发 x%.0fs] ok=%d timeout=%d locked=%d peak_lat=%.3fs db_total=%d"
          % (concurrency, elapsed, n_written, n_http_timeout, n_locked,
             results[concurrency]["peak_latency_s"], total_db), flush=True)


def count_knowledge():
    """统计压测 knowledge 条目落库数（title 前缀 p2k-）"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    n = c.execute("SELECT COUNT(*) FROM knowledge_base WHERE title LIKE 'p2k-%'").fetchone()[0]
    conn.close()
    return n


def buffer_stats(key=""):
    """读写入缓冲 stats（降级直写计数等）"""
    s, d, _ = req("GET", "/api/v1/buffer/stats", key)
    return d if isinstance(d, dict) else {}


def wait_buffer_drain(key, timeout=90):
    """轮询 buffer/stats 直到 queue_depth==0（缓冲 worker 落盘完成）"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        stats = buffer_stats(key)
        if stats.get("queue_depth", 1) == 0:
            return True
        time.sleep(2)
    return False


def knowledge_stress(concurrency, duration, key):
    """T2-1 正主：knowledge/upsert 走写入缓冲（asyncio.Queue 2000 上限 + 0.5s/批 20 条）
    记录: 入队延迟峰值 / locked 次数 / 降级直写次数 / HTTP 超时数 / 数据丢失数"""
    import uuid as _uuid
    n_ok = 0
    n_timeout = 0
    n_locked = 0
    n_other = 0
    delays = []
    lock = threading.Lock()
    # 前置：等上档残留缓冲清空，再采样基线
    wait_buffer_drain(key)
    n_before = count_knowledge()

    def worker(i):
        nonlocal n_ok, n_timeout, n_locked, n_other
        deadline = time.time() + duration
        while time.time() < deadline:
            payload = {
                "title": "p2k-%s" % _uuid.uuid4().hex[:12],
                "content": "知识压测条目 %s 稳定性定性测试" % _uuid.uuid4().hex[:8],
                "tags": ["stress"],
                "category": "stress",
                "importance": 1,
            }
            s, d, dt = req("POST", "/api/v1/knowledge", key, payload)
            with lock:
                if s == 200:
                    n_ok += 1
                    delays.append(dt)
                elif s == 0:
                    n_timeout += 1
                elif s == 503 or "locked" in json.dumps(d):
                    n_locked += 1
                else:
                    n_other += 1
            time.sleep(0.01)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    # 等缓冲完全落盘（队列清空）再核对数据
    drained = wait_buffer_drain(key, timeout=120)
    stats = buffer_stats(key)
    n_after = count_knowledge()
    lost = max(0, n_ok - (n_after - n_before))
    results["k%d" % concurrency] = {
        "concurrency": concurrency,
        "requests_ok": n_ok,
        "http_timeout": n_timeout,
        "http_other": n_other,
        "db_locked": n_locked,
        "peak_enqueue_latency_s": round(max(delays), 3) if delays else 0,
        "avg_enqueue_latency_s": round(sum(delays) / len(delays), 3) if delays else 0,
        "queue_depth_after": stats.get("queue_depth"),
        "sync_fallback_count": stats.get("sync_fallback_count"),
        "total_flushed": stats.get("total_flushed"),
        "db_new": n_after - n_before,
        "data_lost": lost,
        "buffer_drained": drained,
    }
    print("  [K%d并发 x%.0fs] ok=%d timeout=%d other=%d locked=%d peak_enq_lat=%.3fs "
          "fallback=%s db_new=%d lost=%d drained=%s" %
          (concurrency, elapsed, n_ok, n_timeout, n_other, n_locked,
           results["k%d" % concurrency]["peak_enqueue_latency_s"],
           stats.get("sync_fallback_count"), n_after - n_before, lost, drained), flush=True)


def chroma_dual_process():
    """T2-2: 双进程同时打开 ChromaDB，5 轮 × 2 进程"""
    print("\nT2-2 ChromaDB 双进程（5 轮 × 2 进程，各写 1 条）:", flush=True)
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_worker.py")
    rounds = []
    for rnd in range(5):
        procs = [subprocess.Popen([sys.executable, worker], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) for _ in range(2)]
        t0 = time.time()
        try:
            outs = [p.communicate(timeout=60)[0].strip() for p in procs]
            ok = sum(1 for o in outs if "PROC-OK" in o)
            rounds.append((rnd + 1, ok, round(time.time() - t0, 1)))
            print("  round %d: %d/2 OK in %.1fs" % (rnd + 1, ok, time.time() - t0), flush=True)
        except subprocess.TimeoutExpired:
            for p in procs:
                p.kill()
            rounds.append((rnd + 1, 0, 60))
            print("  round %d: TIMEOUT" % (rnd + 1), flush=True)
            break
    results["t22"] = {
        "rounds": rounds,
        "conclusion": "全部成功" if all(r[1] == 2 for r in rounds) else "存在失败",
    }
    return results["t22"]["conclusion"] == "全部成功"


def chroma_degrade(key=""):
    """T2-3: kill Hub -> rename chroma_db -> 占位文件 -> 启动 -> 验证降级 -> 恢复"""
    print("\nT2-3 降级路径故障注入:", flush=True)

    # 观察点A: 运行中 rename
    obs_a = "N/A"
    try:
        os.rename(CHROMA_PATH, BACKUP_PATH)
        obs_a = "rename 成功（无句柄占用？）"
        os.rename(BACKUP_PATH, CHROMA_PATH)
    except Exception as e:
        obs_a = "被拒 %s: %s" % (type(e).__name__, str(e)[:50])

    def kill_hub():
        # wmic 已在新版 Windows 移除：改用 netstat 按监听端口定位 Hub PID
        port = urllib.parse.urlparse(HUB).port or 3060
        r = subprocess.run("netstat -ano", shell=True, capture_output=True, text=True)
        pids = {line.split()[-1] for line in r.stdout.splitlines()
                if (":%d" % port) in line and "LISTENING" in line}
        for pid in pids:
            subprocess.run("taskkill /f /pid %s" % pid, shell=True, capture_output=True)
        time.sleep(3)

    def wait_hub(expect_chroma_ok):
        for _ in range(60):
            time.sleep(1)
            try:
                with urllib.request.urlopen(HUB + "/health", timeout=3) as resp:
                    hd = json.loads(resp.read())
                    st = hd.get("chromadb", {}).get("status", "?")
                    if expect_chroma_ok and st == "ok":
                        return True, st
                    if not expect_chroma_ok and st != "ok" and hd.get("database", {}).get("status") == "ok":
                        return True, st
            except Exception:
                pass
        return False, "timeout"

    # 正常态基线
    s, d, _ = req("POST", "/api/v1/memory/search", key,
                  {"agent_id": AGENT_ID, "query": "压测内容", "top_k": 5})
    base_total = d.get("total", 0)
    print("  [正常态] memory/search total=%d emb_unavail=%s"
          % (base_total, d.get("embedding_unavailable")), flush=True)

    # 注入
    kill_hub()
    if os.path.exists(BACKUP_PATH):
        import shutil
        shutil.rmtree(BACKUP_PATH, ignore_errors=True)
    if os.path.exists(CHROMA_PATH):
        os.rename(CHROMA_PATH, BACKUP_PATH)
    with open(CHROMA_PATH, "w") as f:
        f.write("PLACEHOLDER - chroma disabled for T2-3")
    print("  [注入] renamed + placeholder OK", flush=True)

    env = os.environ.copy()
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=WORKDIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    ready, st = wait_hub(expect_chroma_ok=False)
    print("  [注入] hub ready=%s chromadb=%s" % (ready, st), flush=True)

    # 故障态验证
    s, d, _ = req("POST", "/api/v1/memory/search", key,
                  {"agent_id": AGENT_ID, "query": "压测内容", "top_k": 5})
    deg_total = d.get("total", 0)
    deg_mark = d.get("embedding_unavailable")
    deg_has = bool(d.get("results"))
    print("  [故障态] memory/search total=%d emb_unavail=%s 有结果=%s"
          % (deg_total, deg_mark, deg_has), flush=True)

    s2, d2, _ = req("POST", "/api/v1/memory/semantic_search", key,
                    {"query": "压测内容", "requester_agent_id": AGENT_ID, "n_results": 5})
    ss_status = s2
    ss_body = json.dumps(d2, ensure_ascii=False)[:120]
    print("  [故障态] semantic_search status=%d body=%s" % (s2, ss_body), flush=True)

    # 恢复
    kill_hub()
    os.remove(CHROMA_PATH)
    os.rename(BACKUP_PATH, CHROMA_PATH)
    proc2 = subprocess.Popen([sys.executable, "main.py"], cwd=WORKDIR,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    ready2, st2 = wait_hub(expect_chroma_ok=True)
    print("  [恢复] hub=%s chromadb=%s" % (ready2, st2), flush=True)

    results["t23"] = {
        "observe_a": obs_a,
        "base_total": base_total,
        "degraded_total": deg_total,
        "degraded_mark": deg_mark,
        "degraded_has_result": deg_has,
        "semantic_search_status": ss_status,
        "semantic_search_body": ss_body,
        "recovered": ready2,
    }


def main():
    key = setup_agent()
    if not key:
        print("agent setup failed")
        return 1
    print("agent key ready: %s..." % key[:8])

    # 支持 P2_CONC 环境变量单档运行（T1-1 对照基线用），默认四档
    import os as _os
    conc_sel = _os.environ.get("P2_CONC", "25,50,100,200")
    concs = [int(c) for c in conc_sel.split(",") if c.strip()]

    print("\nT2-1a SQLite 缓冲路径（knowledge/upsert → asyncio.Queue 写入缓冲，HTTP 真实路径）:")
    for conc in concs:
        knowledge_stress(conc, 30, key)

    print("\n=== T2-1a 缓冲路径四档数据汇总 ===")
    print("%5s %7s %8s %7s %10s %10s %9s %7s %5s" %
          ("并发", "ok", "timeout", "locked", "peak_enq_lat", "fallback", "db_new", "lost", "q_depth"))
    for c in concs:
        r = results.get("k%d" % c)
        if r:
            print("%5d %7d %8d %7d %10.3f %10s %9d %7d %5s" %
                  (r["concurrency"], r["requests_ok"], r["http_timeout"], r["db_locked"],
                   r["peak_enqueue_latency_s"], r["sync_fallback_count"], r["db_new"],
                   r["data_lost"], r["queue_depth_after"]))

    print("\nT2-1b SQLite 直写路径（memory/store → 三段去重，HTTP 真实路径，观察项）:")
    for conc in concs:
        stress_level(conc, 30, key)

    print("\n=== T2-1b 直写路径四档数据汇总 ===")
    print("%5s %7s %8s %7s %10s %9s %10s" % ("并发", "ok", "timeout", "locked", "peak_lat", "avg_lat", "db_total"))
    for c in concs:
        r = results.get(c)
        if r and "concurrency" in r:
            print("%5d %7d %8d %7d %10.3f %9.3f %10d"
                  % (r["concurrency"], r["requests_ok"], r["http_timeout"], r["db_locked"],
                     r["peak_latency_s"], r["avg_latency_s"], r["db_total_after"]))

    # T2-2/T2-3 会 kill/重启 Hub 并动 chroma_db——P2_CONC 单档模式（T1-1 对照）跳过
    if _os.environ.get("P2_SKIP_CHROMA") == "1":
        print("\n(P2_SKIP_CHROMA=1: 跳过 T2-2/T2-3，不动 chroma_db)")
    else:
        chroma_dual_process()
        chroma_degrade(key)

    # 结果先落盘（防后续打印崩溃丢数据）
    out = os.path.join(WORKDIR, "tests", "p2-stress-results.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print("\n结果已落盘:", out)

    # 汇总打印
    print("\n=== 稳定性定性结论摘要 ===")
    t21a_locked = sum(results.get("k%d" % c, {}).get("db_locked", 0) for c in [25, 50, 100, 200])
    t21a_to = sum(results.get("k%d" % c, {}).get("http_timeout", 0) for c in [25, 50, 100, 200])
    t21a_lost = sum(results.get("k%d" % c, {}).get("data_lost", 0) for c in [25, 50, 100, 200])
    t21a_fb_vals = [results.get("k%d" % c, {}).get("sync_fallback_count") or 0 for c in [25, 50, 100, 200]]
    t21a_fb = max(t21a_fb_vals)
    print("T2-1a(缓冲): locked 总=%d timeout 总=%d 数据丢失总=%d fallback峰值=%s"
          % (t21a_locked, t21a_to, t21a_lost, t21a_fb))
    print("T2-2:", results.get("t22", {}).get("conclusion"))
    t23 = results.get("t23", {})
    print("T2-3: 降级标记=%s 有结果=%s semantic_status=%s 恢复=%s"
          % (t23.get("degraded_mark"), t23.get("degraded_has_result"),
             t23.get("semantic_search_status"), t23.get("recovered")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
