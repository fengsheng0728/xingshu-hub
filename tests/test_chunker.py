# -*- coding: utf-8 -*-
"""
H1 chunker 单测（附录 E v1.4 冻结验收）
覆盖：
  1. 一级切点：结构边界（markdown 标题/列表/代码块/空行段落）
  2. 二级切点：话题转折（embedding cos < 阈值切分；注入/未注入两态）
  3. 三级切点：长度兜底硬切（512 token 无自然边界）
  4. E.4 最小块合并（<50 token 碎片并入相邻块）
  5. E.4 chunk_hash 幂等（同内容同 hash；dedupe 跳过）
  6. 边界：空文档 / 单块短文档 / 代码块完整性
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chunker import (
    chunk_document,
    chunk_hash,
    dedupe_by_hash,
    estimate_tokens,
    _split_structural,
    _split_by_similarity,
    _merge_small,
    _split_by_length,
    DEFAULT_MIN_TOKENS,
    DEFAULT_MAX_TOKENS,
)


# ── 1. 结构边界 ──

def test_heading_split():
    doc = "# 第一章 概述\n这是第一章内容。\n\n# 第二章 细节\n这是第二章内容。"
    segs = _split_structural(doc)
    assert len(segs) == 2, f"标题应切成 2 块，实际 {len(segs)}: {segs}"
    assert segs[0].startswith("# 第一章"), segs[0][:30]
    assert segs[1].startswith("# 第二章"), segs[1][:30]
    print("PASS test_heading_split")


def test_list_split():
    doc = "- 项目一\n  说明一\n- 项目二\n  说明二"
    segs = _split_structural(doc)
    assert len(segs) == 2, f"列表项应切成 2 块，实际 {len(segs)}: {segs}"
    assert "项目一" in segs[0] and "项目二" in segs[1]
    print("PASS test_list_split")


def test_code_block_kept_intact():
    doc = "正文前。\n```python\nprint('hello')\nprint('world')\n```\n正文后。"
    segs = _split_structural(doc)
    code = [s for s in segs if "print('hello')" in s]
    assert code and "print('world')" in code[0], "代码块应保持完整"
    print("PASS test_code_block_kept_intact")


def test_paragraph_split():
    doc = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
    segs = _split_structural(doc)
    assert len(segs) == 3, f"三段应切 3 块，实际 {len(segs)}"
    print("PASS test_paragraph_split")


# ── 2. 话题转折 ──

def test_similarity_split():
    # 构造 4 段：前 2 段语义相近（金融），后 2 段语义相近（天气）——两话题
    segs = ["股票市场上涨了三个百分点", "基金净值今天大幅增长",
            "今天天气晴朗适合出行", "明天可能有降雨请带伞"]
    # 伪 embed：话题1 向量 (1,0)，话题2 向量 (0,1)——同话题 cos=1，跨话题 cos=0
    topic1_vec = [1.0, 0.0]
    topic2_vec = [0.0, 1.0]
    def fake_embed(items):
        return [topic1_vec if i < 2 else topic2_vec for i in range(len(items))]
    out = _split_by_similarity(segs, fake_embed, threshold=0.75)
    assert len(out) == 2, f"跨话题应切 2 块，实际 {len(out)}: {out}"
    assert "股票" in out[0] and "基金" in out[0]
    assert "天气" in out[1] and "降雨" in out[1]
    print("PASS test_similarity_split")


def test_similarity_continuity_merges():
    # 同话题不切
    segs = ["苹果是水果", "香蕉也是水果", "橙子富含维生素"]
    def fake_embed(items):
        return [[1.0, 0.0]] * len(items)  # 全部同向量 → cos=1
    out = _split_by_similarity(segs, fake_embed, threshold=0.75)
    assert len(out) == 1, f"同话题应合并为 1 块，实际 {len(out)}"
    print("PASS test_similarity_continuity_merges")


def test_no_embed_fn_fallback():
    # 不注入 embed_fn → 二级切分跳过；长段落（>50 token）不被误合并
    para = "这是一段足够长的内容。" * 30  # ~600 字 ≈ 200 token > 50
    doc = para + "\n\n" + para + "\n\n" + para
    chunks = chunk_document("d1", doc, embed_fn=None)
    assert len(chunks) == 3, f"长段落应切 3 块且不被合并，实际 {len(chunks)}"
    print("PASS test_no_embed_fn_fallback")


def test_short_para_merged():
    # E.4：短段落（<50 token）应合并防噪声——3 段短文并成 1 块
    doc = "第一段。\n\n第二段。\n\n第三段。"
    chunks = chunk_document("d2", doc, embed_fn=None)
    assert len(chunks) == 1, f"短段落应合并为 1 块，实际 {len(chunks)}"
    print("PASS test_short_para_merged")


# ── 3. 长度兜底 ──

def test_length_cap():
    # 超长无自然边界文本 → 硬切
    seg = "无" * 3000  # 3000 字 ≈ 1000 token > 512
    chunks = _split_by_length([seg], max_tokens=512)
    assert len(chunks) >= 2, f"超长应切多块，实际 {len(chunks)}"
    for c in chunks:
        assert estimate_tokens(c) <= 512, f"块超限: {estimate_tokens(c)}"
    print("PASS test_length_cap")


# ── 4. 最小块合并 ──

def test_merge_small():
    chunks = ["A" * 30,  # ~10 token < 50
              "B" * 300,  # ~100 token
              "C" * 30]  # ~10 token
    merged = _merge_small(chunks, min_tokens=50)
    assert len(merged) == 1, f"小块应并入相邻块，实际 {len(merged)}: {[len(c) for c in merged]}"
    print("PASS test_merge_small")


def test_merge_small_boundary():
    # 50 token 边界不误合并
    chunks = ["X" * 150,  # ~50 token 正好
              "Y" * 300]
    merged = _merge_small(chunks, min_tokens=50)
    assert len(merged) == 2, f"边界块不应合并，实际 {len(merged)}"
    print("PASS test_merge_small_boundary")


# ── 5. 幂等 hash ──

def test_hash_idempotent():
    assert chunk_hash("同一内容 ") == chunk_hash("同一内容"), "hash 应忽略空白差异"
    assert chunk_hash("A") != chunk_hash("B"), "不同内容 hash 不同"
    print("PASS test_hash_idempotent")


def test_dedupe():
    chunks = [
        {"chunk_hash": "h1", "content": "a"},
        {"chunk_hash": "h2", "content": "b"},
        {"chunk_hash": "h1", "content": "a"},
    ]
    out = dedupe_by_hash(chunks, {"h1"})
    assert len(out) == 1 and out[0]["chunk_hash"] == "h2", f"应只剩 h2，实际 {out}"
    print("PASS test_dedupe")


# ── 6. 边界 ──

def test_empty_doc():
    assert chunk_document("d", "") == []
    assert chunk_document("d", "   \n  ") == []
    print("PASS test_empty_doc")


def test_short_doc_single_chunk():
    chunks = chunk_document("d", "一句话。")
    assert len(chunks) == 1 and chunks[0]["piece_index"] == 0
    assert chunks[0]["chunk_hash"]
    print("PASS test_short_doc_single_chunk")


def test_chunk_metadata():
    doc = "# 标题\n内容段落。"
    chunks = chunk_document("doc-1", doc)
    assert chunks[0]["piece_index"] == 0
    assert chunks[0]["content"]
    assert chunks[0]["tokens"] > 0
    assert chunks[0]["chunk_hash"]
    print("PASS test_chunk_metadata")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nH1 chunker: {len(tests)} 用例全绿")
