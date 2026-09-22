# 审计链持久性与防重写评估（D-4 / 清单 3-6 残余）

> 仓库：`E:\sync-hub-case`｜基线 HEAD：`7417bfe`｜评估日期：2026-09-10｜评估人：Hermes（只读评估，未改任何代码）
> 评估范围 = 清单 3-6 两项残余：① `synchronous=NORMAL` 崩溃丢尾风险；② 「拿到本机 DB 写权限无法无痕重写审计链」这条验收的实际达成度
> 结论摘要：**丢尾可检出、非无痕，但"防链尾重写"当前只有一半**——默认配置下外部锚未开，且没有任何自动与远端锚比对的机制。详见 §4、§5。

---

## 一、清单 3-6 原文与核证后状态

| 原文要求 | 核证后状态（2026-09-10 实证） |
|---|---|
| anchor.txt 外发到独立只读介质/syslog（接口已预留） | **已落地（XS-004，d8f69ae）**：`export_anchor()` 除写本地快照外，按 `CONFIG.AUDIT_ANCHOR_URLS` 逐 URL HTTP POST 外发；启动推一次 + 按 `AUDIT_ANCHOR_INTERVAL` 周期推送（`routes.py:51` 区），并可由 `routes_audit.py:386` 手动触发。**但 `AUDIT_ANCHOR_URLS` 默认空列表 = 默认不联网**（`models.py:79`）。 |
| `synchronous=NORMAL` 崩溃丢尾风险评估记录 | **本次补齐**（见 §3）——此前只有代码注释「WAL + NORMAL：降 fsync 开销」，无风险评估记录。 |
| 验收：拿到本机 DB 写权限无法无痕重写审计链 | **有前提地成立**（见 §4）：需 ① 配好外部锚 ② 有「拿远端锚比对本地链」的运维动作。**当前两者都不具备自动化**。 |

---

## 二、现状实证（代码位置）

| 事实 | 位置 | 关键语句 |
|---|---|---|
| 审计连接为 `synchronous=NORMAL` | `audit_chain.py:104` | `conn.execute("PRAGMA synchronous = NORMAL")  # WAL + NORMAL：降 fsync 开销` |
| WAL 由主库连接设置（审计链复用同库） | `db.py:479-480` | `PRAGMA journal_mode=WAL` / `PRAGMA synchronous=NORMAL` |
| 本地锚文件 + 外部 webhook 外发 | `audit_chain.py:595-646` | 先写 `audit/anchor.txt`（**快照**），再逐 URL `urlopen(req, timeout=5)` POST `{ts,hub,anchor,file}`；异常逐 url 容忍不抛 |
| 链头比对只比本地文件 | `audit_chain.py:649-668` | `verify_anchor()` 读 `_ANCHOR_FILE`，`anchored == row["entry_hash"]` → `valid` |
| 综合校验不含锚比对 | `audit_chain.py:671-690` | `verify_all()` = 主链 + 披露链 + jsonl 窗口，**无 anchor 参与** |
| 外部锚配置默认关闭 | `models.py:79-80` | `AUDIT_ANCHOR_URLS: list = field(default_factory=list)`；`AUDIT_ANCHOR_INTERVAL = 3600` |

---

## 三、风险 1：断电丢尾（`synchronous=NORMAL`）

**语义**（SQLite WAL 模式，权威行为）：

- **进程崩溃不丢**：事务已提交即落在 WAL 文件，进程被杀（含 kill -9）后重放 WAL 即可恢复，审计尾部不丢。→ 日常运维风险（重启、OOM、异常退出）**不涉及**本条。
- **断电 / 内核崩溃 / 存储控制器掉电才可能丢尾**：NORMAL 下 WAL 不做每事务 fsync，检查点才刷盘；此时**最后一次检查点之后的事务可能整体回滚**。
- **丢尾上界**：到上一次检查点为止（SQLite 默认 `wal_autocheckpoint = 1000` 页）。不是"丢一条"，是"可能丢一段尾"。

**对审计链的影响与可检出性**：

1. 主链是**前缀自洽的哈希链**（每条 `entry_hash` 依赖 `prev_hash`）。丢尾 = 链变短，**不会造成"中间断裂"**，`verify_all()` 仍然全绿——本地自检**查不出**丢了尾。
2. 但链头 hash 会**落后于外部锚**（锚是每次外发时刻的链头）。只要拿外部锚与本地链头比对，就**必然可检出**「本地比锚落后 / 不一致」。
3. 结论：**丢尾可检出、但不是自动可检出**——依赖 §4 的外部锚可用性与比对动作。

---

## 四、风险 2：「无法无痕重写审计链」的实际达成度（3-6 验收口径）

这条验收的防线由两部分构成，**必须同时成立**：

| 防线 | 作用 | 当前状态 |
|---|---|---|
| ① 哈希链（`entry_hash`/`prev_hash`） | 防**链中段**篡改/增删（改一条，后续全不匹配） | ✅ 成立 |
| ② 外部锚（独立介质持有链头） | 防**链尾整体重写/截断**（攻击者重写尾段后链仍自洽，只有外部锚能戳穿） | ⚠️ **有前提** |

② 的问题（本次评估最重要的发现）：

- `verify_anchor()` 比的是**本地** `audit/anchor.txt`——而这文件就在**同一个仓库目录、同一台机器**上（`audit_chain.py:592`）。**有 DB 写权限的攻击者顺手就能重写它**，比完还是"一致"。源码 docstring 写「锚定文件是外部只读介质」是**设计意图**，不是**默认现实**。
- 真正的外部锚只有 webhook POST 出去的那份（在另一台机器/审计服务器/对象存储上）。但**全仓没有任何代码把远端锚取回来与本地链头比对**（`verify_all()` 不含 anchor，`verify_anchor()` 只读本地文件）。也就是说：外部锚目前只做到「**存证**」，没做到「**校验**」。
- 加上 `AUDIT_ANCHOR_URLS` 默认空 → **默认部署下②这条防线实际是空的**。

**判定**：3-6 的验收在「攻击者只改 DB、不改本地文件」的乐观假设下成立，在「攻击者拿到 DB 写权限」（原文口径）下**不成立**。属于"接口已备、能力未闭环"。

---

## 五、建议（未改代码，待拍板）

| # | 建议 | 成本 | 优先级 |
|---|---|---|---|
| R1 | **生产 config 必配 `AUDIT_ANCHOR_URLS`** 指向独立介质；上线检查表加一项。文档明确写「`audit/anchor.txt` 只是快照，不作为防重写依据」 | 极低（配置 + 文档） | **高** |
| R2 | 给审计写入连接单独设 `PRAGMA synchronous = FULL`（`audit_chain.py:104` 这一处；审计是低频写，每次多一次 fsync 可接受；**不动 `db.py` 全局热路径**）→ 直接消除断电丢尾 | 低（1 行 + 回归） | 中 |
| R3 | 补「远端锚回拉比对」能力：从 `AUDIT_ANCHOR_URLS` 或其读侧取回锚值，与本地链头比对，纳入 `verify_all()` 或独立端点/巡检脚本 | 中 | 中 |
| R4 | 若选择接受丢尾（不做 R2）：把「断电可能丢最后一段审计，以外部锚为准」写进 SLA/上线检查表，并在审计中心 UI 标注 | 极低 | 低 |

**推荐组合**：R1 + R2（低成本把两条防线都补实），R3 按是否需要自动巡检决定；R4 是 R2 的替代项。

---

## 六、复现与验证方法（供后人复核）

```bash
# 1) 实证审计连接为 NORMAL
grep -n "synchronous" audit_chain.py db.py

# 2) 实证外部锚默认关闭
grep -n "AUDIT_ANCHOR_URLS" models.py

# 3) 实证 verify_anchor 只比本地文件（读 _ANCHOR_FILE，无远端请求）
sed -n '649,668p' audit_chain.py

# 4) 实证 verify_all 不含 anchor
sed -n '671,690p' audit_chain.py
```

**丢尾可检出性的手工验证**（不改代码）：配置一个本地 webhook 接收方 → 触发 `export_anchor()` 记下锚值 → 删除 audit_log 尾部若干行 → 此时 `verify_all()` 仍返回 valid（链前缀自洽），而「本地链头 ≠ 已记录的外部锚」→ 即证明"只有外部锚能检出链尾截断"。

---

## 七、评估记录归档

- 本文件即清单 3-6 要求的「`synchronous=NORMAL` 崩溃丢尾风险评估记录」
- 关联台账：`CD-034`（外部锚闭环缺口 + 审计连接 synchronous 策略待拍板）
- 关联历史：XS-004（d8f69ae，外部锚外发落地）、CD-022（jsonl 轮转）、CD-021（gateway_read_log 保留策略）

---

## 附：R1 / R2 / R3 落地状态（2026-09-17，用户拍板）

拍板结论：**先做 R2；R1 还没有外部落点 → 先落文档声明；R3 等落点与排期。**

### R2 已完成（本次改动）

`audit_chain.py` 审计连接 `PRAGMA synchronous` **NORMAL → FULL**（原 `:104` 附近）。
理由：WAL + NORMAL 只在 checkpoint 时 fsync，机器断电/内核崩溃可能丢「上次 checkpoint 之后」的一段尾；
而链前缀自洽 → 本地校验查不出，只有外部锚能检出。审计是低频写，fsync 代价可接受；**不动 db.py 的全局热路径连接**。
回退方式：把该行改回 `NORMAL`（一行）。

**代价与门禁**：`tests/test_s2_audit_chain.py::test_p99_latency_under_5ms`（1000 条实测 P99<5ms）是**负载敏感**契约，
本次 A/B 测量必须在机器空闲时做（并行波运行期间基线本身就 >5ms，无法作为对照）。
若空闲态实测 FULL 档仍越 5ms 线 → **不许偷偷放宽契约**，需报拍板（选项：批次提交摊薄 fsync / 维持 NORMAL 并只靠外部锚 / 调整契约口径）。

### R1 已落文档声明（外部落点未配）

**声明口径（对客户/审计方举证时必须照此说明）**：

> 审计链的行级篡改可由链自校验检出；但**本地锚 `audit/anchor.txt` 与链同机同目录，仅供防误删，
> 不作为「链未被整段重写」的证据**。当前部署 `AUDIT_ANCHOR_URLS` 未配置，即**尚无本机之外的时间戳证据**。

已同步登记进 `known_limitations.py`：`L2-ANCHOR-001`（description/impact/mitigation 三字段含上述口径与 R1/R3 待办）。
配了外部落点后，把该条 status 与 description 同步更新即可。

### R3 未做（待落点与排期）

远端锚定时回拉比对 + 不一致告警：需一个「可被 Hub 拉取」的外部落点，且落点需支持读取。
当前 `export_anchor()` 只有手动端点触发（`routes_audit.py:380`），既无定时外发、也无回拉比对。

### R2 代价实测（2026-09-17，受控 A/B）

机器在有并行任务时无法取得"空闲基线"（并行波期间实测 P99=5.7~6.7ms，本身就红），故改用**同负载受控 A/B**：
只变 PRAGMA 档位，其他条件完全一致，交替 4 轮、每轮 1000 次单条 append 取中位（脚本 `E:\星枢-待办\_sync\cd034\r2_bench.py`）：

```
档位     mean      p50       p95       p99
NORMAL   2.48ms    2.08ms    4.61ms    6.11ms
FULL     2.93ms    2.43ms    4.91ms    6.39ms
差值     +0.44ms   +0.35ms   +0.30ms   +0.28ms
```

**结论**：FULL 的净代价约 **+0.3~0.45ms/条**（单条 append 多一次 fsync）。该测试的设计文档自述空闲态 P99 约 2ms，
即 FULL 后约 2.3~2.5ms，**5ms 契约仍有余量**。注意上表绝对值受当时负载抬高（Hermes 自身进程 + 并行 agent 都在抢 CPU），
**空闲态复核**安排在下一轮无并行任务的回归里顺带做；若届时越线，按上文"不许偷偷放宽契约"处理（报拍板）。
