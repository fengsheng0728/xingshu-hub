# -*- coding: utf-8 -*-
"""XS-004 验收测试（2026-09-08）：审计锚定外发到外部介质（HTTP webhook）

覆盖：
1. webhook POST 外发：body JSON 的 anchor == 链尾 entry_hash，本地快照照写
2. 默认休眠：AUDIT_ANCHOR_URLS 为空 = 不联网（本地写成功、server 收 0 条）
3. 不可达 URL 容错：不抛异常，written=True / remote_written=False
4. verify_anchor 既有语义回归
5. Config 新字段存在且默认值正确（[] / 3600）
6. anchor_export_loop 挂载点可导入（冒烟，不真跑循环）

网络约束：仅连接本文件自己起的 127.0.0.1 随机端口 http server；不访问任何外部地址。
"""
import json
import os
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import models
from audit_chain import AuditChain, export_anchor, verify_anchor


class _CollectHandler(BaseHTTPRequestHandler):
    """收集 POST body 到 server.received（thread-safe），一律回 200"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        with self.server.lock:
            self.server.received.append(
                {"path": self.path, "body": body,
                 "content_type": self.headers.get("Content-Type")})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture()
def http_server():
    srv = HTTPServer(("127.0.0.1", 0), _CollectHandler)
    srv.received = []
    srv.lock = threading.Lock()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


@pytest.fixture()
def anchor_db(tmp_path, monkeypatch):
    """tmp sqlite 库（audit_log 全列，对齐 db.py CREATE）+ CONFIG.DB_PATH 指向它"""
    db = str(tmp_path / "anchor_test.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL DEFAULT '',
            ref_table TEXT DEFAULT '',
            ref_id TEXT DEFAULT '',
            payload TEXT DEFAULT '',
            prev_hash TEXT NOT NULL DEFAULT '',
            entry_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(models.CONFIG, "DB_PATH", db)
    ac = AuditChain(db)
    ac.append("event", "events", "xs004-1", {"k": "v1"})
    ac.append("event", "events", "xs004-2", {"k": "v2"})
    ac.append("event", "events", "xs004-3", {"k": "v3"})
    return db


def _chain_tail(db):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY log_id DESC LIMIT 1").fetchone()
    conn.close()
    return row[0]


def test_export_with_webhook_posts(anchor_db, http_server):
    url = f"http://127.0.0.1:{http_server.server_address[1]}/anchor"
    r = export_anchor(anchor_db, webhook_urls=[url])
    assert r["written"] is True, r
    assert r["remote_written"] is True, r
    assert r["webhook_results"][0]["ok"] is True, r
    with http_server.lock:
        received = list(http_server.received)
    assert len(received) == 1, f"server 应收到 1 条 POST, 实际 {len(received)}"
    body = json.loads(received[0]["body"].decode("utf-8"))
    assert body["anchor"] == _chain_tail(anchor_db), "POST body 的 anchor 应等于链尾 entry_hash"
    assert body["hub"], "hub 字段应非空（默认 hostname）"
    assert received[0]["content_type"] == "application/json"
    from audit_chain import _ANCHOR_FILE
    assert os.path.exists(_ANCHOR_FILE), "本地锚定快照应存在"
    print(f"PASS test_export_with_webhook_posts (anchor={body['anchor'][:12]}...)")


def test_export_default_no_network(anchor_db, http_server, monkeypatch):
    monkeypatch.setattr(models.CONFIG, "AUDIT_ANCHOR_URLS", [])  # 默认休眠
    r = export_anchor(anchor_db)
    assert r["written"] is True, r
    assert r["remote_written"] is False, r
    assert r["webhook_results"] == [], r
    with http_server.lock:
        assert len(http_server.received) == 0, "空 URL 列表不应产生任何网络请求"
    print("PASS test_export_default_no_network")


def test_export_unreachable_url_tolerated(anchor_db):
    r = export_anchor(anchor_db, webhook_urls=["http://127.0.0.1:1/x"])  # 拒绝连接端口
    assert r["written"] is True, r                      # 本地文件照写
    assert r["remote_written"] is False, r
    assert r["webhook_results"][0]["ok"] is False, r
    assert r["webhook_results"][0]["error"], "失败应记录 error"
    print("PASS test_export_unreachable_url_tolerated "
          f"(error={r['webhook_results'][0]['error'][:60]}...)")


def test_verify_anchor_still_works(anchor_db):
    r = export_anchor(anchor_db, webhook_urls=[])
    assert r["written"] is True, r
    v = verify_anchor(anchor_db)
    assert v["valid"] is True, v
    assert v["chain_tail"] == r["anchor"], "链头应一致"
    print("PASS test_verify_anchor_still_works")


def test_config_fields_exist():
    assert hasattr(models.CONFIG, "AUDIT_ANCHOR_URLS")
    assert hasattr(models.CONFIG, "AUDIT_ANCHOR_INTERVAL")
    assert models.CONFIG.AUDIT_ANCHOR_URLS == [], \
        f"默认应空列表(休眠), 实际 {models.CONFIG.AUDIT_ANCHOR_URLS!r}"
    assert models.CONFIG.AUDIT_ANCHOR_INTERVAL == 3600, \
        f"默认应 3600, 实际 {models.CONFIG.AUDIT_ANCHOR_INTERVAL!r}"
    print("PASS test_config_fields_exist")


def test_export_loop_module_imports():
    from routes_audit import anchor_export_loop
    assert callable(anchor_export_loop)
    print("PASS test_export_loop_module_imports")
