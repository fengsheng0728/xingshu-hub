# 影子双写崩溃一致性设计文档

- 来源：第二波并行任务书 F（只写设计不写码）
- 对象：阶段3-P1 影子双写（`hub_mixins/shadow.py`，交付 commit `aeb4b83`）
- 问题定义（评估报告 §4.2.3）：ShadowWriter 为 daemon 攒批（0.5s/50 条）异步镜像，Hub 崩溃时最近 ≤0.5s 的写入只存在于 SQLite；一致性校验是事后脚本 `tools/shadow_verify.py` 而非在线对账
- 目标：影子镜像「不丢、不重、失败可见」，且对现有影子语义零变化（D4：影子是增强不是依赖，任何失败不阻塞主链路）

---

## 1. 现状与崩溃场景盘点

### 1.1 攒批队列的内存态

`ShadowWriter`（`hub_mixins/shadow.py:64`）的全部在途状态都在进程内存：

- `self._q`（`collections.deque`）+ `self._qlock`：`submit()` 入队 O(1) 零阻塞（`shadow.py:109`）
- `stats = {"submitted", "flushed", "failures", "last_flush_at"}`（`shadow.py:79`）：纯内存计数，无持久化、无对外端点
- `self._origins`（id → 真相源定位）：内存映射，另有主干 `index/.commits.jsonl` 持久化兜底（`shadow.py:178`）
- 关键不变量缺口：`submitted - flushed - failures` = 队列内存态条数，**崩溃即蒸发，无任何持久化记录**

批处理时序（`_drain_once`，`shadow.py:132`）：先从 deque `popleft` 出批（最多 `_BATCH_SIZE=50`），再 `_flush_batch`。**出队先于落盘**——`_flush_batch` 抛异常时整批只记 `stats["failures"] += len(batch)`，批数据不重排队、直接丢弃（`shadow.py:143-145`）。

### 1.2 四路径挂钩点

均为 SQLite 落库 commit **之后** 同步调用 `submit()`（影子在业务事务之外）：

| kind | 挂钩位置 | 触发点 |
|---|---|---|
| memory | `hub_mixins/memory.py:244` | `store_memory` commit 后 |
| knowledge | `hub_mixins/buffer.py:124-142` | `_batch_write_knowledge` commit 后（另有 `ingest.py:456` 直写入口） |
| wiki | `hub_mixins/ingest.py:85` | `ingest_chunks` commit 后（父文档聚合） |
| shared | `shared_workspace.py:146` | `create_doc` commit 后 |

生命周期：构造 + `start()` 在 `hub_core.py:195-198`；优雅退出 `routes.py:65-66` lifespan 调 `stop(flush=True)` 兜底清队列。worker 为 daemon 线程（`shadow.py:92-93`），循环 `_stop.wait(_BATCH_INTERVAL=0.5)` + `_drain_once`（`shadow.py:125-130`）。

### 1.3 flush 内部的原子性缺口（`_flush_batch`，`shadow.py:148`）

一批的落盘顺序：**逐条写文件（分干 vault md + 主干 index 追加）→ 逐分干 commit → 主干 commit → 锚点登记（`_record_origins`，再追加一次主干 commit）**。期间无任何事务保护，且存在两个已被代码证实的静默点：

- `GitRepo.write_file()` 失败只 `return False`（`gitrepo.py:62-74`），`_write_one` **不检查返回值**——文件没落盘也计入成功路径，meta 照常进 `written`
- `GitRepo.commit()` 失败只 `return False`（`gitrepo.py:76-97`，仅 "nothing to commit" 视为成功），`_flush_batch` **同样不检查返回值**——commit 失败仅 gitrepo 内部 `logger.warning`，`stats["flushed"]` 照常增加

### 1.4 崩溃场景清单

**① Hub 进程崩溃（kill -9 / 断电 / 未捕获异常退出）**
队列内存态（最近 ≤0.5s、最多 50 条）全部丢失。SQLite 行已落库（submit 在 commit 后），git 侧无镜像、无 pending 痕迹。恢复后无 replay，只能等人工跑 `shadow_verify.py` 发现缺失。stats 计数同时归零，`submitted/flushed` 差额这一唯一线索也消失。

**② flush 中途崩溃（批处理窗口内崩溃）**
- ②a 文件写了、分干/主干 commit 没成：git 工作区残留未提交改动（`git status` 脏）。下一批 `commit()` 走 `add -A`（`gitrepo.py:86`）会把残留一起提交——**不自毁但批次归属错乱**，且若崩溃后再无新写入，残留永久悬挂
- ②b 部分分干 commit 成、主干 commit 没成（或反之）：分干 vault 与主干 index 版本错位，`index/.commits.jsonl` 锚点缺失，`collect_origins` 回源定位断链
- ②c 锚点登记（`index/.commits.jsonl` / `audit/chain-head.jsonl`）写了但锚点 commit 没成：镜像本体已在历史，定位锚点丢失——数据不丢但「真相源定位」退化为 index 扫描

**③ daemon 线程异常退出**
`_worker` 循环体内的 `_drain_once` 有 try/except 兜底（`shadow.py:139-145`），但兜底之外的意外（如解释器级错误、`_stop.wait` 异常）会使线程死亡。**死得无声无息**：`enabled` 仍为 True、`submit()` 照常入队计数 `submitted`，后续所有写入静默不镜像，deque 无限增长（内存泄漏 + 数据缺口双重恶化）。无看门狗、无 `is_alive()` 检查。

**④ 磁盘/网络错误（git commit 失败）**
现状无重试策略：`commit()` 失败返回 False 被忽略（见 1.3），整批标记 flushed 或 failures 后即丢弃，不进入任何重试队列。磁盘满、`.git/index.lock` 残留（上次崩溃遗留）会导致后续 commit 持续失败且仅日志可见。`GitRepo` 能力边界：有 `commit/log/diff/read_at/status/head_hash`，**无 delete、无 lock 清理、无 fsck**——恢复侧能用的原语有限。

---

## 2. 对齐 buffer_log 模式

写入缓冲的持久化 replay（commit `1f976ad`，实现于 `hub_mixins/buffer.py`）是本项目已验证的 WAL 模式：

- **写前落 pending**：`_record_trace` 先入内存 trace，再经独立异步队列 `_trace_persist_queue` 攒批 INSERT 进 `buffer_log` 表（`_persist_trace_db`，`buffer.py:236`）——入队路径零阻塞，持久化失败静默降级
- **完成后标记**：flush 成功后单事务批量 `UPDATE buffer_log SET flushed_at=?, flush_latency_ms=? WHERE entry_id=? AND flushed_at IS NULL`（`buffer.py:163-170`），软标记而非 DELETE
- **启动 replay**：`_load_buffer_log`（`buffer.py:179`）恢复最近 200 条 trace + 累计计数（`COUNT(*), COUNT(flushed_at)`），日志报「持久化恢复 N 条已 flush 记录」
- 表结构：`(id, action, agent_id, title, entry_id, queued_at, flushed_at, synced_at, flush_latency_ms)` + `(entry_id, flushed_at)` 索引

**影子层可借鉴与不可照搬之处：**

| 维度 | buffer_log | 影子 pending 需要 |
|---|---|---|
| 记录内容 | 仅 trace 元数据（title/entry_id），落库数据本身在 `_write_queue` 内存 | **必须含完整 payload**（含 content 全文）——replay 时要能重建镜像，否则 pending 只是「知道丢了什么」而不能「补回来」 |
| 完成标记 | UPDATE flushed_at（软标记，表只增不删） | 建议同样软标记（`flushed_at`），定期清理已 flush 行；比 DELETE 多一层可审计性，与 buffer_log 风格一致 |
| replay 语义 | 恢复 trace/计数，**不重放数据**（数据在知识库表，重启后从表读） | **必须真 replay**：pending 未 flushed 的行 → 重建 `(kind, payload)` 重走 `_write_one` 路径补镜像 |
| 与业务事务关系 | 与知识库写入同库不同步（异步攒批） | submit 挂钩点在业务 commit 之后，pending INSERT **无法与业务写入同事务**；独立表、独立连接即可，容忍「pending 有、业务无」（不可能：submit 在 commit 后）和「业务有、pending 无」（pending INSERT 失败 → 走失败可见性通道，见 §5） |

结论：影子 pending 表是 buffer_log 模式在「需要重放内容」场景的直接推广，工程模式（独立队列攒批落库、软标记、启动 replay、失败降级）可整套复用。

---

## 3. 候选方案与推荐

### 方案 A：影子 pending 表（WAL 化队列）

`submit()` 成功入内存队后，异步攒批 INSERT `shadow_pending(kind, payload_json, queued_at, flushed_at NULL)`；`_flush_batch` 成功后按 id 批量标记 `flushed_at`；Hub 启动时 `SELECT ... WHERE flushed_at IS NULL` replay 重走 `_write_one` 补镜像，补完标记。

- 优点：恢复**不丢不重**有硬保证；模式与 buffer_log 同构，团队无新认知成本；对影子主路径语义零变化（仍是「攒批→写文件→双 commit」）
- 缺点：双写路径多一步 SQLite 写（可复用 `_trace_persist_worker` 式独立攒批队列摊薄，入队仍零阻塞）；payload 含 content 全文，表体积大于 buffer_log（需配清理策略）
- 覆盖场景：① 全覆盖，② 由 replay 幂等重写覆盖，④ 失败行留在 pending 等下轮

### 方案 B：daemon 自愈

worker 循环外套 watchdog（`_thread.is_alive()` 周期检查 + 自动重启）；flush 失败批保留进内存重试队列（有界，如 500 条，溢出转失败可见性）；启动时对比 stats 与 `index/.commits.jsonl` 末条做不一致检测。

- 优点：改动最小，不动数据结构；直接覆盖场景 ③
- 缺点：**救不了进程崩溃**（重试队列仍是内存态）；只解决「线程死」不解决「进程死」，作为唯一方案不成立
- 覆盖场景：③ 全覆盖，④ 部分（有界内存重试）

### 方案 C：shadow_verify 升级为在线对账

把事后脚本变 Hub 内周期任务：定时增量 diff（`--since-ts` 口径），发现缺失自动补写。

- 优点：兜底一切（含 A/B 都漏掉的未知路径、git 侧人工改动）；复用已验收的校验逻辑
- 缺点：发现滞后（周期窗口内缺失在线）；全量 diff 成本高，增量口径依赖时间戳有边界风险；补写要重建 payload 需回查 SQLite 多表
- 覆盖场景：全部，但都是事后

### 推荐：A 为主 + B 的看门狗并入 A + C 作二期兜底

**推荐 A（pending 表）**，理由对照任务书三准则：

1. **与现有影子语义零变化**：submit/攒批/双 commit/静默降级全部不动，只在入队侧加持久化、启动侧加 replay；enabled=false 依旧全 no-op
2. **恢复不丢不重**：pending 未标记即重放；重放幂等由现有机制保证——`_append_index` 已有同 id 去重（`shadow.py:330-336`），vault md 按固定路径覆盖写天然幂等，git 提交内容相同则 `nothing to commit` 视为成功（`gitrepo.py:90-91`）
3. **失败可见性**：pending 表本身就是可查询的失败面（`flushed_at IS NULL` 的账龄），叠加 §5 通道

**B 的线程看门狗**作为 A 实施批 2 的子项并入（成本极低，直接闭环场景 ③）；B 的内存重试队列不采纳——与 pending 表功能重复且更弱。

**C 作为二期在线对账**：A 解决「崩溃不丢」，C 解决「任何原因导致的存量漂移」（含 git 仓库侧异常、A 自身的 bug），两者是正交防线。C 的完整设计见 §4。

---

## 4. 在线对账设计（方案 C 细化）

### 4.1 对账周期

- 周期任务挂在 Hub 生命周期内（与 `start_write_buffer` 同类，`buffer.py:22`），默认 10 分钟一轮，可配置；启动后首轮延迟 60s（避让启动 replay）
- 每轮结束把本轮起点时间戳持久化（`shadow_reconcile` 单行表或配置项），下轮 `since_ts = 上轮起点 - 重叠窗口（60s）`——重叠防边界漏判，重复判定的代价由幂等补写吸收

### 4.2 diff 判定（--since-ts 口径的增量性）

复用 `tools/shadow_verify.py` 的四表扫描逻辑（memory/knowledge/wiki/shared 各行 → 期望 git 路径），两点改造：

1. **增量口径**：以持久化的 `since_ts` 过滤（unix 时间戳，与 `--since-ts` 同口径，防 UTC/本地时区错位）。注意 shared_docs.created_at 本就是 REAL unix，其余三表是 ISO 串需转换（`shadow_verify.py:67-78` 的 `iso_ge` 已实现）
2. **文件枚举**：`git ls-files` 全量结果在进程内缓存，每轮只刷新一次；不对每条记录单独起 git 进程

增量性的已知边界：--since-ts 按「行写入时间」过滤，若某行在窗口内被 update（knowledge 的 `updated_at` 会推进，memory 的 `created_at` 不会），漏判窗口外写入但镜像后被删的极端情形——由低频次全量轮（如每日一次）兜住。

### 4.3 补写动作（幂等防重放）

对缺失清单逐条回查 SQLite 原表重建 payload，直接调用 `ShadowWriter._write_one` 等价路径（或 `submit()` 重入队，推荐后者——自动复用 pending 表，补写本身也受崩溃保护）：

- **id 级去重**：`_append_index` 同 id 跳过（`shadow.py:330-336`）保证主干 index 不重行；wiki 的 id 为 `{doc_id}-c{piece_index}`（`shadow.py:285`），chunk 级粒度天然对应 `chunk_hash`/piece 级去重
- **覆盖写幂等**：vault md 路径由 id 确定性生成（`_safe_name`），重写即覆盖，无追加重复
- **git 层幂等**：内容相同的重复提交得 `nothing to commit`，`commit()` 视为成功（`gitrepo.py:89-91`）
- 重叠窗口导致的重复补写因此零副作用

### 4.4 对账结果落 audit

每轮结果（`{ts, since_ts, checked, missing, repaired, repair_failed}`）追加主干 `audit/shadow-reconcile.jsonl` 并随下次镜像 commit 进 git 历史——与 `audit/chain-head.jsonl`（`shadow.py:216-220`）同风格，对账行为本身进历史、可审计。`repair_failed > 0` 走 §5 告警通道。

---

## 5. 失败可见性

现状：`stats["failures"]` 只是内存计数（`shadow.py:79`），flush 异常仅 `logger.exception`（`shadow.py:144`），无审计、无告警；更隐蔽的是 §1.3 两个返回值忽略——**实际失败可能连 failures 都不计**。

设计（分层）：

1. **计数修正（前置）**：`_flush_batch` 检查 `write_file`/`commit` 返回值，失败如实计入 `stats["failures"]` 且不标 flushed——这是可见性的地基，不计数的可见性是假可见性
2. **结构化状态**：stats 增加 `pending_depth`（SQLite `flushed_at IS NULL` 计数）、`last_failure_at`、`last_failure_kind`、`daemon_alive`（看门狗心跳）；经 dashboard/状态端点暴露（与既有 stats 暴露方式对齐）
3. **审计落账**：批级失败追加 audit（Hub audit_log 或主干 `audit/shadow-failures.jsonl`），含批大小、失败条数、异常类型、git 侧 stderr 截断（对齐 `gitrepo.py:92` 的 `out[:200]` 截断惯例）
4. **告警通道**：复用通知 fan-out 的 `channel_status` 模式（`hub_mixins/notifications.py:62-78`：异步出站 + 落库标记，失败不重试不风暴、仅标记+日志）——触发条件：单批失败率 > 50%、pending 账龄 > 5 分钟、对账 `repair_failed > 0`、看门狗重启线程。去抖：同类告警 5 分钟内只发一次
5. **降级红线不变**：以上全部失败仍静默降级，影子可见性建设不得引入主链路阻塞（D4）

---

## 6. 实施排期

### 批 1：pending 表 + 崩溃 replay（核心）

- 内容：`shadow_pending(kind, payload_json, queued_at, flushed_at)` 建表（随 `_load_buffer_log` 式启动自检建表）；submit 侧异步攒批 INSERT（复用 trace 攒批模式）；flush 成功后批量软标记；`ShadowWriter.start()` 前置 replay（未 flushed 行重入内存队列）
- 验收标准：
  - 单测：submit→flush 后 pending 行全部标记；replay 后镜像文件齐全
  - **崩溃注入**：Hub 运行中连续写入 30 条（memory/knowledge/wiki/shared 混合），在 0.5s 攒批窗口内 `kill -9`（Windows 侧 `taskkill /F /PID`）；重启后自动 replay，镜像补齐
  - `python tools/shadow_verify.py --db sync_hub.db --trunk ./data-trunk --since-ts <测试起点>` PASS，缺失 0
  - 幂等验证：对同一批 pending 强制 replay 两次，index jsonl 无重复 id 行、git log 无内容重复 commit
  - 回归：影子关（enabled=false）全 no-op，既有测试套件全绿

### 批 2：看门狗 + 计数修正 + 失败可见性

- 内容：worker 看门狗（`is_alive` 检查 + 自动重启 + 重启计数进 stats）；`_flush_batch` 检查 `write_file`/`commit` 返回值如实计数；stats 扩展字段 + 状态暴露；批级失败落 audit；告警通道接入（含去抖）
- 验收标准：
  - 单测：注入 `_write_one` 抛异常 → 线程死亡 → 看门狗 1 个周期内重启，重启后新 submit 正常镜像
  - 注入 git commit 失败（如预置 `.git/index.lock`）→ failures 如实计数、告警触发、audit 有记录；清除 lock 后 pending 自动消化
  - 回归全绿

### 批 3：在线对账（方案 C 落地）

- 内容：周期对账任务（10 分钟 + 60s 重叠窗口）；since_ts 持久化；缺失回查重建 + `submit()` 重入队补写；结果落 `audit/shadow-reconcile.jsonl`；`repair_failed` 告警
- 验收标准：
  - 单测：人为删除某 vault 镜像文件 → 下一轮对账检出并补写成功，`shadow_verify` 回到 PASS
  - 边界：重叠窗口内同一缺失被两轮检出，补写幂等无重复
  - **崩溃注入**：对账补写进行中 `kill -9`，重启后由批 1 的 pending replay 收口，最终一致
  - 对账开销：1000 行存量下单轮 diff < 5s（git ls-files 单次 + 内存集合比对）
  - 回归全绿

### 依赖与顺序

批 1 是批 3 的前置（对账补写重入队依赖 pending 表获得崩溃保护）；批 2 与批 1 可并行但建议紧随其后（计数修正是对账可信度的前提）。每批独立 commit、独立验收，任何一批回退不影响影子现有功能。

---

## 附：关键引用索引

| 事实 | 位置 |
|---|---|
| 攒批参数 / 出队先于落盘 / 失败批丢弃 | `hub_mixins/shadow.py:36-37, 132-145` |
| index 同 id 去重（幂等基石） | `hub_mixins/shadow.py:330-336` |
| write_file / commit 返回值语义 | `gitrepo.py:62-74, 76-97`（shadow 侧未检查） |
| buffer_log WAL replay 模式 | `hub_mixins/buffer.py:179-212`（commit `1f976ad`） |
| --since-ts unix 口径 | `tools/shadow_verify.py:49-50, 67-78` |
| 影子四路径挂钩 | `memory.py:244` / `buffer.py:124` / `ingest.py:85,456` / `shared_workspace.py:146` |
| 生命周期 | `hub_core.py:195-198`（start）/ `routes.py:65-66`（stop flush） |
| channel_status 告警模式 | `hub_mixins/notifications.py:62-78` |
| 阶段3-P1 交付记录 | commit `aeb4b83`（含 e2e 抓出的 3 个真 bug 修复记录） |
