"""
Karpathy LLM Wiki — 三层知识库
raw/ (不可变源) → entities/ concepts/ comparisons/ queries/ (agent 维护) → SCHEMA.md (约束)

路径校验：所有写入/读取必须在 WIKI_ROOT 内。
XSS 防御：markdown 渲染前 HTML 实体转义，不信任任何用户输入。
"""
import hashlib
import logging
import os
import re
import html
import json
import sys
from datetime import datetime

logger = logging.getLogger("xingshu.wiki_engine")

try:
    from models import CONFIG as _CONFIG   # CD-070b：产物根可配（env > config.yaml > 仓库内默认）
except Exception:                          # 独立脚本场景：退回仓库内默认路径
    _CONFIG = None
# CD-070b（2026-09-20）：wiki 根可配。事故：跑一轮测试即重写生产 wiki/ 页面（PROBE 实测 index.md 940→1003）。
WIKI_ROOT = (_CONFIG.WIKI_ROOT if _CONFIG else "") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wiki")



def ensure_wiki():
    """初始化 wiki 目录结构（幂等）"""
    dirs = ["raw/articles", "raw/papers", "raw/transcripts", "raw/assets",
            "entities", "concepts", "comparisons", "queries"]
    for d in dirs:
        os.makedirs(os.path.join(WIKI_ROOT, d), exist_ok=True)

    schema_path = os.path.join(WIKI_ROOT, "SCHEMA.md")
    if not os.path.exists(schema_path):
        with open(schema_path, "w", encoding="utf-8") as f:
            f.write("""# Wiki Schema

## Domain
星枢知识库 — 团队信息协同、披露规则、任务调度、记忆系统

## Conventions
- 文件名: 小写+连字符 (如 `return-policy.md`)
- 使用 `[[wikilinks]]` 链接其他页面（每页至少 2 个出链）
- YAML frontmatter 必填
- 更新页面时同步更新 index.md 和 log.md

## Frontmatter
```yaml
---
title: 页面标题
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: entity | concept | comparison | query
tags: [tag1, tag2]
sources: [raw/articles/source.md]
---
```

## Tag Taxonomy
- 业务: policy, pricing, workflow, onboarding
- 团队: role, permission, disclosure, collaboration
- 技术: architecture, security, api, deployment
- 客户: client, case-study, feedback
""")

    index_path = os.path.join(WIKI_ROOT, "index.md")
    if not os.path.exists(index_path):
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(f"""# Wiki Index
> 页面目录。每页一行：wikilink + 摘要。
> 更新: {datetime.now().strftime('%Y-%m-%d')} | 页面: 0

## Entities

## Concepts

## Comparisons

## Queries
""")

    log_path = os.path.join(WIKI_ROOT, "log.md")
    if not os.path.exists(log_path):
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"""# Wiki Log
> 操作日志（追加式）

## [{datetime.now().strftime('%Y-%m-%d')}] create | Wiki 初始化
- 三层结构已创建: raw/ entities/ concepts/ comparisons/ queries/
- SCHEMA.md, index.md, log.md 已生成
""")


def validate_path(rel_path: str) -> str:
    """路径安全校验：realpath 必须在 WIKI_ROOT 内，拒绝符号链接和上级目录逃逸"""
    if not rel_path or ".." in rel_path or rel_path.startswith("/") or "\\" in rel_path:
        raise ValueError(f"非法路径: {rel_path}")
    abs_path = os.path.realpath(os.path.join(WIKI_ROOT, rel_path))
    wiki_real = os.path.realpath(WIKI_ROOT)
    if not abs_path.startswith(wiki_real + os.sep) and abs_path != wiki_real:
        raise ValueError(f"路径逃逸: {rel_path} → {abs_path}")
    return abs_path


def md_to_html(text: str) -> str:
    """将 markdown 渲染为安全 HTML。
    所有原始 HTML 标签先转义 → 再应用 markdown 规则。
    [[wikilinks]] → <a href='/wiki/view/页面名' class='wikilink'>
    """
    # 1. 转义所有 HTML（防 XSS）
    text = html.escape(text, quote=False)

    # 2. [[wikilinks]]
    text = re.sub(r'\[\[([^\]]+)\]\]', r'<a href="/wiki/view/\1" class="wikilink">\1</a>', text)

    # 3. 代码块 ```
    text = re.sub(r'```(\w*)\n(.*?)```', r'<pre><code class="language-\1">\2</code></pre>', text, flags=re.DOTALL)

    # 4. 行内代码 `
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)

    # 5. 标题
    text = re.sub(r'^#### (.+)$', r'<h4>\1</h4>', text, flags=re.MULTILINE)
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$', r'<h1>\1</h1>', text, flags=re.MULTILINE)

    # 6. 粗体/斜体
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)

    # 7. 链接 [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)

    # 8. 无序列表
    text = re.sub(r'^(  )?[-*] (.+)$', r'<li>\2</li>', text, flags=re.MULTILINE)
    text = re.sub(r'(<li>.*</li>\n?)+', r'<ul>\n\g<0></ul>', text)

    # 9. 段落（双换行）
    paragraphs = text.split('\n\n')
    result = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if p.startswith('<h') or p.startswith('<pre') or p.startswith('<ul') or p.startswith('<table'):
            result.append(p)
        else:
            result.append(f'<p>{p.replace(chr(10), "<br>")}</p>')
    return '\n'.join(result)


def list_pages() -> list[dict]:
    """列出所有 wiki 页面（entities/concepts/comparisons/queries）"""
    pages = []
    for subdir in ["entities", "concepts", "comparisons", "queries"]:
        d = os.path.join(WIKI_ROOT, subdir)
        if not os.path.isdir(d):
            continue
        for fname in sorted(os.listdir(d)):
            if fname.endswith(".md") and not fname.startswith("_"):
                path = os.path.join(d, fname)
                rel = f"{subdir}/{fname}"
                stat = os.stat(path)
                # 尝试读 frontmatter
                try:
                    with open(path, encoding="utf-8") as f:
                        content = f.read()
                    fm = {}
                    if content.startswith("---"):
                        end = content.find("---", 3)
                        if end > 0:
                            for line in content[3:end].strip().split("\n"):
                                if ":" in line:
                                    k, v = line.split(":", 1)
                                    fm[k.strip()] = v.strip()
                except Exception:
                    fm = {}
                pages.append({
                    "path": rel,
                    "title": fm.get("title", fname.replace(".md", "").replace("-", " ").title()),
                    "type": subdir.rstrip("s"),
                    "tags": fm.get("tags", ""),
                    "updated": fm.get("updated", ""),
                    "size": stat.st_size,
                })
    return pages


def get_graph(privileged: bool = False) -> dict:
    """生成 [[wikilinks]] 关系图数据（D3 force-directed 格式）

    CD-067（T29，2026-09-20）：node label 取自 `list_pages_scoped(privileged)` ——
    派生页在「min(页面级别, 主体上限)」不足 full 时其 label（即 title）已脱敏为
    `[记忆] memory-<id 前 8 位>`。**默认 privileged=False 即脱敏**（fail-closed：
    未显式声明特权主体时不给原始 title）。
    """
    nodes = []
    links = []
    node_ids = set()

    for page in list_pages_scoped(privileged):
        path = os.path.join(WIKI_ROOT, page["path"])
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        node_id = page["path"].replace(".md", "")
        if node_id not in node_ids:
            nodes.append({"id": node_id, "label": page["title"], "group": page["type"]})
            node_ids.add(node_id)

        # 提取 [[wikilinks]]
        for match in re.finditer(r'\[\[([^\]]+)\]\]', content):
            target = match.group(1)
            if target not in node_ids:
                nodes.append({"id": target, "label": target, "group": "unknown"})
                node_ids.add(target)
            links.append({"source": node_id, "target": target})

    return {"nodes": nodes, "links": links}


def search_hybrid(query: str, top_k: int = 10, keyword_weight: float = 0.5) -> list:
    """混合搜索：关键词 + 向量相似度
    
    Args:
        query: 搜索关键词
        top_k: 返回最大条数
        keyword_weight: 关键词权重 (0-1)，剩余给向量
    
    Returns: [{path, title, type, tags, score, keyword_score, vector_score, snippet}]
    """
    import sqlite3
    import numpy as np
    from db import LocalEmbedding
    from models import CONFIG
    _DB = CONFIG.DB_PATH

    # 1. 关键词搜索（复用现有逻辑，但不限于 match 判定，收集所有候选打分）
    pages = list_pages()
    candidates = {}  # path -> {title, type, tags, keyword_score, ...}

    for page in pages:
        path = os.path.join(WIKI_ROOT, page["path"])
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        body = content
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                body = content[end + 3 :].strip()

        q = query.lower()
        score = 0.0

        # 标题匹配
        if q in page["title"].lower():
            score += 0.4
        # 标签匹配
        if q in page.get("tags", "").lower():
            score += 0.3
        # 内容匹配（按出现次数加分，最多 0.3）
        count = body.lower().count(q)
        if count > 0:
            score += min(0.3, count * 0.1)

        candidates[page["path"]] = {
            "path": page["path"],
            "title": page["title"],
            "type": page["type"],
            "tags": page.get("tags", ""),
            "keyword_score": min(score, 1.0),
            "content": body,
        }

    # 2. 向量搜索
    encoder = LocalEmbedding()
    try:
        query_emb = encoder.encode(query)
    except Exception:
        query_emb = None

    if query_emb is not None:
        conn = sqlite3.connect(_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT entry_id, title, category, embedding FROM knowledge_base WHERE embedding IS NOT NULL")
        db_rows = c.fetchall()
        conn.close()

        # 构建 entry_id → wiki_path 映射
        id_to_path = {}
        for row in db_rows:
            category = row["category"]
            cat_map = {"product": "entities", "policy": "concepts", "process": "concepts", "faq": "concepts", "general": "concepts"}
            subdir = cat_map.get(category, "concepts")
            slug = re.sub(r'[^\w\u4e00-\u9fff-]', '-', row["title"].strip().lower()).strip('-')
            wiki_path = f"{subdir}/{slug}.md"
            id_to_path[row["entry_id"]] = wiki_path

        # 计算余弦相似度
        query_norm = np.linalg.norm(query_emb)
        for row in db_rows:
            emb_bytes = row["embedding"]
            if not emb_bytes:
                continue
            try:
                emb = np.frombuffer(emb_bytes, dtype=np.float32)
                if len(emb) != len(query_emb):
                    continue
                sim = float(np.dot(query_emb, emb) / (query_norm * np.linalg.norm(emb) + 1e-10))
            except Exception:
                continue

            wiki_path = id_to_path.get(row["entry_id"])
            if wiki_path and wiki_path in candidates:
                candidates[wiki_path]["vector_score"] = max(0, sim)
            elif wiki_path:
                candidates[wiki_path] = {
                    "path": wiki_path,
                    "title": row["title"],
                    "type": "concept",
                    "tags": "",
                    "keyword_score": 0.0,
                    "vector_score": max(0, sim),
                    "content": "",
                }

    # 3. 合并排序
    for c in candidates.values():
        ks = c.get("keyword_score", 0)
        vs = c.get("vector_score", 0)
        c["score"] = round(keyword_weight * ks + (1 - keyword_weight) * vs, 3)

    ranked = sorted(candidates.values(), key=lambda x: x["score"], reverse=True)
    ranked = [r for r in ranked if r["score"] > 0]

    # 生成 snippet
    for r in ranked[:top_k]:
        body = r.get("content", "")
        if body and query.lower() in body.lower():
            idx = body.lower().find(query.lower())
            start = max(0, idx - 40)
            end = min(len(body), idx + len(query) + 80)
            snippet = body[start:end]
            if start > 0: snippet = "..." + snippet
            if end < len(body): snippet += "..."
            r["snippet"] = snippet
        else:
            r["snippet"] = body[:100] if body else ""

    result = ranked[:top_k]
    for r in result:
        r.pop("content", None)

    return result

# ============ CD-054 wiki 组收编（T23，2026-09-20）：读出口级别解析/剥离 helper（仅新增，既有函数一行不改） ============
#
# 冻结口径（用户 2026-09-20）：记忆派生页跟源记忆链披露级别，min 语义、只降不升
# （下调跟随走 reclassify，上调不自动跟随）。
#
# 下调跟随选型 = 路线甲（实时计算）：派生页有效级别不落盘缓存，读出口每次实时按
# memory_pool.disclosure_level 当前值计算 min(页面自身级别, 源记忆当前级别)——
# 天然跟随下调、无同步窗、无「落盘有效级别与源值漂移」的一致性问题；代价仅为
# 每次读取多一次主键点查（SQLite 主键查找，可忽略）。
# 「页面自身级别」= 派生时刻由 wiki_sync 快照进 frontmatter 的 disclosure_level
# （缺省 full = 页面自身不构成额外封顶）：源下调 → min 实时跟随；源上调 → 被
# 页面自身快照压住，不自动跟随（K2 铁律 3 / 阶段4 打标继承同款 min 只降不升）。
# 路线乙（有效级别落盘 + reclassify 钩子）需改动 reclassify 链路且同步窗内会读到
# 过期级别，故不取。

WIKI_LEVEL_RANK = {"none": 0, "metadata": 1, "summary": 2, "full": 3}


def parse_frontmatter(content: str):
    """解析 YAML frontmatter → (meta, body)。轻量行解析，与 list_pages 同口径。"""
    meta = {}
    body = content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            for line in content[3:end].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = content[end + 3:].strip()
    return meta, body


def memory_source_level(memory_id: str):
    """路线甲落点：实时查源记忆当前 disclosure_level。

    查到 → 级别串（小写，库内非法值按 none 处理，fail-closed 方向）；
    查不到行 / 查询异常 → None（由调用方 fail-closed 并留告警）。
    """
    if not memory_id:
        return None
    import sqlite3
    from models import CONFIG
    try:
        conn = sqlite3.connect(CONFIG.DB_PATH)
        try:
            row = conn.execute(
                "SELECT disclosure_level FROM memory_pool WHERE memory_id = ?",
                (memory_id,)).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("wiki 派生页源记忆级别查询失败 memory_id=%s err=%s",
                       memory_id, type(exc).__name__)
        return None
    if not row:
        return None
    level = (row[0] or "none").lower()
    return level if level in WIKI_LEVEL_RANK else "none"


def resolve_page_level(meta: dict, page_path: str = "") -> str:
    """页面级别解析（CD-054 T23 口径）。

    - 记忆派生页（frontmatter 含 memory_id）→ min(页面自身级别, 源记忆当前级别)
      （min 语义、只降不升）；源记忆查不到 / 查询异常 → fail-closed "summary"
      （不得回退全文）+ logger.warning（含 page_path/异常来源，不含正文与凭据）。
    - 手写页 / 知识派生页（无 memory_id）→ "full"（「已发布」语义，
      与 GET /knowledge 读出口一致：内容本身已发布，剥离由主体侧上限决定）。
    返回值为 WIKI_LEVEL_RANK 的键（none/metadata/summary/full）。
    """
    memory_id = (meta.get("memory_id") or "").strip()
    if not memory_id:
        return "full"
    src = memory_source_level(memory_id)
    if src is None:
        logger.warning(
            "wiki 派生页查不到源记忆，fail-closed 摘要级 page_path=%s memory_id=%s",
            page_path, memory_id)
        return "summary"
    own = (meta.get("disclosure_level") or "full").lower()
    own_rank = WIKI_LEVEL_RANK.get(own, WIKI_LEVEL_RANK["full"])
    src_rank = WIKI_LEVEL_RANK[src]
    return own if own_rank <= src_rank else src


def strip_to_summary(body: str) -> str:
    """summary 级剥离：正文前 200 字（CD-033A 定档精度），超长补省略号。"""
    return body[:200] + "..." if len(body) > 200 else body


def granted_page_level(page_level: str, privileged: bool) -> str:
    """响应级别二元化：min(页面级别, 主体上限)（主体上限：特权 full / 非特权 summary）。
    返回 "full" 或 "summary"（none/metadata 一律塌缩为 summary 摘要形）。"""
    cap = WIKI_LEVEL_RANK["full"] if privileged else WIKI_LEVEL_RANK["summary"]
    granted = min(WIKI_LEVEL_RANK.get(page_level, 0), cap)
    return "full" if granted >= WIKI_LEVEL_RANK["full"] else "summary"


# ═══════════ CD-067（T29，2026-09-20）：派生页 title/tags 按级别脱敏（只作用于读出口返回体） ═══════════
# 背景：派生页 frontmatter 的 title（如 `[记忆] conv-张三-20260725`）与 tags
# （如 `[客服对话, 张三, memory, fact]`）含人名与业务标识，而正文已按 CD-054 剥离——
# 元数据此前漏在出口外面。冻结口径（用户 2026-09-20 选 C）：派生页（含 memory_id）
# 按 min(页面级别, 主体上限) 过滤，级别不足 full 时 title 脱敏为 `[记忆] memory-<id 前 8 位>`、
# tags 置空；手写页 / 知识派生页（无 memory_id）不受影响。
# **只改读出口返回值，磁盘 frontmatter 一律不动。**

_DERIVED_PATH_RE = re.compile(r"^concepts/memory-.*\.md$")


def sanitize_derived_meta(page: dict, meta: dict, privileged: bool, page_path: str = "") -> dict:
    """派生页元数据脱敏：返回新 dict（只加 level 键，不删键；磁盘不动）。"""
    out = dict(page)
    memory_id = ((meta or {}).get("memory_id") or "").strip()
    if not memory_id:
        return out
    granted = granted_page_level(resolve_page_level(meta, page_path), privileged)
    if granted == "full":
        return out
    out["title"] = f"[记忆] memory-{memory_id[:8]}"
    out["tags"] = ""
    out["level"] = granted
    return out


def list_pages_scoped(privileged: bool = False) -> list:
    """list_pages() + 派生页元数据脱敏（读出口用；默认脱敏 = fail-closed）。

    读不到 frontmatter 时按 fail-closed 处理：路径命中派生页命名约定
    （`concepts/memory-*.md`）即脱敏（title 用路径哈希，不暴露文件名中的人名 / 主题）。
    """
    out = []
    for page in list_pages():
        rel = page.get("path", "")
        try:
            with open(os.path.join(WIKI_ROOT, rel), encoding="utf-8") as f:
                meta, _body = parse_frontmatter(f.read())
        except Exception as exc:
            logger.warning("wiki 列表读 frontmatter 失败，按 fail-closed 脱敏 page_path=%s err=%s",
                           rel, type(exc).__name__)
            p = dict(page)
            if _DERIVED_PATH_RE.match(rel):
                p["title"] = "[记忆] memory-" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:8]
                p["tags"] = ""
                p["level"] = "summary"
            out.append(p)
            continue
        out.append(sanitize_derived_meta(page, meta, privileged, rel))
    return out
