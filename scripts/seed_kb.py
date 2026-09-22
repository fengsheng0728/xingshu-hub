#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""K-1/T3: corpus/ 语料幂等灌入脚本（知识库 → 统一 chroma 集合）

用法：
    python scripts/seed_kb.py --key <API_KEY> [--hub http://127.0.0.1:3060]
                              [--agent-id <id>] [--corpus corpus/] [--rebuild]

行为：
    1. 扫描 corpus/*.md（排除 README.md 与点文件），解析 front-matter
       （title/category/tags/source，`---` 包裹，自带极简解析器，不引第三方 yaml）
    2. 逐篇 POST /api/v1/knowledge（Authorization: Bearer <key>）
    3. 可选 --rebuild：灌完后调 POST /api/v1/embeddings/rebuild
       （默认不需要——K-1 起 knowledge_upsert 写侧已同步入向量；
        仅换 embedding 模型后才需要全量重建，见 docs/architecture-decision-kb-retrieval.md §八）
    4. GET /api/v1/stats 取 chroma_vectors 前后对比，GET /api/v1/knowledge 取条目数，
       结构化摘要 JSON 落盘 corpus/.seed-report.json（已被 .gitignore 排除，不会入库）

幂等（连跑两次行数/向量数不变）：
    - entry_id 由文件名稳定派生：kb-<文件名 stem>（不用随机/时间戳）
    - knowledge_base 侧 INSERT OR REPLACE（同一 entry_id 覆盖，行数不变）
    - chroma 侧 chunk id = kb:{entry_id}:{piece_index} 确定性，写前先删该条目旧 chunk
    - document_chunks 不经过本管道（知识条目不写该表），行数天然不变

⚠️ 鉴权前提（任务书 §四.T3 坑位，务必先读）：
    POST /api/v1/knowledge 有角色门：role ∈ (manager, orchestrator) 否则 403。
    - Agent 走 /api/v1/agents/bootstrap 会覆盖角色为 worker → bootstrap 的 key 不可用。
    - 正确姿势 A：/api/v1/agents/register 注册时显式 role:"manager"，用返回的 api_key。
    - 正确姿势 B：hub_token（config.yaml auth.hub_token）做 Bearer，并用 --agent-id
      声明一个已注册为 manager 的 agent_id（hub_token 认证时身份取请求声明的 id，
      角色门仍查该 id 在 hub.agents 里的 role——hub.agents 是内存 dict，
      直写 DB 改 role 不生效，必须先经 register/心跳让内存态就位）。
    - 开发态 SYNC_HUB_NO_AUTH=1 时角色门整体绕过，可不传 --key。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CORPUS = os.path.join(REPO_ROOT, "corpus")
REPORT_NAME = ".seed-report.json"


# ═══════════ front-matter 极简解析（不引第三方 yaml） ═══════════

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_INLINE_LIST_RE = re.compile(r"\[(.*)\]")


def parse_front_matter(text: str) -> tuple:
    """解析 `---` 包裹的极简 YAML 子集。返回 (meta: dict, body: str)。

    只支持 `key: value` 与 `key: [a, b]` 行内列表；不满足即抛 ValueError。
    """
    m = _FM_RE.match(text)
    if not m:
        raise ValueError("缺少 front-matter（--- 包裹段）")
    meta = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"front-matter 行非法（缺冒号）: {line!r}")
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        lm = _INLINE_LIST_RE.fullmatch(val)
        if lm:
            meta[key] = [x.strip() for x in lm.group(1).split(",") if x.strip()]
        else:
            meta[key] = val
    return meta, text[m.end():]


def entry_id_from_filename(filename: str) -> str:
    """entry_id = kb-<文件名 stem>（稳定派生，幂等关键）。stem 必须已是 ascii slug。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", stem):
        raise ValueError(f"文件名须为 ascii slug（小写字母/数字/连字符）: {filename}")
    return f"kb-{stem}"


def build_entries(corpus_dir: str) -> list:
    """扫描 corpus 目录 → 条目列表（确定性顺序）。可独立导入供测试复用。"""
    entries = []
    for name in sorted(os.listdir(corpus_dir)):
        if not name.endswith(".md") or name == "README.md" or name.startswith("."):
            continue
        path = os.path.join(corpus_dir, name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        meta, body = parse_front_matter(text)
        for field in ("title", "category", "tags", "source"):
            if field not in meta:
                raise ValueError(f"{name}: front-matter 缺字段 {field}")
        entries.append({
            "entry_id": entry_id_from_filename(name),
            "title": meta["title"],
            "content": body.strip(),
            "category": meta["category"],
            "tags": meta["tags"] if isinstance(meta["tags"], list) else [meta["tags"]],
            "source": meta["source"],
            "source_file": name,
        })
    return entries


# ═══════════ HTTP 层（urllib，不用 curl——项目历史坑） ═══════════

def _http(method: str, url: str, key: str = "", payload: dict = None,
          timeout: int = 30) -> tuple:
    """返回 (status, data)。中文一律经 urllib.parse.quote（调用方拼 query 时）。"""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return e.code, {"error": "HTTPError body 非 JSON"}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _chroma_vectors(hub_url: str, key: str) -> int:
    s, d = _http("GET", hub_url.rstrip("/") + "/api/v1/stats", key)
    if s != 200:
        return -1
    v = (d.get("wiki") or {}).get("chroma_vectors")
    return v if isinstance(v, int) else -1


def _knowledge_count(hub_url: str, key: str) -> int:
    s, d = _http("GET", hub_url.rstrip("/") + "/api/v1/knowledge", key)
    if s != 200:
        return -1
    return len(d.get("entries") or [])


def seed(hub_url: str, key: str, corpus_dir: str, agent_id: str = "",
         do_rebuild: bool = False) -> dict:
    """灌入主流程。返回结构化摘要 dict（同时落盘 report）。"""
    t0 = time.time()
    hub_url = hub_url.rstrip("/")
    entries = build_entries(corpus_dir)

    before_vectors = _chroma_vectors(hub_url, key)
    before_kb = _knowledge_count(hub_url, key)

    ok, failed = [], []
    qs = ("?agent_id=" + urllib.parse.quote(agent_id)) if agent_id else ""
    for e in entries:
        payload = {
            "entry_id": e["entry_id"], "title": e["title"], "content": e["content"],
            "category": e["category"], "tags": e["tags"],
            "created_by": agent_id or "seed_kb",
        }
        s, d = _http("POST", hub_url + "/api/v1/knowledge" + qs, key, payload)
        if s == 200 and d.get("status") == "ok":
            ok.append(e["entry_id"])
        else:
            failed.append({"entry_id": e["entry_id"], "status": s,
                           "detail": str(d)[:200]})

    rebuild_result = None
    if do_rebuild and not failed:
        s, d = _http("POST", hub_url + "/api/v1/embeddings/rebuild" + qs, key, {})
        rebuild_result = {"status": s, "detail": d}

    # 写侧向量是异步前的同步段，但落库走缓冲队列 → 稍等再读统计
    time.sleep(2)
    after_vectors = _chroma_vectors(hub_url, key)
    after_kb = _knowledge_count(hub_url, key)

    report = {
        "corpus_dir": corpus_dir,
        "total_files": len(entries),
        "upserted": len(ok),
        "failed": failed,
        "knowledge_base_rows": {"before": before_kb, "after": after_kb},
        "chroma_vectors": {"before": before_vectors, "after": after_vectors},
        "rebuild": rebuild_result,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    report_path = os.path.join(corpus_dir, REPORT_NAME)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def main():
    ap = argparse.ArgumentParser(description="corpus/ 语料幂等灌入（K-1/T3）")
    ap.add_argument("--hub", default="http://127.0.0.1:3060")
    ap.add_argument("--key", default=os.environ.get("SYNC_HUB_SEED_KEY", ""),
                    help="manager/orchestrator 的 api_key 或 hub_token；也可用环境变量 SYNC_HUB_SEED_KEY（禁止硬编码）")
    ap.add_argument("--agent-id", default=os.environ.get("SYNC_HUB_SEED_AGENT", ""),
                    help="hub_token 模式下声明的已注册 manager agent_id")
    ap.add_argument("--corpus", default=DEFAULT_CORPUS)
    ap.add_argument("--rebuild", action="store_true",
                    help="灌完后全量重建向量（默认不需要；换 embedding 模型后才用）")
    args = ap.parse_args()

    report = seed(args.hub, args.key, args.corpus, args.agent_id, args.rebuild)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["failed"]:
        print(f"\n⚠️ {len(report['failed'])} 篇失败（403=角色门，见脚本头注释）", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
