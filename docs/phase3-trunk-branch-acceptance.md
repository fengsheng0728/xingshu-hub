# 阶段3 主干-分干数据层 — 验收表（P1 影子双写 + P2 输出侧收口）

> 日期：2026-08-31（P1）/ 2026-09-01（P2）　|　前置：阶段3-P0（f08877a，主干-分干数据底座基建）全绿
> 契约：`C:\Users\zero\Desktop\星枢-阶段3-主干分干数据层-执行方案.md` P1/P2 通过条件

## P1 通过条件逐项

| # | 通过条件 | 结果 | 证据 |
|---|---|---|---|
| 1 | 四路径双写实测：写 10 记忆 + 5 知识 + 3 wiki chunk + 3 shared → git 仓库对应文件全部存在 | ✅ | e2e 脚本（p1-shadow-e2e / -2）写入后 `git ls-files` 实测：vault/memory 20 / knowledge 10 / wiki 6 / shared 6（两次 e2e 累计，新增 10+5+3+3 全落） |
| 2 | 打标文件内容含 6 维敏感度标记 + 来源信任级 + 时间 | ✅ | 抽查 `vault/memory/<date>/<id>.md`：front-matter 含 kind/id/owner/trust/level/date/tags + 正文 |
| 3 | 主干 index/ 四 jsonl 元数据条目数与 SQLite 行数一致（内容不上行） | ✅ | index/memory 12 + knowledge 6 + wiki 6 + shared 4 行（含修复前批内覆盖 bug 后的完整数据）；`test_index_no_content` 断言 content 全文零出现 |
| 4 | shadow_verify 0 差异；关闭 enabled 后全量回归 ≥ 392 不降 | ✅ | `shadow_verify --since-ts <Hub启动>` PASS 0 缺失；回归见下方 |
| 5 | git commit 历史可回溯，message 带 doc_id/来源 | ✅ | 双仓库 `git log`：`阶段3-P1: 影子镜像 N 条 [kind...]` |

## e2e 抓出并修复的真 bug（3+1 个）

| # | bug | 根因 | 修复 |
|---|---|---|---|
| 1 | index 批内互相覆盖只剩最后一条 | `_append_index` 用 `read_at`（读已 commit 的 HEAD），同批未 commit 的追加互相覆盖 | 改读工作区磁盘文件累积（hub_mixins/shadow.py `_append_index`） |
| 2 | entry_id 含冒号（`doc:p1-doc-0`）→ Windows 写文件 OSError → 整批 abort + 队列条目永久丢失 | Windows 文件名非法字符 | `_safe_name()` 正则替换 `<>:"/\|?*` 为下划线；index 的 id 保留原始值；`_flush_batch` 逐条容错（单条失败记 failure 不 abort） |
| 3 | shadow_verify 假 PASS（0 行） | DB 存 UTC ISO，`--since` 按本地日期过滤错位 | 统一 `--since-ts` unix 时间戳口径（ISO fromisoformat → epoch，shared REAL 直比） |
| 4 | `doc:` 父文档条目无镜像 | `_upsert_doc_entry`（knowledge.py）直写 knowledge_base 不走缓冲，buffer.py 挂钩覆盖不到 | knowledge.py + ingest.py review_entity（ent: 前缀）两处直写路径补挂钩；全项目扫描确认生产代码 knowledge 写入口 3 处全覆盖 |

## 单测

- tests/test_shadow.py 8/8：disabled no-op / kind 开关 / 批处理落 vault+index / index 去重 / 四 kind payload / commit message / 冒号安全化 / stop flush
- 新增 test_colon_in_entry_id_windows_safe（bug #2 回归钉）

## 回归

- 全量 `SYNC_HUB_DATA_TRUNK=0 python -m pytest`（影子关闭零影响验证）→ 结果见回归日志

## 影子数据流（交付后语义）

```
SQLite 落库成功（memory/knowledge/wiki/shared 四路径）
  → ShadowWriter.submit() 线程安全入队（O(1) 零阻塞，D4）
  → daemon worker 攒批（0.5s / 50 条）
  → 打标 md → 分干 vault/<kind>/<date>/<id>.md（front-matter：trust/level/tags/owner）
  → 元数据 → 主干 index/<kind>.jsonl（不含 content 全文，蓝图「内容不上行」红线）
  → 分干 + 主干双 commit（message 带 kind 列表与失败数）
enabled=false / shadow[kind]=false → 全部 no-op（回归零影响）
```

---

# 阶段3-P2 输出侧收口 — 验收表

> 日期：2026-09-01　|　前置：阶段3-P1（aeb4b83）全绿　|　HEAD=c67a8fa
> 契约：`C:\Users\zero\Desktop\星枢-阶段3-主干分干数据层-执行方案.md` P2 段（85-97 行，通过条件原文冻结）

## P2 四项交付逐项

| # | 交付 | 结果 | 证据 |
|---|---|---|---|
| 1 | key_scopes 三层 scope ↔ 分干映射（默认全走 default；config.data_trunk.branches {agent_id: branch} 映射表预留） | ✅ | `DataTrunk.branch_for_agent(agent_id, scope)` 解析序：显式映射 → scope.data_domain 命中已登记分干 → default；`ensure_branch()` 惰性开通非默认分干（结构 + branches.jsonl 登记 + mapped_agents）；ShadowWriter 按属主 agent（owner/created_by/source_agent_id）解析分干写入；`models.py` 新增 `DATA_TRUNK_BRANCHES` + yaml `data_trunk.branches` 段读取；生产 config.yaml 加 `branches: {}` 预留段（该文件 .gitignore 排除，未入库） |
| 2 | 网关读取端点（/api/v1/gateway/read）返回元数据附真相源定位（index 条目 → git 路径 + commit hash），读取仍走 SQLite | ✅ | 响应条目新增可选 `origin` 字段：`{kind, branch, path, trunk_commit, branch_commit, ts}`；ShadowWriter 批 commit 后登记内存 `_origins` + 主干 `index/.commits.jsonl`（随「锚点登记」commit 落 git，重启后 `collect_origins` 纯文件重建）；三 kind（semantic/memory/doc）全挂接；data-trunk 未启用 / 历史存量条目 → 无 origin 字段，存量请求零影响 |
| 3 | BOUNDARY.md 生效断言测试（披露规则链/敏感度链/分干隔离声明 ↔ disclosure.py + sensitivity.py 双向锚点） | ✅ | `tests/test_boundary_assertions.py` 22/22：A 生成方向（rule_table 每条 id/名称/描述全渲染、30 机密词全渲染、静态节锚点、生成幂等）；B 行为方向（r1/r2/r3/r5/r6/r7/r8/r10 + 4.5 fail-closed 真实 DisclosureEngine 判定逐条锚定；敏感度 6 维 classify 真实打标逐维锚定 + 30 词逐词生效断言；分干隔离独立仓库/互不互读/index 零 content） |
| 4 | 审计绑定：每 git commit 的链头 hash 追加主干 audit/chain-head.jsonl（哈希链 ↔ git 历史互证） | ✅ | `audit_chain.current_chain_head()`（GENESIS 兜底，语义同 `_tail_hash`）；ShadowWriter 批 commit 后追加 `audit/chain-head.jsonl`：`{ts, git_commit, branches, chain_head, batch}`，随锚点登记 commit 进 git 历史；hub_core 启动传 `audit_db_path=CONFIG.DB_PATH`；未传 db 路径不写（交付1/2 行为零影响）；与 export_anchor 外部介质锚定并存不冲突 |

## P2 通过条件逐项（冻结原文核对）

| # | 通过条件（原文） | 结果 | 证据 |
|---|---|---|---|
| 1 | 网关读取响应带 commit hash 定位字段（存量请求兼容，新字段可选） | ✅ | `test_gateway_memory_read_attaches_origin`：origin.trunk_commit/branch_commit 均在对应仓库 git log 实测命中；`test_gateway_read_disabled_no_origin` + `test_gateway_read_unknown_id_no_origin`：无定位 → 无字段不报错 |
| 2 | chain-head.jsonl 与 audit_chain 当前链头一致 | ✅ | `test_chain_head_appended_after_batch_commit`（chain_head == AuditChain 尾 hash == current_chain_head）；`test_chain_head_tracks_chain_growth`（链增长逐批跟踪，尾行 == 当前链头）；空链 GENESIS 兜底语义一致 |
| 3 | BOUNDARY 断言测试全绿；全量回归 Hub ≥ 392 不降 | ✅ | 断言 22/22 绿；全量 463 passed / 60 skipped / 0 failed（≥392 且 ≥ P1 的 417，只多不少） |
| 4 | 桌面《星枢模块进度.md》同步（三处易漏：更新时间/Git 状态/历史双段） | ✅ | 表头时间行 + 七运行状态表（回归/Git 状态 HEAD）+ 八 Git 历史 Hub 段 4 commit + 十测试数字，均已同步 |

## P2 commit 列表（每项独立 commit，前缀「阶段3-P2:」）

| commit | 交付 |
|---|---|
| `81f5a90` | 交付1 scope↔分干映射（models/data_trunk/shadow + test_branch_mapping 9 例） |
| `70a8ed9` | 交付2 网关 origin 定位（shadow/routes_gateway + test_gateway_origin 7 例） |
| `c695e64` | 交付3 BOUNDARY 生效断言（test_boundary_assertions 22 例，纯测试） |
| `c67a8fa` | 交付4 审计绑定 chain-head（audit_chain/shadow/hub_core + test_chain_head_binding 8 例） |

## 回归实测

- 基线（P1 后）：`python -m pytest tests/ -q` → **417 passed / 60 skipped / 0 failed**（174.97s）
- P2 后全量：**463 passed / 60 skipped / 0 failed**（195.93s）——新增 46 例全绿，存量零回退
- P2 新增测试单跑：46/46（branch_mapping 9 + gateway_origin 7 + boundary_assertions 22 + chain_head_binding 8）

## 铁律执行核对

- 测试先行：四项交付均先写测试（红）→ 实现 → 绿 → commit；无新增路由（/api/v1/gateway/read 为存量端点，仅加可选响应字段），无需 EXPECTED_ROUTES 变更
- CRLF：全部改动文件实测为 LF（models/data_trunk/shadow/routes_gateway/audit_chain/tests），二进制探测无 \r\n 混入
- 影子铁律：origin/chain-head 全部 try 包裹静默降级（`test_db_failure_silent` 实证）；index 仍零 content（`test_index_never_carries_content`）；identity 明文零落仓（P0 断言未动）；BOUNDARY 生成幂等（`test_boundary_generation_idempotent`）
- 测试隔离：全部新测试用 tmp_path 独立仓库/独立 db，未碰生产 sync_hub.db 与 data-trunk 实仓；cwd 未离开项目根
- 只改 Hub 仓库；4 项交付 4 个独立 commit

## 锚点数据流（P2 交付后语义）

```
ShadowWriter._flush_batch:
  逐条 _write_one → branch_for_agent(owner/scope) 解析分干（默认 default）
    → 打标 md 落 <分干> vault/ + 元数据追加主干 index/<kind>.jsonl（内容不上行）
  → 各分干 commit + 主干 commit（数据 commit）
  → _record_origins: 内存 _origins[id]={branch,path,trunk_commit,branch_commit}
    + 追加 index/.commits.jsonl（持久化，重启可重建）
    + 追加 audit/chain-head.jsonl {git_commit ↔ chain_head}（审计互证）
  → 主干「锚点登记」commit（定位与互证信息本身进 git 历史）

网关 /api/v1/gateway/read（读取仍走 SQLite）:
  结果条目 → _attach_origin → collect_origins（内存优先，文件回源）
    → 命中附 origin 字段；未启用/未镜像/异常 → 无字段（存量兼容）
```
