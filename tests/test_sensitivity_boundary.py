# -*- coding: utf-8 -*-
r"""
CD-066 / T25：PII 正则 `\b` 词界盲区修复回归（只断言结果，不改实现）

背景：Python re 中汉字属 \w，旧 `\b` 词界导致与汉字粘连的 PII 漏检
（改动前 scan_pii("联系人13800138000") 返回 []，已实测确认先红）。
修复：五个 RE_* 的 `\b` 换成数字/字母边界 lookaround，只扩大召回不缩窄。

覆盖：
  S-1 粘连汉字手机号（先红核心）
  S-2 粘连汉字身份证
  S-3 粘连银行卡 / 密钥串 / 邮箱
  S-4 只扩大不缩窄：既有可命中形态（空格分隔/标点相邻/行首行尾/粘连英数）逐样本断言
  S-5 反例不误命中：超长数字串内部 / 非 1[3-9] 开头 / 19 位纯数字（非卡号前缀）
  S-6 classify() 粘连 PII → NONE + locked
  S-7 chunk_level() 含粘连 PII 的 chunk 定级 NONE
  S-8 邮箱/密钥串粘连与反例
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sensitivity import scan_pii, classify, chunk_level

PHONE = "13800138000"
ID_CARD = "11010519491231002X"
BANK_CARD = "6222021234567890123"  # 19 位，62 前缀
SECRET_KEY = "sk-abcdefghijklmnop1234"  # sk- + 20 位
EMAIL = "zhangsan@example.com"


def _types(text):
    return {h["type"] for h in scan_pii(text)}


# ── S-1 粘连汉字手机号（先红核心） ──

def test_s1_phone_glued_to_chinese():
    hits = scan_pii(f"联系人{PHONE}")
    assert any(h["type"] == "phone" for h in hits), f"粘连汉字手机号应命中: {hits}"
    print("PASS test_s1_phone_glued_to_chinese")


# ── S-2 粘连汉字身份证 ──

def test_s2_id_card_glued_to_chinese():
    hits = scan_pii(f"身份证{ID_CARD}")
    assert any(h["type"] == "id_card" for h in hits), f"粘连汉字身份证应命中: {hits}"
    print("PASS test_s2_id_card_glued_to_chinese")


# ── S-3 粘连银行卡 / 密钥串 / 邮箱 ──

def test_s3_bank_card_glued_to_chinese():
    hits = scan_pii(f"卡号{BANK_CARD}")
    assert any(h["type"] == "bank_card" for h in hits), f"粘连汉字银行卡应命中: {hits}"
    print("PASS test_s3_bank_card_glued_to_chinese")


def test_s3_secret_key_glued_to_chinese():
    hits = scan_pii(f"密钥{SECRET_KEY}请保存")
    assert any(h["type"] == "secret_key" for h in hits), f"粘连汉字密钥串应命中: {hits}"
    print("PASS test_s3_secret_key_glued_to_chinese")


def test_s3_email_glued_to_chinese():
    hits = scan_pii(f"邮箱{EMAIL}备用")
    assert any(h["type"] == "email" for h in hits), f"粘连汉字邮箱应命中: {hits}"
    print("PASS test_s3_email_glued_to_chinese")


# ── S-4 只扩大不缩窄：既有可命中形态改后仍命中（逐样本断言） ──

def test_s4_no_narrowing_phone_forms():
    # 空格分隔（改动前即可命中）
    assert "phone" in _types(f"联系人 {PHONE}"), "空格分隔形态不得丢失"
    # 行首 / 行尾（独占一行）
    assert "phone" in _types(PHONE), "独占行形态不得丢失"
    assert "phone" in _types(f"{PHONE}\n下一行"), "行首形态不得丢失"
    assert "phone" in _types(f"上一行\n{PHONE}"), "行尾形态不得丢失"
    # 标点相邻（改动前即可命中）
    assert "phone" in _types(f"（{PHONE}）"), "全角括号相邻形态不得丢失"
    assert "phone" in _types(f"tel:{PHONE},"), "冒号/逗号相邻形态不得丢失"
    print("PASS test_s4_no_narrowing_phone_forms")


def test_s4_phone_glued_to_alnum():
    # 粘连英数：数字边界 lookaround 下字母相邻也命中（只扩大召回）
    assert "phone" in _types(f"id{PHONE}"), "粘连字母手机号应命中"
    assert "phone" in _types(f"no.{PHONE}"), "no. 粘连手机号应命中"
    print("PASS test_s4_phone_glued_to_alnum")


# ── S-5 反例不误命中 ──

def test_s5_overlong_digit_run_not_matched():
    # 12 位超长数字串：11 位手机号窗口右侧仍是数字 → 不得命中
    assert "phone" not in _types("138001380001"), "12 位数字串内部不得命中手机号"
    # 非 1[3-9] 开头（0 开头 12 位，内部含合法手机号窗口但左侧是数字）
    assert "phone" not in _types("013800138000"), "非 1[3-9] 开头不得命中手机号"
    # 20 位长数字串内部
    hits = _types("12345678901234567890")
    assert "phone" not in hits and "bank_card" not in hits, \
        f"长数字串内部不得命中 phone/bank_card: {hits}"
    print("PASS test_s5_overlong_digit_run_not_matched")


def test_s5_nineteen_digit_non_card_prefix_not_matched():
    # 19 位纯数字但非卡号前缀（12 开头）→ 不得命中 bank_card
    assert "bank_card" not in _types("1234567890123456789"), \
        "19 位纯数字（非卡号前缀）不得命中 bank_card"
    # 卡号右侧再跟数字（20 位数字串）→ 不得命中
    assert "bank_card" not in _types("62220212345678901234"), \
        "卡号形态右侧跟数字（更长数字串内部）不得命中"
    # 身份证右侧跟数字（19 位数字串内部）→ 不得命中 id_card
    assert "id_card" not in _types("1101051949123100212"), \
        "身份证形态右侧跟数字（更长数字串内部）不得命中"
    print("PASS test_s5_nineteen_digit_non_card_prefix_not_matched")


# ── S-6 classify() 粘连 PII → NONE + locked（只断言结果，不改实现） ──

def test_s6_classify_glued_pii_none_locked():
    r = classify(f"联系人{PHONE}已确认")
    assert r["level"] == "none", f"粘连 PII 应定级 none，实际 {r['level']}"
    assert r["locked"] is True, f"粘连 PII 应 locked，实际 {r}"
    assert r["rule"] == "r2_pii"
    # 掩码约束：原始串不出现在判定结果里
    import json
    assert PHONE not in json.dumps(r, ensure_ascii=False), "判定结果不得含原始手机号"
    print("PASS test_s6_classify_glued_pii_none_locked")


# ── S-7 chunk_level() 含粘连 PII 的 chunk 定级不低于 NONE ──

def test_s7_chunk_level_glued_pii():
    r = chunk_level(f"联系人{PHONE}", chunk_hash="cd066-h1", parent_level="summary")
    assert r["level"] == "none", f"含粘连 PII 的 chunk 应定级 none，实际 {r['level']}"
    assert r["locked"] is True
    print("PASS test_s7_chunk_level_glued_pii")


# ── S-8 邮箱/密钥串粘连与反例 ──

def test_s8_email_secret_key_boundary():
    # 粘连命中
    assert "email" in _types(f"联系{EMAIL}，谢谢")
    assert "secret_key" in _types(f"凭据{SECRET_KEY}。")
    # 反例：TLD 不足 2 位不得命中邮箱
    assert "email" not in _types("a@b.c"), "TLD 1 位不得命中邮箱"
    # 反例：邮箱右侧跟字母数字（更长串内部）不得命中
    assert "email" not in _types("zhangsan@example.com2"), \
        "邮箱右侧跟数字不得命中"
    # 反例：sk- 后不足 16 位不得命中密钥串
    assert "secret_key" not in _types("sk-tooshort123"), \
        "sk- 后不足 16 位不得命中密钥串"
    # 反例：密钥串左侧粘字母（更长字母数字串内部）不得命中
    assert "secret_key" not in _types(f"x{SECRET_KEY}"), \
        "密钥串左侧粘字母不得命中内部片段"
    print("PASS test_s8_email_secret_key_boundary")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nCD-066 boundary: {len(tests)} 用例全绿")
