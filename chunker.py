# -*- coding: utf-8 -*-
"""
chunker.py — 文档语义切割（H1 数据汇入管道，附录 E v1.4，2026-08-06）

三级切点（按优先级）：
  1. 结构边界：markdown 标题/段落/列表/代码块（换行簇）
  2. 话题转折：embedding 相邻窗口 cos 相似度低于阈值即切
  3. 长度兜底：超过 MAX_TOKENS 硬切（仅无自然边界时）

工程细节（E.4）：
  - 最小块合并：< MIN_TOKENS 的碎片并入相邻块（防噪声 chunk 洪水）
  - chunk_hash：内容规范化 sha256，幂等重入（同文档重复汇入跳过已判定 chunk）
  - cos 阈值可配置（默认 0.75），真实语料标定后冻结进 config（SYNC_HUB_CHUNK_COS_THRESHOLD）

设计约束：
  - 无外部依赖（embedding 可选注入，不注入时只走结构边界 + 长度兜底）
  - token 估算用 len//3 近似（无 tiktoken 时的兜底口径，与 Agent 端一致）
  - chunk 是语义完整的披露单元；披露级别继承父文档只降不升（H2 敏感度链消费）
"""
import hashlib
import os
import re
from typing import Callable, List, Optional

# ── 可配置参数（E.4：阈值标定后冻结进 config，env 可覆盖） ──
DEFAULT_MAX_TOKENS = int(os.environ.get("SYNC_HUB_CHUNK_MAX_TOKENS", "512"))
DEFAULT_MIN_TOKENS = int(os.environ.get("SYNC_HUB_CHUNK_MIN_TOKENS", "50"))
DEFAULT_COS_THRESHOLD = float(os.environ.get("SYNC_HUB_CHUNK_COS_THRESHOLD", "0.75"))

# 结构边界正则
_HEADING_RE = re.compile(r"^(#{1,6}\s.*)$", re.M)           # markdown 标题
_CODE_FENCE_RE = re.compile(r"^```.*$", re.M)               # 代码块围栏
_LIST_ITEM_RE = re.compile(r"^(\s*[-*+]\s|\s*\d+[.、]\s)", re.M)  # 列表项
_EMPTY_LINE_RE = re.compile(r"\n\s*\n")                     # 空行 = 段落边界


def estimate_tokens(text: str) -> int:
    """token 估算（len//3 兜底口径，无 tiktoken 依赖）"""
    if not text:
        return 0
    # 中文按字符/1.5 估算，混合内容 len//3 是保守值
    return max(1, len(text) // 3)


def chunk_hash(content: str) -> str:
    """内容规范化 sha256 — 幂等重入的 key（E.4：重复汇入按 hash 跳过）"""
    norm = re.sub(r"\s+", " ", content).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _split_structural(text: str) -> List[str]:
    """一级切点：结构边界（标题/代码块/列表/空行段落）

    保持代码块完整（围栏内不切）；标题作为新块起点；列表项成块。
    """
    segments: List[str] = []
    # 先按代码块围栏保护代码段
    parts = []
    cursor = 0
    in_code = False
    for m in _CODE_FENCE_RE.finditer(text):
        if not in_code:
            parts.append((text[cursor:m.start()], False))
        else:
            parts.append((text[cursor:m.end()], True))
        cursor = m.end()
        in_code = not in_code
    if cursor < len(text):
        parts.append((text[cursor:], in_code))

    for part, is_code in parts:
        if is_code or not part.strip():
            if part.strip():
                segments.append(part.strip())
            continue
        # 非代码段：按标题/列表/空行切
        current = []
        lines = part.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                # 空行：flush 当前段（若已有内容）
                if current:
                    segments.append("\n".join(current).strip())
                    current = []
                i += 1
                continue
            if _HEADING_RE.match(line) or _LIST_ITEM_RE.match(line):
                if current:
                    segments.append("\n".join(current).strip())
                    current = []
                # 标题/列表项开新块
                block = [line]
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    if not nxt:
                        break
                    if _HEADING_RE.match(lines[j]) or _LIST_ITEM_RE.match(lines[j]):
                        break
                    block.append(lines[j])
                    j += 1
                segments.append("\n".join(block).strip())
                i = j
                continue
            current.append(line)
            i += 1
        if current:
            segments.append("\n".join(current).strip())

    return [s for s in segments if s]


def _split_by_similarity(segments: List[str], embed_fn: Callable[[str], List[float]],
                         threshold: float = DEFAULT_COS_THRESHOLD) -> List[str]:
    """二级切点：话题转折（embedding 相邻窗口 cos < 阈值即切）"""
    if not segments or embed_fn is None:
        return segments
    try:
        vecs = embed_fn(segments)
    except Exception:
        return segments  # embedding 失败降级为不切（结构边界已够）

    chunks: List[str] = []
    for i, seg in enumerate(segments):
        if i == 0:
            chunks.append(seg)
            continue
        cos = _cosine(vecs[i - 1], vecs[i])
        if cos < threshold:
            chunks.append(seg)  # 话题转折 → 新块
        else:
            chunks[-1] = chunks[-1] + "\n" + seg  # 语义连续 → 并入上一块
    return chunks


def _cosine(a: List[float], b: List[float]) -> float:
    """cosine 相似度（向量维度不一致返回 0）"""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _split_by_length(segments: List[str], max_tokens: int = DEFAULT_MAX_TOKENS) -> List[str]:
    """三级切点：长度兜底（仅当单个 segment 超限时硬切）"""
    result: List[str] = []
    for seg in segments:
        if estimate_tokens(seg) <= max_tokens:
            result.append(seg)
            continue
        # 硬切：按句子/逗号优先，最后按长度
        while estimate_tokens(seg) > max_tokens:
            # 找最近的自然断点（句号/分号/逗号）
            cut = _find_cut_point(seg, max_tokens * 3)
            piece, seg = seg[:cut].strip(), seg[cut:].strip()
            if piece:
                result.append(piece)
        if seg:
            result.append(seg)
    return result


def _find_cut_point(text: str, approx_chars: int) -> int:
    """在 approx_chars 附近找自然断点（。；，.!? 等），找不到返回 approx_chars"""
    candidates = []
    for sep in ("。", "；", "，", ". ", "; ", ", ", "！", "？", "!", "?", "\n"):
        idx = text.find(sep)
        while idx != -1:
            candidates.append(idx + len(sep))
            idx = text.find(sep, idx + len(sep))
    if not candidates:
        return min(approx_chars, len(text))
    # 取不超过 approx_chars 的最近断点
    best = None
    for c in candidates:
        if c <= approx_chars:
            best = c
        else:
            break
    return best if best else candidates[0]


def _merge_small(chunks: List[str], min_tokens: int = DEFAULT_MIN_TOKENS) -> List[str]:
    """E.4 最小块合并：< min_tokens 的碎片并入相邻块

    双向策略：非首块的小块并入前块；首块若 < min_tokens 且后面有块，
    则向后并入（与下一块合并后重新评估）——避免开头碎片漏合并。
    """
    if not chunks:
        return []
    merged: List[str] = []
    for chunk in chunks:
        if not merged:
            merged.append(chunk)
            continue
        if estimate_tokens(chunk) < min_tokens:
            # 小块并入前块（保持语义连续）
            merged[-1] = merged[-1] + "\n" + chunk
        else:
            merged.append(chunk)
    # 首块补合并：若首块本身是碎片且后续有块 → 并入下一块
    if len(merged) >= 2 and estimate_tokens(merged[0]) < min_tokens:
        merged[0] = merged[0] + "\n" + merged[1]
        del merged[1]
    return merged


def chunk_document(doc_id: str, content: str,
                   embed_fn: Optional[Callable[[List[str]], List[List[float]]]] = None,
                   max_tokens: int = DEFAULT_MAX_TOKENS,
                   min_tokens: int = DEFAULT_MIN_TOKENS,
                   cos_threshold: float = DEFAULT_COS_THRESHOLD) -> List[dict]:
    """主入口：文档 → chunk 列表（含 piece_index + chunk_hash）

    embed_fn: 接收文本列表，返回向量列表（每项 List[float]）。
              注入后启用二级切点（话题转折）；None 时只走结构边界 + 长度兜底。

    返回: [{"piece_index": 0, "content": "...", "chunk_hash": "...", "tokens": N}, ...]
    """
    if not content or not content.strip():
        return []

    # 一级：结构边界
    segments = _split_structural(content)

    # 二级：话题转折（embedding 注入时）
    if embed_fn is not None:
        try:
            segments = _split_by_similarity(segments, embed_fn, cos_threshold)
        except Exception:
            pass  # 转折切分失败不阻塞（结构边界已保证 chunk 完整）

    # 三级：长度兜底
    segments = _split_by_length(segments, max_tokens)

    # E.4：最小块合并
    segments = _merge_small(segments, min_tokens)

    # 组装（幂等 hash）
    chunks = []
    for idx, seg in enumerate(segments):
        chunks.append({
            "piece_index": idx,
            "content": seg,
            "chunk_hash": chunk_hash(seg),
            "tokens": estimate_tokens(seg),
        })
    return chunks


def dedupe_by_hash(chunks: List[dict], existing_hashes: set) -> List[dict]:
    """E.4 幂等重入：按 chunk_hash 跳过已判定 chunk"""
    return [c for c in chunks if c["chunk_hash"] not in existing_hashes]


def calibrate_cos_threshold(embed_fn: Callable[[List[str]], List[List[float]]],
                            sample_texts: List[str],
                            min_tokens: int = DEFAULT_MIN_TOKENS) -> dict:
    """K1c cos 阈值标定（附录 F 2026-08-06）— 换 embedding 模型后重新测量。

    0.75 是词袋分布下标定的（K1 修正 3：不可迁移）。
    流程：样本文本先结构切割 → 最小块合并 → 计算相邻块 cos 分布 →
          建议阈值 = 分布 P25（保守，宁可多切不可漏切）。

    返回: {"p10": x, "p25": y, "p50": z, "suggested": y, "samples": n}
    """
    # 结构切割 + 合并（与 chunk_document 前两段一致）
    segments = _split_structural("\n\n".join(sample_texts))
    segments = _merge_small(segments, min_tokens)

    if len(segments) < 2:
        return {"p10": 0.75, "p25": 0.75, "p50": 0.75, "suggested": 0.75, "samples": len(segments)}

    try:
        vecs = embed_fn(segments)
    except Exception:
        return {"p10": 0.75, "p25": 0.75, "p50": 0.75, "suggested": 0.75, "samples": 0}

    cosines = []
    for i in range(1, len(vecs)):
        cosines.append(_cosine(list(vecs[i - 1]), list(vecs[i])))

    cosines.sort()
    n = len(cosines)
    def pct(p):
        return cosines[min(n - 1, int(n * p))]
    p10, p25, p50 = pct(0.10), pct(0.25), pct(0.50)
    # 建议：P25（多数相邻块 cos 高于它 = 不误切；低于它 = 话题转折应切）
    return {"p10": round(p10, 4), "p25": round(p25, 4), "p50": round(p50, 4),
            "suggested": round(p25, 4), "samples": n}
