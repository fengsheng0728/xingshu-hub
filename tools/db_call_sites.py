#!/usr/bin/env python3
"""tools/db_call_sites.py — 存量 SQLite 调用点统计（D-10 基线 / D-11 对照用）

统计口径（固定不变，D-11 复跑对照同一口径）
-------------------------------------------
- 扫描范围：仓根 `**/*.py`，排除 `tests/`、`examples/`、`node_modules/`、
  `build/`、`dist/`、`__pycache__/`、`docs/`（生产代码口径）。
- 计数方式：**逐行纯文本计数**（不用 ast，理由：调用点常出现在字符串拼接、
  注释样例与多行动态属性上，ast 会漏掉文本形态；D-11 要证明的是"文本级调用
  点数字下降"）。每行每模式出现几次计几次。
- 三个模式（字面量子串匹配）：
    `sqlite3.connect(`  — 连接建立点
    `.execute(`         — 执行点（注意：会含非 db 的同名方法调用，属已知噪声，
                          口径固定即可横向对照）
    `.executemany(`     — 批量执行点
- 输出：按总数降序的 Markdown 表格 + 汇总行；`--json PATH` 另落盘结构化数据。

用法：
    python tools/db_call_sites.py                # 打印 Markdown 表
    python tools/db_call_sites.py --json out.json
"""
import json
import os
import sys

PATTERNS = ("sqlite3.connect(", ".execute(", ".executemany(")
EXCLUDE_DIRS = {"tests", "examples", "node_modules", "build", "dist",
                "__pycache__", "docs"}


def scan(root):
    """逐文件计数，返回 {relpath: {"connect": n, "execute": n, "executemany": n}}。"""
    results = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, root).replace(os.sep, "/")
            counts = {"connect": 0, "execute": 0, "executemany": 0}
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "sqlite3.connect(" in line:
                            counts["connect"] += line.count("sqlite3.connect(")
                        if ".execute(" in line:
                            counts["execute"] += line.count(".execute(")
                        if ".executemany(" in line:
                            counts["executemany"] += line.count(".executemany(")
            except OSError:
                continue
            if counts["connect"] or counts["execute"] or counts["executemany"]:
                results[rel] = counts
    return results


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    json_path = None
    if "--json" in sys.argv:
        i = sys.argv.index("--json")
        json_path = sys.argv[i + 1] if i + 1 < len(sys.argv) else "db_call_sites.json"

    results = scan(root)
    rows = sorted(
        results.items(),
        key=lambda kv: -(kv[1]["connect"] + kv[1]["execute"] + kv[1]["executemany"]),
    )
    total_c = sum(v["connect"] for v in results.values())
    total_e = sum(v["execute"] for v in results.values())
    total_m = sum(v["executemany"] for v in results.values())

    print("# db 调用点统计（生产代码口径，排除 tests/examples/docs/build/dist）")
    print()
    print("| 文件 | sqlite3.connect( | .execute( | .executemany( | 合计 |")
    print("|---|---:|---:|---:|---:|")
    for rel, c in rows:
        tot = c["connect"] + c["execute"] + c["executemany"]
        print(f"| {rel} | {c['connect']} | {c['execute']} | {c['executemany']} | {tot} |")
    print(f"| **汇总（{len(rows)} 文件）** | **{total_c}** | **{total_e}** | "
          f"**{total_m}** | **{total_c + total_e + total_m}** |")

    if json_path:
        payload = {
            "scope_exclude": sorted(EXCLUDE_DIRS),
            "patterns": list(PATTERNS),
            "totals": {"connect": total_c, "execute": total_e,
                       "executemany": total_m,
                       "all": total_c + total_e + total_m},
            "files": {rel: c for rel, c in rows},
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\njson 已落盘: {json_path}")


if __name__ == "__main__":
    main()
