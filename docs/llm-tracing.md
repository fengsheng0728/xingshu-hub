# LLM 调用级 trace（CD-085(c)）

标准 OTLP 导出，默认关闭，不引第三方 SaaS、不写死后端。

## ① 开关与两档 env

两个开关任一满足即开，且仍要求装了 OTel（`pip install -r requirements-otel.txt`）：

| 变量 | 作用 |
|------|------|
| `SYNC_HUB_LLM_TRACE=1` | 显式打开 trace |
| `OTEL_EXPORTER_OTLP_ENDPOINT` 非空 | OTel 标准变量，存在即视为「要导出」，同时作为 OTLP HTTP endpoint |

辅助变量：

| 变量 | 默认 | 作用 |
|------|------|------|
| `SYNC_HUB_OTEL_SERVICE_NAME` | `xingshu-sync-hub` | OTLP resource `service.name` |

真值表（`tracing.llm_trace_enabled()`）：

| OTel 已装 | `SYNC_HUB_LLM_TRACE=1` | `OTEL_EXPORTER_OTLP_ENDPOINT` | 结果 |
|-----------|------------------------|-------------------------------|------|
| 否 | 任意 | 任意 | `False`（降级 no-op） |
| 是 | 否 | 无 | `False`（默认关闭） |
| 是 | 是 | 无 | `True` |
| 是 | 否 | 有 | `True` |
| 是 | 是 | 有 | `True` |

## ② 支持的后端

只走标准 OTLP（`opentelemetry-exporter-otlp-proto-http`），任何 OTLP 接收端均可：

- 本地 OpenTelemetry Collector
- Jaeger（原生 OTLP 收取）
- 自托管 Langfuse（其 OTLP 端点）

## ③ 为什么不接 Langfuse 云

本项目**不接 Langfuse 云**：接云意味着客户数据外发，与「可审计」产品叙事直接冲突。
自托管 Langfuse 又要多维护一个服务。OTLP 是标准出口，本地起 collector/Jaeger 即可演示，
将来还能随时换后端 —— 三条里只有它能现在做、将来还能换。

## ④ 数据边界（硬性）

span **只记**：

- 请求/响应**长度**（`llm.request_chars` / `llm.response_chars`）
- **token 用量**（`llm.prompt_tokens` / `llm.completion_tokens`，响应 `usage` 取不到则为空）
- **延迟**（`llm.latency_ms`）
- **状态**（`llm.status`：`ok` / `error`）与**错误类型**（`llm.error`）
- **模型名**（`llm.model`）与 **provider**（`llm.provider`）

span **不记** prompt / response 全文，不记 messages / content / 任意请求体、响应体文本。
「可审计但不外泄」：可对账、可回放调用元数据，不搬运客户原文。

埋点位置：

| span 名 | 调用点 |
|---------|--------|
| `llm.chat` | `hub_agent.py` 3 处 `client.post(.../chat/completions)`（test_connection / audit_disclosure / auto_complete_knowledge） |
| `llm.extract` | `entity_extraction.py` `_llm_extract` |

## ⑤ 本地演示（参考命令，非本项目维护）

起 Jaeger all-in-one（原生 OTLP 4318）：

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:1.57
```

或起 OpenTelemetry Collector（OTLP HTTP 接收）后自行配置 exporter。

演示：

```bash
export SYNC_HUB_LLM_TRACE=1
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
# 启动 Hub 后触发任意 LLM 调用，Jaeger UI: http://127.0.0.1:16686
```
