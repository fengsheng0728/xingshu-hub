# CD-077 自动备份改造 · 验收记录（运维轮 · 2026-09-23）

## 执行方与背景

- 基线：`6e81fa9`（worktree `E:\xingshu-wt-cd077`，分支 `ops-cd077`）
- 派单：`E:\星枢-待办\星枢任务书-2026-09-23\T1-CD077-自动备份改造-任务书.md`
- **执行方 = Hermes 自实现（无外部执行方）**：原派 kimi-code v0.29.1，执行到「先红用例写完并跑出 3 条 AssertionError」时被 **5 小时配额 403 中断**（`You've reached your 5-hour usage limit`），实现部分一行未落盘。
  → 按「有产物就地接管、绝不重派」纪律：**先红用例文件由 kimi 产出并保留**（`tests/test_backup_consistency.py`），实现、验收、复跑由 Hermes 完成。**执行方未输出完成报告**（配额断在报告前），本文件的全部证据由验收方独立取得。

## 一、基线缺陷（台账 CD-077）

`hub_mixins/maintenance.py::_run_backup()`：

1. `shutil.copy2(CONFIG.DB_PATH, dst)` 裸拷主库文件 —— 生产库是 **WAL 模式**（`journal_mode=wal`），已 commit 但未 checkpoint 的提交还在 `-wal` 里，裸拷拿到的是**旧快照**（丢最近写入）
2. **只拷 DB**：`chroma_db/` 不在备份里 → 恢复后向量索引与记忆池对不上
3. 失败只 `logger.warning` —— **静默失败**，没有可机器判读的痕迹、没有告警（对照 `hub_cli.py backup` 是对的：VACUUM INTO 一致性快照 + ChromaDB 目录 + marker/manifest 对齐，但只是手动命令）

**终端用户可感知**：只在真正需要恢复的那天暴露（备份看起来「一直在跑」，实际拿到的是残缺快照）。

## 二、改动

`hub_mixins/maintenance.py`（1 文件，+63 / −17）：

- `_run_backup()` 改为**复用 `hub_cli.cmd_backup(backup_dir, CONFIG.DB_PATH, CONFIG.CHROMA_PATH)`** —— 手动 CLI 与自动备份**同一份实现**，不再维护两份备份逻辑；`hub_cli.cmd_backup` 的签名/返回键未动（另有 `tests/test_o2_backup_cli.py` 8 例守护）
- 失败路径：`logger.warning` → **`logger.error`（带异常类型）+ `events` 落 `backup_failed`**（`agent_id="__system__"`，payload 含 `error` / `error_type` / `backup_dir`；落链失败再记一条 error，不覆盖主异常）
- 保留天数读取顺序：`database.backup_keep_days` → 回退既有 `database.backup_interval_days` → 默认 7（历史字段名语义就是保留天数，保持向后兼容）
- 新增 `_cleanup_old_backups(backup_dir, keep_days)`：按 **mtime** 清理 `sync_hub.*.db` / `manifest.json` / `chroma_db/`（目录），**外来文件一律不碰**（`manual_important.db`、`user_notes/`、`keep.me` 实测保留）；清理失败只告警
- 既有语义保留：`backup_enabled=false` 跳过、1 小时冷却、触发频率（每 15 次维护循环）不变

## 三、验收证据

### 3.1 独立复跑先红（验收方手法：把 `maintenance.py` 换回基线 `6e81fa9` 版本，用同一份用例跑）

```
E  AssertionError: 备份副本里读不到已 commit 的数据（WAL 一致性破坏）：读到 0 行，预期 500 行   tests/test_backup_consistency.py:154
E  AssertionError: 备份目录缺少 chroma_db/ 目录: ...\config\backups                              tests/test_backup_consistency.py:166
E  AssertionError: 备份失败未在 events 表留下 backup_failed 痕迹（仍为静默失败）                  tests/test_backup_consistency.py:205
E  AssertionError: backup_keep_days=7 应优先于 backup_interval_days=1，3 天前的备份不应被删        tests/test_backup_consistency.py:295
E  AttributeError: '_MiniHub' object has no attribute '_cleanup_old_backups'                     tests/test_backup_consistency.py:271
→ 5 failed, 2 passed（基线上即绿的 2 条是 backup_enabled 跳过 / 冷却语义护栏）
```

判据形态：4 条为 **AssertionError（行为红，打中判定）**；1 条为退化红 `AttributeError`（新符号 `_cleanup_old_backups` 在基线不存在）——该用例文件内已自附一段**不依赖新符号的行为探针**（过期 `sync_hub.*.db` 被清 / 外来文件不被删），在基线与改后均通过。

### 3.2 转绿

```
$ python -m pytest tests/test_backup_consistency.py -q
7 passed, 1 warning in 1.47s
```

### 3.3 回归

```
$ python -m pytest tests/test_o2_backup_cli.py -q      # 备份 CLI 契约守护
8 passed, 1 warning in 1.55s
```

### 3.4 任务书验收断言逐条自跑

| 断言 | 结果 |
|---|---|
| `grep -n "shutil.copy2" hub_mixins/maintenance.py` → 0 命中 | ✅ rc=1（无命中；注释也已改成不带符号的文字） |
| `grep -n "cmd_backup\|hub_cli" hub_mixins/maintenance.py` → 有命中 | ✅ 2 处（import + 调用） |
| `grep -n "def cmd_backup" hub_cli.py` → 仍在且签名/返回键未变 | ✅ 未改 `hub_cli.py`（`git diff --stat` 只有 `maintenance.py`） |

### 3.5 验收方对执行方用例的一处修正

kimi 产出的 `test_retention_new_artifact_matrix` 里 `old_db` 建好后**漏了 `_touch(old_db, old_ts)`**（判据是 mtime，不 age 就属于「未过期」→ 断言测的是反例）。已补该行（用例内注明「补（验收方 2026-09-23）」）并复核 7 passed。**这是用例缺陷，不是实现缺陷**——按 mtime 保留过期文件是正确行为。

## 四、未实测项（判据落点写清）

| 未实测 | 原因 | 判据落点 |
|---|---|---|
| 生产 `config/backups/` 下的真实自动备份（现网 81 份历史产物 + 7 天保留清理） | 测试纪律：不写生产库/生产 config（CD-070 教训） | 部署环境首次自动备份，或运维手动 `python hub_cli.py backup --out <目录>` 后 `python hub_cli.py verify --from <目录>` |
| 备份失败「通知渠道告警」 | CD-018：钉钉/SMTP 从未真发过，无凭据 | 落链 `events(backup_failed)` 已生效（本轮判据）；渠道到凭据后补真发一次 |
| 大库（GB 级）备份耗时/磁盘占用 | 本机无同规模库 | 部署环境观测（VACUUM INTO 为在线热备，不停机） |
| 与 `wiki/` `ystore.db` 的打包 | `hub_cli.cmd_backup` 现契约只覆盖 SQLite + `chroma_db/` | 属 CD-078/后续项（异地与整机故障恢复演练）范围 |

## 五、结论

CD-077 主体收口：**备份从「裸拷 WAL 主库、只拷 DB、失败静默」改为「VACUUM INTO 一致性快照 + chroma 一起备 + 同一实现复用 + 失败落链」**，先红/转绿/回归三类证据齐备。残留（未实测项）已按上表登记，不阻塞收口。
