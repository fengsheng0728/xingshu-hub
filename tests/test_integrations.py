# -*- coding: utf-8 -*-
"""集成层（§七 门框）单测（2026-08-08）
覆盖：
  1. 目录扫描发现：example_connector + dummy2_finance 自动注册（门框有效性）
  2. 凭证加密：密钥字段 enc: 落库 / 解密还原 / API 掩码 ***
  3. 配置合并：掩码提交保留旧值；状态行字段完整
  4. 实体映射：example map_entity → CanonicalEntity（taint=external / dept / 溯源）
  5. 出口标准预埋：C-003 含机密词"客户名单"、C-004 含 PII 手机号（e2e 验证分级结果）
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import contextmanager
from integrations.base import (decrypt_config_secrets, encrypt_config_secrets,
                               is_secret_field, redact_config)
from integrations.registry import ConnectorRegistry


def _mk_registry():
    tmpdir = tempfile.mkdtemp(prefix="intg-")
    db_path = os.path.join(tmpdir, "t.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE integrations_state (
        name TEXT PRIMARY KEY, display_name TEXT DEFAULT '',
        enabled INTEGER DEFAULT 0, config_json TEXT DEFAULT '{}',
        field_mapping TEXT DEFAULT '{}', last_sync_at TEXT DEFAULT '',
        last_status TEXT DEFAULT 'never', last_error TEXT DEFAULT '',
        record_count INTEGER DEFAULT 0, pull_interval_min INTEGER DEFAULT 0,
        created_at TEXT, updated_at TEXT)""")
    conn.commit()
    conn.close()

    @contextmanager
    def db():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    return ConnectorRegistry(db), db_path


def test_discovery():
    reg, _ = _mk_registry()
    names = sorted(reg._classes.keys())
    assert "example_connector" in names and "dummy2_finance" in names, names
    avail = {a["name"]: a for a in reg.available()}
    assert avail["example_connector"]["category"] == "crm"
    assert avail["dummy2_finance"]["category"] == "finance"
    print("PASS test_discovery")


def test_secret_crypto():
    cfg = {"server_url": "http://erp.local", "api_token": "tok-abc-123", "timeout": "30"}
    enc = encrypt_config_secrets(cfg)
    assert enc["server_url"] == "http://erp.local", "非密钥字段应明文"
    assert enc["api_token"].startswith("enc:"), "密钥字段应加密"
    assert "tok-abc-123" not in json.dumps(enc), "明文不应出现在加密配置中"
    dec = decrypt_config_secrets(enc)
    assert dec["api_token"] == "tok-abc-123", "解密应还原"
    red = redact_config(cfg)
    assert red["api_token"] == "***" and red["server_url"] == "http://erp.local"
    assert is_secret_field("password") and is_secret_field("api_key")
    assert not is_secret_field("server_url")
    print("PASS test_secret_crypto")


def test_configure_and_merge():
    reg, db_path = _mk_registry()
    reg.configure("example_connector",
                  {"server_url": "http://crm.local", "api_token": "tok-1"},
                  pull_interval_min=15)
    # 库里是密文
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT config_json, pull_interval_min, enabled FROM integrations_state WHERE name='example_connector'").fetchone()
    conn.close()
    stored = json.loads(row[0])
    assert stored["api_token"].startswith("enc:"), f"落库应加密: {stored}"
    assert row[1] == 15 and row[2] == 1
    # 掩码提交保留旧值
    reg.configure("example_connector", {"api_token": "***", "timeout": "60"})
    inst = reg._get_configured("example_connector")
    assert inst._cfg["api_token"] == "tok-1", "掩码提交应保留旧密钥"
    assert inst._cfg["timeout"] == "60"
    # list_status 掩码出参
    st = {s["name"]: s for s in reg.list_status()}["example_connector"]
    assert st["config"]["api_token"] == "***"
    assert st["configured"] and st["enabled"]
    assert st["pull_interval_min"] == 15
    print("PASS test_configure_and_merge")


def test_field_mapping_and_entity():
    reg, _ = _mk_registry()
    reg.configure("example_connector", {"server_url": "http://crm.local"},
                  field_mapping={"name": "name", "note": "note"})
    inst = reg._get_configured("example_connector")
    raws = list(inst.pull(None))
    assert len(raws) == 4, "example 内置 4 条模拟记录"
    ent = inst.map_entity(raws[0], {"name": "name", "note": "note"})
    assert ent.entity_type == "customer"
    assert ent.source_system == "example_connector", "溯源：source_system"
    assert ent.source_id == "C-001", "溯源：source_id"
    assert ent.trust_level == "external", "taint 恒为 external"
    assert ent.dept == "sales", "数据域标签"
    assert "恒星贸易" in ent.content
    print("PASS test_field_mapping_and_entity")


def test_acceptance_fixtures():
    """出口标准预埋数据断言：C-003 命中机密词、C-004 命中 PII（分级结果由 e2e 验证）"""
    reg, _ = _mk_registry()
    inst = reg._classes["example_connector"]
    data = {r["id"]: r for r in inst._MOCK_DATA}
    assert "客户名单" in data["C-003"]["note"], "C-003 应含机密词（→ SUMMARY 封顶）"
    assert "13812345678" in data["C-004"]["note"], "C-004 应含手机号（→ PII NONE）"
    # dummy2 最小契约可用
    d2 = reg._classes["dummy2_finance"]
    d2.configure({"server_url": "x"})
    assert d2.test_connection().ok
    raws = list(d2.pull(None))
    ent = d2.map_entity(raws[0], {})
    assert ent.entity_type == "voucher" and ent.dept == "finance"
    assert d2.handle_webhook({}) == [], "未实现的 webhook 默认空列表"
    print("PASS test_acceptance_fixtures")


def test_unknown_connector():
    reg, _ = _mk_registry()
    try:
        reg.configure("no_such", {})
        assert False, "应 KeyError"
    except KeyError:
        pass
    r = reg.test_connection("no_such")
    assert r["ok"] is False
    print("PASS test_unknown_connector")


if __name__ == "__main__":
    test_discovery()
    test_secret_crypto()
    test_configure_and_merge()
    test_field_mapping_and_entity()
    test_acceptance_fixtures()
    test_unknown_connector()
    print("\nALL INTEGRATION UNIT TESTS PASSED")
