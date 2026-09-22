# CD-033A 验收：「企业已发布 → 全员摘要级」披露规则 + 可关开关

- 项目：星枢 Sync Hub｜实施：外部 agent（kimi，session_6ebb358f，worktree `E:\xingshu-wt-cd033a` 分支 `cd033a-published-public`）
- 基线：`7af378e`｜任务书：`E:\星枢-待办\星枢任务书-2026-09-17\T6-CD033A-已发布全员可见规则-任务书.md`
- 拍板：2026-09-17 用户「披露链加一条『已发布内容（知识层+手写页）对全员可见到摘要级』+ 可关开关，不覆盖记忆自身 NONE」

## 一、为什么

已实测：worker 身份查知识库返回 **0 条**（8 规则链兜底 r7 worker×worker → NONE），「员工问公司制度」这条产品路径默认是死的。
本任务只做这一条语义，让"企业已发布内容"对全员可见到摘要级。

## 二、实现（链尾"只提升不降级"）

- **提升点**：`disclosure._calculate_disclosure_level` 的 r7 尾与默认兜底链尾各一处 —— 只有"本将判 NONE"才能走到，
  故**绝不降级**任何既有判定（r1 自查 FULL / r2 白名单 / r2b 组交集 / r5 主管看下属 / r6 店长全局一字不动）
- **r3 记忆 NONE 阻断在提升点之前早退** → 「不覆盖记忆自身 NONE」在结构上成立
- **标记只由已发布内容携带**：`disclosure._knowledge_hit` 的伪 dict 加 `published: True`；**记忆行永不带**
- **开关**：`disclosure.published_public`（默认 True），`hub_core._load_disclosure_policy` 默认字典 + `config.example.yaml` 注释示例
- **镜像同步**：`disclosure_rules.simulate()` 同款两处提升（否则 shadow 对比告警，这就是安全网）；`RULES` 表登记 `r4_published_public`

## 三、验收（Hermes 独立复跑）

**新用例** `tests/test_published_public_rule.py`（kimi 写，6 条含先红）→ 我复跑通过。
**文件集**（新用例 + kb_unified + a3_simulator + xs001 + xs003 + sensitivity + gateway）→ **68 passed**。

**我（验收方）改的既有断言 2 处（kimi 主动上报、未擅自动**）：

1. `test_kb_unified_retrieval.py::test_knowledge_hit_disclosure_none_filtered` → 更名 `test_knowledge_hit_published_visible_to_worker`：
   该用例断言"worker-b 查不到 worker-a 的知识"，正是**被你 2026-09-17 拍板推翻的旧死路径** → 改为断言 worker-b 能看到
   且**级别必须是 SUMMARY（不得给 FULL）**；orchestrator 对照组保留，防止"整类被丢"的假绿。
2. `test_a3_disclosure_simulator.py::test_rule_table_enumerable`：规则数 10→11、优先级连续 1→11。
   顺带把 `RULES` 表优先级**唯一化重排**（kimi 按我任务书给 r4_published_public 用了 priority 3，与 r2b 撞号）：
   r2b=3 / r4_published_public=4 / r3=5 / r5..r9=6..10 / r10=11。优先级是纯文档字段（代码不读），重排无行为影响。

**真实 Hub E2E**（`E:\星枢-待办\_sync\cd033a\e2e_published_public.py` + 同目录 JSON；独立 config/db/chroma/端口 3080）：

| 断言 | 实测 |
|---|---|
| E-2 worker 查已发布知识 | **200，命中 1 条，disclosure_level = "summary"**（旧行为 0 条） |
| E-3 摘要级封顶 | 知识体 409 字 → worker 只拿到 **200 字**（`summary = chunk[:200]`）；owner 同一条拿 **409 字全文** |
| E-5 不降级 | owner 自查级别 = **full**（未被提升逻辑影响） |
| E-4 开关回退 | `disclosure.published_public: false` + 重启 → worker 命中 **0 条**（完全回到旧行为，开关可用即真） |

## 四、语义精度（写给产品口径，避免误读）

「已发布内容对全员可见到**摘要级**」在本系统里的实际含义是：**每命中一个 chunk，暴露该 chunk 的前 200 字**
（`_extract_by_level(SUMMARY)` 取 `summary` 字段，而知识层伪 dict 的 summary = `chunk_text[:200]`）。
- 409 字知识体（单块）→ worker 看到前 200 字；**其余部分 worker 拿不到**（不是"看到全文"）
- 若将来要"整篇摘要"而非"前 N 字"，需要给知识条目另做摘要生成（属产品增强，不在本任务）

## 五、kimi 上报的其它发现（已记录）

- `hub_mixins/knowledge.py:173` `knowledge_graph()` 也构造知识层伪 dict 做节点可见性过滤 → **本轮未打标**（不在白名单），
  图谱节点维持旧行为；若阶段 B 要让图谱同步可见，需另行打标。
- `routes_audit.py:301` / `routes_gateway.py:181` 的伪 dict 数据源是 memory_pool / document_chunks（非"已发布内容"）→ 未打标，正确。
