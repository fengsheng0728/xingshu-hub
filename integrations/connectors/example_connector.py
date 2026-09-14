"""示例连接器（《适配器开发指南》配套）— 模拟一个迷你 CRM 系统。

用途：跑通全链路验证 —— webhook/pull → 实体映射 → 附录 E 管道 → 检索可见。
内置 4 条模拟记录，其中 C-003 含机密词"客户名单"（→ 敏感度封顶 SUMMARY），
C-004 含 PII 手机号（→ 强制 NONE，只进审计不进图谱/wiki）。
"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from integrations.base import (CanonicalEntity, Connector, HubEvent, RawRecord,
                               TestResult)


class ExampleConnector:
    name = "example_connector"
    display_name = "示例 CRM（模拟）"
    category = "crm"

    # 模拟外部系统数据（真实适配器这里走 HTTP/DB 拉取）
    _MOCK_DATA: List[Dict[str, Any]] = [
        {"id": "C-001", "type": "customer", "name": "恒星贸易有限公司",
         "contact": "王经理", "level": "A",
         "note": "年框客户，主营工业耗材，合作稳定。",
         "updated_at": "2026-08-01T09:00:00+00:00"},
        {"id": "C-002", "type": "customer", "name": "蓝湾餐饮连锁",
         "contact": "陈店长", "level": "B",
         "note": "三季度有扩店计划，关注账期政策。",
         "updated_at": "2026-08-02T10:30:00+00:00"},
        {"id": "C-003", "type": "customer", "name": "北区渠道部",
         "contact": "（内部）", "level": "S",
         "note": "此表为客户名单，含全部渠道返点政策，严禁外传。",
         "updated_at": "2026-08-03T14:00:00+00:00"},
        {"id": "C-004", "type": "customer", "name": "个人客户-李雷",
         "contact": "李雷", "level": "C",
         "note": "联系电话 13812345678，偏好微信沟通。",
         "updated_at": "2026-08-04T16:20:00+00:00"},
    ]

    def __init__(self):
        self._cfg: Dict[str, Any] = {}

    # ── Connector 契约 ────────────────────────────────────
    def configure(self, cfg: Dict[str, Any]) -> None:
        self._cfg = cfg or {}

    def test_connection(self) -> TestResult:
        # 模拟：要求配置里必须有 server_url（演示"未配置→失败→配置→成功"闭环）
        if not self._cfg.get("server_url"):
            return TestResult(ok=False, detail="未配置 server_url")
        return TestResult(ok=True, detail=f"模拟连接成功 → {self._cfg['server_url']}",
                          latency_ms=3.0)

    def pull(self, since: Optional[datetime]) -> Iterator[RawRecord]:
        for rec in self._MOCK_DATA:
            if since:
                try:
                    if datetime.fromisoformat(rec["updated_at"]) <= since:
                        continue
                except ValueError:
                    pass
            yield RawRecord(source_id=rec["id"], entity_hint=rec["type"],
                            payload=rec, updated_at=rec["updated_at"])

    def handle_webhook(self, payload: Dict[str, Any]) -> List[RawRecord]:
        """外部系统推送单条记录：{"id": ..., "type": ..., "name": ..., "note": ...}"""
        if not payload.get("id"):
            return []
        return [RawRecord(
            source_id=str(payload["id"]),
            entity_hint=payload.get("type", "customer"),
            payload=payload,
            updated_at=payload.get("updated_at")
                       or datetime.now(timezone.utc).isoformat())]

    def map_entity(self, raw: RawRecord,
                   mapping: Optional[Dict[str, str]] = None) -> CanonicalEntity:
        """字段映射：mapping 非空时按 server config 映射表取字段（可视化编辑），
        否则用内置默认映射。"""
        p = raw.payload
        m = mapping or {}
        def g(field: str, default: str = "") -> str:
            src = m.get(field, field)     # 规范字段 → 外部字段名
            return str(p.get(src, default))

        title = g("name", raw.source_id)
        note = g("note")
        contact = g("contact")
        level = g("level")
        content = (f"客户档案 {raw.source_id}：{title}。"
                   f"联系人：{contact}；等级：{level}。备注：{note}")
        return CanonicalEntity(
            entity_type="customer",
            source_system=self.name,
            source_id=raw.source_id,
            title=title, content=content,
            fields={"contact": contact, "level": level},
            dept="sales",
            updated_at=raw.updated_at)

    def push_outbound(self, event: HubEvent) -> None:
        # 门框期不会被直调（registry.run_outbound 先过审批门）
        print(f"[example_connector] 收到出站事件（模拟）: {event.event_type}")


def get_connector() -> Connector:
    return ExampleConnector()
