# 对外接入（放出 API）操作说明

> CD-069（2026-09-20）。适用场景：把 Hub 的 REST/WS 能力交付给**本企业以外**的调用方
> （客户系统、集成方、临时协作者），希望「给对方一把只能读到指定范围、随时可吊销的钥匙」。

本文只讲**怎么发钥匙 + 这把钥匙的边界**。Hub 侧已知限制（明文传输、限流进程内计数、
本地审计不构成防篡改证据）见 `known_limitations.py`，不要对着客户承诺超出这些的能力。

## 0. 前提：先解决传输层

没有 TLS 之前不要对外放 key——Bearer 是明文过网的（唯一加密的是 Hub↔Hub 联邦信道）。
TLS 打开方式见 §3 与 `config.example.yaml` 的 `server.tls` 段。

## 1. 三步发放

```bash
# ① 预建身份（guarded 注册模式下必须先预建；它只是身份壳，不交付其 api_key）
python hub_cli.py agent create --id ext-partner-a --name "外部集成方A" \
    --role worker --department proj-ext --db ./sync_hub.db

# ② 签发受限 key：endpoints 精确列读端点 + methods 只给读方法（明文仅此一次可见）
python hub_cli.py key create --agent ext-partner-a \
    --endpoints "/memory/disclose,/memory/search,/tasks" \
    --methods "GET,HEAD" \
    --data-domain proj-ext --level-cap summary \
    --expires 2026-12-31T00:00:00 --db ./sync_hub.db

# ③ 把 key 明文交给对方（安全渠道），自己记下 key_id；随时可吊销：
python hub_cli.py key revoke --key-id key-xxxxxxxxxxxx --db ./sync_hub.db
```

- `--endpoints`：路径白名单，逗号分隔；**空 = 全部端点**（对外交付不要留空）。
- `--methods`：HTTP 方法白名单（`GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS`）；空/不写 = **不限方法**（既有 key 行为不变）。对外只读务必写 `GET,HEAD`。
- `--data-domain`：数据域（部门/项目标签），叠加在披露判定上。
- `--level-cap`：最高披露级别（`full|summary|metadata|none`）。
- `--expires`：过期时间（ISO）。长期对外交付建议配上，避免永久钥匙。
- 非法 `--methods` / `--level-cap` 一律 `400` 退出——**不会静默放宽**。

REST 等价入口（需 manager/orchestrator 的全权 api_key 或 hub_token，供控制台使用）：

```
POST /api/v1/keys
{"agent_id": "ext-partner-a",
 "scope": {"endpoints": ["/memory/disclose"], "methods": ["GET", "HEAD"], "level_cap": "summary"},
 "expires_at": "2026-12-31T00:00:00"}
```

## 2. 这把钥匙拿不到什么（fail-closed 硬边界）

| 边界 | 语义 | 落在哪 |
| --- | --- | --- |
| 凭据/配置类端点**一律拒绝** | 无论 endpoints 怎么写、无论绑定的身份是不是 manager | `routes.py` `SCOPED_PRINCIPAL_DENY_PREFIXES`：`/api/v1/keys`、`/api/v1/access`、`/api/v1/server`、`/api/v1/agents/quota`、`/api/v1/agents/full-access`、`/api/v1/agents/register`、`/api/v1/agents/bootstrap` |
| 方法白名单 | 声明后按大写精确匹配；不在名单 → `403`（含 `method` 字样） | `routes.py` `_scope_method_allowed` |
| 路径白名单 | 精确 + 边界匹配（`/mem` **不会**放行 `/memory/*`） | `routes.py` `_endpoint_allowed` |
| **WS 通道默认拒绝** | WS 是全双工，绕过上面两层白名单；需 `scope.ws = true` 才放行 | `routes_ws.py` `_ws_auth_accept` |
| 披露级别上限 | `level_cap` 与规则链结果取 `min`（叠加，不是旁路） | `disclosure.py` `disclose_for_principal` |

> 为什么凭据端点要「不看白名单直接拒」：受限 key 若绑定的是 manager 身份，
> 它能自己签发新 key（`POST /api/v1/keys`）、改配额、或把 `lan_enabled` 打开把 Hub
> 暴露到 0.0.0.0——**对外交付的「只读」钥匙可以自我提权成全权**。这类端点只能由
> 全权 api_key / hub_token 调用。

## 3. 对外开启 TLS

```yaml
server:
  host: 0.0.0.0
  port: 3060
  tls:
    enabled: true
    certfile: /path/to/server.crt   # PEM
    keyfile: /path/to/server.key    # PEM（未加密私钥）
```

- 打开后 REST 走 `https://`、WS 走 `wss://`（客户端按同一 scheme 推导，见 §4）。
- 证书用企业 CA / 公网证书时，系统信任链即可，客户端零配置；自签证书仅建议联调，
  外部方需显式信任该证书。
- 未启用 TLS（默认）时行为与旧版完全一致。

## 4. 调用方示例

```bash
curl -s -H "Authorization: Bearer sk-..." https://hub.example.com/api/v1/tasks
```

- Agent 端 / 控制台：`hub_url` 写 `https://...` 即可，WS 自动用 `wss://`（同一 hub_url 推导）。
- 联邦（Hub↔Hub）：`remote_hub_url` 写 `https://...`，链路同样适用 TLS。

## 5. 建议的再收紧（按需）

- **配额**：`agents/quota` 里给对外身份设 `mode=reject` + 每窗口上限（默认 `alert_only` 只告警不拦）。
- **审计**：`GET /api/v1/audit/*` 查 `key_created` / `key_revoked` / `read_deny` 判断谁在用这把钥匙。
- **轮换**：`--expires` 到期即失效；提前换发 = 先签新 key 再 `revoke` 旧的。
