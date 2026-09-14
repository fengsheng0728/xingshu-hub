# -*- coding: utf-8 -*-
"""
entity_extraction.py — 实体抽取（K2，附录 F v1.7，2026-08-06）

铁律（用户审定，冻结）：
  1. 先分级再喂 LLM：NONE 级 chunk 不送 LLM（尤其云端模型碰敏感内容是安全事故）
     → level == "none" 直接返回 skipped，内容零外呼
  2. 抽取结果走审查队列（entity_review 表）——LLM 幻觉实体不能直接污染图谱
     → 本模块只产出候选实体，落库/发布由审查流程控制
  3. 实体/关系继承证据 chunk 的级别，图谱下钻仍过披露判定
     → 每条实体带 evidence_level；图谱侧复用 N4 过滤

输出 schema（严格）：
  {
    "entities": [{"name": str, "type": str, "evidence": str, "level": str}],
    "relations": [{"src": str, "rel": str, "dst": str, "level": str}],
    "source": "llm" | "heuristic" | "skipped"
  }
"""
import json
import logging
import os
import re
from typing import Dict, List, Optional

logger = logging.getLogger("entity_extraction")

# ── 本地启发式词典（无 LLM 时零外呼兜底） ──
_ENTITY_TYPES = {
    "person": ["张三", "李四", "王五", "赵六", "客户", "经理", "主管", "员工"],
    "org": ["公司", "部门", "团队", "供应商", "客户", "董事会", "研发部", "销售部", "市场部", "客服部"],
    "product": ["产品", "系统", "平台", "服务", "方案", "工具"],
    "project": ["项目", "计划", "方案", "任务", "迭代"],
    "metric": ["销售额", "利润", "成本", "毛利率", "转化率", "GMV", "ROI", "增长率", "库存", "客单价"],
    "doc": ["报告", "文档", "合同", "协议", "会议纪要", "手册"],
}
_RELATION_PATTERNS = [
    (r"([\u4e00-\u9fffA-Za-z0-9]+)负责([\u4e00-\u9fffA-Za-z0-9]+)", "负责"),
    (r"([\u4e00-\u9fffA-Za-z0-9]+)属于([\u4e00-\u9fffA-Za-z0-9]+)", "属于"),
    (r"([\u4e00-\u9fffA-Za-z0-9]+)参与([\u4e00-\u9fffA-Za-z0-9]+)", "参与"),
    (r"([\u4e00-\u9fffA-Za-z0-9]+)合作([\u4e00-\u9fffA-Za-z0-9]+)", "合作"),
]

_LLM_PROMPT = """你是知识图谱实体抽取器。从给定文本中抽取实体与关系，严格输出 JSON（不要任何其他文字）。

输出格式：
{"entities": [{"name": "实体名", "type": "person|org|product|project|metric|doc|other"}],
 "relations": [{"src": "实体A", "rel": "关系", "dst": "实体B"}]}

规则：
- 只抽取文本中明确出现的实体，禁止编造
- 实体名用原文，不翻译不缩写
- 最多 10 个实体、10 条关系，没有则给空数组
- type 只允许: person, org, product, project, metric, doc, other

文本：
"""


# ── 启发式抽取（零 LLM 兜底） ──

def _heuristic_extract(content: str) -> Dict:
    """纯本地抽取：词典匹配 + 关系正则，零外呼"""
    entities: List[Dict] = []
    seen = set()
    for etype, words in _ENTITY_TYPES.items():
        for w in words:
            if w in content and w not in seen:
                seen.add(w)
                entities.append({"name": w, "type": etype,
                                 "evidence": content[max(0, content.find(w) - 15): content.find(w) + 15].strip(),
                                 "level": "summary"})
    relations: List[Dict] = []
    for pattern, rel in _RELATION_PATTERNS:
        for m in re.finditer(pattern, content):
            src, dst = m.group(1), m.group(2)
            if src and dst and src != dst:
                relations.append({"src": src, "rel": rel, "dst": dst, "level": "summary"})
    return {"entities": entities[:20], "relations": relations[:20], "source": "heuristic"}


# ── LLM 抽取（严格 JSON） ──

async def _llm_extract(content: str, llm_config: Dict) -> Dict:
    """调用 LLM 抽取（OpenAI 兼容 chat/completions）"""
    import httpx

    api_key = llm_config.get("api_key", "")
    model = llm_config.get("model", "deepseek-chat")
    base = llm_config.get("api_base", "") or "https://api.deepseek.com/v1"
    if not api_key:
        return _heuristic_extract(content)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是实体抽取器，只输出严格 JSON。"},
            {"role": "user", "content": _LLM_PROMPT + content[:2000]},
        ],
        "temperature": 0.1,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                logger.warning(f"LLM 抽取失败 HTTP {resp.status_code}，降级启发式")
                return _heuristic_extract(content)
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            parsed = json.loads(text)
            entities = parsed.get("entities", [])[:20]
            relations = parsed.get("relations", [])[:20]
            # 归一化：保证 level 字段（继承证据 chunk 级别，由调用方覆盖）
            for e in entities:
                e.setdefault("level", "summary")
                e.setdefault("evidence", "")
            for r in relations:
                r.setdefault("level", "summary")
            return {"entities": entities, "relations": relations, "source": "llm"}
    except Exception as e:
        logger.warning(f"LLM 抽取异常（降级启发式）: {type(e).__name__}: {e}")
        return _heuristic_extract(content)


# ── 主入口 ──

async def extract_entities(content: str, level: str = "summary",
                           llm_config: Optional[Dict] = None) -> Dict:
    """实体抽取主入口（K2 铁律 1：NONE 级零 LLM 接触）。

    Args:
        content: chunk 全文
        level:   披露级别（none/summary/full）
        llm_config: {"api_key","model","api_base"} — None 或缺 key → 启发式

    Returns:
        {"entities": [...], "relations": [...], "source": "llm"|"heuristic"|"skipped"}
    """
    if not content or not content.strip():
        return {"entities": [], "relations": [], "source": "skipped"}
    if level == "none":
        # K2 铁律 1：NONE 级不送 LLM（云端模型碰敏感内容 = 安全事故）
        return {"entities": [], "relations": [], "source": "skipped",
                "reason": "level=none 不送 LLM"}

    if llm_config and llm_config.get("api_key"):
        return await _llm_extract(content, llm_config)
    return _heuristic_extract(content)
