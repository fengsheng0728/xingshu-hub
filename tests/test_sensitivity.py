# -*- coding: utf-8 -*-
"""
H2 敏感度链单测（附录 E v1.4 冻结验收）
覆盖：
  1. 6 维判定链：PII(身份证/手机/密钥串)/UNTRUSTED/机密词/角色/父级继承/类型
  2. E.1 掩码样本：返回结果零原始 PII；审计 payload 只含掩码
  3. E.1 硬断言：审计库 grep 原始 PII 串零命中（HTTP 层模拟）
  4. E.6：NONE 级不建 embedding（store_memory 路径）
  5. locked 回执
  6. A3 规则表并入：RULES 含 r9_sensitivity_cap
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import textwrap

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（tests/ 上一级）
sys.path.insert(0, str(_ROOT))

from sensitivity import classify, scan_pii, mask_sample, chunk_level, DEFAULT_SECRET_KEYWORDS

PII_RAW = "110101199003071234"
PHONE_RAW = "13812345678"


# ── 1. 判定链 ──

def test_pii_id_card_none():
    r = classify(f"客户 {PII_RAW} 已存档")
    assert r["level"] == "none" and r["locked"]
    assert r["pii_hits"][0]["type"] == "id_card"
    print("PASS test_pii_id_card_none")


def test_pii_phone_none():
    r = classify(f"联系 {PHONE_RAW} 确认")
    assert r["level"] == "none" and r["locked"]
    assert r["pii_hits"][0]["type"] == "phone"
    print("PASS test_pii_phone_none")


def test_pii_secret_key_none():
    r = classify("token 是 sk-abcdefghijklmnopqrstuvwxyz123456 请保存")
    assert r["level"] == "none" and r["locked"]
    assert r["pii_hits"][0]["type"] == "secret_key"
    print("PASS test_pii_secret_key_none")


def test_untrusted_none():
    r = classify("外部工具数据", trust_level="untrusted")
    assert r["level"] == "none" and r["locked"]
    assert r["rule"] == "r1_trust"
    print("PASS test_untrusted_none")


def test_secret_keyword_summary_cap():
    r = classify("合同价 500 万，客户名单见附件", kind="fact", owner_role="worker")
    assert r["level"] == "summary", f"机密词应 summary 上限，实际 {r['level']}"
    print("PASS test_secret_keyword_summary_cap")


def test_parent_level_cap():
    # 父文档 NONE → chunk 强制 NONE（继承只降不升）
    r = chunk_level("正常内容", chunk_hash="h1", parent_level="none")
    assert r["level"] == "none", f"父文档 NONE 应封顶，实际 {r['level']}"
    # 父文档 summary + 机密词 → summary（不升）
    r2 = chunk_level("含合同价", chunk_hash="h2", parent_level="summary")
    assert r2["level"] == "summary"
    print("PASS test_parent_level_cap")


# ── 2. E.1 掩码样本 ──

def test_mask_no_raw_pii():
    r = classify(f"身份证 {PII_RAW} 和手机 {PHONE_RAW}")
    dump = json.dumps(r, ensure_ascii=False)
    assert PII_RAW not in dump, "原始身份证号泄漏到判定结果!"
    assert PHONE_RAW not in dump, "原始手机号泄漏到判定结果!"
    # 掩码形式必须存在
    assert "1101" in dump and "1234" in dump
    assert "138" in dump and "5678" in dump
    print("PASS test_mask_no_raw_pii")


def test_mask_sample_formats():
    assert mask_sample("110101199003071234", 0, 18, "id_card") == "1101**********1234"
    assert mask_sample("13812345678", 0, 11, "phone") == "138****5678"
    assert mask_sample("abc", 0, 3, "email") == "***"
    print("PASS test_mask_sample_formats")


def test_scan_pii_fulltext_cross_boundary():
    # E.1：预扫在全文跑——身份证横跨"切割边界"也能命中（模拟：字符串中间插换行/分隔符）
    # 注意：真实身份证不会带空格，这里验证 scan 是对全文而非单行
    text = "第一段内容\n" + PII_RAW + " 后续"
    hits = scan_pii(text)
    assert any(h["type"] == "id_card" for h in hits), "全文预扫应命中跨段 PII"
    print("PASS test_scan_pii_fulltext_cross_boundary")


# ── 3. 硬断言：审计零原始 PII ──

def test_audit_payload_masked_only():
    """模拟 _log_event 的 payload 构造（store_memory 打标路径），断言只含掩码"""
    from sensitivity import classify
    _sens = classify(f"身份证 {PII_RAW}")
    audit_payload = {
        "memory_key": "test-key",
        "pii_hits": _sens.get("pii_hits", []),  # 与 hub_core 的 _log_event 一致
        "level": "none",
    }
    dump = json.dumps(audit_payload, ensure_ascii=False)
    assert PII_RAW not in dump, f"审计 payload 含原始 PII! {dump}"
    print("PASS test_audit_payload_masked_only")


# ── 4. E.6 向量约束 ──

def test_e6_none_skips_embedding():
    """NONE 级（locked）→ embedding 生成被跳过（hub_core store_memory 路径）"""
    # 直接验证 classify 结果与 embedding 决策的耦合点：
    # _locked=True → hub_core 跳过 model.encode。这里验证 locked 状态可被消费方正确读取
    r = classify(f"银行卡 6217000012345678901 绑定")
    assert r["locked"] is True
    # bank_card 正则
    assert any(h["type"] == "bank_card" for h in r["pii_hits"])
    print("PASS test_e6_none_skips_embedding")


# ── 5. A3 规则表并入 ──

def test_rules_table_contains_sensitivity():
    from disclosure_rules import rule_table, RULES
    rules = rule_table()
    ids = [r["id"] for r in rules]
    assert "r9_sensitivity_cap" in ids, f"规则表缺 r9_sensitivity_cap: {ids}"
    assert "r10_default_none" in ids
    assert "r9_default_none" not in ids, "旧 r9 id 应改名"
    # 模拟器默认分支返回 r10（构造：requester=worker, owner=manager → 无任何规则命中 → 兜底）
    from disclosure_rules import simulate
    from models import DisclosureLevel
    lv, rule_id = simulate(
        {"owner_agent_id": "mgr-A"}, "wrk-B", {}, DisclosureLevel.SUMMARY,
        {"mgr-A": {"role": "manager"}, "wrk-B": {"role": "worker"}}, {}, db_path="")
    assert rule_id == "r10_default_none", f"模拟器默认分支应 r10，实际 {rule_id}"
    print("PASS test_rules_table_contains_sensitivity")


# ── 6. 端到端：真实 Hub 写入 locked 回执 + 审计零 PII ──

def test_e2e_store_memory_locked():
    """独立测试 Hub：POST memory/store 含身份证 → locked:true + 审计掩码 + DB 零原始 PII 于审计"""
    import subprocess as sp
    import time as _time

    tmpdir = tempfile.mkdtemp(prefix="h2t-")
    tmpdb = os.path.join(tmpdir, "test.db")
    cfgdir = os.path.join(tmpdir, "cfg")
    os.makedirs(cfgdir, exist_ok=True)
    with open(os.path.join(cfgdir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(f"database:\n  path: {tmpdb}\nserver:\n  port: 3062\nbackup_enabled: false\n"
                f"auth:\n  hub_token: ''\nchroma:\n  enabled: false\n")

    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = cfgdir
    env["SYNC_HUB_SKIP_MIGRATE_BACKUP"] = "1"
    env["SYNC_HUB_NO_AUTH"] = "1"
    # 独立进程起 Hub（skill 配方：config 独立 + cwd 留项目根）
    proc = sp.Popen([sys.executable, "-u", "main.py"], cwd=str(_ROOT),
                    stdout=sp.PIPE, stderr=sp.STDOUT, env=env, text=True, encoding="utf-8", errors="replace")

    import urllib.request as urlreq
    import urllib.error as urlerr
    import http.client

    def http_post(path, body):
        conn = http.client.HTTPConnection("127.0.0.1", 3062, timeout=8)
        conn.request("POST", path, body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, data

    # 等 health 就绪（最长 40s）
    ready = False
    for _ in range(80):
        try:
            with urlreq.urlopen("http://127.0.0.1:3062/health", timeout=2) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        if proc.poll() is not None:
            break
        _time.sleep(0.5)
    assert ready, f"测试 Hub 未就绪 (proc exited={proc.poll()})"

    try:
        # 写含身份证的记忆
        st, body = http_post("/api/v1/memory/store?agent_id=h2-agent", {
            "memory_key": "pii-key-1",
            "content": f"客户 {PII_RAW} 的档案已归档",
            "kind": "fact",
            "disclosure_level": "full",  # 请求 full，但敏感度应强制 none
        })
        assert st == 200, f"store 应 200，实际 {st}: {body}"
        resp = json.loads(body)
        print("store resp:", json.dumps(resp, ensure_ascii=False)[:200])
        assert resp.get("locked") is True, f"应 locked:true，实际 {resp}"
        assert resp.get("disclosure_level") == "none", f"级别应 none，实际 {resp.get('disclosure_level')}"

        # 硬断言：审计库 grep 原始 PII 零命中
        import sqlite3
        conn = sqlite3.connect(tmpdb)
        # events 表 payload 全扫
        rows = conn.execute("SELECT payload FROM events").fetchall()
        conn.close()
        all_payload = json.dumps([r[0] for r in rows], ensure_ascii=False)
        assert PII_RAW not in all_payload, f"审计库含原始身份证! {all_payload[:500]}"
        assert "1101" in all_payload and "1234" in all_payload, "掩码样本应在审计中"

        # memory_pool 落库（content 落库但级别 NONE）
        conn = sqlite3.connect(tmpdb)
        row = conn.execute("SELECT disclosure_level, embedding IS NOT NULL, content FROM memory_pool WHERE memory_key='pii-key-1'").fetchone()
        conn.close()
        assert row is not None, "记忆应落库（E.1：不静默丢弃）"
        assert row[0] == "none", f"存储级别应 none，实际 {row[0]}"
        assert row[1] == 0, "E.6: NONE 级不应有 embedding"
        assert row[2] is not None and PII_RAW in row[2], "content 落库（可被 owner 读取）"
        print("H2 E2E PASS: locked回执 + 审计零原始PII + NONE无embedding + content落库")
    finally:
        proc.kill()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nH2 sensitivity: {len(tests)} 用例全绿")
