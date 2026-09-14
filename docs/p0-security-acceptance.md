# 安全底线 P0 验收表 — REST token 鉴权

> 方案：《星枢-安全底线-执行方案.md》P0（85 分钟预算）
> 实际耗时：约 150 分钟（含 T0-2 实操抓出 bootstrap 漏洞 + 修复 + 视觉验证）
> commit：`32bbc7b`（P0 主体）+ `5794fad`（P0-fix bootstrap 漏洞）

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T0-1 401/200 矩阵 | pytest `test_auth_matrix.py`（独立进程真实 Hub 3062） | ✅ 5/5 | 无 token 100% 401；错 token 100% 401；正确 token 100% 非 401（含 register/bootstrap 引导端点） |
| T0-2 Agent 真实路径 | spawn agent_client 三态实测 | ✅ | 对 token：connect ok + 0 auth_failed；错 token：connect 失败 + stderr `auth_failed` 事件 + stdout 无污染；无 token：bootstrap 401 → auth_failed。Electron 视觉确认：无 token 连接失败回退「连接星枢」配置页（D6 不挂死） |
| T0-3 dashboard 真实路径 | Edge headless + HTTP | ✅ | 无 token 打开 `/` → DashAuth 全屏输入门（position:fixed + 输入框 + 保存按钮，headless DOM 实证）；wiki.html 自带 api-key 输入框提示；带 token `/wiki/inbox` 200 + 真实数据；6 页壳全部 200 |
| T0-4 泄露面 | git grep + 日志检查 | ✅ | 仓库无 hub_token 明文；config/config.yaml 已 gitignore；audit 日志无 token 字符串 |
| T0-5 回归 | pytest 双端 | ✅ | Hub 151（146 基线 + 5 矩阵）/ Agent 118 全绿 |

## 实现要点

1. **TokenAuthMiddleware**（纯 ASGI，routes.py）：统一门卫，除 allowlist 外全 HTTP 请求强制 Bearer 凭据；hub_token（hmac 常时比较）或 agents.api_key 任一有效
2. **allowlist 实测驱动**：`/health` + 6 页面壳 + `/docs`/`/static`/`/mcp` + proxy/disclose（函数内自认证 remote_api_key）
3. **引导端点特殊规则（T0-2 抓出的漏洞）**：hub_token 已配置时，register/bootstrap 必须带 hub_token——否则任何人可无 token bootstrap 注册新 agent 拿 key 绕过门卫。hub_token 空（旧模式）时维持无 key 死锁豁免
4. **get_current_agent 补 hub_token 分支**：hub_token 无身份语义（D1 不做 RBAC），返回请求声明的 agent_id；业务层身份校验不变
5. **dashboard**：`auth.js` 统一 fetch 封装（localStorage token + 401 弹输入门），5 个 API 页注入；showcase 纯静态
6. **Agent 端**：设置页/连接页「Hub 连接令牌」字段 → config.json hubToken → agent_client ToolContext 携带（api_key 优先，hub_token 兜底）→ 401 时 stderr 推送 `auth_failed`（D6 不静默）→ 前端 status-dot.err + 错误文案

## 配置方式

```yaml
# config/config.yaml（已 gitignore，不进仓库）
auth:
  enabled: true
  hub_token: "python -c \"import secrets; print(secrets.token_urlsafe(32))\" 生成的强随机值"
```

- 留空 = 退化为仅 api_key 认证（旧部署兼容）
- 轮换 = 改 token 重启 Hub
- 分发：Agent 设置页「Hub 连接令牌」；dashboard 首次打开输入框持久化 localStorage

## 台账登记

- CD-013：MCP `/mcp` 无认证（方案锚点表记「已有 Bearer」实测不存在）→ P0 放行，待做
- CD-014：`/ws/buffer`、`/ws/shared/{doc_id}`、`/ws/shared/watch/{doc_id}` 三通道无认证 → P1 首帧鉴权
- CD-015：test_l6_auth.py 为 pass 注释桩 → 被 test_auth_matrix.py 替代，保留观察

## 本阶段明确不做（已遵守）

- 不多 token/多用户/角色区分；不做 token 过期轮换；不改业务逻辑；不动披露引擎；不动 WS（P1）
