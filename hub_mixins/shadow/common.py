# -*- coding: utf-8 -*-
"""hub_mixins/shadow.py — 影子双写器（阶段3-P1）

SQLite 落库成功后，把同一条数据镜像落主干-分干 git 仓库群（真相源）：
- **打标 md** → 分干 vault/（含 6 维敏感度标记 + 来源信任级 + 时间）
- **元数据索引** → 主干 index/<kind>.jsonl（**不含 content 全文**——蓝图「内容不上行」红线）

设计（对照方案 D4/D5）：
- `submit(kind, payload)` 线程安全入队（deque + Lock），O(1) 零阻塞——主写入路径无感知
- daemon worker 攒批（0.5s 或 50 条触发）→ 写文件 + 分干/主干双 commit
- P2 交付1：按属主 agent 的 scope/映射表解析可写分干（branch_for_agent），默认全走 default
- P2 交付2：批 commit 后登记真相源定位（内存 _origins + 主干 index/.commits.jsonl），
  网关读取经 collect_origins 附 origin 字段
- P2 交付4：批 commit 后追加主干 audit/chain-head.jsonl（audit 哈希链链头 ↔ git 历史互证，
  需构造时传 audit_db_path；空则不写）
- enabled=false 或 shadow[kind]=false 时 submit 直接 no-op（回归零影响）
- 任何失败静默降级 + 失败计数（影子是增强不是依赖）
- G1 批1（崩溃一致性，docs/shadow-consistency-design.md §3 方案A）：
  submit 入队前先落 shadow_pending 表（SQLite WAL，独立连接）；
  flush 成功软标记 done；flush 失败 attempts+1、超限（3 次）标记 failed；
  start() 启动 replay 未完成的 pending 行重入队（幂等靠 index 同 id 去重）
- 阶段4-B2（反哺精确去重合并，docs/phase4-backfeed-design.md §2.3/§3/§6-B2）：
  execute_merge / undo_merge / scan_and_merge —— chunk_hash 相等自动合并为
  主干 customers/ canonical 档案；index 追加 merged_into/unmerged 修正行
  （读取端 collect_origins 重指向）；fail-closed：data_trunk.backfeed.enabled
  缺省即整体 no-op
- G1 批2（同文档 §3 方案B 并入项 + §5 失败可见性）：
  worker 看门狗——独立 daemon 巡检线程定期 is_alive() 检查，worker 意外死亡
  （enabled 且非停止中）则重建 Thread 对象重启（threading 线程不可 restart），
  记 stats.watchdog_restarts。选独立巡检线程而非「flush 后自检」：worker 自己
  死了就没有 flush 可自检，只有体外巡检能闭环场景③；
  write_file() 返回 False 计入失败路径（与批1 commit 检查同模式）；
  stats_snapshot() 供 stats 端点暴露；attempts 超限标 failed 时落 audit_log
  （entry_type=shadow_batch_failed，复用 audit_chain 行级哈希链）；
  连续 flush 失败触发告警钩子（hub_mixins.notifications.shadow_alert 去抖）

挂钩点（P1）：
- memory  → hub_mixins/memory.py store_memory commit 后
- knowledge → hub_mixins/buffer.py _batch_write_knowledge commit 后（同步 def）
- wiki    → hub_mixins/ingest.py ingest_chunks commit 后（父文档聚合）
- shared  → shared_workspace.py create_doc commit 后
"""
import collections
import datetime
import json
import logging
import os
import re
import sqlite3
import threading
import time

logger = logging.getLogger("xingshu.shadow")

_BATCH_SIZE = 50      # 每批最大条数
_BATCH_INTERVAL = 0.5  # 攒批间隔（秒）
_PENDING_MAX_ATTEMPTS = 3  # flush 失败重试上限，超限标记 failed（G1 批1）
_WATCHDOG_INTERVAL = 2.0   # worker 看门狗巡检周期（秒，G1 批2）

# shadow_pending 表 DDL。正式迁移在 db.py（SCHEMA_VERSION=6）；
# 此处防御性建表只为 D4 降级兜底（如 pending 库未迁移/独立测试库），不替代正式迁移。
_PEND_DDL = """CREATE TABLE IF NOT EXISTS shadow_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0
)"""


def _today() -> str:
    return datetime.date.today().isoformat()


def _safe_name(name: str) -> str:
    """Windows 文件名安全化：entry_id/doc_id 可能含冒号等非法字符（如
    'doc:p1-doc-0'）——实测直接写文件抛 OSError 整批失败。"""
    return re.sub(r'[<>:"/\\|?*]', "_", str(name))


# ── 阶段4-B2 反哺归并辅助（设计 §2.3/§3，chunk_hash 精确去重档）──

_MERGE_MIN_AGE_SEC = 60.0  # 不可合并窗口（§2.3-3）：刚写入 <60s 的条目不参与归并，
                           # 防镜像途中 origin 悬空（攒批间隔 0.5s × 安全余量）


def _ts_epoch(ts: str):
    """ISO 时间戳 → epoch 秒；解析失败返回 None。"""
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def _strip_front_matter(text: str) -> str:
    """剥 YAML front-matter 取正文（chunk_hash 判定只针对正文，不含打标头）。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _with_front_matter_field(text: str, key: str, value: str) -> str:
    """front-matter 追加/覆盖一个字段（归档标记 merged_into 用）。无 front-matter 则新建。"""
    line = "%s: %s" % (key, value)
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head = [ln for ln in text[:end].splitlines()
                    if not ln.startswith(key + ":")]
            head.append(line)
            return "\n".join(head) + text[end:]
    return "---\n%s\n---\n\n%s" % (line, text)


def _drop_front_matter_field(text: str, key: str) -> str:
    """front-matter 移除一个字段（回滚时去掉 merged_into 用）。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head = [ln for ln in text[:end].splitlines()
                    if not ln.startswith(key + ":")]
            return "\n".join(head) + text[end:]
    return text


def _index_rows(dt, kind: str) -> list:
    """读主干 index/<kind>.jsonl 全部行（工作区，含未 commit 尾部，同 _append_index 教训）。"""
    rows = []
    for ln in _read_worktree(dt.trunk.root, f"index/{kind}.jsonl").splitlines():
        if not ln.strip():
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def _index_state(rows: list) -> dict:
    """index 修正行语义：同 id 最后一行生效（§2.3-1，同 _origins_from_trunk
    「后批覆盖前批」）。返回 id → {"row": 最后一条含 path 的原始行,
    "merged_into": 当前生效的 canonical_id 或 None（unmerged 修正行撤销）}。"""
    state = {}
    for rec in rows:
        rid = rec.get("id")
        if not rid:
            continue
        st = state.setdefault(rid, {"row": None, "merged_into": None})
        if rec.get("path"):
            st["row"] = rec
        if rec.get("unmerged"):
            st["merged_into"] = None
        elif rec.get("merged_into"):
            st["merged_into"] = rec["merged_into"]
    return state


def append_index_correction(dt, kind: str, record: dict) -> bool:
    """index/<kind>.jsonl 追加修正行（merged_into / unmerged）。

    与 _append_index 纯追加风格一致但不按 id 去重——修正行本就同 id 多行，
    读取端取最后一行生效；历史行不改。
    """
    rel = f"index/{kind}.jsonl"
    full = os.path.join(dt.trunk.root, rel)
    old = ""
    try:
        if os.path.exists(full):
            with open(full, "r", encoding="utf-8") as f:
                old = f.read()
    except Exception:
        old = ""
    line = json.dumps(record, ensure_ascii=False)
    text = (old.rstrip("\n") + "\n" + line + "\n") if old else line + "\n"
    return dt.trunk.write_file(rel, text)


def _front_matter(kind: str, meta: dict) -> str:
    """打标 md 文件头：YAML front-matter（元数据可机器读，正文是内容）。"""
    lines = ["---"]
    for k in ("kind", "id", "owner", "trust", "level", "date"):
        v = meta.get(k, "")
        if v is not None:
            lines.append(f"{k}: {v}")
    tags = meta.get("tags") or []
    if tags:
        lines.append("tags: [" + ", ".join(str(t) for t in tags) + "]")
    lines.append("---")
    return "\n".join(lines)


# ── 真相源定位查询（P2 交付2，网关读取端点用）──

_INDEX_KINDS = ("memory", "knowledge", "wiki", "shared")


def collect_origins(data_trunk, shadow_writer=None, ids=()) -> dict:
    """id → 真相源定位 {kind, branch, path, trunk_commit, branch_commit, ts}。

    优先本进程内存映射（shadow_writer._origins）；未命中回源主干
    index/.commits.jsonl + index/<kind>.jsonl（重启后历史数据仍可定位）。
    任何失败静默返回已找到部分（D4：定位是增强，不影响读取主链路）。
    """
    want = [str(i) for i in ids if i]
    out = {}
    try:
        if shadow_writer is not None:
            for i in want:
                o = shadow_writer._origins.get(i)
                if o:
                    out[i] = dict(o)
        missing = [i for i in want if i not in out]
        if missing and data_trunk is not None \
                and getattr(data_trunk, "enabled", False):
            out.update(_origins_from_trunk(data_trunk, missing))
    except Exception:
        logger.exception("collect_origins 异常（降级返回部分结果）")
    return out


def _read_worktree(root: str, rel: str) -> str:
    """读主干工作区文件（.commits.jsonl 可能含未 commit 尾部，与 _append_index 同理）。"""
    try:
        full = os.path.join(root, rel)
        if os.path.exists(full):
            with open(full, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as _exc:
        logger.debug("shadow silent-except @1338: %s", _exc)
    return ""


def _origins_from_trunk(dt, ids) -> dict:
    """纯文件回源：index/<kind>.jsonl 给 path/branch，.commits.jsonl 给 commit。

    阶段4-B2：命中 merged_into 修正行（且未被后续 unmerged 撤销）的 id 重指向
    canonical 档案（customers/<canonical_id>.json，主干侧、无分干），并附
    merged: true 提示（§2.3-3 删除传播对策：index 修正行是权威）。
    """
    want = set(ids)
    # 1) id → (kind, branch, path)；修正行语义：同 id 最后一行生效
    loc = {}
    merged_into = {}
    for kind in _INDEX_KINDS:
        for rid, st in _index_state(_index_rows(dt, kind)).items():
            if rid not in want:
                continue
            if st.get("row"):
                loc[rid] = {"kind": kind, "branch": st["row"].get("branch", ""),
                            "path": st["row"].get("path", "")}
            if st.get("merged_into"):
                merged_into[rid] = st["merged_into"]
    if not loc:
        return {}
    # 2) id → commit（后批覆盖前批：取最后一次写入的定位）
    commits = {}
    text = _read_worktree(dt.trunk.root, "index/.commits.jsonl")
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        heads = rec.get("branches") or {}
        for rid in rec.get("ids") or []:
            if rid in loc:
                branch = loc[rid]["branch"]
                commits[rid] = {"trunk_commit": rec.get("commit", ""),
                                "branch_commit": heads.get(branch, ""),
                                "ts": rec.get("ts", "")}
    # 3) join
    out = {}
    for rid, meta in loc.items():
        c = commits.get(rid)
        if c:
            out[rid] = {**meta, **c}
    # 4) 阶段4-B2：merged_into 重指向 canonical 档案（附 merged: true 提示）
    for rid, cid in merged_into.items():
        if rid in out:
            out[rid]["branch"] = ""  # canonical 档案在主干侧，无分干
            out[rid]["path"] = dt.canonical_path(cid)
            out[rid]["merged"] = True
            out[rid]["canonical_id"] = cid
    return out
