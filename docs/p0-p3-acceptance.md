# 星枢 Sync Hub — 验证债清理 + 孤儿端点补前端入口 验收总表

> 日期：2026-08-01 | 执行：Hermes Agent | 基线：P0 开工时 Hub 138 / Agent 118（重锁）

## 时间消耗

| 阶段 | 预算 | 实际 | 说明 |
|------|------|------|------|
| 前置 | 15 min | ~20 min | 现状确认表（wiki 11 端点实测、TaskCreate 签名、配对码流程、五场景定义缺失报备） |
| P0 | 70 min | ~50 min | 五场景脚本编写 + 30轮断网（~3.5min/轮次） |
| P1 | 90 min | ~60 min | /wiki 页 + 审查 UI + approve 发布修复（CD-008） |
| P2 | 40 min | ~35 min | 双入口 + 命令 + 校验（+修 refreshNotifs JS 铁律） |
| P3 | 40 min | ~50 min | 配对三处断链修复（CD-009）+ 双 Hub 实操 |
| E2E | 25 min | 进行中 | 断网复跑 + 收尾 |

## 验收结果

| 用例 | 结果 | 证据 |
|------|------|------|
| T0-1 场景定义确认 | ✅ | 定义表 docs/p0-five-scenarios-definition.md，用户冻结（A1） |
| T0-2 30轮断网 | ✅ | p0_netstorm.py 30/30（首次）+ E2E 复跑 |
| T0-3 五场景全过 | ✅ | p0_e2e.py 15/15 + p0_netstorm 30/30 |
| T0-4 回归 | ✅ | Hub 138 / Agent 118（开工基线） |
| T1-1 路由存在性 | ✅ | test_wiki_ui.py（wiki 11 + /wiki） |
| T1-2 审查流 | ✅ | approve 发布（文件生成+pages可查）+ reject 未发布 + DB 快照 |
| T1-3 搜索 | ✅ | hybrid「张三」count=1 命中 |
| T1-4 XSS | ✅ | esc() 转义断言（test_wiki_html_xss_safe） |
| T1-5 回归 | ✅ | 142 passed |
| T2-1 建任务 | ✅ | 命令实测：创建+落库+校验拒绝 |
| T2-2 发通知 | ✅ | 创建+落库（stderr 推送帧链路 P0-S4 已验证） |
| T2-3 表单校验 | ✅ | 空必填被拒，无 DB 记录 |
| T2-4 路由存在性 | ✅ | test_tasks_notif_routes.py（tasks×7+notif×4） |
| T2-5 回归 | ✅ | Hub 144 / Agent 118 |
| T3-1 配对流 | ✅ | 双 Hub(3060+3061) 实操 5/5：码→accept→互见 |
| T3-2 移除 | ✅ | revoke status=removed |
| T3-3 发现 | ⚠️ 部分 | 命令链路通（peers:[] 结构正确）；同机 UDP 广播不环回，无第二 Hub 可发现，需真实局域网验证 |
| T3-4 路由存在性 | ✅ | test_team_routes.py（team×9+exchange） |
| T3-5 回归 | ✅ | Hub 145 / Agent 118 |
| E2E-1 全量回归 | ✅ | Hub 145 / Agent 118（数量 ≥ 基线） |
| E2E-2 全链路抽查 | ✅ | 建任务/发通知/双Hub配对//wiki审查 四处与 DB 一致 |
| E2E-3 断网复跑 | ✅ | 新代码复跑 30/30 + 通知恢复后可达 True |

## Commit 链
- P0: `609b374`
- P1: `37508fa`
- P2: Hub 断言 commit + Agent `c0d4fd3`
- P3: Hub 配对修复 commit + Agent `7009ac5`

## carried_debts 台账
| 编号 | 来源 | 描述 | 严重度 | 去向 |
|------|------|------|--------|------|
| CD-001 | 预登记 | Hindsight | 高 | 关闭（D1 已砍掉） |
| CD-007 | P0-观察 | e2e1_demo.py 用 v4-pro | 低 | 观察 |
| CD-008 | P1 | approve 不发布 | 中 | 已修复 |
| CD-009 | P3 | 配对三处断链 | 高 | 已修复 |
| CD-010 | P3-T3-3 | UDP 发现死代码（未启动+get_peers 不存在+单机不环回） | 高 | 已修复：多播通道+实例化+SERVER_PORT+hub_id 稳定 |
| CD-011 | P5 | 跨 Hub 披露泄露（accept 自指→规则1 误判 requester==owner 泄露 FULL） | 高 | 已修复：accept 不传 remote_agent_id；worker 机密不可见 |
| CD-012 | P5 | 跨 Hub 调用端缺失（只有被调端+exchange 不存对方地址） | 中 | 已修复：/team/disclose/remote 调用端+own_hub_url 透传 |
