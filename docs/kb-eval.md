# KB 检索评估框架（CD-081 最小骨架）

状态：**最小骨架已落地**——评估框架 + 口径钉死 + 结果落库 + corpus 26 篇第一组数字。
评估入口是 CLI 工具 `tools/kb_eval.py`（刻意不做 HTTP 端点，避开路由层）。

## 1. 指标口径（钉死，改动 = 新口径版本，须同步本文档）

| 指标 | 口径 |
|---|---|
| **recall@5** | 每条查询的正例 `entry_id` 是否出现在检索结果**前 5 条**（chunk 命中去重到条目级、保持返回序），命中率 = 命中查询数 / 总查询数（算术平均） |
| **拒答率** | 检索结果**为空**，或全部命中的 `similarity < 0.50` 的查询占比。`similarity = 1 − cosine distance`（与 `disclosure.py` 返回字段同款口径）；阈值 **0.50** 为先验中点，v1 钉死，不做事后调参 |
| **p95 延迟** | 单条查询 `DisclosureEngine.semantic_search` 端到端墙钟毫秒（含 query embedding、chroma 查询、SQLite 回查），**线性插值**分位数（与 numpy 默认 `method='linear'` 一致） |

## 2. 评估集口径（自监督 v1）

corpus/ 无人工问答标注 → 自监督构造：**逐篇取 front-matter 的 `title` 作查询，
该篇 `entry_id` 为正例**（`kb-<文件名 slug>`，与 `scripts/seed_kb.py` 同口径派生）。
空标题条目跳过。数据集名 `corpus-selfsup-v1`。

检索口径：限定 `layer="knowledge"` 统一 chroma 集合；请求方为编排员角色
（`orchestrator`，披露规则链 6 全局可见）——**评估的是检索召回，不是权限边界**。
灌库走真实写侧（`KnowledgeMixin.knowledge_upsert` → chunker 切片 → 统一
collection），检索走真实读侧（`DisclosureEngine.semantic_search`），
chroma 用临时 EphemeralClient，不起 HTTP 服务、不碰生产 `chroma_db/`。

## 3. 结果落库

结果 INSERT 进 `evaluation_tasks` 表（alembic `0012_evaluation_tasks` 与
`db.py` init_db 内联 DDL 双侧同步，CD-060 硬等式门禁覆盖）：

| 列 | 说明 |
|---|---|
| `dataset` / `name` | 数据集名 / 本次运行名 |
| `recall_at_5` / `refusal_rate` / `p95_ms` | 三项指标（REAL 可空：dry-run 等只登记场景允许 NULL） |
| `sample_count` | 查询条数 |
| `config_hash` / `config_json` | 评估配置（provider/top_k/阈值/语料指纹等）的 sha256 前 16 位 + 完整快照；**同 hash = 同口径可比** |
| `created_at` | DB 默认 `datetime('now')`（UTC） |

## 4. 用法

```bash
python tools/kb_eval.py --dry-run                 # 只构造评估集并打印（不检索不落库）
python tools/kb_eval.py --db eval/kb-eval.db      # 真跑并落库（库不存在则 init_db 建全 schema）
python tools/kb_eval.py --db sync_hub.db          # 指向生产库（需先 alembic upgrade head 到 0012）
python tools/kb_eval.py --db x.db --provider sentence --model-path <本地模型目录>
```

默认 embedding provider = **hasher**（仓内零依赖默认档，384 维 HashingVectorizer，
与 Hub 未配置本地模型时的默认行为一致）；`--provider sentence` 走
sentence-transformers 本地模型目录。

## 5. 第一组数字（基线）

- 日期：2026-09-23；环境：Windows，Python 3.14.4，chromadb 1.5.9，EphemeralClient
- 数据集：corpus 26 篇（`corpus-selfsup-v1`，语料指纹 `51f90923949ef2ae`）
- 配置：hasher / top_k=5 / 阈值 0.50 / orchestrator / layer=knowledge（config_hash `18193e7083c9ce4d`）

| 指标 | 值 |
|---|---|
| recall@5 | **1.0000**（26/26 正例全部召回进前 5） |
| 拒答率 | **0.8846**（23/26） |
| p95 延迟 | **13.10 ms** |
| 样本数 | 26 |

落库：`eval/kb-eval-first-run.db` 的 `evaluation_tasks` 表第 1 行（name=`cd081-first-run`）。

**基线解读（诚实标注）**：recall@5 满分是自监督口径的**下界参考**——标题同时是
正文首行，词袋必然重叠，该口径测不出难例。拒答率 88% 是**阈值与 hasher 档
相似度量程不匹配**的直接证据：实测 top1 相似度分布 min 0.149 / 中位 0.372 /
max 0.570，hasher 词袋在中文短文本上绝对相似度系统性偏低，0.50 阈值过严
（同数据阈值 0.30 时拒答率 0.346）。这不改口径（阈值先验钉死），而是基线的
真实信息：**sentence 档重标定是后续动作，不是本轮范围**。

## 6. 遗留 / 升级路径

1. **golden 集**：自监督 title→正例 是 v1 过渡口径；真实问答标注集（配比见
   `corpus/README.md` §2）到位后换 `dataset` 名另起口径，两组数字不可混比。
2. **sentence 档标定**：配本地 bge 模型后重跑，观察相似度量程；若量程显著
   抬升，阈值版本化为 v2（文档升版 + config_hash 区分），不回改 v1 数字。
3. **regression 门禁**：`evaluation_tasks` 已有 config_hash 可比性锚点，
   后续可加「同 hash 数字回跌即告警」的巡检（本轮未做）。
4. **生产库迁移**：仓库 `sync_hub.db` 截至本轮仍在 0011；跑
   `alembic upgrade head`（或 Hub 下次启动自动迁移）后，`--db sync_hub.db`
   即可直接落库。已在生产库逐字节副本上验证 0012 upgrade 幂等。

## 7. 阈值版本化 + 回跌告警（CD-109(b)）

### 7.1 档位表与优先级

模块级 `THRESHOLD_PROFILES`（`tools/kb_eval.py`）：

| 档位 | 含义 | 阈值 |
|---|---|---|
| `v1`（默认） | 既有钉死口径，行为逐字不变 | `0.50`（与 `DEFAULT_THRESHOLD` 同值） |

新档（如 sentence 档标定后的 v2）**在标注集到位后追加**，不在本轮制造实际标定值。

阈值取值优先级（高 → 低）：

1. **显式 `--threshold`**（最高：即使与档位值不同也覆盖档位）；
2. **档位值**：`THRESHOLD_PROFILES[档位]["default"]`；
3. **档位 default**：档位 dict 无 `"default"` 键时回落 `DEFAULT_THRESHOLD`（0.50）。

CLI：`--threshold-profile`（默认 `v1`）选档；未知档位名报 `ValueError`（退出码 1）。

### 7.2 **不变量：v1 默认路径的 `config_hash` 不变**

**`--threshold-profile` 默认（v1，含显式传 v1）时，`config_json` 一个字段都不加、
`config_hash` 与 CD-081 历史数字完全一致**（首跑基线 `18193e7083c9ce4d`）。

理由：`config_hash = sha256(config_json)[:16]`，是「同口径才可比」的唯一锚点。
给 `config_json` 无脑加字段会改变 hash → 历史数字（`corpus-selfsup-v1` 的
recall@5=1.0000 / 拒答率 0.8846 / p95=13.10ms）全部失去可比性 → 回跌告警永远失效。
新机制只在**显式传非 v1 档**时把 `threshold_profile` 注入 `config_json`
（hash 变化 = 独立口径，与 v1 数字隔离）。
**后来者加字段前必读本条**——「加个配置项很便宜」在本表是错的。

### 7.3 回跌判据（`--regression-check`，阈值定死）

先按既有流程跑评估并落库（落库行为不变），再取 `evaluation_tasks` 中**同
`dataset` + 同 `config_hash`**、且 id 在本次之前的历史记录（NULL 值跳过），
历史最优取法：`recall_at_5` **最大** / `p95_ms` **最小** / `refusal_rate` **最小**。

| 指标 | 回跌判据 | 阈值 | delta 口径 |
|---|---|---|---|
| `recall_at_5` | 下降 **> 0.02**（绝对差） | 0.02 | `current − best`（负数） |
| `p95_ms` | 上升 **> 20%**（相对历史最优） | 0.20 | `(current − best) / best`（相对） |
| `refusal_rate` | 上升 **> 0.05**（绝对差） | 0.05 | `current − best`（正数） |

任一命中即判**回跌**；三项各自独立比较（可同时命中，逐指标各插一条事件）。

**首次基线不算回跌**：无同口径历史记录（或该指标历史全 NULL）→ 直接正常退出 0，
不插事件；下一轮起才有比较对象。**不同 `config_hash` 严禁互比**（口径不同）。

### 7.4 退出码语义（`--regression-check`）

| 退出码 | 含义 |
|---|---|
| `0` | 正常（含：首次基线、无回跌、dry-run） |
| `3` | **回跌**（命中 §7.3 任一判据） |

既有语义保持不变：评估过程抛出的未捕获异常仍走 Python 默认退出码 `1`；
argparse 参数用法错误沿用其内建退出码 `2`。
**回跌码取 `3` 而非 `2`**（验收裁决 2026-09-24）：与 argparse 的 `2` 数值撞车会让
CI/脚本把「参数写错」误读成「评估回跌」；argparse 的 `2` 是 Python 约定不动，自定义码避开它。
另：CI 判回跌可与 stderr 的 `EVAL REGRESSION` 告警行 / `events` 表 `eval_regression` 行交叉确认。

### 7.5 回跌落点

命中回跌时：① 同库 `events` 表插 `event_type='eval_regression'`、`agent_id='__eval__'`
（payload 键名：`dataset / config_hash / metric / current / best / delta / detected_at`；
插入失败只 warning，**不吞回跌结论**）；② stderr 打印一行人类可读告警
（`EVAL REGRESSION`，含各指标 current/best/delta）；③ `main()` 返回 3。
未开启 `--regression-check` 时：不读历史、不插事件、退出码不变（行为与现状逐字一致）。
