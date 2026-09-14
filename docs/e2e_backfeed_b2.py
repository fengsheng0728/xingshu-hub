# -*- coding: utf-8 -*-
"""e2e_backfeed_b2.py — B2 黑盒验收装置（最终状态断言库）

任务书 batch-b2e2e 交付物。把 docs/phase4-backfeed-design.md §6-B2 的 e2e 判据
（双分干写同内容 → 主干一档案、分干归档、origin 重指向、回滚可逆）变成
可复跑、可断言的状态检查库。

设计约束（任务书硬性约束）：
- **黑盒**：只断言最终状态（文件系统 / data-trunk git 仓库 / index jsonl /
  档案 JSON），不 import、不调用 batch-b2 的任何实现符号——与实现并行零耦合。
  本文件只依赖既有基建 gitrepo.py（GitRepo API）与标准库。
- 每个 assert_* 函数失败抛 AssertionError 并带中文诊断信息；通过时返回
  结构化结果 dict 供报告使用。

index/<kind>.jsonl 行格式（hub_mixins/shadow.py _append_index 口径）：
  {"kind","id","owner","trust","level","date","tags","title","branch","path"}
  合并修正行：同 id 追加一行带 "merged_into": <canonical_id>；
  回滚修正行：同 id 追加一行带 "unmerged": true。
  读取语义：jsonl 不改历史行，**同 id 最后一行生效**（同 §2.3-1 /
  _origins_from_trunk「后批覆盖前批」语义）。

分干 vault md：YAML front-matter（kind/id/owner/trust/level/date/tags），
归档后 front-matter 追加 merged_into；归档路径 vault/_merged/<date>/<id>.md。

────────────────────────────────────────────────────────────────
真场景驱动方式（T3，Hermes 合流 batch-b2 后使用）：
  1. Hermes 起独立测试 Hub（DATA_TRUNK_ENABLED=true，独立 data-trunk 目录）；
  2. 经 Hub 正常写入链路向两个分干写同内容条目（memory/knowledge 均可）；
  3. 调 batch-b2 的自动合并入口（入口函数名占位
     `hub.backfeed_scan_and_merge(kinds=...)`——若 batch-b2 定名不同，
     由 Hermes 合流时同步此处文档与下方调用点）；
  4. 用本装置的断言函数对最终状态逐项断言：
       pairs = find_dup_pairs_by_hash(trunk_root, kind, chunk_hash)   # 合并前
       assert_trunk_has_single_canonical(trunk_root, kind, title)
       assert_source_archived(branch_root, src_relpath)
       assert_index_has_merged_into(trunk_root, kind, merged_id, canonical_id)
       entry = assert_origin_redirected(trunk_root, kind, merged_id)
       # 触发 batch-b2 回滚后：
       assert_rollback_restored(trunk_root, branch_root, kind, merged_id, src_relpath)
  其中 trunk_root = data-trunk 主干仓库根；branch_root =
  <trunk_root>/branches/<分干名>；src_relpath = 分干内原 vault 相对路径。

本文件 `__main__` 冒烟（T2 装置自测）：在临时目录用 GitRepo API 手工构造
「合并已完成」的假场景（不依赖 batch-b2），跑全部断言并打印 PASS/FAIL 清单；
另含负向探针（状态不满足时断言必须抛 AssertionError）验证判定逻辑不是恒真。
"""
import itertools
import json
import os
import sys
import tempfile

# 装置只依赖既有基建 gitrepo.py（repo 根目录）；docs/ 下运行需引导 sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gitrepo import GitRepo  # noqa: E402


# ════════════════════════════════════════════════════════════════
# 内部工具
# ════════════════════════════════════════════════════════════════

def _read_index_lines(trunk_root: str, kind: str) -> list:
    """读主干 index/<kind>.jsonl 全部行（解析失败的行跳过）。文件不存在返回 []。"""
    full = os.path.join(trunk_root, "index", f"{kind}.jsonl")
    if not os.path.isfile(full):
        return []
    rows = []
    with open(full, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def _effective_entry(rows: list, entry_id: str):
    """同 id 最后一行生效（§2.3-1 修正行语义）。无该 id 返回 None。"""
    eff = None
    for rec in rows:
        if rec.get("id") == entry_id:
            eff = rec
    return eff


def _parse_front_matter(text: str) -> dict:
    """解析打标 md 的 YAML front-matter（仅 key: value 平铺，够装置用）。"""
    if not text or not text.startswith("---"):
        return {}
    lines = text.splitlines()
    fm = {}
    closed = False
    for ln in lines[1:]:
        if ln.strip() == "---":
            closed = True
            break
        if ":" in ln:
            k, _, v = ln.partition(":")
            fm[k.strip()] = v.strip()
    return fm if closed else {}


def _strip_front_matter(text: str) -> str:
    """去掉 front-matter 取正文（find_dup_pairs_by_hash 的判定内容口径）。"""
    if not text or not text.startswith("---"):
        return text or ""
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    return text


def _branch_root(trunk_root: str, branch: str) -> str:
    """分干仓库根：data_trunk.ensure 布局 <trunk_root>/branches/<branch>。"""
    return os.path.join(trunk_root, "branches", branch)


# ════════════════════════════════════════════════════════════════
# T1 状态检查函数（断言库，黑盒；签名冻结，供 Hermes 合流后驱动真场景）
# ════════════════════════════════════════════════════════════════

def assert_trunk_has_single_canonical(trunk_root: str, kind: str,
                                      title_substr: str) -> dict:
    """断言主干 customers/ 存在恰好 1 个匹配档案，返回档案 dict。

    匹配口径：档案 JSON 的 kind 字段相等 且 title 字段含 title_substr。
    0 个或多于 1 个均抛 AssertionError。
    """
    cdir = os.path.join(trunk_root, "customers")
    matched = []
    if os.path.isdir(cdir):
        for name in sorted(os.listdir(cdir)):
            if not name.endswith(".json"):
                continue
            full = os.path.join(cdir, name)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except Exception:
                continue
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") == kind and title_substr in (doc.get("title") or ""):
                matched.append((name, doc))
    if not matched:
        raise AssertionError(
            f"主干 customers/ 未找到匹配档案：kind={kind} "
            f"title含'{title_substr}'（目录 {cdir}）")
    if len(matched) > 1:
        raise AssertionError(
            f"主干 customers/ 匹配档案不唯一（{len(matched)} 个，应为恰好 1 个）："
            f"{[n for n, _ in matched]}")
    name, doc = matched[0]
    doc = dict(doc)
    doc["_file"] = f"customers/{name}"
    return doc


def assert_source_archived(branch_root: str, src_relpath: str,
                           date_prefix: str = None) -> dict:
    """断言分干原路径已归档（§2.3-2：归档而非删除）。

    判定：
    1. 原路径 src_relpath 在 HEAD 不存在（git show HEAD:path 读不到）；
    2. vault/_merged/ 下存在同名归档文件（可配 date_prefix 限定归档日期目录），
       且其 front-matter 含非空 merged_into；
    3. read_at(src_relpath, "HEAD~1") 原路径历史可读（归档前版本留痕）。
    返回 {"archived_path", "merged_into", "history_excerpt"}。
    """
    repo = GitRepo(branch_root)
    if not repo.exists():
        raise AssertionError(f"分干仓库不存在：{branch_root}")
    head_content = repo.read_at(src_relpath, "HEAD")
    if head_content is not None:
        raise AssertionError(
            f"分干原路径在 HEAD 仍存在（应已归档搬走）：{src_relpath}")
    # 找归档文件：vault/_merged/<date>/<同名>.md
    base = os.path.basename(src_relpath)
    merged_root = os.path.join(branch_root, "vault", "_merged")
    candidates = []
    if os.path.isdir(merged_root):
        for dirpath, _dirnames, filenames in os.walk(merged_root):
            for fn in filenames:
                if fn != base:
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn),
                                      branch_root).replace(os.sep, "/")
                # rel = vault/_merged/<date>/<file>
                parts = rel.split("/")
                date_dir = parts[2] if len(parts) >= 4 else ""
                if date_prefix and not date_dir.startswith(date_prefix):
                    continue
                candidates.append(rel)
    if not candidates:
        raise AssertionError(
            f"分干 vault/_merged/ 下未找到归档文件：{base}"
            + (f"（date_prefix={date_prefix}）" if date_prefix else ""))
    if len(candidates) > 1:
        raise AssertionError(
            f"分干 vault/_merged/ 归档文件不唯一：{candidates}")
    archived = candidates[0]
    with open(os.path.join(branch_root, archived), "r", encoding="utf-8") as f:
        fm = _parse_front_matter(f.read())
    merged_into = fm.get("merged_into", "")
    if not merged_into:
        raise AssertionError(
            f"归档文件 front-matter 缺 merged_into：{archived}")
    history = repo.read_at(src_relpath, "HEAD~1")
    if history is None:
        raise AssertionError(
            f"分干原路径历史不可读（read_at HEAD~1 返回 None）：{src_relpath}")
    return {"archived_path": archived, "merged_into": merged_into,
            "history_excerpt": history[:120]}


def assert_index_has_merged_into(trunk_root: str, kind: str, merged_id: str,
                                 canonical_id: str) -> dict:
    """断言 index/<kind>.jsonl 含 merged_into 修正行（同 id 最后一行生效）。

    判定：merged_id 的生效条目（最后一行）的 merged_into == canonical_id。
    返回该生效条目。
    """
    rows = _read_index_lines(trunk_root, kind)
    eff = _effective_entry(rows, merged_id)
    if eff is None:
        raise AssertionError(
            f"index/{kind}.jsonl 不存在 id={merged_id} 的条目（共 {len(rows)} 行）")
    got = eff.get("merged_into", "")
    if got != canonical_id:
        raise AssertionError(
            f"index/{kind}.jsonl 中 id={merged_id} 的生效条目 "
            f"merged_into={got!r}，应为 {canonical_id!r}")
    return eff


def assert_origin_redirected(trunk_root: str, kind: str, merged_id: str) -> dict:
    """按「同 id 最后一行」语义读 index，返回 merged_id 当前生效条目。

    判定：生效条目带 merged_into，且主干 customers/ 下存在
    canonical_id 与之相等的档案 JSON（origin 重指向的落点真实存在，
    §2.3-3 读取端据此重指向 canonical 档案）。
    返回生效条目（附加 "_canonical_file" 字段）。
    """
    rows = _read_index_lines(trunk_root, kind)
    eff = _effective_entry(rows, merged_id)
    if eff is None:
        raise AssertionError(
            f"index/{kind}.jsonl 不存在 id={merged_id} 的条目（共 {len(rows)} 行）")
    merged_into = eff.get("merged_into", "")
    if not merged_into:
        raise AssertionError(
            f"index/{kind}.jsonl 中 id={merged_id} 的生效条目无 merged_into，"
            f"origin 未重指向：{eff}")
    cdir = os.path.join(trunk_root, "customers")
    hit = None
    if os.path.isdir(cdir):
        for name in sorted(os.listdir(cdir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(cdir, name), "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except Exception:
                continue
            if isinstance(doc, dict) and doc.get("canonical_id") == merged_into:
                hit = f"customers/{name}"
                break
    if hit is None:
        raise AssertionError(
            f"origin 重指向落空：merged_into={merged_into} 在主干 customers/ "
            f"无对应档案（canonical_id 不匹配）")
    out = dict(eff)
    out["_canonical_file"] = hit
    return out


def assert_rollback_restored(trunk_root: str, branch_root: str, kind: str,
                             merged_id: str, src_relpath: str) -> dict:
    """断言 undo 后状态还原（§2.3-5：归档可逆）。

    判定：
    1. 分干原路径 src_relpath 在 HEAD 重新可读（文件移回原路径）；
    2. index/<kind>.jsonl 中 merged_id 的生效条目带 unmerged 修正标记。
    返回 {"restored_path", "index_entry"}。
    """
    repo = GitRepo(branch_root)
    if not repo.exists():
        raise AssertionError(f"分干仓库不存在：{branch_root}")
    content = repo.read_at(src_relpath, "HEAD")
    if content is None:
        raise AssertionError(
            f"回滚后分干原路径在 HEAD 仍不可读（应已移回）：{src_relpath}")
    rows = _read_index_lines(trunk_root, kind)
    eff = _effective_entry(rows, merged_id)
    if eff is None:
        raise AssertionError(
            f"index/{kind}.jsonl 不存在 id={merged_id} 的条目（共 {len(rows)} 行）")
    if not eff.get("unmerged"):
        raise AssertionError(
            f"index/{kind}.jsonl 中 id={merged_id} 的生效条目无 unmerged "
            f"修正标记（回滚未落账）：{eff}")
    return {"restored_path": src_relpath, "index_entry": eff}


def find_dup_pairs_by_hash(trunk_root: str, kind: str, hash_fn) -> list:
    """从 index + 分干 vault md 找出内容哈希相等的条目对（§2.1 判定单元复刻）。

    判定单元：index/<kind>.jsonl 的生效条目（同 id 最后一行生效；
    生效行带 merged_into 的已合并条目跳过——内容已归档不在原路径）。
    条目内容取分干 vault md 正文（去 front-matter），用注入的 hash_fn
    规范化哈希；哈希相等且 id 不同的条目两两成对。
    （§2.1 精确重复短路：chunk_hash 相等的对直接自动合并。）

    hash_fn: Callable[[str], str]，如 chunker.chunk_hash。
    返回 [{id_a,id_b,branch_a,branch_b,path_a,path_b,hash}, ...]（按 id 排序）。
    """
    rows = _read_index_lines(trunk_root, kind)
    by_id = {}
    for rec in rows:  # 同 id 最后一行生效
        rid = rec.get("id")
        if rid:
            by_id[rid] = rec
    buckets = {}
    for rid, rec in by_id.items():
        if rec.get("merged_into"):
            continue  # 已合并条目内容已归档，不参与再判定
        branch = rec.get("branch", "")
        path = rec.get("path", "")
        if not branch or not path:
            continue
        full = os.path.join(_branch_root(trunk_root, branch), path)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "r", encoding="utf-8") as f:
                body = _strip_front_matter(f.read())
        except Exception:
            continue
        h = hash_fn(body)
        buckets.setdefault(h, []).append(
            {"id": rid, "branch": branch, "path": path})
    pairs = []
    for h, items in buckets.items():
        for a, b in itertools.combinations(sorted(items, key=lambda x: x["id"]), 2):
            pairs.append({"id_a": a["id"], "id_b": b["id"],
                          "branch_a": a["branch"], "branch_b": b["branch"],
                          "path_a": a["path"], "path_b": b["path"],
                          "hash": h})
    return pairs


# ════════════════════════════════════════════════════════════════
# T2 装置自测（手工假场景，不依赖 batch-b2）+ T3 __main__ 冒烟
# ════════════════════════════════════════════════════════════════

def _fm(meta: dict) -> str:
    """打标 md front-matter（对齐 hub_mixins/shadow.py _front_matter 口径）。"""
    lines = ["---"]
    for k in ("kind", "id", "owner", "trust", "level", "date"):
        v = meta.get(k, "")
        if v is not None:
            lines.append(f"{k}: {v}")
    for k, v in meta.items():  # merged_into 等附加字段
        if k not in ("kind", "id", "owner", "trust", "level", "date", "tags"):
            lines.append(f"{k}: {v}")
    tags = meta.get("tags") or []
    if tags:
        lines.append("tags: [" + ", ".join(str(t) for t in tags) + "]")
    lines.append("---")
    return "\n".join(lines)


def _append_jsonl(repo: GitRepo, rel: str, record: dict):
    """主干 jsonl 纯追加（读工作区累积——同 _append_index 的教训）。"""
    full = os.path.join(repo.root, rel)
    old = ""
    if os.path.exists(full):
        with open(full, "r", encoding="utf-8") as f:
            old = f.read()
    line = json.dumps(record, ensure_ascii=False)
    text = (old.rstrip("\n") + "\n" + line + "\n") if old else line + "\n"
    repo.write_file(rel, text)


def run_self_test() -> int:
    """手工构造「合并已完成」假场景并跑全部断言。返回进程退出码。"""
    from chunker import chunk_hash  # 既有规范化哈希（chunker.py:45），非 batch-b2 符号

    results = []

    def check(name, fn, expect_fail=False):
        """expect_fail=True 为负向探针：fn 必须抛 AssertionError 才算通过。"""
        try:
            out = fn()
        except AssertionError as e:
            ok = expect_fail
            detail = ("符合预期地拒绝: " if expect_fail else "") + str(e)
        except Exception as e:
            ok, detail = False, f"意外异常 {type(e).__name__}: {e}"
        else:
            if expect_fail:
                ok, detail = False, "状态不满足却未抛 AssertionError（判定逻辑失效）"
            else:
                ok, detail = True, json.dumps(out, ensure_ascii=False,
                                              default=str)[:150]
        results.append(ok)
        print(("PASS" if ok else "FAIL"), "-", name, "|", detail, flush=True)

    KIND = "memory"
    TITLE = "客户X的交付周期偏好"
    BODY = "客户X偏好两周交付周期，里程碑每周五对齐。"
    D1, D2 = "2026-09-01", "2026-09-02"
    ID_A, ID_B = "mem-001", "mem-117"        # A=default 分干保留方，B=proj-alpha 被合并方
    CANON = "cust:c-demo"                    # canonical_id（含冒号，文件名需安全化）
    CANON_FILE = "customers/cust_c-demo.json"
    SRC_REL = f"vault/memory/{D1}/{ID_B}.md"
    DST_REL = f"vault/_merged/{D2}/{ID_B}.md"

    tmp = tempfile.mkdtemp(prefix="b2e2e-selftest-")
    trunk_root = os.path.join(tmp, "data-trunk")
    trunk = GitRepo(trunk_root)
    assert trunk.ensure(), "主干 git init 失败"
    # 主干 .gitignore 忽略 branches/（同 data_trunk.py:171-173——嵌套仓库防 add -A 失败）
    trunk.write_file(".gitignore", "branches/\n")
    b_default = GitRepo(_branch_root(trunk_root, "default"))
    b_alpha = GitRepo(_branch_root(trunk_root, "proj-alpha"))
    assert b_default.ensure() and b_alpha.ensure(), "分干 git init 失败"

    # ── 步骤1：双分干写同内容（合并前状态）──
    meta_a = {"kind": KIND, "id": ID_A, "owner": "cs-bot-2", "trust": "internal",
              "level": "summary", "date": D1}
    meta_b = {"kind": KIND, "id": ID_B, "owner": "sales-bot-1", "trust": "federated",
              "level": "full", "date": D1}
    b_default.write_file(f"vault/{KIND}/{D1}/{ID_A}.md", _fm(meta_a) + "\n\n" + BODY)
    b_alpha.write_file(SRC_REL, _fm(meta_b) + "\n\n" + BODY)
    assert b_default.commit("假场景: default 写入 mem-001")
    assert b_alpha.commit("假场景: proj-alpha 写入 mem-117")
    idx_rel = f"index/{KIND}.jsonl"
    _append_jsonl(trunk, idx_rel, {**meta_a, "title": TITLE, "branch": "default",
                                   "path": f"vault/{KIND}/{D1}/{ID_A}.md"})
    _append_jsonl(trunk, idx_rel, {**meta_b, "title": TITLE, "branch": "proj-alpha",
                                   "path": SRC_REL})
    assert trunk.commit("假场景: 主干 index 两条目")

    # ── 步骤2：合并前判定 —— 找重 + 负向探针 ──
    check("T1.find_dup_pairs_by_hash 找出同内容条目对（合并前）", lambda: (
        lambda pairs: pairs if (ID_A, ID_B) in
        {(p["id_a"], p["id_b"]) for p in pairs} else (_ for _ in ()).throw(
            AssertionError(f"未找出 ({ID_A},{ID_B}) 对: {pairs}"))
    )(find_dup_pairs_by_hash(trunk_root, KIND, chunk_hash)))
    check("负向: 合并前 assert_source_archived 必须拒绝（原路径仍在）",
          lambda: assert_source_archived(b_alpha.root, SRC_REL), expect_fail=True)
    check("负向: 合并前 assert_trunk_has_single_canonical 必须拒绝（无档案）",
          lambda: assert_trunk_has_single_canonical(trunk_root, KIND, TITLE),
          expect_fail=True)

    # ── 步骤3：模拟合并执行（装置外手工造终态，代替 batch-b2 执行器）──
    # 3a. 主干写 canonical 档案（§3 schema；文件名安全化，canonical_id 含冒号）
    trunk.write_file(CANON_FILE, json.dumps({
        "canonical_id": CANON, "kind": KIND, "title": TITLE,
        "content_digest": "sha256:" + chunk_hash(BODY),
        "identity": {"owner_key_fp": "a1b2c3d4e5f60708",
                     "owner_agent_ids": ["sales-bot-1", "cs-bot-2"]},
        "sources": [
            {"branch": "default", "kind": KIND, "id": ID_A,
             "path": f"vault/{KIND}/{D1}/{ID_A}.md",
             "trunk_commit": trunk.head_hash(),
             "branch_commit": b_default.head_hash()},
            {"branch": "proj-alpha", "kind": KIND, "id": ID_B,
             "path": DST_REL,
             "trunk_commit": trunk.head_hash(),
             "branch_commit": b_alpha.head_hash()}],
        "tags_snapshot": {"trust": "federated", "level": "summary",
                          "tainted_at": "", "locked": False},
        "merge_history": [{"ts": f"{D2}T03:00:00+08:00", "action": "auto_merge",
                           "cos": 1.0, "absorbed": [f"proj-alpha:{ID_B}"],
                           "actor": "system", "queue_id": None,
                           "before": {"trust": "internal", "level": "summary"},
                           "after": {"trust": "federated", "level": "summary"}}],
        "created_at": f"{D2}T03:00:00+08:00",
        "updated_at": f"{D2}T03:00:00+08:00",
    }, ensure_ascii=False, indent=2))
    # 3b. index 追加 merged_into 修正行（同 id 最后一行生效）
    _append_jsonl(trunk, idx_rel, {**meta_b, "title": TITLE, "branch": "proj-alpha",
                                   "path": SRC_REL, "merged_into": CANON})
    assert trunk.commit("假场景: canonical 档案 + index 修正行")
    # 3c. 分干归档：先在原路径把 merged_into 写进 front-matter 并 commit
    #     （保证 HEAD~1 原路径历史可读），再 move_file 搬到 vault/_merged/
    b_alpha.write_file(SRC_REL, _fm({**meta_b, "merged_into": CANON}) + "\n\n" + BODY)
    assert b_alpha.commit("假场景: 归档前打 merged_into 标")
    assert b_alpha.move_file(SRC_REL, DST_REL, f"假场景: 归档 {ID_B} -> _merged")

    # ── 步骤4：合并后终态断言（T1 全量）──
    check("T1.assert_trunk_has_single_canonical 主干恰好一档案",
          lambda: assert_trunk_has_single_canonical(trunk_root, KIND, TITLE))
    check("T1.assert_source_archived 分干归档三判定",
          lambda: assert_source_archived(b_alpha.root, SRC_REL, date_prefix=D2))
    check("T1.assert_index_has_merged_into index 修正行生效",
          lambda: assert_index_has_merged_into(trunk_root, KIND, ID_B, CANON))
    check("T1.assert_origin_redirected origin 重指向 canonical",
          lambda: assert_origin_redirected(trunk_root, KIND, ID_B))
    check("负向: 已合并条目不再参与找重（find_dup_pairs_by_hash 应为空）", lambda: (
        lambda pairs: (_ for _ in ()).throw(
            AssertionError(f"已合并条目仍被找出: {pairs}")) if pairs else {"pairs": 0}
    )(find_dup_pairs_by_hash(trunk_root, KIND, chunk_hash)))
    check("负向: 回滚前 assert_rollback_restored 必须拒绝",
          lambda: assert_rollback_restored(trunk_root, b_alpha.root, KIND, ID_B,
                                           SRC_REL),
          expect_fail=True)

    # ── 步骤5：模拟回滚（§2.3-5：移回原路径 + index unmerged 修正行）──
    assert b_alpha.move_file(DST_REL, SRC_REL, f"假场景: 回滚 {ID_B} 移回原路径")
    _append_jsonl(trunk, idx_rel, {**meta_b, "title": TITLE, "branch": "proj-alpha",
                                   "path": SRC_REL, "unmerged": True})
    assert trunk.commit("假场景: 回滚 unmerged 修正行")

    check("T1.assert_rollback_restored 回滚后文件回原路径 + unmerged 落账",
          lambda: assert_rollback_restored(trunk_root, b_alpha.root, KIND, ID_B,
                                           SRC_REL))
    check("负向: 回滚后 assert_index_has_merged_into 必须拒绝（unmerged 行生效）",
          lambda: assert_index_has_merged_into(trunk_root, KIND, ID_B, CANON),
          expect_fail=True)

    print(f"\n假场景目录: {tmp}")
    passed = sum(1 for ok in results if ok)
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(run_self_test())
