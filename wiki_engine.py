"""
Karpathy LLM Wiki — 三层知识库
raw/ (不可变源) → entities/ concepts/ comparisons/ queries/ (agent 维护) → SCHEMA.md (约束)

路径校验：所有写入/读取必须在 WIKI_ROOT 内。
XSS 防御：markdown 渲染前 HTML 实体转义，不信任任何用户输入。
"""
import os
import re
import html
import json
import sys
from datetime import datetime

WIKI_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki")


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


def get_graph() -> dict:
    """生成 [[wikilinks]] 关系图数据（D3 force-directed 格式）"""
    nodes = []
    links = []
    node_ids = set()

    for page in list_pages():
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