# 集成层适配器开发指南（§七 门框）

> 目标：**接入一个新外部系统 = 只写一个适配器文件，不动 Hub 核心任何一行代码。**
> 实证：`integrations/connectors/dummy2_connector.py` 就是为验证此点新增的第二个连接器。

## 1. 三步接入

1. 在 `integrations/connectors/` 下新建 `<你的系统>.py`
2. 实现 Connector 契约（`integrations/base.py`），暴露模块级 `get_connector()`
3. 重启 Hub —— 注册表目录扫描自动发现，⑩集成页立即可见

## 2. Connector 契约

```python
from integrations.base import (BaseConnector, CanonicalEntity, HubEvent,
                               RawRecord, TestResult)

class MyErpConnector(BaseConnector):     # BaseConnector 提供可选方法默认实现
    name = "my_erp"                      # 唯一标识（出现在 doc_id/审计/溯源）
    display_name = "我的 ERP"
    category = "erp"                     # erp / crm / finance / oa / other

    def configure(self, cfg: dict) -> None:
        self._cfg = cfg                  # 密钥字段已解密为明文，仅驻内存

    def test_connection(self) -> TestResult:
        ...                              # ⑩页"测试连接"按钮

    def pull(self, since):               # 增量拉取；since=None 表示全量
        yield RawRecord(source_id=..., entity_hint="customer",
                        payload={...}, updated_at=...)

    def map_entity(self, raw, mapping=None) -> CanonicalEntity:
        ...                              # mapping = server config 字段映射表（用户可视化编辑）
```

可选方法（`BaseConnector` 已有空实现）：`handle_webhook(payload)`、`push_outbound(event)`。

文件末尾：

```python
def get_connector():
    return MyErpConnector()
```

## 3. 关键正确性保证（不要绕过）

| 机制 | 说明 |
|---|---|
| **附录 E 管道** | 所有集成数据经 `hub.ingest_chunks` 汇入：切割 → PII 预扫 → 机密词封顶 → 幂等去重 → 三路分流。适配器**不要**直接写库 |
| **taint=external** | 入汇 `trust_level` 恒为 `external`，落 `document_chunks.trust_level` 列，全链可溯源 |
| **溯源** | `doc_id = intg:{source_system}:{entity_type}:{source_id}`；`source_agent_id = integration:{name}` |
| **凭证安全** | 配置中含 secret/token/password/api_key 等字段名的值 → AES-GCM 加密落库（主密钥 `integration_keys/master.key`，链外存储）；API 出参一律掩码 `***` |
| **出站审批门** | 写外部系统 = 高危操作。统一入口 `run_outbound` 一律拦截 + 记审计链，门框期不真正下发 |
| **webhook 鉴权** | `/api/v1/integrations/{name}/webhook` 走 scoped key 体系，用 `scope.endpoints` 前缀限定授权 |

## 4. 字段映射

字段映射表存 `integrations_state.field_mapping`（⑩页可视化编辑）。`map_entity` 收到
`mapping` 参数：`{规范字段: 外部字段名}`。不同客户同种 ERP 字段可能定制过——**不硬编码**。

## 5. 分级语义（适配器不需要也不能控制）

- 含 PII（手机/身份证/银行卡/邮箱/密钥串）→ 强制 NONE：只进审计，不进图谱/wiki
- 命中机密词库（客户名单/合同价/薪资等）→ SUMMARY 封顶
- worker 角色写入的业务内容 → SUMMARY 上限（r4 规则）

`CanonicalEntity.trust_level` 固定 `external`；`dept` 填数据域标签（如 `sales`/`finance`，附录 K）。

## 6. 验证

- 单测：`python -m pytest tests/test_integrations.py -q`
- 全链路 e2e（§7.6 出口标准）：起 Hub 后 `python e2e_integrations.py`
- 参考实现：`integrations/connectors/example_connector.py`（4 条模拟记录覆盖 SUMMARY 封顶 + PII NONE 两案例）
