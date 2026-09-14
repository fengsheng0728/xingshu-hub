"""集成层 — Connector 注册表 + 入汇执行 + 拉取调度

发现机制：扫描 integrations/connectors/*.py，import 后调模块级 get_connector()。
新增连接器不改本文件、不改 Hub 核心任何一行代码（§7.6 出口标准实证点）。

状态持久化：integrations_state 表（db.py 建表）。配置中密钥字段 AES-GCM 加密落库。
入汇：CanonicalEntity → hub.ingest_chunks(trust_level="external") → 附录 E 管道全链能力。
"""
import importlib
import json
import pkgutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from integrations.base import (CanonicalEntity, Connector, HubEvent,
                               decrypt_config_secrets, encrypt_config_secrets,
                               redact_config)

import integrations.connectors as _connectors_pkg


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectorRegistry:
    """目录扫描注册表。实例与配置解耦：连接器实例驻内存（配置已解密），状态落库。"""

    def __init__(self, db_conn_factory):
        """
        db_conn_factory: hub._db（contextmanager → sqlite3.Connection，row 可用 dict()）
        """
        self._db = db_conn_factory
        self._classes: Dict[str, Any] = {}       # name -> connector 实例（未配置）
        self._configured: Dict[str, Any] = {}    # name -> 已 configure 的实例
        self.discover()

    # ── 发现 ──────────────────────────────────────────────
    def discover(self) -> List[str]:
        """扫描 connectors/ 目录注册全部连接器。返回发现的 name 列表。"""
        found = []
        for mod_info in pkgutil.iter_modules(_connectors_pkg.__path__):
            if mod_info.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"integrations.connectors.{mod_info.name}")
                conn = mod.get_connector()
                if not getattr(conn, "name", ""):
                    continue
                self._classes[conn.name] = conn
                found.append(conn.name)
            except Exception as e:
                print(f"[integrations] 连接器加载失败 {mod_info.name}: {type(e).__name__}: {e}")
        return sorted(found)

    def available(self) -> List[Dict[str, str]]:
        """全部已发现连接器的元信息（含未配置的）"""
        return [{"name": c.name, "display_name": getattr(c, "display_name", c.name),
                 "category": getattr(c, "category", "other")}
                for c in self._classes.values()]

    # ── 状态读写 ──────────────────────────────────────────
    def _get_row(self, name: str) -> Optional[Dict]:
        with self._db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM integrations_state WHERE name = ?", (name,))
            row = c.fetchone()
            return dict(row) if row else None

    def list_status(self) -> List[Dict[str, Any]]:
        """⑩页列表：发现到的连接器 ∪ 库里有状态的，配置出参掩码。"""
        rows = {}
        with self._db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM integrations_state")
            for r in c.fetchall():
                rows[r["name"]] = dict(r)
        out = []
        for meta in self.available():
            name = meta["name"]
            row = rows.pop(name, None)
            if row:
                cfg = json.loads(row["config_json"] or "{}")
                out.append({**meta, "enabled": bool(row["enabled"]),
                            "configured": True,
                            "config": redact_config(cfg),
                            "field_mapping": json.loads(row["field_mapping"] or "{}"),
                            "last_sync_at": row["last_sync_at"],
                            "last_status": row["last_status"],
                            "last_error": row["last_error"],
                            "record_count": row["record_count"],
                            "pull_interval_min": row["pull_interval_min"]})
            else:
                out.append({**meta, "enabled": False, "configured": False,
                            "config": {}, "field_mapping": {},
                            "last_sync_at": "", "last_status": "never",
                            "last_error": "", "record_count": 0,
                            "pull_interval_min": 0})
        # 库里有但目录里已删除的连接器 → 标记 missing
        for name, row in rows.items():
            out.append({"name": name, "display_name": row["display_name"] or name,
                        "category": "other", "enabled": bool(row["enabled"]),
                        "configured": True, "missing": True,
                        "config": redact_config(json.loads(row["config_json"] or "{}")),
                        "field_mapping": json.loads(row["field_mapping"] or "{}"),
                        "last_sync_at": row["last_sync_at"], "last_status": row["last_status"],
                        "last_error": row["last_error"], "record_count": row["record_count"],
                        "pull_interval_min": row["pull_interval_min"]})
        return sorted(out, key=lambda x: x["name"])

    # ── 配置 ──────────────────────────────────────────────
    def configure(self, name: str, cfg: Dict[str, Any],
                  field_mapping: Optional[Dict[str, str]] = None,
                  enabled: bool = True, pull_interval_min: Optional[int] = None) -> Dict[str, Any]:
        conn_cls = self._classes.get(name)
        if not conn_cls:
            raise KeyError(f"未知连接器: {name}")
        old = self._get_row(name)
        # 合并：未提交的密钥字段（前端掩码 *** 或缺省）保留旧值
        old_cfg = json.loads(old["config_json"]) if old else {}
        merged = dict(old_cfg)
        for k, v in (cfg or {}).items():
            if v == "***" or v is None:
                continue
            merged[k] = v
        enc_cfg = encrypt_config_secrets(merged)
        mapping_json = json.dumps(field_mapping if field_mapping is not None
                                  else (json.loads(old["field_mapping"]) if old else {}))
        now = _now()
        with self._db() as conn:
            c = conn.cursor()
            c.execute("""INSERT OR REPLACE INTO integrations_state
                (name, display_name, enabled, config_json, field_mapping,
                 last_sync_at, last_status, last_error, record_count,
                 pull_interval_min, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, getattr(conn_cls, "display_name", name), 1 if enabled else 0,
                 json.dumps(enc_cfg), mapping_json,
                 old["last_sync_at"] if old else "",
                 old["last_status"] if old else "never",
                 "", old["record_count"] if old else 0,
                 pull_interval_min if pull_interval_min is not None
                     else (old["pull_interval_min"] if old else 0),
                 old["created_at"] if old else now, now))
            conn.commit()
        # 重建已配置实例
        instance = type(conn_cls)() if not callable(getattr(conn_cls, "reclone", None)) else conn_cls.reclone()
        instance.configure(decrypt_config_secrets(enc_cfg))
        self._configured[name] = instance
        return {"status": "ok", "name": name, "enabled": enabled}

    def _get_configured(self, name: str) -> Any:
        """取已配置实例（懒加载：内存没有则从库还原）"""
        if name in self._configured:
            return self._configured[name]
        row = self._get_row(name)
        if not row:
            raise KeyError(f"连接器未配置: {name}")
        conn_cls = self._classes.get(name)
        if not conn_cls:
            raise KeyError(f"连接器文件缺失: {name}")
        instance = type(conn_cls)()
        instance.configure(decrypt_config_secrets(json.loads(row["config_json"] or "{}")))
        self._configured[name] = instance
        return instance

    def _update_status(self, name: str, status: str, error: str = "",
                       record_delta: int = 0) -> None:
        with self._db() as conn:
            c = conn.cursor()
            c.execute("""UPDATE integrations_state
                         SET last_status = ?, last_error = ?, last_sync_at = ?,
                             record_count = record_count + ?, updated_at = ?
                         WHERE name = ?""",
                      (status, error, _now(), record_delta, _now(), name))
            conn.commit()

    # ── 操作 ──────────────────────────────────────────────
    def test_connection(self, name: str) -> Dict[str, Any]:
        try:
            inst = self._get_configured(name)
        except KeyError as e:
            return {"ok": False, "detail": str(e)}
        r = inst.test_connection()
        return {"ok": r.ok, "detail": r.detail, "latency_ms": r.latency_ms}

    async def ingest_entities(self, hub, name: str,
                              entities: List[CanonicalEntity]) -> Dict[str, int]:
        """CanonicalEntity → 附录 E 管道。taint: trust_level 恒为 external。"""
        stats = {"received": len(entities), "ingested": 0, "locked_none": 0,
                 "summary_capped": 0, "errors": 0}
        for ent in entities:
            try:
                doc_id = f"intg:{ent.source_system}:{ent.entity_type}:{ent.source_id}"
                r = await hub.ingest_chunks(
                    doc_id=doc_id, content=ent.content,
                    source_agent_id=f"integration:{name}",
                    kind="fact", trust_level="external",  # taint（§七 关键设计决策）
                    owner_role="worker")
                stats["ingested"] += 1
                lv = r.get("disclosure_level", "")
                if lv == "none":
                    stats["locked_none"] += 1
                elif lv == "summary":
                    stats["summary_capped"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"[integrations] 入汇失败 {name}/{ent.source_id}: {type(e).__name__}: {e}")
        return stats

    async def run_pull(self, hub, name: str, full: bool = False) -> Dict[str, Any]:
        """手动/调度拉取：pull → map_entity → E 管道 → 状态更新 + 审计。"""
        inst = self._get_configured(name)
        row = self._get_row(name)
        mapping = json.loads(row["field_mapping"]) if row else {}
        since = None if full or not row or not row["last_sync_at"] else datetime.fromisoformat(row["last_sync_at"])
        raw_records = list(inst.pull(since))
        entities = [inst.map_entity(r, mapping) for r in raw_records]
        stats = await self.ingest_entities(hub, name, entities)
        self._update_status(name, "ok" if stats["errors"] == 0 else "partial",
                            record_delta=stats["ingested"])
        await hub._log_event("integration_pull", f"integration:{name}", {
            "connector": name, "mode": "full" if full else "incremental",
            **stats})
        return {"status": "ok", "connector": name, **stats}

    async def run_webhook(self, hub, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """入站 webhook：handle_webhook → map_entity → E 管道。"""
        inst = self._get_configured(name)
        row = self._get_row(name)
        mapping = json.loads(row["field_mapping"]) if row else {}
        handler = getattr(inst, "handle_webhook", None)
        raw_records = handler(payload) if callable(handler) else []
        entities = [inst.map_entity(r, mapping) for r in raw_records]
        stats = await self.ingest_entities(hub, name, entities)
        self._update_status(name, "ok", record_delta=stats["ingested"])
        await hub._log_event("integration_webhook", f"integration:{name}", {
            "connector": name, **stats})
        return {"status": "ok", "connector": name, **stats}

    async def run_outbound(self, hub, name: str, event: HubEvent,
                           requester: str) -> Dict[str, Any]:
        """出站统一入口（§7.4）：写外部系统 = 高危操作 → 一律审批门拦截 + 审计。
        门框期不真正下发；审批机制落地后在此放行。"""
        await hub._log_event("integration_outbound_blocked", requester, {
            "connector": name, "event_type": event.event_type,
            "reason": "outbound_requires_approval"})
        return {"status": "pending_approval",
                "detail": "出站写外部系统属高危操作，已拦截并记入审计链，待审批门放行",
                "connector": name, "event_type": event.event_type}


# ── 调度循环（lifespan 启动）────────────────────────────────

async def integration_scheduler(hub, registry: ConnectorRegistry,
                                tick_sec: int = 60) -> None:
    """拉取调度：每分钟扫 integrations_state，到期 enabled 连接器自动增量拉取。"""
    import asyncio
    while True:
        await asyncio.sleep(tick_sec)
        try:
            with registry._db() as conn:
                c = conn.cursor()
                c.execute("""SELECT name, last_sync_at, pull_interval_min
                             FROM integrations_state
                             WHERE enabled = 1 AND pull_interval_min > 0""")
                rows = [dict(r) for r in c.fetchall()]
            now = datetime.now(timezone.utc)
            for row in rows:
                due = True
                if row["last_sync_at"]:
                    last = datetime.fromisoformat(row["last_sync_at"])
                    due = (now - last).total_seconds() >= row["pull_interval_min"] * 60
                if due:
                    try:
                        await registry.run_pull(hub, row["name"])
                    except Exception as e:
                        registry._update_status(row["name"], "error", str(e)[:200])
        except Exception as e:
            print(f"[integrations] 调度循环异常: {type(e).__name__}: {e}")
