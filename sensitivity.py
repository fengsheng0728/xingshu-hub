# -*- coding: utf-8 -*-
"""
sensitivity.py — 敏感度判定链（H2，附录 E v1.4，2026-08-06）

定位：写入时打标（E.2）——决定内容以什么级别落库。
与读取时 8 规则链的关系：读取判定必须再叠 min(请求方判定, 存储级别)。

6 维判定链（fail-closed，顺序命中即停）：
  1. 来源信任  trust_level == UNTRUSTED          → NONE + taint
  2. PII 正则  身份证/手机/银行卡/护照/密钥串      → NONE（E.1：预扫在切割前全文跑，命中区间映射回 chunk）
  3. 机密词库  合同价/客户名单/密码/内部代号        → SUMMARY 上限
  4. 写入者角色 owner 是 worker 且含业务数据       → SUMMARY 上限
  5. 父文档级别 parent 的 disclosure_level        → 继承
  6. 类型      kind: secret 最高 / todo 最低       → 按类型映射

E.1 关键约束：
  - PII 预扫必须在切割前的父文档全文上跑（正则跨 chunk 边界如身份证被切两半 → 漏检）
  - 审计只记掩码样本（命中类型 + 位置偏移 + 掩码后样本），原始串不落审计
  - 写入方收到 locked: true 回执（已接收但已锁定），不静默丢弃
  - H2 出口硬断言：对审计库 grep 原始 PII 串必须零命中

E.6 向量约束（消费方）：
  - NONE 级不建 ChromaDB 向量（防语义检索绕过披露）
  - SUMMARY 级只对 summary 字段建向量
"""
import hashlib
import os
import re
from typing import Dict, List, Optional, Tuple

# ── 机密词库（E.4/E.5：可更新，更新触发 stale 重判定） ──
# K3（附录 F 2026-08-06）：交付物补齐 — base 30 词 + 分行业起始包 + 维护约定
# 加载优先级：目录(secret_words_dir + industries) > 单文件(SYNC_HUB_SECRET_WORDS) > 内置默认
DEFAULT_SECRET_KEYWORDS: List[str] = [
    # 商业机密
    "合同价", "成交价", "底价", "报价单", "投标", "客户名单", "供应商名单",
    "渠道政策", "返点", "毛利率",
    # 财务
    "工资", "薪资表", "股权", "财务数据", "审计报告", "预算明细", "现金流", "资产负债表",
    # 凭据
    "密码", "密钥", "token", "api_key", "apikey", "access_key", "secret",
    # 内部标记
    "内部代号", "机要", "保密", "机密", "内部资料",
]

# 机密词库路径（config 可覆盖，词库更新 → E.5 stale 重判定）
_SECRET_WORDS_FILE = os.environ.get("SYNC_HUB_SECRET_WORDS", "")
# K3：词库目录（多文件：base + 行业包）
_SECRET_WORDS_DIR = os.environ.get("SYNC_HUB_SECRET_WORDS_DIR", "")
_SECRET_WORDS_INDUSTRIES = [s.strip() for s in
                            os.environ.get("SYNC_HUB_SECRET_WORDS_INDUSTRIES", "").split(",") if s.strip()]


def _read_words_file(path: str) -> List[str]:
    """读词库文件（每行一词，# 注释；去空行）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except Exception:
        return []


def _load_secret_keywords() -> List[str]:
    """加载机密词库（目录 > 单文件 > 内置默认）"""
    # K3：目录模式（base.txt 必载 + 行业包）
    if _SECRET_WORDS_DIR and os.path.isdir(_SECRET_WORDS_DIR):
        words: List[str] = []
        base_path = os.path.join(_SECRET_WORDS_DIR, "base.txt")
        if os.path.exists(base_path):
            words.extend(_read_words_file(base_path))
        for ind in _SECRET_WORDS_INDUSTRIES:
            p = os.path.join(_SECRET_WORDS_DIR, f"industry-{ind}.txt")
            if os.path.exists(p):
                words.extend(_read_words_file(p))
        if words:
            return words
        return list(DEFAULT_SECRET_KEYWORDS)
    # 单文件模式
    if _SECRET_WORDS_FILE and os.path.exists(_SECRET_WORDS_FILE):
        words = _read_words_file(_SECRET_WORDS_FILE)
        return words or list(DEFAULT_SECRET_KEYWORDS)
    return list(DEFAULT_SECRET_KEYWORDS)


# ── PII 正则（E.1：预扫在切割前全文跑） ──

# 身份证：18 位（17 数字 + 校验位 X），可带空格
RE_ID_CARD = re.compile(r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")
# 手机号：1[3-9] 开头 11 位
RE_PHONE = re.compile(r"\b1[3-9]\d{9}\b")
# 银行卡：13-19 位纯数字（非手机号前缀）
RE_BANK_CARD = re.compile(r"\b(?:62|60|52|53|54|55|56|58)\d{11,17}\b")
# 密钥串：sk-/ghp_/AKIA/eyJ(长 base64) 等
RE_SECRET_KEY = re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
# 邮箱（隐私）
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

PII_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("id_card", RE_ID_CARD),
    ("phone", RE_PHONE),
    ("bank_card", RE_BANK_CARD),
    ("secret_key", RE_SECRET_KEY),
    ("email", RE_EMAIL),
]


def mask_sample(text: str, start: int, end: int, pii_type: str) -> str:
    """E.1：掩码样本 —— 命中区间的原始串不落审计，只留掩码形式。
    身份证/银行卡保留前 4 后 4，其余类型全掩。
    """
    raw = text[start:end]
    if pii_type in ("id_card", "bank_card") and len(raw) >= 8:
        return raw[:4] + "*" * (len(raw) - 8) + raw[-4:]
    if pii_type == "phone" and len(raw) >= 7:
        return raw[:3] + "****" + raw[-4:]
    return "*" * len(raw)


def scan_pii(text: str) -> List[dict]:
    """PII 预扫（全文级，E.1：切割前跑，防跨 chunk 边界漏检）。

    返回: [{"type": "id_card", "start": 10, "end": 28, "masked": "1101******1234"}, ...]
    仅返回命中类型 + 位置偏移 + 掩码样本，绝不返回原始串。
    """
    hits: List[dict] = []
    if not text:
        return hits
    for pii_type, pat in PII_PATTERNS:
        for m in pat.finditer(text):
            hits.append({
                "type": pii_type,
                "start": m.start(),
                "end": m.end(),
                "masked": mask_sample(text, m.start(), m.end(), pii_type),
            })
    return hits


# ── 级别常量（与 disclosure.py DisclosureLevel 对齐） ──

NONE = "none"
SUMMARY = "summary"
FULL = "full"


def classify(content: str,
             kind: str = "fact",
             trust_level: str = "trusted",
             owner_role: str = "worker",
             parent_level: Optional[str] = None,
             secret_keywords: Optional[List[str]] = None) -> dict:
    """6 维敏感度判定链（fail-closed，顺序命中即停）。

    参数:
      content:      内容（全文或 chunk）
      kind:         fact/secret/todo
      trust_level:  S3a 信任级别（trusted/internal/untrusted）
      owner_role:   写入者角色（worker/manager/orchestrator）
      parent_level: 父文档披露级别（None=无父文档）
      secret_keywords: 机密词库覆盖（None=用默认/配置文件）

    返回:
      {
        "level": "none"|"summary"|"full",
        "rule": "r1_trust|r2_pii|r3_secret|r4_role|r5_parent|r6_kind",
        "sensitivity_score": 0.0~1.0,
        "locked": bool,        # True = 强制 NONE（PII/UNTRUSTED）
        "pii_hits": [...],     # 掩码样本（仅 PII 命中时非空）
        "reasons": [str],      # 命中链记录（审计用，不含原始 PII）
      }
    """
    reasons: List[str] = []
    score = 0.0

    # 规则 1：来源信任（S3a）——UNTRUSTED 强制 NONE + taint
    if trust_level == "untrusted":
        return {
            "level": NONE, "rule": "r1_trust", "sensitivity_score": 1.0,
            "locked": True, "pii_hits": [],
            "reasons": ["r1_trust: UNTRUSTED 来源强制锁定"],
        }

    # 规则 2：PII 预扫（全文级）
    pii_hits = scan_pii(content)
    if pii_hits:
        return {
            "level": NONE, "rule": "r2_pii", "sensitivity_score": 1.0,
            "locked": True, "pii_hits": pii_hits,
            "reasons": [f"r2_pii: 命中 {len(pii_hits)} 处 PII（类型: {hits_type(pii_hits)}）"],
        }

    # 规则 3：机密词库 → SUMMARY 上限
    words = secret_keywords if secret_keywords is not None else _load_secret_keywords()
    hit_words = [w for w in words if w and w in content]
    if hit_words:
        score = max(score, 0.7)
        reasons.append(f"r3_secret: 命中机密词 {len(hit_words)} 个")

    # 规则 4：写入者角色 —— worker 写业务内容 → SUMMARY 上限
    if owner_role == "worker" and len(content) > 50:
        score = max(score, 0.5)
        reasons.append("r4_role: worker 写入内容上限 SUMMARY")

    # 规则 5：父文档级别继承
    if parent_level:
        score = max(score, _level_score(parent_level))
        reasons.append(f"r5_parent: 继承父文档级别 {parent_level}")

    # 规则 6：类型映射
    kind_score = {"secret": 0.9, "fact": 0.3, "todo": 0.1}.get(kind, 0.3)
    score = max(score, kind_score)
    reasons.append(f"r6_kind: {kind} → {kind_score}")

    # 汇总：分数 → 级别
    level = _score_to_level(score, parent_level)
    return {
        "level": level, "rule": "r3_secret" if score >= 0.7 else "r6_kind",
        "sensitivity_score": round(score, 3),
        "locked": False, "pii_hits": [],
        "reasons": reasons,
    }


def hits_type(hits: List[dict]) -> str:
    return ",".join(sorted({h["type"] for h in hits})) or "-"


def _level_score(level: str) -> float:
    return {"none": 1.0, "summary": 0.6, "full": 0.2}.get(level, 0.3)


def _score_to_level(score: float, parent_level: Optional[str]) -> str:
    if parent_level == NONE:
        return NONE
    if score >= 0.7:
        return SUMMARY  # 机密词/secret 类型最高 summary（除非 PII 已在 r2 拦下）
    if score >= 0.5:
        return SUMMARY
    return FULL if parent_level == FULL else SUMMARY


def chunk_level(content: str, chunk_hash: str, parent_level: str = "summary",
                trust_level: str = "trusted", kind: str = "fact",
                owner_role: str = "worker") -> dict:
    """chunk 打标（H2b 汇入管道用）：min(敏感度链, 父文档级别) 只降不升

    _level_score 越大越严（none=1.0 > summary=0.6 > full=0.2）。
    继承封顶：判定比父级宽松（score 更小）→ 降级到父级；判定比父级严 → 保持。
    PII/UNTRUSTED 硬锁（none, score 1.0）永远比父级严 → 不受父级影响。
    """
    result = classify(content, kind=kind, trust_level=trust_level,
                      owner_role=owner_role, parent_level=parent_level)
    # 继承只降不升：判定更宽松（score 小于父级）→ 封顶到父级
    if _level_score(result["level"]) < _level_score(parent_level):
        result["level"] = parent_level
        result["reasons"].append(f"r5_parent_cap: 父文档 {parent_level} 封顶")
    result["chunk_hash"] = chunk_hash
    return result
