"""第二个 dummy 连接器 — §7.6 出口标准实证：新增连接器不改 Hub 核心任何一行代码。

模拟一个迷你财务系统，只提供 2 条凭证记录。仅实现契约最小集
（configure/test_connection/pull/map_entity），webhook/出站用默认空实现。
"""
from datetime import datetime
from typing import Any, Dict, Iterator, Optional

from integrations.base import (BaseConnector, CanonicalEntity, Connector,
                               RawRecord, TestResult)


class Dummy2Connector(BaseConnector):
    name = "dummy2_finance"
    display_name = "示例财务（模拟）"
    category = "finance"

    _MOCK_DATA = [
        {"id": "V-1001", "type": "voucher", "title": "8月办公用品报销",
         "amount": "1280.00", "applicant": "行政组",
         "updated_at": "2026-08-05T09:00:00+00:00"},
        {"id": "V-1002", "type": "voucher", "title": "Q3 服务器续费",
         "amount": "21600.00", "applicant": "运维组",
         "updated_at": "2026-08-06T11:00:00+00:00"},
    ]

    def __init__(self):
        self._cfg: Dict[str, Any] = {}

    def configure(self, cfg: Dict[str, Any]) -> None:
        self._cfg = cfg or {}

    def test_connection(self) -> TestResult:
        return TestResult(ok=bool(self._cfg.get("server_url")),
                          detail="模拟财务连接" if self._cfg.get("server_url")
                                 else "未配置 server_url", latency_ms=2.0)

    def pull(self, since: Optional[datetime]) -> Iterator[RawRecord]:
        for rec in self._MOCK_DATA:
            yield RawRecord(source_id=rec["id"], entity_hint=rec["type"],
                            payload=rec, updated_at=rec["updated_at"])

    def map_entity(self, raw: RawRecord,
                   mapping: Optional[Dict[str, str]] = None) -> CanonicalEntity:
        p = raw.payload
        content = (f"凭证 {raw.source_id}：{p.get('title', '')}。"
                   f"金额：{p.get('amount', '')} 元；申请人：{p.get('applicant', '')}。")
        return CanonicalEntity(
            entity_type="voucher", source_system=self.name,
            source_id=raw.source_id, title=str(p.get("title", raw.source_id)),
            content=content,
            fields={"amount": p.get("amount"), "applicant": p.get("applicant")},
            dept="finance", updated_at=raw.updated_at)


def get_connector() -> Connector:
    return Dummy2Connector()
