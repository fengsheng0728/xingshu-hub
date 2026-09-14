# U4 e2e 重跑：第 6-10 项（knowledge / wiki / memory）
import json, urllib.request, urllib.error

BASE = "http://127.0.0.1:3060"
H = {"Authorization": "Bearer e2e-u4-token", "Content-Type": "application/json"}
MGR = "u4-mgr"

def req(method, path, body=None, raw=False):
    r = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None, headers=H)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, text
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def check(name, status, text, expect_json=True):
    ok = status == 200
    shape = ""
    if expect_json and ok:
        try:
            d = json.loads(text)
            if isinstance(d, dict):
                shape = "keys=" + ",".join(list(d.keys())[:8])
                for k in ("entries", "pages", "items", "results", "memories"):
                    if k in d:
                        shape += f" {k}[{len(d[k])}]"
            elif isinstance(d, list):
                shape = f"list[{len(d)}]"
        except Exception as ex:
            ok = False
            shape = f"JSON-ERR: {ex}"
    print(f"{'PASS' if ok else 'FAIL'} {name}: status={status} {shape}")
    if not ok:
        print("  body[:300]:", text[:300])
    return ok

results = []

# 6. knowledge list
s, t = req("GET", f"/api/v1/knowledge?agent_id={MGR}")
results.append(check("knowledge_list", s, t))

# 6b. knowledge single（取列表第一条再查单条）
if s == 200:
    d = json.loads(t)
    entries = d.get("entries") or []
    if entries:
        eid = entries[0]["entry_id"]
        s2, t2 = req("GET", f"/api/v1/knowledge/{eid}?agent_id={MGR}")
        results.append(check("knowledge_single", s2, t2))
        s3, t3 = req("GET", f"/api/v1/knowledge/no-such-id?agent_id={MGR}")
        ok404 = s3 == 404
        print(f"{'PASS' if ok404 else 'FAIL'} knowledge_404: status={s3}")
        results.append(ok404)
    else:
        print("SKIP knowledge_single: 列表为空")

# 7. wiki pages
s, t = req("GET", "/api/v1/wiki/pages")
results.append(check("wiki_pages", s, t))

# 8. wiki inbox
s, t = req("GET", f"/api/v1/wiki/inbox?agent_id={MGR}")
results.append(check("wiki_inbox", s, t))

# 9. wiki search
s, t = req("GET", "/api/v1/wiki/search?q=test")
results.append(check("wiki_search", s, t))

# 10. memory list（自己查自己）
s, t = req("GET", f"/api/v1/memory?agent_id={MGR}")
results.append(check("memory_list_self", s, t))

# 10b. memory list 越权（查别人应 403）
s, t = req("GET", "/api/v1/memory?agent_id=someone-else")
ok403 = s == 403
print(f"{'PASS' if ok403 else 'FAIL'} memory_list_forbidden: status={s}")
results.append(ok403)

print(f"\n== {sum(results)}/{len(results)} passed ==")
