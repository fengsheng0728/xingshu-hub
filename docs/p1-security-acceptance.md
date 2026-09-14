# 安全底线 P1 验收表 — WS 五通道首帧鉴权

> 方案：《星枢-安全底线-执行方案.md》P1（55 分钟预算）
> 实际耗时：约 130 分钟（含 websocket-client 1.9 API 适配 + CRLF 修复 + L6 回归重写）
> commit：Hub `7da5015` + Agent `c7d0ddb`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T1-1 五通道矩阵 | pytest `test_ws_auth_matrix.py`（独立进程真实 Hub 3063） | ✅ 4/4 | 5 通道逐一：无 auth 帧发业务帧被拒；错 token → close 4401；正确 auth 首帧正常；3s 超时 → 4401 |
| T1-2 认证前零注册 | 代码断言 + test_l6_ws_auth_regression（mock 实测） | ✅ | `_ws_auth_accept` 失败直接 return，`hub.active_ws`/notifications 在鉴权通过后才写入；6/6 用例含「拒绝路径不注册 + accept 路径注册后断连注销」 |
| T1-3 Agent 真实路径 | spawn agent_client 双态实测 | ✅ | 对 token：connect ok + WS connected + 0 auth_failed；错 token：bootstrap 401 → stderr `auth_failed` 1 次（10s 内 ≤2）；stdout 无污染 |
| T1-4 断网复跑 | 复跑 P0 S5 `docs/p0_netstorm.py` | ✅ 30/30 | 30 轮 kill/重启 Hub：重连成功 + online + chat 响应全 True，通知恢复后可达 |
| T1-5 回归 | pytest 双端 | ✅ | Hub 158（151+7）/ Agent 118 全绿 |

## 实现要点

1. **`_ws_auth_accept(websocket, agent_id_hint, strict_agent)`**（routes.py）：accept → 3s 内收首帧 `{"type":"auth","token"}` → hub_token（hmac 常时比较，D1 无身份语义信任路径声明）或 agents.api_key（strict_agent 时校验归属防冒充）→ 失败 close **4401**
2. **5 通道统一接入**：/ws/dashboard、/ws/buffer、/ws/{agent_id}(strict)、/ws/shared/watch/{doc_id}、/ws/shared/{doc_id}；认证成功前连接不入 NotificationManager/active_ws/YRoom
3. **Agent 适配**：`_start_ws`/`_start_shared_watch` 去掉 query param api_key（D3：会进日志），首帧 auth；4401 → stderr `auth_failed` + 指数退避重连（1→2→4→…→30s 封顶）
4. **dashboard**：connectWS 首帧 auth（DashAuth.getToken）+ onclose 4401 → 弹鉴权输入门
5. **D3 落实**：不用 query param（进日志）、不用 header（浏览器 WS 不支持）

## 工程债修复

- **CRLF 双重化**（P0 遗留）：dashboard 三文件 `\r\r\n` → `\r\n`，git 中坏版本一并修复，内容零变化
- **websocket-client 1.9 API**：无 `close_code` 属性，close 码走 `recv_data_frame()`（opcode 8 + struct 解包）；矩阵 helper 据此适配
- **test_l6_ws_auth_regression 重写**：旧 query-param 模型（4001 before accept）→ P1 首帧模型（accept 后 4401）；保留防认证绕过意图并新增「断连注销」断言

## 台账更新

- CD-014（三 WS 裸通道）→ 已修复（7da5015）

## 本阶段明确不做（已遵守）

- 不做 WS 消息级加密（P2 联邦加密，本机 WS 不加密）
- 不做 per-connection 权限分级；不改 pycrdt YRoom 同步协议
