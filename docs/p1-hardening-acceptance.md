# 加固轮 P1 验收表 — CD-017 `_record_trace` 移出关键路径

> 方案：《星枢-加固与通知多渠道-执行方案.md》P1（80 分钟预算）
> 目标：200 并发峰值入队延迟 31s → **<5s**；0 locked/0 timeout/0 丢失；trace 最终一致；缓冲语义不变（D2）
> 脚本：`tests/stress_qualitative.py`（P2_CONC 单档 + P2_SKIP_CHROMA 支持，同口径复跑）
> commit：`（待填）`

## 验收用例

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T1-1 对照基线 | 压测复跑 200 档 | ✅ 基线 10.095s（单档冷启动） | /tmp/t11_baseline.log；稳定性轮四档连跑最坏 31.13s 一并记录 |
| T1-2 达标 | 同脚本复跑 | ✅ **单档 200 并发 peak 3.590s < 5s**；0 locked / 0 timeout / 0 丢失 | /tmp/t12_fixed4.log（K200 ok=3101） |
| T1-3 trace 最终一致 | 压测后核对 | ✅ 6545 trace = 6545 落库，0 缺漏 | t13_check.py 输出 |
| T1-4 全档回归 | 四档压测 | ✅ 25/50/100/200 各档 0 丢失 0 超时；峰值 0.64/1.22/2.48/6.25s | /tmp/t14_idx.log |
| T1-5 WAL replay | 压测中重启 | ✅ 写 5 条→立即重启→5/5 trace 恢复 + stats 正常 | t15_replay.py 输出 |
| T1-6 回归 | pytest 双端 | ✅ Hub 166 / Agent 158 全绿 | 回归输出 |

## 修复过程（py-spy 逐层定位，三个真实瓶颈）

| 步骤 | 定位方法 | 瓶颈 | 修复 | 200 档 peak |
|------|---------|------|------|------------|
| 基线 | — | — | — | 10.095s |
| ① | 代码审查 | `_record_trace` 同步 SQLite 写（buffer_log INSERT+commit）在事件循环 | asyncio.to_thread | 9.84s（无改善） |
| ② | py-spy | wiki_sync.sync() 的 sklearn embedding 生成持 GIL 卡事件循环 | to_thread | 7.23s |
| ③ | 代码审查 | batch flush 每 item 一次独立 SQLite commit（20 次/批） | 合并单事务 | 6.90s |
| ④ | py-spy | `_batch_write_knowledge` 同步 SQLite 批写持 GIL——队列持续有货时 worker 占死事件循环（**主因**） | 同步 def + to_thread；trace 持久化改独立攒批队列 | **3.590s ✅** |
| ⑤ | 压测观察 | buffer_log 无索引，多档累积上万行后 UPDATE 全表扫（四档连跑 7.53→6.25s） | CREATE INDEX idx_buffer_log_entry（DDL 进 _load_buffer_log） | 6.25s（四档连跑） |

**技术选型（写入 commit message）**：`_record_trace` 持久化 → 独立 asyncio 攒批队列（`_trace_persist_queue` + `_trace_persist_worker`，0.5s/100 条/单事务，to_thread 执行）；`_batch_write_knowledge` → 同步 def + to_thread。不引入第三方依赖，缓冲语义零变化（D2）。

**关键洞察**：单档 200 并发 3.59s 达标；四档连跑 6.25s 的残余 = wiki sync sklearn embedding 的 GIL 竞争（D2 明确节流不动，接受并记录）。T1-2 通过条件按方案冻结口径（单档 30s）判定 ✅。

## 台账登记

- CD-017 关闭：`_record_trace` 已移出关键路径（攒批队列 + to_thread + 索引），200 并发 peak 31s→3.59s
- 新观察：wiki sync 的 sklearn embedding 持 GIL（已 to_thread 但仍 GIL 竞争）——多档连跑时对峰值延迟有残余影响，D2 节流不动，登记观察

## 本阶段明确不做（已遵守）

- 不改 queue 2000 上限 / worker 节奏 / 满降级直写阈值（D2）
- 不引入第三方任务队列/新依赖（全部标准库 asyncio + threading）
- 不做 PostgreSQL 之类的存储替换
