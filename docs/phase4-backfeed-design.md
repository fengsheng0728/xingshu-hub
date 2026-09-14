# 阶段4 反哺归并设计文档（分干 → 主干归并三件套）

> 任务书 C（第一波并行，2026-09-02）：**只写设计不写码**。
> 定位：系统架构「流②反哺（去重 → 合并 → 打标继承 · 人工队列）· 最后上线」；
> 进度文档「阶段4 反哺（预告待拍板）」。本文档为拍板依据。
> 所有代码引用均为 2026-09-02 本 worktree（分支 batchC-backfeed-design）实测行号。

---

## 1. 现状盘点

### 1.1 gitrepo.py —— git 操作层（168 行）

单仓库操作句柄 `GitRepo`（`gitrepo.py:30`），能力面：

- `ensure()`（:43）—— 仓库不存在则 `git init -b main` + 配置作者，幂等；
- `write_file()`（:62）—— 写文件自动建父目录；
- `commit()`（:76）—— add + commit，`"nothing to commit"` 视为成功；
- 查询面：`log()`（:100）/ `head_hash()`（:118）/ `status()`（:126）/ `diff()`（:134）/ `read_at()`（:143，历史版本读取）。

**边界（反哺设计直接相关）**：

- **只读历史，不改历史**：有 `read_at`/`diff`/`log`，但**没有 delete、rename、gc**。反哺的「分干删除/归档」目前只能"删文件 + commit"，git 历史里内容永存——这与「合并后分干删除」的语义必须对齐（见 §2.3）。
- **失败静默降级（D4）**：所有操作失败返回 `False`/`None`/空列表，绝不抛异常（`gitrepo.py:8-12` 铁律）。反哺流程若在影子路径上做合并，同样必须静默降级，不得阻塞主链路。
- 写操作经模块级 `threading.RLock _LOCK`（`gitrepo.py:27`）串行化；`_git` 超时 30s（:158）。合并批操作要考虑这个串行锁的吞吐上限。

### 1.2 data_trunk.py —— 主干-分干数据底座（302 行）

`DataTrunk`（`data_trunk.py:101`），`enabled=false` 全部 no-op。`ensure()`（:159）建立的主干目录结构（:169）：

```
data-trunk/            # 主干仓库（真相源元数据）
├── BOUNDARY.md        # 边界规则单一事实源（_boundary_md() 生成，:46，幂等）
├── identity/          # agents.jsonl + keys.jsonl（sync_identity(), :255）
├── customers/         # ★ 已建空目录，注释明确"预留给阶段4 反哺 canonical 档案"（:168）
├── index/             # <kind>.jsonl 元数据 + .commits.jsonl 锚点（shadow 写入）
├── audit/             # chain-head.jsonl（git 链头 ↔ 审计哈希链互证）
├── projects/          # branches.jsonl 分干指针登记（_register_branch, :201）
└── branches/<name>/   # 分干独立 git 仓库：inbox/ vault/ _originals/ audit/（:186）
```

关键机制：

- `branch_for_agent()`（:116）——三层 scope → 分干解析：① `config.data_trunk.branches` 显式映射 `{agent_id: branch}`；② `scope.data_domain` 命中已登记分干；③ 默认 `default`（D1）。**反哺归并的输入面就是这套分支枚举**（`_known_branches()` :132）。
- `key_fingerprint()`（:27）——`sha256(key)[:16]`，identity 只落指纹不落明文。canonical 档案的 identity 关联必须沿用此口径。
- BOUNDARY.md §5 分干隔离声明（:85-89）：「分干之间永不互读；跨项目查询只走主干 index/」「内容不上行」。**反哺归并是边界内唯一被允许的跨分干动作**——它只能在主干侧操作元数据 + 经 canonical 档案搬运内容，不能开"分干互读"口子。
- 注意：**BOUNDARY.md 不是本代码仓库的文件**，是 Hub 启动时由 `_boundary_md()` 从 `disclosure_rules.py`/`sensitivity.py`/配置生成、落在 data-trunk 主干仓库根的产物（`data_trunk.py:51-53`「请勿手工编辑」）。

### 1.3 hub_mixins/shadow.py —— 影子双写（430 行）

`ShadowWriter`（`hub_mixins/shadow.py:64`）：SQLite 落库成功后异步镜像 git 仓库群。

- 写入面：`_write_one()`（:255）把打标 md 落分干 `vault/<kind>/...`（YAML front-matter：kind/id/owner/trust/level/date/tags，`_front_matter()` :50），元数据追加主干 `index/<kind>.jsonl`（**不含 content 全文**——「内容不上行」红线，:6）。
- kind 支持：`memory` / `knowledge` / `wiki` / `shared`（:263-303）；属主字段映射 `_OWNER_KEYS`（:241）。
- 攒批 daemon worker：0.5s 或 50 条触发（:36-37），逐条容错（:159）。
- **去重现状**：`_append_index()`（:313）按 **id 相等**跳过重复行（:331-334）——只防重放，**不做内容相似度去重**。跨分干的同一内容因 id 不同必然双写，这正是反哺归并要解决的问题。
- 定位锚点：`_record_origins()`（:178）登记 `id → {branch, path, trunk_commit, branch_commit, ts}`，持久化 `index/.commits.jsonl`；`collect_origins()`（:351）供网关读取附 origin。
- 审计互证：批 commit 后追加 `audit/chain-head.jsonl`（:214-220，需构造时传 `audit_db_path`）。

**边界**：影子层没有"更新/删除已镜像条目"的任何动作——纯追加。反哺合并需要新增「变更传播」能力（见 §2.3）。

### 1.4 披露与信任语义（min / 只降不升的既有实现）

- 披露级别秩：`_level_rank()`（`disclosure.py:19`）——`none=0, metadata=1, summary=2, full=3`。读取侧语义 `min(请求方判定, 存储级别)`（规则 r9，`disclosure_rules.py:39`；chunk 检索 `disclosure.py:642`）。
- 信任级序：`TRUST_ORDER = {"system": 4, "internal": 3, "federated": 2, "external": 1}`（`hub_core.py:40`）；合并/覆盖时 `_merge_trust()`（`hub_core.py:320`）**取较脏者——trust 只降不升，防 taint 被更新洗掉**。
- 敏感度 6 维判定链（`sensitivity.py`）：trust=untrusted → NONE + taint 硬锁（`sensitivity.py:176`）。「PII/UNTRUSTED 硬锁永远比父级严」（`sensitivity.py:249`）——这正是反哺打标继承「只降不升」要复用的既有语义。

### 1.5 人工审查基础设施（D6 review_queue）

通用审查队列表（`db.py:673-685`）：

```sql
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL DEFAULT 'entity',  -- 已有类型：entity / n1_delete / wiki / sensitivity
    doc_id TEXT NOT NULL,
    name TEXT NOT NULL,
    detail TEXT DEFAULT '',
    level TEXT DEFAULT 'summary',
    status TEXT DEFAULT 'pending',             -- pending / approved / rejected
    source TEXT DEFAULT 'llm',
    created_at TEXT DEFAULT (datetime('now')),
    reviewed_at TEXT,
    reviewed_by TEXT
)
```

既有消费模式（反哺队列直接复用）：

- `routes_n1.py:21-36` `_n1_enqueue()`——拦截动作入队 `item_type='n1_delete'`，`detail` 存 JSON 参数；审批端点带 role 门（仅 manager/orchestrator，`routes_n1.py:148`），decision ∈ {approved, rejected}，已处理返回 409（:190-192），审批动作 `_log_event` 落审计（:202）。
- `hub_mixins/ingest.py:379` 实体入队；`review_entity()`（:412 起）approved → 写 `knowledge_base` + `UPDATE review_queue SET status=...` + `_log_event("entity_reviewed", ...)`（:467）。
- 披露审批参照系：`disclosure_requests` 状态机 pending/approved/denied + 惰性 TTL 自动拒绝（`_expire_stale_disclosures()`，`hub_mixins/disclosure_ops.py:254`，24h 超时 `resolved_by='system'`）——反哺队列可借用 TTL 兜底思路。

### 1.6 相似度标定现状（chunker 的 cos 标定，附录 E）

- `chunker.py:28` `DEFAULT_COS_THRESHOLD = 0.75`（env `SYNC_HUB_CHUNK_COS_THRESHOLD` 可覆盖）——但这是**切点阈值**（相邻块 cos 低于它则切），语义与归并去重阈值相反，不能直接搬数值。
- `calibrate_cos_threshold()`（`chunker.py:263`，K1c）：样本文本 → 结构切割 → 相邻块 cos 分布 → 建议阈值 = P25。已挂 API：`POST /api/v1/embeddings/calibrate`（`routes_pipeline.py:114`）。
- 重要警告（`chunker.py:268`）：「0.75 是词袋分布下标定的（K1 修正 3：**不可迁移**）」——换 embedding 模型必须重新标定。
- 现状空窗（`docs/skipped-tests-ledger.md`）：60 个 skipped 全部是 `requires_sentence_model`——常态 provider=hasher（词袋，`db.py:60` `LocalEmbedding` 384 维），**真语义 embedding（bge）未就位**。hasher 词袋无语义区分度（同义词验收 cos>0.45 都过不了），意味着**反哺去重阈值在 hasher 档下的标定结论不可用于 sentence 档**，且当前环境下"语义重复"的检出能力有限。这是阶段4 排期的硬前提（见 §6）。

### 1.7 能力缺口小结（反哺需要新增什么）

| 能力 | 现状 | 反哺需要 |
|---|---|---|
| git 文件删除/改名 | 无（GitRepo 无 delete/rename） | 新增 `remove_file()`/`move_file()`（失败静默） |
| index 条目更新/删除 | 无（`_append_index` 纯追加 + id 去重） | canonical 化后的 index 重指向/合并标记 |
| 内容级相似度去重 | 无（只有 `chunk_hash` 精确去重，`chunker.py:45`） | embedding cos 相似度判定 + 三档分流 |
| 跨分干归并动作 | 无（分干永不互读） | 主干侧 canonical 档案 + 分干归档 |
| 归并人工队列 | 无 | 复用 `review_queue`（新增 item_type） |

---

## 2. 归并三件套设计

### 2.1 去重：相似度三档分流

**判定单元**：主干 `index/<kind>.jsonl` 的条目对（跨分干 + 同分干跨时间）。内容取自分干 vault 打标 md（origin 定位经 `collect_origins()`，`hub_mixins/shadow.py:351`）。

**分流规则**：

| cos 相似度 | 动作 | 落点 |
|---|---|---|
| ≥ 0.9 | **自动合并** | 直接走 §2.3 合并动作，全程审计落链 |
| 0.6 – 0.9 | **进人工队列** | `review_queue`（`item_type='backfeed_merge'`，见 §4） |
| < 0.6 | **新建** | 各自保留独立 canonical 档案 |

**阈值来源与可配置性**：

- 0.9/0.6 为**初始拍板值**，标定方法复用 `calibrate_cos_threshold()`（`chunker.py:263`）的流程：取已知「同一事实的两种表述」正样本对 + 已知无关负样本对，分别求 cos 分布，自动合并阈值 = 正样本 P10（宁可漏合不可错合），人工阈值 = 负样本 P90（宁可多问不可漏问）。
- 遵循 K1 修正 3 的教训（`chunker.py:268`）：**阈值绑定 embedding provider**。config 键建议 `data_trunk.backfeed.cos_auto_merge` / `cos_review_floor`，并随 provider 名存版本（如 `cos_auto_merge@hasher`、`cos_auto_merge@bge-small-zh`），换模型后旧阈值自动失效走保守默认（自动合并关、全部进人工队列）。
- 降级档：provider=hasher 时同义词区分度不足（`docs/skipped-tests-ledger.md` 结论），hasher 档下 `cos_auto_merge` 默认抬高到 0.97（近似精确去重）或干脆禁用自动档，全部 ≥0.6 进人工队列——直到 sentence 模型就位重新标定。
- 精确重复短路：`chunk_hash()`（`chunker.py:45`，规范化 sha256）相等的对直接自动合并，不消耗 embedding 预算——复用 `dedupe_by_hash()`（:258）的幂等语义。

### 2.2 打标继承规则：只降不升（对齐披露 min 语义）

合并结果的打标 = 各来源**取最严**：

- **level**：`min` by `_level_rank()`（`disclosure.py:19`）。例：来源 A=full、来源 B=summary → canonical=summary。
- **trust**：复用 `_merge_trust()`（`hub_core.py:320`）语义——`TRUST_ORDER` 数值小者胜（external < federated < internal < system）。任一来源 untainted 不算数，任一来源 tainted → canonical 记 taint（时间取最早 `tainted_at`）。
- **锁定传染**：任一来源被敏感度链硬锁（PII/UNTRUSTED → NONE，`sensitivity.py:176`）→ canonical 直接 NONE + taint，与「PII/UNTRUSTED 硬锁永远最严」（`sensitivity.py:249`）一致。
- **tombstone 不可逆**：合并只能让 canonical 更严。后续新来源并入时重算 min——只可能降，升级别必须走披露审批流（`disclosure_requests`，`hub_mixins/disclosure_ops.py:24`），不允许归并流程升标。
- **审计**：每次打标合成把 `{sources: [{id, trust, level}], result: {trust, level}}` 记入 canonical 档案的合并历史（§3）+ `_log_event("backfeed_merge", ...)` 双写 `audit_log` 哈希链（`hub_core.py:562-569` 的既有双写模式）。

### 2.3 合并动作：分干归档 + 主干 canonical 更新 + 影子同步

**动作序列**（自动合并与人工批准共用同一执行器，仅触发源不同）：

1. **主干**：写/更新 `customers/<canonical_id>.json` 档案（§3）；`index/<kind>.jsonl` 中被合并条目标记 `merged_into: <canonical_id>`（追加修正行，jsonl 不改历史行——与 `_append_index` 纯追加风格一致，读取端取同 id 最后一行生效，同 `_origins_from_trunk` 的「后批覆盖前批」语义，`hub_mixins/shadow.py:407`）。
2. **分干**：被合并的 vault md **归档而非删除**——移动到 `vault/_merged/<date>/<id>.md`（需要 `GitRepo` 新增 move 能力），front-matter 追加 `merged_into`。选择归档的理由：GitRepo 无 delete 能力且 git 历史本就留存内容（§1.1），"删除"在 git 语义下是假删除；归档路径可读、可回滚、审计自解释。**例外**：`_originals/` 受控区若含 PII 硬锁内容，按合规要求可真删工作区文件（历史抹除属另一独立议题，不在本期）。
3. **删除传播问题（必须正视）**：`ShadowWriter` 是纯追加模型，没有任何"变更/撤销"通道。合并后若不处理，网关读取仍能通过旧 origin 定位到分干旧文件。对策：
   - index 修正行是权威：`collect_origins()` 读取端对命中 `merged_into` 的 id 重指向 canonical 档案，并在响应附 `merged: true` 提示；
   - 内存 `_origins`（`hub_mixins/shadow.py:85`）重启即失，纯文件回源本就取最后定位，修正行天然兼容；
   - **不可合并期窗口**：刚写入 < T 秒的条目不参与归并（T ≥ 影子攒批间隔 0.5s × 安全余量，建议 60s），防止正在镜像途中的条目被搬走导致 origin 悬空。
4. **审计**：每个合并批次（同 `ShadowWriter._flush_batch` 的批粒度）commit 信息含 canonical_id 列表；`audit/chain-head.jsonl` 追加链头互证（复用 `hub_mixins/shadow.py:214-220` 模式）。
5. **回滚**：归档是可逆的——拒绝/误合时把文件移回原路径 + index 追加 `unmerged` 修正行即可，git 历史全程留痕。

---

## 3. customers/canonical 档案 schema

落点：主干 `customers/` 目录（`data_trunk.py:168` 注释已预留「customers 预留给阶段4 反哺 canonical 档案」）。每档案一文件 `customers/<canonical_id>.json`，走主干 git 版本化。

```json
{
  "canonical_id": "cust:c-8f3a2b91",
  "kind": "memory",
  "title": "客户X的交付周期偏好",
  "content_digest": "sha256:<规范化正文哈希>",   // chunk_hash() 同口径，chunker.py:45
  "identity": {
    "owner_key_fp": "a1b2c3d4e5f60708",          // key_fingerprint() 口径，data_trunk.py:27，绝不落明文 key
    "owner_agent_ids": ["sales-bot-1", "cs-bot-2"]
  },
  "sources": [                                    // 来源分干清单（归并的血缘）
    {"branch": "default", "kind": "memory", "id": "mem-001",
     "path": "vault/memory/2026-09-01/mem-001.md",
     "trunk_commit": "<40hex>", "branch_commit": "<40hex>"},
    {"branch": "proj-alpha", "kind": "memory", "id": "mem-117",
     "path": "vault/_merged/2026-09-02/mem-117.md",
     "trunk_commit": "<40hex>", "branch_commit": "<40hex>"}
  ],
  "tags_snapshot": {                              // 打标快照（只降不升，§2.2）
    "trust": "federated",                         // = min by TRUST_ORDER
    "level": "summary",                           // = min by _level_rank
    "tainted_at": "2026-09-01T08:00:00+08:00",    // 最早 taint，空串=未污染
    "locked": false
  },
  "merge_history": [                              // 合并历史（每次合并追加）
    {"ts": "2026-09-02T03:00:00+08:00", "action": "auto_merge",
     "cos": 0.93, "absorbed": ["proj-alpha:mem-117"],
     "actor": "system", "queue_id": null,
     "before": {"trust": "internal", "level": "summary"},
     "after":  {"trust": "federated", "level": "summary"}}
  ],
  "created_at": "...", "updated_at": "..."
}
```

对齐说明：

- 与主干既有结构的关系：`identity/` 存 agent/key 指纹（`identity/keys.jsonl`，`data_trunk.py:273-279`），canonical 档案的 `owner_key_fp` 与之关联；`index/` 仍是查询入口（元数据级，内容不上行），`customers/` 是归并后的**内容级正本**——这是阶段4 对「内容不上行」红线的唯一受控例外，需要在 BOUNDARY.md 第 5 节追加声明（改 `_boundary_md()` 生成源，不手编，见 §1.2 注意点）。
- 文件级而非 jsonl 追加：档案会被反复更新（merge_history 增长），单文件 + git diff 的审计可读性优于 jsonl 修正行；jsonl 修正行只用于 index（既有读取端兼容）。

---

## 4. 人工归并队列（复用 review_queue 模式）

**零新表**：`review_queue`（`db.py:673`）加新 `item_type='backfeed_merge'`，遵守 D6「新功能一律用此组件，禁造 inbox 仿制品」（`db.py:671-672` 注释）。

字段映射：

| review_queue 列 | 归并语义 |
|---|---|
| `item_type` | `'backfeed_merge'` |
| `doc_id` | 候选 canonical_id（或 `pair:<idA>:<idB>` 未建档案时） |
| `name` | 人读标题：`"[cos=0.78] 客户X交付偏好 × 2 来源"` |
| `detail` | JSON：`{pair: [{branch,id,path,excerpt}], cos, suggested: "merge"|"keep", reasons}`（excerpt ≤200 字，沿用 ingest 的截断惯例，`hub_mixins/ingest.py:382-385`） |
| `level` | 候选对中最严级别（展示侧即遵守披露） |
| `status` | `pending` / `approved` / `rejected`（状态机与 N1 一致，`routes_n1.py:190` 已处理返回 409） |
| `source` | `'backfeed'` |
| `reviewed_at` / `reviewed_by` | 审批人（manager/orchestrator role 门，同 `routes_n1.py:148`） |

**审计落链**：审批动作 `_log_event("backfeed_merge_reviewed", reviewer, {queue_id, decision, canonical_id, cos})`——沿用 `_log_event` 的 events + audit_log 双写（`hub_core.py:538-569`）。

**TTL 兜底**：借用 `_expire_stale_disclosures()` 的惰性 TTL 思路（`hub_mixins/disclosure_ops.py:254`）——但归并队列超期**不得自动 approved**（方向相反），超期保持 pending 或转 `rejected`（保守 = 不合），由 §5 的积压指标驱动人工处理。

**UI**：dashboard 复用现有队列页模式加一个 `item_type` 过滤 tab（本轮不含实现，仅约定字段已够渲染）。

---

## 5. 影子 → 回切量化退出标准

「影子双写」→「双写 + 回切（读路径走主干）」必须同时满足以下**可测**条件（建议做成 `/api/v1/data-trunk/cutover-readiness` 只读端点输出）：

| # | 指标 | 达标线 | 测量方法 |
|---|---|---|---|
| 1 | 一致性校验 | `shadow_verify`（`tools/shadow_verify.py`）全量比对 **0 差异连续 14 天** | 每日 cron 跑校验，差异数入台账 |
| 2 | 影子失败率 | `ShadowWriter.stats["failures"]`（`hub_mixins/shadow.py:79`）/ submitted < 0.1%，且连续 14 天无整批失败（`_flush_batch` 异常） | stats 导出 + 日志 grep |
| 3 | 归并队列积压 | `review_queue` 中 `item_type='backfeed_merge' AND status='pending'` 且 created_at 超 72h 的条数 **< 20**；自动合并零投诉（无 `unmerged` 回滚）连续 14 天 | SQL 直查 |
| 4 | 审计互证 | `audit/chain-head.jsonl` 链头与 `audit_log` 当前链头每日对账 0 不符，连续 14 天 | 复用 `current_chain_head` 对账 |
| 5 | 归并正确性抽验 | 人工抽验最近 100 次自动合并，误合率 **= 0**、漏合率 < 5% | 抽检表签字记录 |
| 6 | 读路径覆盖 | 回切灰度期网关读取 100% 经 `collect_origins()` 附 origin；抽 1000 条读请求，origin 命中（内存 + 文件回源合计）≥ 99.5% | `gateway_read_log`（`db.py:719`）统计 |
| 7 | 语义底座就位 | `EMBEDDING_PROVIDER=sentence` 上线且 `pytest -m requires_sentence_model` 全绿（60 个 skipped 归零，`docs/skipped-tests-ledger.md`） | 测试台账 |

回切后仍保留回退开关：`DATA_TRUNK_ENABLED` + 新增 `data_trunk.read_cutover=false` 一键退回影子只写——回切是灰度不是跳崖（对齐「最后上线」定位）。

---

## 6. 实施排期建议（commit 批次）

| 批次 | 内容 | 验收标准 | 依赖 |
|---|---|---|---|
| B1 | `GitRepo` 增 `remove_file`/`move_file`（失败静默 + 测试）；`review_queue` 消费器骨架（`item_type='backfeed_merge'` 入队/审批/409 幂等，role 门，审计落链） | 单测：移动后 `read_at(HEAD~1)` 可读历史；审批状态机测试对齐 `tests/test_guard_liveness.py` N1 探针模式 | 无 |
| B2 | 精确去重合并（`chunk_hash` 相等 → 自动合并）+ customers/ canonical schema 落盘 + index 修正行 + 分干归档 + `_boundary_md()` 增 canonical 声明 | e2e：双分干写同内容 → 主干一档案、分干归档、origin 重指向、回滚可逆 | B1 |
| B3 | embedding cos 相似度判定 + 三档分流（hasher 档自动合并默认关）+ 标定端点扩展（复用 `/api/v1/embeddings/calibrate` 流程产出 backfeed 阈值） | 注入假 embed_fn 的三档边界测试（0.59/0.6/0.9/0.91）；阈值随 provider 版本化失效测试 | B2 |
| B4 | 打标继承执行器（min/只降不升/taint 传染）+ 合并历史 + 拒绝回滚 | 规则矩阵测试：全组合 trust×level 合成结果 = 各来源最严；升标尝试被拒 | B2 |
| B5 | 回切就绪端点（§5 七指标）+ 灰度开关 + 抽验台账工具 | 端点输出七指标实测值；指标造假注入测试（人为制造差异 → readiness=false） | B1-B4 |
| B6 | sentence 模型就位后：重新标定阈值、开自动合并、跑 14 天达标观察 → 回切 | §5 全绿；`requires_sentence_model` 60 项归零 | B5 + 模型资产 |

**与架构图「最后上线」的衔接**：反哺是唯一改变存量数据语义（删除/合并/打标降级）的流程，且是「内容不上行」红线的唯一受控例外，必须等主干-分干影子（流①）被 §5 指标证明可信之后才动存量。B1-B4 可在影子期并行开发但**默认关闭**（`data_trunk.backfeed.enabled=false`，与 `DATA_TRUNK_ENABLED` 同款 fail-closed）；B5 是纯观测；B6 才是「上线」。任何一批出问题，退回开关不影响流①影子既有能力（D4：反哺也是增强不是依赖）。

---

## 附：本文档引用的代码符号索引

- `gitrepo.py`：`GitRepo`（:30）、`ensure`（:43）、`write_file`（:62）、`commit`（:76）、`log`（:100）、`head_hash`（:118）、`diff`（:134）、`read_at`（:143）、`_LOCK`（:27）
- `data_trunk.py`：`key_fingerprint`（:27）、`_boundary_md`（:46）、`DataTrunk`（:101）、`branch_for_agent`（:116）、`_known_branches`（:132）、`ensure`（:159，customers 预留 :168）、`_register_branch`（:201）、`sync_identity`（:255）
- `hub_mixins/shadow.py`：`ShadowWriter`（:64）、`_front_matter`（:50）、`_write_one`（:255）、`_append_index`（:313，id 去重 :331）、`_record_origins`（:178）、`collect_origins`（:351）、`_origins_from_trunk`（:387）、`_OWNER_KEYS`（:241）、`_BATCH_INTERVAL`（:37）
- `chunker.py`：`DEFAULT_COS_THRESHOLD`（:28）、`chunk_hash`（:45）、`dedupe_by_hash`（:258）、`calibrate_cos_threshold`（:263，不可迁移警告 :268）
- `disclosure.py`：`_level_rank`（:19）、search_chunks min 语义（:642）
- `disclosure_rules.py`：r9_sensitivity_cap（:39）
- `hub_core.py`：`TRUST_ORDER`（:40）、`_merge_trust`（:320）、`_log_event` audit 双写（:562-569）
- `sensitivity.py`：UNTRUSTED 硬锁（:176）、硬锁最严（:249）
- `db.py`：`review_queue`（:673-685）、`gateway_read_log`（:719）
- `hub_mixins/ingest.py`：实体入队（:379）、`review_entity`（:412 起）
- `routes_n1.py`：`_n1_enqueue`（:21）、role 门（:148）、409 幂等（:190）
- `hub_mixins/disclosure_ops.py`：`_expire_stale_disclosures`（:254）
- `routes_pipeline.py`：`/api/v1/embeddings/calibrate`（:114）
- `docs/skipped-tests-ledger.md`：60 skipped = requires_sentence_model
