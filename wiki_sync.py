"""
星枢 Wiki 同步器 — 将数据库（knowledge_base + memory_pool）同步到 LLM Wiki markdown 文件

用法:
  python wiki_sync.py              # 增量同步
  python wiki_sync.py --force      # 强制覆盖所有页面
  python wiki_sync.py --dry-run    # 预览变更，不写入
"""
import logging
logger = logging.getLogger("xingshu.wiki_sync")

import os
import re
import json
import hashlib
import sqlite3
import argparse
from datetime import datetime

from wiki_engine import WIKI_ROOT, ensure_wiki, validate_path

# 数据库路径（与 CONFIG 保持一致）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_hub.db")

# 知识库分类 → wiki 子目录映射
CATEGORY_DIR_MAP = {
    "product": "entities",
    "policy": "concepts",
    "process": "concepts",
    "faq": "concepts",
    "general": "concepts",
}


def slugify(title: str) -> str:
    """标题 → 文件名（小写+连字符）"""
    s = title.strip().lower()
    # 保留中文和英文字母数字
    s = re.sub(r'[^\w\u4e00-\u9fff-]', '-', s)
    s = re.sub(r'-{2,}', '-', s)
    s = s.strip('-')
    return s or "untitled"


def _resolve_entry_id(entry_id: str, entry_map: dict) -> str:
    """将 entry_id 解析为页面标题（用于 wikilink）"""
    entry = entry_map.get(entry_id)
    if entry:
        title = entry.get("title", "")
        # 确定目标目录
        category = entry.get("category", "general")
        subdir = CATEGORY_DIR_MAP.get(category, "concepts")
        slug = entry.get("_slug") or slugify(title)
        return f"{subdir}/{slug}"
    return None


def _build_frontmatter(title: str, page_type: str, tags: list, created: str,
                       updated: str, importance: float = 1.0,
                       sources: list = None, extra: dict = None) -> str:
    """生成 YAML frontmatter"""
    lines = ["---"]
    lines.append(f"title: {title}")
    lines.append(f"created: {created}")
    lines.append(f"updated: {updated}")
    lines.append(f"type: {page_type}")
    if tags:
        tag_str = ", ".join(tags)
        lines.append(f"tags: [{tag_str}]")
    else:
        lines.append("tags: []")
    if importance < 1.0:
        lines.append(f"importance: {importance}")
    if sources:
        src_str = ", ".join(sources)
        lines.append(f"sources: [{src_str}]")
    if extra:
        for k, v in extra.items():
            if isinstance(v, list):
                lines.append(f"{k}: [{', '.join(v)}]")
            elif isinstance(v, str):
                lines.append(f"{k}: {v}")
            else:
                lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _build_wikilinks(link_ids: list, entry_map: dict, knowledge_title_set: set) -> str:
    """生成 [[wikilinks]] 板块"""
    if not link_ids:
        return ""
    links = []
    for lid in link_ids:
        resolved = _resolve_entry_id(lid, entry_map)
        if resolved:
            # resolved 格式: "subdir/slug" → 取 slug 部分作为显示名
            slug_part = resolved.split("/")[-1]
            # 尝试找到标题
            title = slug_part
            for eid, entry in entry_map.items():
                if eid == lid:
                    title = entry.get("title", slug_part)
                    break
            links.append(f"[[{title}]]")
    if not links:
        return ""
    return "\n## 关联页面\n\n" + "\n".join(f"- {l}" for l in links) + "\n"


def _build_tag_links(tags: list, entry_map: dict, own_entry_id: str) -> str:
    """基于标签匹配生成交叉链接"""
    if not tags or not entry_map:
        return ""
    matched = []
    for eid, entry in entry_map.items():
        if eid == own_entry_id:
            continue
        entry_tags = entry.get("_tags_list", [])
        if set(tags) & set(entry_tags):
            title = entry.get("title", "")
            if title:
                matched.append(f"[[{title}]]")
    if not matched:
        return ""
    return "\n## 相关主题\n\n" + "\n".join(f"- {m}" for m in matched[:8]) + "\n"


def _compute_hash(content: str) -> str:
    """计算内容 SHA256"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _federate_pull(dry_run: bool = False):
    """从已配对的 Hub 拉取 Wiki 页面（联邦同步）"""
    import urllib.request as _req
    import json as _json
    from models import CONFIG

    conn = sqlite3.connect(CONFIG.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT remote_hub_url, remote_api_key FROM team_members WHERE revoked_at IS NULL")
    members = c.fetchall()
    conn.close()

    if not members:
        return

    total = 0
    for member in members:
        hub_url = member["remote_hub_url"].rstrip("/")
        api_key = member["remote_api_key"]
        try:
            req = _req.Request(
                f"{hub_url}/api/v1/wiki/export",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with _req.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
            pages = data.get("pages", {})
            if not pages:
                continue

            if dry_run:
                print(f"  [联邦] 将从 {hub_url} 拉取 {len(pages)} 页")
                continue

            # Import pages
            import_req = _req.Request(
                f"http://localhost:3060/api/v1/wiki/import",
                data=_json.dumps({"pages": pages}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _req.urlopen(import_req, timeout=30) as resp:
                result = _json.loads(resp.read())
            imported = result.get("imported", 0)
            total += imported
            print(f"  [联邦] {hub_url}: +{imported} 页")
        except Exception as e:
            print(f"  [联邦] {hub_url}: 失败 ({e})")

    if total and not dry_run:
        _update_index(dry_run=False)


def _generate_embeddings(kb_rows: list, dry_run: bool = False):
    """为知识库条目生成 embedding 并写回 SQLite（混合搜索用）"""
    if not kb_rows:
        return
    try:
        from db import LocalEmbedding
        encoder = LocalEmbedding()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for row in kb_rows:
            text = f"{row.get('title','')} {row.get('content','')}"
            if not text.strip():
                continue
            emb = encoder.encode(text)
            if dry_run:
                continue
            c.execute(
                "UPDATE knowledge_base SET embedding = ? WHERE entry_id = ?",
                (emb.tobytes(), row["entry_id"]),
            )
        if not dry_run:
            conn.commit()
        conn.close()
        if not dry_run:
            print(f"  [embedding] 已生成 {len(kb_rows)} 个向量")
    except Exception as e:
        print(f"  [embedding] 失败: {e}")


def _clean_orphans(kb_rows: list, mp_rows: list, dry_run: bool = False):
    """删除 wiki 中数据库已不存在的页面（防止残留）"""
    # 收集所有应该存在的 wiki 路径
    expected = set()
    for row in kb_rows:
        category = row.get("category", "general")
        subdir = CATEGORY_DIR_MAP.get(category, "concepts")
        slug = row.get("_slug") or slugify(row.get("title", ""))
        expected.add(f"{subdir}/{slug}.md")
    for row in mp_rows:
        memory_key = row.get("memory_key", "unknown")
        slug = slugify(f"memory-{memory_key}")
        expected.add(f"concepts/{slug}.md")

    # 扫描 wiki 目录，删除非预期的页面（保留 raw/ 和 _ 开头的）
    import re as _re
    for subdir in ["entities", "concepts", "comparisons", "queries"]:
        dir_path = os.path.join(WIKI_ROOT, subdir)
        if not os.path.isdir(dir_path):
            continue
        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith(".md") or fname.startswith("_"):
                continue
            rel_path = f"{subdir}/{fname}"
            if rel_path not in expected:
                # 额外检查：如果文件中 entry_id/memory_id 在 DB 中已不存在，则是孤儿
                abs_path = os.path.join(WIKI_ROOT, rel_path)
                try:
                    with open(abs_path, encoding="utf-8") as f:
                        content = f.read()
                    has_db_ref = False
                    if content.startswith("---"):
                        end = content.find("---", 3)
                        if end > 0:
                            fm = content[3:end]
                            for line in fm.split("\n"):
                                if ":" in line:
                                    k, v = line.split(":", 1)
                                    if k.strip() in ("entry_id", "memory_id"):
                                        has_db_ref = True
                                        break
                    if not has_db_ref:
                        continue  # 手动创建的 wiki 页面，保留
                except Exception as _exc:
                    logger.debug("wiki_sync silent-except @257: %s", _exc)

                if dry_run:
                    print(f"  [DRY-RUN] 将删除孤儿: {rel_path}")
                else:
                    os.remove(abs_path)
                    print(f"  [清理] 孤儿页面: {rel_path}")


def sync(dry_run: bool = False, force: bool = False, federate: bool = False):
    """执行数据库 → Wiki 同步。federate=True 时先从已配对 Hub 拉取页面。"""
    ensure_wiki()

    if federate:
        _federate_pull(dry_run)

    # 读取数据库
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 知识库条目
    c.execute("SELECT * FROM knowledge_base ORDER BY importance DESC")
    kb_rows = [dict(row) for row in c.fetchall()]

    # 记忆池（仅同步重要性 >= 0.8 的条目）
    c.execute(
        "SELECT * FROM memory_pool WHERE importance >= 0.8 ORDER BY importance DESC"
    )
    mp_rows = [dict(row) for row in c.fetchall()]
    conn.close()

    if not kb_rows and not mp_rows:
        print("数据库为空，无需同步")
        return {"created": 0, "updated": 0, "skipped": 0, "errors": []}

    # 构建 entry_id → entry 映射（用于 wikilink 解析）
    entry_map = {}
    for row in kb_rows:
        # 预解析 tags
        tags_raw = row.get("tags", "[]") or "[]"
        try:
            tags_list = json.loads(tags_raw)
        except (json.JSONDecodeError, TypeError):
            tags_list = []
        row["_tags_list"] = tags_list
        row["_slug"] = slugify(row.get("title", ""))
        entry_map[row["entry_id"]] = row

    # 收集所有 knowledge 标题用于跨引用匹配
    kb_title_set = {row.get("title", "") for row in kb_rows}

    today = datetime.now().strftime("%Y-%m-%d")
    stats = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    new_page_paths = []
    updated_page_paths = []

    def _write_page(rel_path: str, content: str):
        """写入 wiki 页面（路径安全校验）"""
        try:
            abs_path = validate_path(rel_path)
        except ValueError as e:
            stats["errors"].append(f"路径非法: {rel_path} → {e}")
            return

        if dry_run:
            if os.path.exists(abs_path):
                print(f"  [DRY-RUN] 将更新: {rel_path}")
            else:
                print(f"  [DRY-RUN] 将创建: {rel_path}")
            return

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        existed = os.path.exists(abs_path)

        if existed and not force:
            # 比较内容 hash
            old_hash = _compute_hash(open(abs_path, encoding="utf-8").read())
            new_hash = _compute_hash(content)
            if old_hash == new_hash:
                stats["skipped"] += 1
                return

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        if existed:
            stats["updated"] += 1
            updated_page_paths.append(rel_path)
        else:
            stats["created"] += 1
            new_page_paths.append(rel_path)

    # ===== 同步 knowledge_base =====
    for row in kb_rows:
        title = row.get("title", "Untitled")
        category = row.get("category", "general")
        content_body = row.get("content") or ""
        tags = row.get("_tags_list", [])
        importance = row.get("importance", 1.0)
        entry_id = row.get("entry_id", "")
        entry_links_raw = row.get("links", "[]") or "[]"
        created_at = (row.get("created_at") or today)[:10]
        updated_at = (row.get("updated_at") or today)[:10]

        # 目录
        subdir = CATEGORY_DIR_MAP.get(category, "concepts")
        page_type = "entity" if subdir == "entities" else "concept"
        slug = row["_slug"]
        rel_path = f"{subdir}/{slug}.md"

        # 解析 links
        try:
            link_ids = json.loads(entry_links_raw)
        except (json.JSONDecodeError, TypeError):
            link_ids = []

        # 构建页面
        fm = _build_frontmatter(
            title=title,
            page_type=page_type,
            tags=tags,
            created=created_at,
            updated=updated_at,
            importance=importance,
            extra={"category": category, "entry_id": entry_id},
        )

        body = f"# {title}\n\n"
        if content_body:
            body += f"{content_body}\n\n"
        # wikilinks (from links field)
        body += _build_wikilinks(link_ids, entry_map, kb_title_set)
        # tag-based cross-links
        body += _build_tag_links(tags, entry_map, entry_id)
        body += f"\n> 来源: 数据库知识库 | 同步时间: {today}\n"

        content = fm + "\n\n" + body
        _write_page(rel_path, content)

    # ===== 同步 memory_pool =====
    for row in mp_rows:
        memory_key = row.get("memory_key", "unknown")
        content_body = row.get("content") or ""
        summary = row.get("summary") or ""
        tags_raw = row.get("tags", "[]") or "[]"
        try:
            tags = json.loads(tags_raw)
        except (json.JSONDecodeError, TypeError):
            tags = []
        kind = row.get("kind", "fact")
        owner = row.get("owner_agent_id", "unknown")
        memory_id = row.get("memory_id", "")
        importance = row.get("importance", 1.0)
        created_at = (row.get("created_at") or today)[:10]
        updated_at = (row.get("updated_at") or today)[:10]

        slug = slugify(f"memory-{memory_key}")
        rel_path = f"concepts/{slug}.md"

        # 摘要优先，没有则截取内容前200字
        display_content = summary or (content_body[:200] + "..." if len(content_body) > 200 else content_body)

        fm = _build_frontmatter(
            title=f"[记忆] {memory_key}",
            page_type="concept",
            tags=tags + ["memory", kind],
            created=created_at,
            updated=updated_at,
            importance=importance,
            extra={
                "kind": kind,
                "owner": owner,
                "memory_id": memory_id,
            },
        )

        body = f"# {memory_key}\n\n"
        body += f"**类型**: {kind}  \n"
        body += f"**来源**: {owner}  \n\n"
        body += f"{display_content}\n\n"
        # tag-based cross-links to knowledge entries
        body += _build_tag_links(tags, entry_map, "")
        body += f"\n> 来源: 数据库记忆池 | 同步时间: {today}\n"

        content = fm + "\n\n" + body
        _write_page(rel_path, content)

    # ===== 生成 embedding（混合搜索用） =====
    _generate_embeddings(kb_rows, dry_run)

    # ===== 清理孤儿页面 =====
    _clean_orphans(kb_rows, mp_rows, dry_run)

    # ===== 更新 index.md =====
    _update_index(dry_run)

    # ===== 更新 log.md =====
    _update_log(stats, new_page_paths, updated_page_paths, dry_run)

    # ===== Wiki 收件箱：新页面注册为 pending =====
    stats["inbox_new"] = 0
    if not dry_run and new_page_paths:
        try:
            inbox_conn = sqlite3.connect(DB_PATH)
            for path in new_page_paths:
                title = path.replace("wiki/", "").replace(".md", "").replace("-", " ").title()
                cur = inbox_conn.execute(
                    "INSERT OR IGNORE INTO wiki_inbox (page_path, title, status, source, trust_level) VALUES (?,?,?,?,?)",
                    (path, title, "pending", "auto-sync", "external"))  # S3: 外部导入内容默认低可信,审查通过后升 internal
                stats["inbox_new"] += cur.rowcount
            inbox_conn.commit()
            inbox_conn.close()
        except Exception as _exc:
            logger.debug("wiki_sync silent-except @471: %s", _exc)

    return stats


def _update_index(dry_run: bool = False):
    """根据文件系统重建 index.md"""
    index_path = os.path.join(WIKI_ROOT, "index.md")
    today = datetime.now().strftime("%Y-%m-%d")

    sections = {
        "Entities": "entities",
        "Concepts": "concepts",
        "Comparisons": "comparisons",
        "Queries": "queries",
    }

    lines = [
        "# Wiki Index",
        f"> 页面目录。每页一行：wikilink + 摘要。",
        f"> 更新: {today}",
        "",
    ]

    total = 0
    for label, subdir in sections.items():
        dir_path = os.path.join(WIKI_ROOT, subdir)
        pages = []
        if os.path.isdir(dir_path):
            for fname in sorted(os.listdir(dir_path)):
                if fname.endswith(".md") and not fname.startswith("_"):
                    path = os.path.join(dir_path, fname)
                    rel = f"{subdir}/{fname}"
                    # 读取标题
                    title = fname.replace(".md", "").replace("-", " ").title()
                    try:
                        with open(path, encoding="utf-8") as f:
                            content = f.read()
                        if content.startswith("---"):
                            end = content.find("---", 3)
                            if end > 0:
                                fm = content[3:end]
                                for line in fm.split("\n"):
                                    if line.startswith("title:"):
                                        title = line.split(":", 1)[1].strip()
                                        break
                    except Exception as _exc:
                        logger.debug("wiki_sync silent-except @518: %s", _exc)
                    pages.append(f"- [[{title}]] ({rel})")
                    total += 1

        lines.append(f"## {label}")
        if pages:
            lines.extend(pages)
        else:
            lines.append("(暂无)")
        lines.append("")

    lines.insert(3, f"> 页面总数: {total}")

    content = "\n".join(lines) + "\n"

    if dry_run:
        print(f"\n  [DRY-RUN] index.md: {total} 个页面")
        return

    with open(index_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\n  index.md 已更新 ({total} 个页面)")


def _update_log(stats: dict, new_paths: list, updated_paths: list, dry_run: bool = False):
    """追加同步日志"""
    log_path = os.path.join(WIKI_ROOT, "log.md")
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    entry = f"\n## [{today}] sync | DB → Wiki"
    entry += f"\n- 新建: {stats['created']} | 更新: {stats['updated']} | 跳过: {stats['skipped']}"
    if stats["errors"]:
        entry += f"\n- 错误: {len(stats['errors'])}"
    for p in new_paths:
        entry += f"\n- [new] {p}"
    for p in updated_paths:
        entry += f"\n- [update] {p}"

    if dry_run:
        print(f"\n  [DRY-RUN] log.md 追加: {entry[:200]}...")
        return

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"  log.md 已追加同步记录")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="星枢 Wiki 同步器")
    parser.add_argument("--force", action="store_true", help="强制覆盖所有页面")
    parser.add_argument("--dry-run", action="store_true", help="预览变更，不写入")
    args = parser.parse_args()

    print(f"同步模式: {'强制覆盖' if args.force else '增量更新'} {' (预览)' if args.dry_run else ''}")
    print(f"Wiki 根目录: {WIKI_ROOT}")
    print()

    result = sync(dry_run=args.dry_run, force=args.force)

    print(f"\n=== 同步结果 ===")
    print(f"  新建: {result['created']}")
    print(f"  更新: {result['updated']}")
    print(f"  跳过: {result['skipped']}")
    print(f"  错误: {len(result['errors'])}")
    if result["errors"]:
        for e in result["errors"]:
            print(f"    - {e}")
