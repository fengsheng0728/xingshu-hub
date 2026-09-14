# P3 Agent 私聊验收表(2026-08-03)

> 方案：《星枢-团队协作型个人工作台-执行方案.md》P3
> 目标：侧栏会话列表可与任意在线 Agent 私聊(Hub 路由中转), 比通知更像团队
> 测试：Hub `tests/test_dm.py`（6 用例）+ Agent `tests/e2e_dm.py`（真实双 agent spawn）
> commit：`（待填）`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T3-1 反向断言 | ("POST","/api/v1/messages/send") + ("GET","/api/v1/messages") | ✅ 已注册 | test_t3_1 |
| T3-2 在线直达 | B 连 WS → A send → B 的 WS 收到 direct_message + 入库 | ✅ | test_t3_2 |
| T3-3 防伪造 | body 伪造 from_agent_id 不生效, 身份以调用者为准 | ✅ | test_t3_3 |
| T3-4 离线落通知 | to 未连 WS → 消息入库 + 通知落库(上线可见, 不丢) | ✅ | test_t3_4 |
| T3-5 会话化列表 | 双向 3 条按时间升序, 双方视角一致 | ✅ | test_t3_5 |
| T3-6 鉴权 | 独立鉴权 Hub 无 token → 401 | ✅ | test_t3_6 |
| 真实双 agent E2E | A dm_send → B 回复 → Hub 双向入库 → A dm_list 分组 2 条 | ✅ | e2e_dm.py |
| 回归 | Hub pytest 全量 / Agent pytest 全量 | 207 / （待填） | 回归输出 |

## 实现要点

1. **messages 表**（db.py DDL + 生产库建表）：message_id/from/to/content/is_read/created_at
2. **POST /api/v1/messages/send**（挂 get_current_agent）：to 在线 → `active_ws[to].send_json({type:"direct_message",...})` 直达（delivered=ws）；离线/推送失败 → INSERT notifications 落通知（delivered=notification）。from 恒为调用者（body 伪造无效）
3. **GET /api/v1/messages?agent_id=X**：双向消息时间升序
4. **Agent 端**：`dm_send`/`dm_list` 命令 + WS `direct_message` 事件 → push 前端（toast + 刷新私聊列表 + 打开中会话实时显示）
5. **前端**：侧栏 `#dm-minilist` 私聊分区（与本地会话独立）——💬 条目（peer/条数/最后消息）→ 点开 openDm 对话视图 → 发送走 dm_send；doSend 按 dmPeer 分流

## 本阶段明确不做

- 不做已读回执/输入中状态；不做私聊消息删除/撤回；不做跨 Hub 私聊（联邦）；不做私聊里的富媒体（纯文本）
