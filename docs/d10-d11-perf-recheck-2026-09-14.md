# D-10/D-11 性能验收同口径复跑 — 2026-09-14

> 对象：台账 CD-039（「200 并发不劣于 3.59s 基线」未取得同口径证据 → 待复跑，不判定劣化）
> 执行：Hermes（本人实测，非委派）｜证据目录：`E:\星枢-待办\_sync\cd039\`（logs + results-*.jsonl + runs.py/analyze.py）

## 一、口径与控变量

| 项 | 取值 |
|---|---|
| harness | `tests/stress_qualitative.py`，`P2_CONC=200` + `P2_SKIP_CHROMA=1`（T2-2/T2-3 跳过，不动 chroma） |
| 指标 | T2-1a 缓冲路径（knowledge/upsert → asyncio.Queue 写入缓冲）的 `peak_enq_latency_s` |
| 被测 Hub | 每轮**冷启**（kill 干净 → 起 → 轮询 /health） |
| DB | 每轮开始前**从同一快照恢复**（消除 DB 增长漂移）：`sync_hub.db.bak-before-run`，含 `knowledge_base` 4111 条 p2k 行 / `buffer_log` 4122 行 / 32.8MB |
| 对照树 | 同机 git worktree（各自复制同一份 config + chroma_db + DB 快照 + dashboard_dist），**唯一变量 = 代码版本** |
| 构建 | 仅跑 code 与 harness，未改任何业务代码；对照树跑完即 remove |

三方代码版本：`3ce5b25`（CD-017 基线提交自身）｜`e73f9da`（D-9 末，D-10/D-11 之前）｜`5e9b456`（当前 HEAD，含 D-10/D-11）。

## 二、实测数据（peak_enq_latency_s，越低越好）

| 代码版本 / 树 | n | 中位 | 极值 | 各轮 |
|---|---|---|---|---|
| 3ce5b25（CD-017 基线提交） | 3 | **3.383** | 3.532 | 3.258, 3.532, 3.383 |
| e73f9da（D-10/D-11 之前） | 5 | **4.574** | 4.721 | 4.366, 4.578, 4.721, 4.574, 4.339 |
| HEAD 5e9b456（同 hygiene worktree） | 3 | **4.507** | 4.654 | 4.325, 4.507, 4.654 |
| HEAD 5e9b456（主仓） | 5 | 4.854 | 5.050 | 5.050, 4.879, 4.780, 4.854, 4.559 |
| HEAD（主仓，`data_trunk` 影子**开** = 当前生产现状） | 2 | 5.595 | 6.172 | 5.018, 6.172 |

全部 18 轮：`locked=0 / timeout=0 / other=0 / sync_fallback=0 / 数据丢失=0`（HEAD 轮 db_new==ok 精确匹配；对照轮因 3ce5b25 的 harness 硬编码 DB_PATH，用独立行数核对：6711 = 4111+2600、6013 = 4111+1902、5982 = 4111+1871，均无丢失）。

## 三、结论

1. **D-10/D-11 无劣化（CD-039 原判定成立，且由「无证据」升级为「有证据」）**：同一 worktree hygiene 下 HEAD 4.507s vs e73f9da 4.574s，中位差 −0.07s（HEAD 略快），两边分布完全重叠；D-11 本就未触碰缓冲/入库路径（`hub_mixins/buffer.py` 不在迁移清单）。
2. **但存在真漂移（新发现，见台账 CD-040）**：相对 CD-017 冻结基线，本机同口径实测 3ce5b25 自身复现 3.383s（≈当时的 3.59s，口径有效），而 HEAD 4.507s → **+1.12s（+33%）**。差值不是库状态（同快照）、不是环境（同机同 harness），是 2026-08-02 → 2026-09-14 之间 156 个提交的代码累积。故 3-1 的验收口径「200 并发不劣于 3.59s 基线」**当前不满足**。
3. **主仓 vs worktree 差 ~0.35s** 属环境噪声（主仓 audit/wiki 存量更大），非代码差异 —— 同口径比较必须用同 hygiene 的树。
4. `data_trunk` 影子双写开启后 +0.5~1.4s（5.02/6.17），是当前生产现实口径；2 轮样本不足以定论。

## 四、漂移候选成因（已定位到具体代码，未动手）

`routes.py:255-292 _agent_quota_ok()`：鉴权中间件对**每一个已认证请求**调用它，函数体内 `sqlite3.connect(CONFIG.DB_PATH)` + `SELECT ... FROM agent_quotas` + `close()` 全部**同步跑在事件循环里**（未走 `to_thread`/db_facade）。200 并发下与写缓冲的 `BEGIN IMMEDIATE` 争锁，正是 CD-024「事件循环延迟炸弹」的形态；D-11 迁移清单（7 文件 42 函数）不含 `routes_*.py`（属 CD-038 登记的长尾 ~76 函数）。

旁证（本轮实测）：`GET /api/v1/wiki/sync` 在 wiki 目录 6429 页时要 23-24s、清成 415 页后 0.38s；同一轮里 `test_allowlist_health_200` 因同步期超时被打成 0（详见 CD-041）。→ 说明「每请求固定成本 × 事件循环占用」在本负载下对 peak 影响显著。

## 五、未做 / 后续

- 漂移**定位**（bisect 156 commit 区间）未做：需另立任务，约 8 轮对照（每轮 ~2.5min + 建树）。
- `data_trunk` 影子成本需 ≥5 轮才可定性。
- 本轮的对照 worktree 已全部 remove，主仓零残留；生产库已恢复快照口径。

## 六、复跑方法（可重放）

```bash
cd E:/星枢-待办/_sync/cd039
python runs.py off 3                                   # 主仓 HEAD，shadow 关
CD039_REPO=E:/sync-hub-case-<tree> CD039_LABEL=<x>_ python runs.py off 3   # 任意 worktree 对照
python analyze.py                                      # 汇总表
```


## 七、后续：CD-040 定位与修复（2026-09-14 同日）

**定位（不改代码，先短路对照）**：在 worktree 里用环境变量短路候选点做配对实验（同一 DB 快照、同 harness、同树）：
- 短路 `_agent_quota_ok()` 的每请求 `sqlite3.connect`+SELECT：4.437/4.200 vs 不短路 4.745/4.769（峰 −0.44s，吞吐 1817→2135/+17%）→ 配额查库确为贡献者；
- 再短路 `LocalProvider._lookup_agent()`（PRAGMA+SELECT）：**3.146s / 吞吐 3339**（vs 4.623/1841）→ 主因是「鉴权路径每请求同步查库」（每请求 2-4 次 DB 往返，全在事件循环里，与写缓冲 `BEGIN IMMEDIATE` 争锁）。

**修复（commit 0a5d091）**：
- `routes_common.agent_quotas_snapshot()`：O4 配额改进程内快照（TTL 30s，`asyncio.Lock` 单飞刷新 + `to_thread`），写点（`POST /api/v1/agents/quota`）调用 `invalidate_agent_quotas()` 立即失效；语义不变（无行=不限流、表异常=空快照）。
- `LocalProvider._agent_columns()`：列集（PRAGMA）进程内缓存，**默认开**（DDL 运行期不变）。
- `LocalProvider` token→row 缓存：**默认关**，`SYNC_HUB_AUTH_ROW_CACHE=1` 显式开（只缓存正命中 / TTL 8s 可调 / `rotate_keys()` 挂钩失效）。默认关的原因：它把「凭据变更立即生效」放宽为「≤TTL 生效」，与 `tests/test_s1_auth_provider.py` 的轮换·过期语义冲突（3 用例），不能靠改测试糊过去。
- 配套测试：`tests/test_auth_lookup_cache.py`(7) + `tests/test_agent_quota_cache.py`(4)。

**同口径复测（worktree@0a5d091，同一 DB 快照，配对 2 轮）**：

| 配置 | 峰 peak_enq_lat | 吞吐 ok |
|---|---|---|
| 修复前（5e9b456） | 4.745 / 4.769 | 1817 / 1846 |
| 修复后·默认档（列集+配额快照） | 3.957 / 4.144 | 2332 / 2314 |
| 修复后·开 `SYNC_HUB_AUTH_ROW_CACHE=1` | **2.761 / 3.066** | 3498 / 3483 |
| CD-017 冻结基线（3ce5b25 同口径复现） | 3.383 | 2600–2816 |

**结论**：主因已消除并落地；默认档 4.05s（较修复前 −0.7s、吞吐 +26%）仍略高于冻结口径 3.59s，**达标依赖显式开启行缓存**（2.91s，吞吐 +90%）。默认档是否改开、以及行缓存 TTL 取值，属安全/性能取舍 → 报用户拍板（见台账 CD-040 残余项）。
