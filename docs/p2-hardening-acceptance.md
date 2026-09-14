# 加固轮 P2 验收表 — 通知多渠道出站（钉钉 webhook + 邮件 SMTP）

> 方案：《星枢-加固与通知多渠道-执行方案.md》P2（100 分钟预算）
> 目标：站内通知（DB+WS+Agent stderr）之外新增钉钉/SMTP 出站；渠道故障零阻塞主链路（D4）
> 凭据走 config.yaml（D5），不进 git 不进日志
> commit：`（待填）`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T2-1 钉钉出站 | 独立 HTTP 接收进程（:19125） | ✅ PASS | markdown 载荷含 title/body + 钉钉算法本地验签通过 |
| T2-2 邮件出站 | 独立 SMTP 接收进程（aiosmtpd :19126） | ✅ PASS | MIME 解析：Subject=title、正文含 body |
| T2-3 故障隔离 | 接收端关闭注入 | ✅ PASS | 渠道全挂创建 10 条：10/10 落库、耗时 0.2s 零阻塞 |
| T2-4 全关闭回归 | config 无 notify_channels | ✅ PASS | 通知创建 200 + 0.02s，与基线一致 |
| T2-5 真实路径 | dashboard 实操 | （E2E-2 覆盖） | Agent stderr 推送 + UI 角标已有上轮验证 |
| T2-6 真实联调（可选轨） | 真实凭据 | ⏸ 未提供凭据 | 登记台账「真实渠道联调」待办（不算失败，D3） |
| T2-7 回归 | pytest 双端 | ✅ Hub 166 / Agent 158 全绿 | 回归输出 |

## 实现要点

1. **config.yaml `notify_channels` 段**：dingtalk（webhook/secret/enabled）+ smtp（host/port/user/password/from/to/enabled）；缺省全关，凭据占位符（D5）
2. **`notify_channels.py`（新模块）**：
   - `_dingtalk_send`：markdown 消息 POST + HMAC-SHA256 加签（timestamp+secret → base64 → quote_plus），5s 超时
   - `_smtp_send`：MIME（Subject=title/From=星枢/To），465 SSL / 587 STARTTLS，5s 超时；**服务器不支持 AUTH 时降级直接发送**（实测抓出的真实 bug：aiosmtpd/无 AUTH 服务器 login 会崩）
   - `fan_out`：asyncio.gather 并行发所有 enabled 渠道，每渠道独立 try/except + to_thread，失败仅日志+状态
3. **create_notification 接入**：WS 推送后 `asyncio.create_task(_fanout_and_mark)` 异步 fan-out（D4：主链路零阻塞）；`_fanout_and_mark` 写 notifications.channel_status（JSON {dingtalk: ok|fail, smtp: ok|fail}）
4. **channel_status 字段**：db.py 增量迁移 `ALTER TABLE notifications ADD COLUMN channel_status TEXT DEFAULT ''`（同 source/artifact_path 迁移模式）

## 实测抓出的真实问题（均已修复）

1. **SMTP AUTH 崩溃**：有 user/password 配置但服务器不支持 AUTH（测试 SMTP/本地接收端）→ `server.login` 抛 SMTPNotSupportedError → 整个出站失败。修复：捕获后降级直接发送
2. **测试隔离陷阱**：测试 Hub 用独立 config（SYNC_HUB_CONFIG_DIR + database.path），但**脚本不 kill 子进程 → 旧 Hub 占端口 → 新 Hub 启动失败、health 打到旧实例**（测试读到旧 DB）。修复：测试末尾统一 kill + rmtree

## 台账登记

- CD-016 关闭（P0 12cafa1）：semantic_search 降级
- CD-017 关闭（P1 3ce5b25）：_record_trace 移出关键路径
- 新登记：真实渠道联调待办（T2-6，用户提供钉钉/SMTP 凭据后补做真实发送 1 次）

## 本阶段明确不做（已遵守）

- 不做双向交互（钉钉内审批按钮/回消息控制 Agent——出站 only）
- 不做渠道管理 UI、不做失败重发队列/持久化重试
- 不做微信/短信/通用 webhook 平台
