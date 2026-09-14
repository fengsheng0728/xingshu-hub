# 稳定性轮 E2E 总验收表

> 方案：《星枢-稳定性轮-执行方案.md》（300 分钟预算）
> 顺序：P0 → P1 → P2 → E2E，每阶段独立 commit，回归门槛 Hub ≥163 / Agent ≥118

## 验收总表

| 阶段 | 用例 | 方法 | 结果 | commit | 证据 |
|------|------|------|------|--------|------|
| P0 | T0-1 攻击面六连 | pytest（backend/tests/test_approval_gate_attack.py） | ✅ 40/40 | Agent 80fbc68 | A1-A6 全 fail-closed；A5 抓出 `$(...)` 子命令逃逸漏洞已修 |
| P0 | T0-2 分类边界 | pytest 参数化 30 条 | ✅ 0 误放行 | Agent 80fbc68 | 写/禁命令无一条被 READONLY 放行 |
| P0 | T0-3 真实路径 | Agent UI 实操 | ✅ | Agent 80fbc68 | WRITE 命令→approval_request→拒绝无副作用；批准→执行 |
| P0 | T0-4 回归 | pytest 双端 | ✅ Hub 163 / Agent 158 | — | — |
| P1 | T1-1 依赖真值 | pip freeze + git grep | ✅ | Hub 7bf6e28 | pydantic 2.12.5，无 v1 依赖、`git grep pydantic.v1` 零命中 |
| P1 | T1-2 测试快照比对 | pytest collect-only + 全量 | ✅ 163/158 | Hub 7bf6e28 | 无测试消失（Agent +40 攻击面） |
| P1 | T1-3 启动 warning | 启动日志 | ✅ | Hub 7bf6e28 | Hub 启动 pydantic warning=0（langchain 上游 1 条登记） |
| P1 | T1-4 真实链路 | 双端实操 | ✅ | Hub 7bf6e28 | connect/chat/任务/wiki/加密披露 全通 |
| P1 | T1-5 断网复跑 | S5 30 轮 | ✅ 30/30 | — | p0_netstorm.py |
| P2 | T2-1 SQLite 压测 | 阶梯并发 25/50/100/200 | ✅ | Hub 6d4cde6 | 0 locked/0 timeout/0 丢失；peak 延迟 2.95→31.13s（CD-017） |
| P2 | T2-2 ChromaDB 多进程 | 双进程 5 轮 | ✅ 10/10 | Hub 6d4cde6 | 无 filelock 冲突（假想风险排除） |
| P2 | T2-3 降级路径 | 故障注入 | ✅ | Hub 6d4cde6 | memory/search 降级生效 emb_unavail=True；semantic_search 无降级（CD-016） |
| P2 | T2-4 结论表 | 文档 | ✅ | Hub 6d4cde6 | docs/p2-stability-acceptance.md |
| P2 | T2-5 回归 | pytest 双端 | ✅ Hub 163 / Agent 158 | — | 压测未压坏环境 |
| E2E | E2E-1 全量回归 | 双端 pytest | ✅ Hub 163 / Agent 158 | — | 与基线清单比对无消失 |
| E2E | E2E-2 冷启动全链路 | spawn agent_client 真实协议 | ✅ 8/8 | — | ready/llm_cfg/connect/WS/chat/任务/通知推送/wiki inbox |
| E2E | E2E-3 审批门复验 | 攻击面抽测 | ✅ 40/40 | Agent 80fbc68 | A1/A2/A4 真实路径 fail-closed |
| E2E | E2E-4 断网复跑 | S5 30 轮 | ✅ 30/30 | — | p0_netstorm.py 输出 |

## 收尾交付物

1. **验收总表**：本文件（17/17 用例 ✅）
2. **carried_debts 台账滚动**：
   - 旧债标去向：CD-008/009/010/011/012/014 已修复；CD-013 MCP 待做；CD-007 观察
   - 新债登记：CD-016（semantic_search 无降级）、CD-017（_record_trace 同步 SQLite 写阻塞）
3. **进度文档更新**（桌面《星枢模块进度.md》）：
   - 6.1 Shell 审批绕过 [ ]→[x]（P0 80fbc68）
   - 6.2 SQLite 并发 [ ]→[x]（P2 实测 6d4cde6）、pydantic [ ]→[x]（P1 7bf6e28）、ChromaDB [ ]→[x]（假想风险）
   - 6.3 记忆可视化 [ ]→[x]（49e4aff）
4. **时间消耗表**：见下

## 时间消耗表（实际 vs 预算）

| 阶段 | 预算 | 实际 | 说明 |
|------|------|------|------|
| 前置 | 15 min | ~20 min | 现状确认 + 脚本环境 |
| P0 | 60 min | ~90 min | 含 `$(...)` 逃逸漏洞修复 |
| P1 | 120 min | ~180 min | 依赖探测 + 快照比对 |
| P2 | 55 min | ~150 min | 多轮脚本修正（token 401/buffer drain/角色）+ 完整压测 |
| E2E | 25 min | ~60 min | 冷启动链路 8/8 + 断网 30/30 |
| 机动 | 25 min | — | 超预算部分由 P0 漏洞修复 + P2 脚本迭代消耗 |

**总评：** 方案通过条件全部达成，无砍尾。超预算根因：P0 抓出真实逃逸漏洞（非预期工作量）、P2 压测脚本需适配真实环境（auth/角色/缓冲 drain），均为有效投入。
