# 集成层（§七）e2e — §7.6 出口标准全项验证（真实 Hub）
import json, sqlite3, urllib.request, urllib.error

BASE = "http://127.0.0.1:3060"
H = {"Authorization": "Bearer e2e-intg-token", "Content-Type": "application/json"}
MGR = "u4-mgr"   # 已注册 manager 角色

def req(method, path, body=None):
    r = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None, headers=H)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except Exception: return e.code, {}

results = []
def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'} {name} {extra}")
    results.append(bool(cond))

# 0. 出口标准⑤前置：两个连接器被发现（dummy2 不改核心即注册）
s, d = req("GET", f"/api/v1/integrations?agent_id={MGR}")
names = [c["name"] for c in d.get("connectors", [])]
check("discovery: example+dummy2", "example_connector" in names and "dummy2_finance" in names, str(names))

# 1. 配置 example（含密钥字段 + 字段映射 + 拉取间隔）
s, d = req("POST", f"/api/v1/integrations/example_connector/configure?agent_id={MGR}", {
    "config": {"server_url": "http://crm.example.local", "api_token": "tok-e2e-secret"},
    "field_mapping": {"name": "name", "note": "note", "contact": "contact", "level": "level"},
    "enabled": True, "pull_interval_min": 0})
check("configure", s == 200 and d.get("status") == "ok", f"status={s}")

# 2. 列表掩码 + 状态
s, d = req("GET", f"/api/v1/integrations?agent_id={MGR}")
ex = {c["name"]: c for c in d["connectors"]}["example_connector"]
check("config_masked", ex["config"].get("api_token") == "***", json.dumps(ex["config"]))
check("configured_enabled", ex["configured"] and ex["enabled"])

# 3. 测试连接
s, d = req("POST", f"/api/v1/integrations/example_connector/test?agent_id={MGR}")
check("test_connection", d.get("ok") is True, d.get("detail", ""))

# 4. 出口标准②：手动拉取闭环 — 4 条模拟记录入汇，C-003 SUMMARY 封顶 / C-004 PII NONE
s, d = req("POST", f"/api/v1/integrations/example_connector/pull?agent_id={MGR}", {"full": True})
check("pull_full_4_records", d.get("received") == 4 and d.get("ingested") == 4, json.dumps(d))
check("pull_summary_capped", d.get("summary_capped", 0) >= 1, f"summary_capped={d.get('summary_capped')}")
check("pull_pii_locked_none", d.get("locked_none", 0) >= 1, f"locked_none={d.get('locked_none')}")

# 5. 出口标准①③：落库验证 — trust_level=external + 分级正确 + 溯源
conn = sqlite3.connect("sync_hub.db"); conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT parent_doc_id, disclosure_level, trust_level, source_agent_id FROM document_chunks "
    "WHERE parent_doc_id LIKE 'intg:example_connector%'").fetchall()
conn.close()
by_doc = {}
for r in rows:
    d0 = by_doc.setdefault(r["parent_doc_id"], {"levels": set(), "trust": r["trust_level"], "src": r["source_agent_id"]})
    d0["levels"].add(r["disclosure_level"])
c3 = by_doc.get("intg:example_connector:customer:C-003", {})
c4 = by_doc.get("intg:example_connector:customer:C-004", {})
c1 = by_doc.get("intg:example_connector:customer:C-001", {})
check("taint_external", all(v["trust"] == "external" for v in by_doc.values()),
      str({k: v["trust"] for k, v in by_doc.items()}))
check("c3_summary_capped", "summary" in c3.get("levels", set()) and "full" not in c3.get("levels", set()),
      str(c3.get("levels")))
check("c4_none_locked", c4.get("levels") == {"none"}, str(c4.get("levels")))
# worker 角色入汇 → r4 规则 SUMMARY 上限（正确语义：业务内容 worker 写入封顶 summary）
check("c1_worker_capped", "summary" in c1.get("levels", set()), str(c1.get("levels")))
check("source_tracing", all(v["src"] == "integration:example_connector" for v in by_doc.values()))

# 6. 检索可见：knowledge 列表含 intg 聚合条目（C-004 NONE 不建条目）
s, d = req("GET", f"/api/v1/knowledge?agent_id={MGR}")
intg_entries = [e for e in d.get("entries", []) if str(e.get("entry_id", "")).startswith("doc:intg:")]
intg_ids = {e["entry_id"] for e in intg_entries}
check("knowledge_visible", any("C-001" in i for i in intg_ids), f"{len(intg_entries)} intg entries")
check("knowledge_none_excluded", not any("C-004" in i for i in intg_ids))

# 7. webhook 入站 → 检索可见
s, d = req("POST", "/api/v1/integrations/example_connector/webhook", {
    "id": "C-900", "type": "customer", "name": "webhook 推送客户",
    "contact": "测试", "level": "B", "note": "webhook 实时推送验证"})
check("webhook_ingested", d.get("ingested") == 1, json.dumps(d))
s, d = req("GET", f"/api/v1/knowledge?agent_id={MGR}")
check("webhook_visible", any("C-900" in str(e.get("entry_id", "")) for e in d.get("entries", [])))

# 8. 出口标准④：出站审批门拦截 + 审计
s, d = req("POST", f"/api/v1/integrations/example_connector/outbound?agent_id={MGR}", {
    "event_type": "customer_updated", "payload": {"id": "C-001"}})
check("outbound_blocked", d.get("status") == "pending_approval", json.dumps(d))

# 9. 审计链：integration_pull / webhook / outbound_blocked / configured 均入链
s, d = req("GET", f"/api/audit/events?agent_id={MGR}&limit=200")
acts = set()
for e in d.get("rows", []):
    try: acts.add(json.loads(e.get("payload") or "{}").get("event_type", ""))
    except Exception: pass
need = {"integration_configured", "integration_pull", "integration_webhook", "integration_outbound_blocked"}
check("audit_chain", need <= acts, f"found={sorted(acts & need)}")

# 10. dummy2 配置 + 拉取（门框有效性二次实证）
s, d = req("POST", f"/api/v1/integrations/dummy2_finance/configure?agent_id={MGR}", {
    "config": {"server_url": "http://fin.example.local"}, "enabled": True})
s, d = req("POST", f"/api/v1/integrations/dummy2_finance/pull?agent_id={MGR}", {"full": True})
check("dummy2_pull", d.get("ingested") == 2, json.dumps(d))

# 11. 角色门：worker 配置应 403
s, d = req("POST", "/api/v1/integrations/example_connector/configure?agent_id=u4-worker-x",
           {"config": {"server_url": "http://evil"}})
check("role_gate_worker_403", s == 403, f"status={s}")

# 12. 记录数闭环：列表 record_count 反映拉取
s, d = req("GET", f"/api/v1/integrations?agent_id={MGR}")
ex = {c["name"]: c for c in d["connectors"]}["example_connector"]
check("record_count_loop", ex["record_count"] >= 5, f"record_count={ex['record_count']}")

print(f"\n== {sum(results)}/{len(results)} passed ==")
