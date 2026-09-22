# 星枢数据库裂缝 · 核证判定与修复计划（2026-09-17）

> 输入清单：`星枢数据库裂缝全集.md`（外部审查稿，自述基于 **2026-09-14 仓库快照** /mnt/agents/xingshu-hub-v2）。
> 核证基准：本仓 HEAD `a4911f9`（2026-09-16，含 K-1 知识库检索方案 B）。
> 方法：逐条 grep/读码，判定 = 属实 / 修正 / 不实；每行附可复核的 file:line 证据。
> 归档：原清单副本 `E:\星枢-待办\星枢数据库裂缝全集-原清单.md`。

## 一、总判定

| 编号 | 清单主张 | 判定 | 证据（HEAD 实测） |
|---|---|---|---|
| 1A | Chroma 先写、SQLite 后 commit | **属实** | `hub_mixins/memory.py:331`（`collection.add` 在 `_txn` 内）vs `:240`（`conn.commit()`）；成功 `:329-346` |
| 1A | 后果 = 索引里有指向不存在数据的向量 | **修正（影响被高估）** | 检索端回查丢弃：`disclosure.py:489-492` `row = query_one(...); if not row: continue` → 不是错答，是脏向量残留 |
| 1B | Chroma 写入失败只记日志、无重试无 pending | **属实** | `hub_mixins/memory.py:343-344` 仅 `logger.warning`；全仓无补偿队列 |
| 1B | 影响面 | **修正（升）** | Chroma **是活检索路径**（见 2 条判定），漏建 = 该条永久少召回，非"等全量重建才发现" |
| 1C | 覆盖更新（conflict_overwrite）全程不碰 Chroma | **属实** | 两个分支 `memory.py:121-149`（同 key）与 `:197-219`（相似度 0.75~0.90）都只 `UPDATE memory_pool` |
| 2 | Chroma metadatas 无密级字段、存 `content[:500]` 明文 | **属实** | 灌库 SQL `ingest.py:341` 无 `disclosure_level`；metadata 组装 `:347-358`（有 owner 无密级）；知识侧 `knowledge.py:34-42` 同病 |
| 2 | 「语义检索根本没走 Chroma / Chroma 是只写不读的摆设」 | **不实** | 清单看的是 `memory.py:447-484`（Agent 查自己，走 SQLite blob）；跨 Agent 检索走 `routes_memory.py:136` → `hub.semantic_search` → `disclosure.py:469 hub._chroma_collection.query()`。本仓台账 CD-044（2026-09-14）已明写「轨A = memory_pool → chroma 经 semantic_search 暴露」 |
| 2 | 「启用即泄露」的引信 | **不实** | 引信早已点燃（该路径在线）；且披露过滤在**回查环节**完成：`disclosure.py:494-502` `disclose_for_principal` + 8 规则链，NONE 即 `continue`；知识命中 `:546-551` fail-closed。真实暴露面 = 索引内的明文片段**静态存在**（离线拿到 chroma_db 即得 500 字片段 + 全库向量），不是 API 泄露 |
| 3 洞1 | commit 与 submit 之间的崩溃窗口 | **属实** | `hub_mixins/shadow/lifecycle.py:88` pending 行在 submit 时才 INSERT → 未入队即崩溃者 pending 表也救不了；`memory.py:240` commit 与 `:248` submit 之间无保护 |
| 3 洞2 | 影子存提交时刻快照，级别变更不重镜像 | **属实** | `memory.py:254` `"level": ... if not _locked else "none"` 写死快照；全仓无 Chroma/影子的 update 重镜像路径 |
| 3 洞3 | 锚点登记弱保证 | **属实** | `hub_mixins/shadow/write.py:109-115` docstring 自认「失败静默降级（D4）」；顺带：此处失败同时丢 `audit/chain-head.jsonl` 链头互证 |
| 4 | `audit_memory` 在事务内、commit 之前 | **属实，本轮最高** | 事务内 6 处：`memory.py:146/189/215/226/235`（写/合并/覆盖）+ `:371`（删除）；commit 在 `:240`/delete 分支末尾。链侧独立：`audit/memory_audit.py:58-66` 写 jsonl + 每 1000 行锚入 `audit_log`（`audit_chain.py:432-463`），不参与业务事务 → commit 失败留下删不掉的假记录 |
| 附 | 「重新评估 ChromaDB 去留，建议删掉」 | **不实（与现状冲突）** | K-1（`934821c`，2026-09-16）刚落地「统一 collection + layer=memory\|knowledge 分流回查」，ADR `docs/architecture-decision-kb-retrieval.md`；`/api/v1/stats` 已暴露 chroma 向量数。现在删 = 回滚两天前的定档；清单的立论前提（纯包袱）本身不成立 |

## 二、清单漏项（核证中发现，同一片区域）

- **L1 审计链的反向洞**：`audit/memory_audit.py:67-68` `except Exception: pass` —— 审计写失败完全静默。真写入而漏审计，比假审计更难发现；清单只考了单向。
- **L2 索引明文已被消费**：`disclosure.py:534-544` 知识命中直接拿 chroma metadata 的 `content` 当返回正文（`chunk_text = metadata.get("content")`）。"明文躺在索引里"不是未来时。
- **L3 影子开关静默 no-op**：`lifecycle.py:85-86` kind 开关关闭时既不镜像也不入 pending —— "影子双写已开启"与"该 kind 真在镜像"是两件事，无观测面区分。

## 三、行号漂移说明（为什么清单是过期快照）

清单引 `ingest.py:333`（灌库 SQL）与 `:353`（重建失败注释），HEAD 实际为 `:340-341` 与 `:382-384`，差约 40 行 —— 正是 K-1 改过该文件的位置。`memory.py` 的引用则逐行吻合（331/448-456/225-234），说明快照取自 K-1 之前的提交。

## 四、修复顺序与台账

顺序 = 清单建议 ∩ 当前架构可行性（用户 2026-09-17 拍板）：

| 序 | 台账 | 项 | 修法 | 单项验收口径 |
|---|---|---|---|---|
| 1 | CD-045 | 裂缝4 审计原子性 + L1 | **outbox 同事务**：新增 `event_outbox` 表，事务内只写事件行（随业务数据原子提交）；后台消费者顺序消费 → 审计 jsonl / 哈希链；失败进 pending + attempts + 告警，重启 replay | 原子性反例（commit 失败 → 业务行/事件行/jsonl **三者皆无**）+ 补偿（审计写失败 → 业务已提交、事件留 pending、恢复后补记）+ 顺序保持 + 重启 replay |
| 2 | CD-046 | 裂缝1B/1C + 1A | 向量写入失败/覆盖时记 outbox 事件，消费者重灌或重建该条向量；`collection.add` 移至 commit 之后 | 覆盖后 chroma 向量与 metadata 与库一致；注入 add 失败 → 事件补偿后一致 |
| 3 | CD-047 | 裂缝3 三洞 + L3 | submit 改由 outbox 事件驱动（与 CD-045 同表同消费者）；级别/内容变更时重新入队；`_record_origins` 失败不再静默 | commit→镜像全链无裸窗口；级别变更后影子文件反映新级别；锚点失败可观测 + 可 replay |
| 4 | CD-048 | 裂缝2 + L2 | Chroma 最小披露：metadata 只留 `id/owner/layer/密级`，删 content/summary 明文（消费点改回查 SQLite）；检索 where 补密级过滤 | chroma_db 内不再有正文片段；检索结果与改前逐条等价（披露级别/内容均一致） |

**共同纪律**：每项独立任务书 + 独立 commit（先 commit 再验证下一项）；台账由 Hermes 验收后补 commit hash；桌面《星枢模块进度.md》在每项 commit 后同步；回归门禁口径固定
`python -m pytest tests/ -q -k "not test_cross_agent_403 and not test_memory_search_self_access and not test_memory_list_self_access" --ignore=tests/test_team_integration.py`（基线：K-1 后 723 passed / 60 skipped / 0 failed，用例数只增不减）。

**不采纳项**：清单「删除 ChromaDB」建议 —— 与 K-1 定档冲突，若确需重新评估，须先改 ADR 再谈（另立需求，不在本轮）。

---

## 五、后续队列（2026-09-17 冻结）

排定顺序（前一项全绿 + 独立 commit 后才开下一项，串行不并行改同一仓）：

| 序 | 台账 | 项 | 状态 |
|---|---|---|---|
| 1 | CD-045 | 裂缝4 审计原子性（outbox 同事务） | 进行中（任务书 `E:\星枢-待办\星枢任务书-2026-09-17\T1-裂缝4-审计outbox-任务书.md` 已派） |
| 2 | CD-046 | 裂缝1 向量一致性（1B/1C/1A） | 待修（依赖 CD-045 的事件表） |
| 3 | CD-047 | 裂缝3 影子三洞 + 开关静默 | 待修（与 CD-045 同一张事件表） |
| 4 | CD-048 | 裂缝2 索引权限盲 + 索引内明文 | 待修（最小披露） |
| 5 | CD-033 | **安全轮 S1**：网关旁路收编（18 项，其中 15 项未做披露剥离的内容读取直返正文） | 已排队 |
| 6 | CD-034 | **安全轮 S2**：审计链防重写前提（外部锚 / 远端回拉 / 同步等级 三待拍板项） | 已排队（与 CD-045 同主题，紧随其后） |

CD-013（MCP `/mcp` 匿名访问）**不入队**——核证发现已是闭环项，见 §六。

**安全轮派单前置（写进任务书，不许省）：**

- **CD-033**：清点表 `docs/gateway-bypass-inventory.md` 基线为 `90c595b`（2026-09-10）。**派单前必须逐条对 HEAD 复核**（routes 已拆分到 `routes_*.py`，端点可能改名/移位/已被收编），复核结论先入台账再动手。
- **CD-033 收编口径**：每条要么走网关（`/api/v1/gateway/read`），要么在 handler 内按披露级别剥离；**不得仅加认证了事**——认证 ≠ 披露，这是 CD-033 与 CD-013 的本质区别（CD-013 就是只要「不匿名」而解了）。
- **CD-034**：三项都是配置/语义决策（R1 生产必配外部锚 + 文档声明本地快照不作依据 / R2 审计连接改 `synchronous=FULL` / R3 远端锚回拉比对），须先拍板再动手。

## 六、顺带核证：CD-013 已是闭环项（台账此前未收口）

核证证据：`routes.py:121` 注释「/mcp 已移出豁免（T0-3，CD-013），走统一认证」+ commit `59e536e`（2026-09-09，`git merge-base --is-ancestor` 确认在 HEAD 历史内）——`AUTH_ALLOWLIST_PREFIXES` 移除 `/mcp`，复用统一中间件；实测记录：无凭据 `/mcp`、`/mcp/`、`/mcp/sse` 全 401，hub_token `/mcp/sse` 200，`mcp_server.py` 零改动；`tests/test_auth_matrix.py:241-253` 三用例在跑。

处置：台账 CD-013 改「已修复（59e536e）」。修复方案与登记时的设想不同（不在 FastMCP 挂载点前置 Bearer 校验，而是取消豁免、复用统一中间件），效果等价且更省。

**教训（已写进队列前置）**：台账「登记待做」标记存在落地后 8 天未收口的情况 → **队列开工前逐条对 HEAD 复核**，本条即实证。

## 七、本轮收口结论（2026-09-17 晚，用户拍板：收口）

**收口口径**：清单四条裂缝全绿 + CD-033 阶段A（已发布全员可见语义）落地 + CD-034 按当日拍板（R2+R1）收尾 = 本轮结束；
阶段B（端点收编本体）、CD-051、CD-052、CD-053、CD-034-R3 登记在册，**下一轮再排**。

### 7.1 清单判定与修复结果（逐条）

| 裂缝 | 判定 | 修复 commit | 验收证据 |
|------|------|-------------|----------|
| 裂缝4 审计先于提交 | 属实（+同区漏项 L1） | b8a8a78 | 注入 commit 失败 → 业务行/事件行/jsonl 三者皆空；731 passed |
| 裂缝1 向量索引不一致 | 属实（+核证新增「删除留孤儿向量」） | 2553c40 | 先红 3 failed → 739 passed；真 Hub+真 ChromaDB 四项 E2E |
| 裂缝3 影子双写三洞 | 属实（+漏项 L3 开关静默 + 核证新增 CD-050 rollback 不联动） | 81e7061 | 先红在无改动树上独立复现 8 failed；定点套件 53 passed |
| 裂缝2 索引权限盲 + 索引内明文 | 属实（清单「只写不读/删掉 Chroma」判定不实） | cfbcfb0→ded38f4 | 真 chroma 目录**字节级搜标记串零命中**；跨 worker 仍 fail-closed |
| 清单未列：知识 chunk 写序 | 核证发现 | 1b77651→76f69e3 | 先红「零 chunk 中间态」3 failed → 29 passed |
| 安全轮：已发布内容全员可见 | 当日拍板（收编前置语义） | b47caef→6396359 | 真 Hub：worker 0→1 条 summary、开关关掉回退 0 条 |
| 安全轮：审计链耐久性 | 当日拍板 R1+R2 | 003df43→8b34154 | 受控 A/B +0.3~0.45ms/条；空闲窗口 P99 契约通过 |

### 7.2 回归基线（本轮新基线）

```
python -m pytest tests/ -q -rf -k "not test_cross_agent_403 and not test_memory_search_self_access and not test_memory_list_self_access" --ignore=tests/test_team_integration.py
→ 767 passed, 60 skipped, 5 deselected, 0 failed  (收集 832)
```

账目精确闭合：739（CD-046 基线）+ 5（CD-049）+ 8（CD-047）+ 6（CD-048）+ 9（CD-033A）= **767**（一条没多、一条没少）。
下一次接手的基线就是 **767 / 0 failed**（不要再拿 739 当基线）。

### 7.3 下一轮队列（不需要重新论证，直接可派）

1. **CD-051 知识向量对账**（已拍板 A+B，任务书现成 `T7-CD051-知识向量对账-任务书.md`）
2. **CD-033 阶段B / CD-054 端点收编本体**：memory 组 / knowledge 组 / wiki+shared 组（按文件域可开 3 路并行）
3. **CD-052 知识层明文**：三候选待拍板（检索时重切 / 落库双写 / 保留但截断）
4. **CD-053**：CD-047 残余边界（删除不补镜像 / rollback 不重算 embedding / 跨天覆盖旧档案）
5. **CD-034-R3**：远端锚回拉比对（需先有可读的外部落点）

### 7.4 本轮收口时的机器状态（可复现）

- 主仓 `master` HEAD `cca2d89`，工作区 0 项未提交，worktree 仅 1 个（主工作区），存活外部 agent 0
- 压测残留数据已清（含派生侧对齐）：memory 395→5、知识 12→3、共享文档 218→8、wiki 415→12 页、向量 23→8、inbox 393→0
- 备份：`E:\星枢-待办\_sync\cleanup-20260917\sync_hub.db.bak-20260917`
