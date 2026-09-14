# Skipped 测试台账（batch2 · 2026-09-02 实测）

## 统计

- 采集命令：`python -m pytest tests/ -rs -q 2>&1`（E:\sync-hub-case，全量回归 192.49s）
- 结果行：`454 passed, 60 skipped, 5 warnings, 9 errors in 192.49s`
- **skipped 总数：60**（按任务书口径约占全量 463 passed + 60 skipped 的 11.5%）
- 运行条件备注：本次实测为 454 passed + 9 errors，与文档基线 463 passed 差 9 个——`tests/test_team_integration.py` 的 9 个用例在 setup 阶段连接 127.0.0.1:3060 被拒（ConnectionRefusedError WinError 10061），即该文件需要真实运行中的 Hub；执行前已确认 :3060 无 dev Hub 占用（netstat 为空），符合任务书"本次预期无占用"。skip 统计不受此影响。
- skip 原因输出原文（`-rs` 汇总区，仅 2 行）：
  ```
  SKIPPED [50] tests\test_embedding_synonyms.py:90: requires_sentence_model: 当前 provider=hasher（模型未就位，hasher 无语义区分度）
  SKIPPED [10] tests\test_embedding_synonyms.py:101: requires_sentence_model: 当前 provider=hasher（模型未就位，hasher 无语义区分度）
  ```

## 按 skip 原因分类

| 原因 | 数量 | 代表测试 | 影响说明 |
|------|------|----------|----------|
| requires_sentence_model（K1 中文同义词验收集，`test_embedding_synonyms.py:90`，50 组参数化） | 50 | `test_zh_synonym[销售~营销]` | 附录 F v1.7 同义词验收用例：要求 sentence embedding 有语义区分度（cos>0.45 且相对无关基线>0.15）；hasher 词袋无区分度，provider=hasher 时必然跳过 |
| requires_sentence_model（K1 跨语言验收集，`test_embedding_synonyms.py:101`，10 组参数化） | 10 | `test_cross_lingual[销售~sales]` | 中英跨语言对齐用例（cos>0.35 且相对基线>0.10）；hasher 对跨语言完全失效，同上必然跳过 |
| **合计** | **60** | — | — |

## 结论

60 个 skipped 全部来自同一类别：**requires_sentence_model**（sentence embedding 依赖）。默认 hasher 档（`EMBEDDING_PROVIDER` 非 `sentence`，模型未就位）下，K1 同义词验收测试集（50 组中文同义词 + 10 组跨语言，`tests/test_embedding_synonyms.py` 全文件）必然整体跳过——即语义检索的"真语义"路径（语义区分度/跨语言对齐验收）在常态回归中从不执行，常态回归只覆盖 hasher 词袋路径。该测试集是"测试先于证据"的空窗期设计：bge 模型文件就位、`EMBEDDING_PROVIDER=sentence` 并 rebuild 后，`pytest -m requires_sentence_model` 一条命令即可出验收结论。
