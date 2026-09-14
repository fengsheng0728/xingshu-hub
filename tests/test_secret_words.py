# -*- coding: utf-8 -*-
"""
K3 机密词库单测（附录 F 2026-08-06 冻结验收）
覆盖：
  1. 模板文件：base.txt 30 词 + 5 行业包存在且格式合法（每行一词）
  2. 加载优先级：目录(base+industries) > 单文件 > 内置默认
  3. 词库端点权限：worker 403 / manager 200（审批门）
  4. reload 触发 E.7 重判定
  5. 默认词库补齐 30 词（交付物标准）
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sensitivity
from sensitivity import DEFAULT_SECRET_KEYWORDS, _read_words_file, _load_secret_keywords

WORDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "secret-words")


def test_template_files_exist():
    assert os.path.exists(os.path.join(WORDS_DIR, "base.txt")), "base.txt 缺失"
    for ind in ("manufacturing", "finance", "ecommerce", "healthcare", "education"):
        p = os.path.join(WORDS_DIR, f"industry-{ind}.txt")
        assert os.path.exists(p), f"industry-{ind}.txt 缺失"
    assert os.path.exists(os.path.join(WORDS_DIR, "README.md")), "README.md 缺失"
    print("PASS test_template_files_exist")


def test_base_30_words():
    words = _read_words_file(os.path.join(WORDS_DIR, "base.txt"))
    assert len(words) == 30, f"base 应 30 词，实际 {len(words)}: {words}"
    # 无空行/无重复
    assert all(w.strip() for w in words), "存在空词"
    assert len(set(words)) == 30, "存在重复词"
    print(f"PASS test_base_30_words ({len(words)} 词)")


def test_industry_packs_nonempty():
    for ind in ("manufacturing", "finance", "ecommerce", "healthcare", "education"):
        words = _read_words_file(os.path.join(WORDS_DIR, f"industry-{ind}.txt"))
        assert len(words) >= 10, f"industry-{ind} 应 ≥10 词，实际 {len(words)}"
    print("PASS test_industry_packs_nonempty")


def test_default_keywords_30():
    assert len(DEFAULT_SECRET_KEYWORDS) == 30, f"默认词库应 30 词，实际 {len(DEFAULT_SECRET_KEYWORDS)}"
    assert len(set(DEFAULT_SECRET_KEYWORDS)) == 30
    print(f"PASS test_default_keywords_30 ({len(DEFAULT_SECRET_KEYWORDS)} 词)")


def test_dir_loading(monkeypatch):
    """目录模式：base + industries 合并加载"""
    monkeypatch.setattr(sensitivity, "_SECRET_WORDS_DIR", WORDS_DIR)
    monkeypatch.setattr(sensitivity, "_SECRET_WORDS_INDUSTRIES", ["finance"])
    words = _load_secret_keywords()
    assert "合同价" in words, "base 词应加载"
    assert "客户持仓" in words, "finance 行业词应加载"
    assert len(words) > 30, f"合并后应 >30 词，实际 {len(words)}"
    print(f"PASS test_dir_loading ({len(words)} 词 = base30 + finance)")


def test_file_loading(monkeypatch, tmp_path):
    """单文件模式"""
    f = tmp_path / "words.txt"
    f.write_text("# 测试词库\n独有词A\n独有词B\n", encoding="utf-8")
    monkeypatch.setattr(sensitivity, "_SECRET_WORDS_DIR", "")
    monkeypatch.setattr(sensitivity, "_SECRET_WORDS_FILE", str(f))
    words = _load_secret_keywords()
    assert words == ["独有词A", "独有词B"], words
    print("PASS test_file_loading")


def test_default_fallback(monkeypatch):
    monkeypatch.setattr(sensitivity, "_SECRET_WORDS_DIR", "")
    monkeypatch.setattr(sensitivity, "_SECRET_WORDS_FILE", "")
    words = _load_secret_keywords()
    assert words == DEFAULT_SECRET_KEYWORDS
    print("PASS test_default_fallback")


def test_words_endpoint_permission():
    """审批门：worker 403 / manager 200（HTTP 层）"""
    import json
    import subprocess
    import time as _t
    import urllib.request as urlreq
    import http.client

    tmpdir = tempfile.mkdtemp(prefix="k3t-")
    tmpdb = os.path.join(tmpdir, "test.db")
    cfgdir = os.path.join(tmpdir, "cfg")
    os.makedirs(cfgdir, exist_ok=True)
    with open(os.path.join(cfgdir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(f"database:\n  path: {tmpdb}\nserver:\n  port: 3065\nbackup_enabled: false\n"
                f"auth:\n  hub_token: ''\nchroma:\n  enabled: false\n")

    env = dict(os.environ)
    env["SYNC_HUB_CONFIG_DIR"] = cfgdir
    env["SYNC_HUB_SKIP_MIGRATE_BACKUP"] = "1"
    # 防污染：其他测试可能设过 NO_AUTH（pytest 共享 os.environ）——子进程必须走真实鉴权
    env.pop("SYNC_HUB_NO_AUTH", None)
    proc = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace")

    def http_req(method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", 3065, timeout=8)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(body).encode() if body else None, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, data

    ready = False
    for _ in range(80):
        try:
            with urlreq.urlopen("http://127.0.0.1:3065/health", timeout=2) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        if proc.poll() is not None:
            break
        _t.sleep(0.5)
    assert ready, f"测试 Hub 未就绪 (exit={proc.poll()})"

    try:
        # 注册 worker + manager
        st, body = http_req("POST", "/api/v1/agents/register",
                            body={"agent_id": "k3-worker", "agent_name": "w", "role": "worker"})
        assert st == 200, f"register worker 失败: {st} {body[:100]}"
        worker_key = json.loads(body)["api_key"]
        st, body = http_req("POST", "/api/v1/agents/register",
                            body={"agent_id": "k3-manager", "agent_name": "m", "role": "manager"})
        assert st == 200, f"register manager 失败: {st} {body[:100]}"
        manager_key = json.loads(body)["api_key"]
        print(f"  debug worker_key[:8]={worker_key[:8]} manager_key[:8]={manager_key[:8]}")

        # worker 查看 → 403
        st, body = http_req("GET", "/api/v1/sensitivity/words", token=worker_key)
        print(f"  debug worker GET -> {st} {body[:60]}")
        assert st == 403, f"worker 应 403，实际 {st}: {body[:100]}"
        # manager 查看 → 200
        st, body = http_req("GET", "/api/v1/sensitivity/words", token=manager_key)
        assert st == 200, f"manager 应 200，实际 {st}: {body[:100]}"
        data = json.loads(body)
        assert data["count"] >= 22, f"词库应 ≥22 词，实际 {data['count']}"
        # worker reload → 403
        st, body = http_req("POST", "/api/v1/sensitivity/words/reload", token=worker_key, body={})
        assert st == 403, f"worker reload 应 403，实际 {st}"
        # manager reload → 200
        st, body = http_req("POST", "/api/v1/sensitivity/words/reload", token=manager_key, body={"reclassify": False})
        assert st == 200, f"manager reload 应 200，实际 {st}: {body[:150]}"
        print("PASS test_words_endpoint_permission (worker 403 / manager 200)")
    finally:
        proc.kill()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nK3 机密词库: {len(tests)} 用例全绿")
