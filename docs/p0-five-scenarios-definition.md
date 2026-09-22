# 五场景定义表（P0 交付物，方案增补 A1 冻结）

> 冻结日期：2026-08-01 | 状态：已确认 | 脚本映射见各场景
> 执行规则：① S1/S5 优先复用 e2e1_demo.py / e2e_file_tools.py 模式 ② 命中 known_limitations.py 中 status=accepted 的项引用编号标注，不判失败

| # | 场景 | 真实路径 | 通过条件（冻结） | 脚本映射 |
|---|------|---------|----------------|---------|
| S1 | 基础对话+工具 | Agent stdin→chat→LLM→工具→Hub→回显 | 真实 LLM 会话中 ≥1 次真实工具调用（audit jsonl 有记录），回显含该工具返回的真实数据；全程无降级/mock | `p0_e2e.py`（扩展 e2e_file_tools.py 的 stdin/stdout 模式） |
| S2 | 记忆写入/检索 | 对话写记忆→Hub memory_pool 落库→语义检索命中 | DB 快照出现该条；语义检索（/api/v1/memory/search）返回命中 | `p0_e2e.py` |
| S3 | 任务全生命周期 | Hub 建任务→派发→Agent 收→start→complete | tasks 表状态 assigned→in_progress→completed 依次真实发生，每次迁移有事件日志 | `p0_e2e.py` |
| S4 | 通知推送 | create→Hub WS 推送→Agent stderr 帧 | Agent stderr 出现推送帧且 stdout 无推送帧（双流铁律） | `p0_e2e.py` |
| S5 | 断网重连 30 轮 | Agent WS→测试Hub(3061) 断开/重连×30 | 30/30 重连成功；每轮恢复 online；重连后首条 chat 有响应；断连期通知恢复后可达；无未捕获异常 | `p0_netstorm.py`（独立测试 Hub 3061） |

## accepted 限制索引（命中即引用，不判失败）
- ~~L1-EVICT-001~~ / ~~L1-EVICT-002~~：**已退役**（2026-09-21，CD-075）—— 属旧 Agent 端执行层实现，随「自研 Agent 端砍掉、只放通用 API」一并退出；
  协议层口径改为「Hub 在 hello 时按 checkpoint 重放未确认派单，**幂等由接入方自行保证**」
- ~~L0-COMPAT-001~~：**已退役并下线**（2026-09-21，CD-032）—— 兼容分支已从 `envelope.py` / `routes_ws.py` 删除，平铺旧格式一律不接受
- L5-TIMEOUT-001：真断开后最长 90s 触发重连（3 心跳周期判半开）

> 补注（2026-09-21）：除上面这条索引外，本文档其余内容保持 2026-08-01 冻结原文（历史口径，不追改）。

## 30 轮断网设计（S5）
- 独立测试 Hub 实例：`SYNC_HUB_CONFIG_DIR=<临时目录>` + config port=3061 + 独立 sync_hub.db
- Agent 连 3061 → 每轮 kill 测试 Hub → 等待 Agent 重连（backoff 1s 起）→ 重启 Hub → 验证 agent online + 首条 chat 响应
- 30 轮循环，逐轮记录结果表
