# API 错误码策略：存在性不泄露（T17 · CD-056 配套，2026-09-19 冻结）

## 策略原文（逐字冻结）

**资源存在但无权（403）必须与资源不存在同响应**——即对**非特权主体**，「不存在」与「无权」返回**同一状态码 + 同一 detail 文本**，不得由响应差异反推资源是否存在（防 id/key 枚举预言机）；对**特权主体或资源 owner**，保持「存在但无权 = 403 / 不存在 = 404」的可区分语义（控制台与 owner 自查流程不受影响）。

术语：
- **非特权主体**：`principal_is_privileged(principal)` 为 False 的请求者（含 NO_AUTH 模式下 principal=None）。
- **特权主体**：hub_token 或归属 agent 角色 ∈ PRIVILEGED_ROLES（见 `routes_common.principal_is_privileged`）。
- **同响应**：`status_code` 与 `detail` 逐字相同；响应体结构不得因路径不同而增删字段。

矩阵断言见 `tests/test_403_policy_matrix.py`（P-1..P-6）。

## 对齐状态清单

| 端点 | 位置 | 当前行为 | 是否对齐 | 待对齐原因 | 归属轮次 |
|---|---|---|---|---|---|
| `GET /api/v1/shared/docs/{doc_id}` | `routes_shared.py` `api_shared_get` | 先鉴权后取内容：非特权主体「不存在/私有无权」同一 403「无权访问该文档」；特权主体不存在 → 404 / 无权 → 403 可区分；owner 正常 200 | ✅ 已对齐（T17 本任务） | — | shared 组（已完成） |
| `GET /api/v1/memory/{key}/versions` | `routes_memory.py:91-111` | CD-056 owner-only：归属不成立与 key 不存在同一 403「无权访问该记忆的版本历史」 | ✅ 已对齐（CD-056，矩阵对照组 P-4） | — | memory 组（已完成） |
| `GET /api/v1/knowledge/{entry_id}` | `routes_knowledge.py:100-119` | 只有认证门、无权限门（CD-052 仅内容降级剥离）；不存在 → 404「知识条目不存在」→ 存在性可枚举 | ❌ 未对齐 | 无「无权」侧且缺失即 404；策略适用性待拍（条目是否引入 owner/可见性概念） | knowledge 组收编轮 |
| `POST /api/v1/shared/docs/{doc_id}/blocks` | `routes_shared.py` `api_shared_append` | 先 `can_access` 后写：`can_access` 对不存在 doc_id 返回 False → 非特权「不存在/无权」天然同一 403「无权编辑该文档」；但特权主体不存在也 → 403（无可区分 404 侧） | ⚠️ 部分对齐 | 特权主体侧缺「不存在 → 404」可区分语义（写端点，是否补齐待拍） | shared 组后续轮 |
| `DELETE /api/v1/shared/docs/{doc_id}` | `routes_shared.py` `api_shared_delete` | 先取行判 404「doc not found」，再判 private 非创建者 403「仅创建者可归档私有文档」→ 非特权主体可由 404/403 反推存在性 | ❌ 未对齐 | 与本次修复前 `api_shared_get` 同型缺陷（先取后鉴） | shared 组后续轮 |
| `GET /api/v1/wiki/{page_path}` | `routes_wiki.py:118-119` | 认证门后按文件存在性 → 404「页面不存在: {path}」，detail 回显路径；无权限门 | ❌ 未对齐 | 存在性可枚举（且 detail 回显入参）；wiki 披露分级归 CD-052/054 线 | knowledge/wiki 组 |
| `POST /api/v1/gateway/read`（kind=doc） | `routes_gateway.py:170-183` | 404 detail「文档不存在或无分块」——存在性与「无分块」已合并表述 | ⚠️ 部分对齐 | 「不存在」与「存在但无分块」对调用方不可区分（已合并），但与「存在但无权」侧的 403 是否同响应未审 | 网关组 |
| `POST /api/v1/automation/missed/retry`、`POST /api/v1/automation/jobs/{job_id}/run` | `routes_automation.py:209-269` | 查询按 `owner_agent_id=current_agent` 限定 →「不存在」与「非本人」天然合并为同一 404「job not found」，无 403 侧 | ✅ 天然对齐（404 合并模式） | — | automation 组（免改） |
| 特权限定端点的 404：`routes_audit.py:296`（memory 不存在）、`routes_n1.py:193`（审批项不存在）、`routes_keys.py:58`（key 不存在）、`routes_access.py:260`（账号不存在或已禁用）、`routes_integrations.py` 各 404 | 见左 | 端点前置「仅主管/店长」403 门，404 只对特权主体暴露 | ✅ 对齐（特权侧本就可区分） | — | 各组（免改） |
| `POST /api/v1/hub-agent/audit/{request_id}` | `routes_hubagent.py:49-59` | handler 签名无 `Depends(get_current_agent)`，缺失 → 404「请求不存在」 | ❓ 待审 | 未见端点级认证/权限门（是否由全局中间件兜底未核实）；若无门则存在性对任何人可枚举 | 披露/hub-agent 组 |

## 未对齐词条（登记，不修）

1. `GET /api/v1/knowledge/{entry_id}` — 见上表；测试登记：`tests/test_403_policy_matrix.py::test_p6_knowledge_entry_existence_oracle_registered`（xfail run=False）。
2. `DELETE /api/v1/shared/docs/{doc_id}` — 先取后鉴，404/403 可枚举（与本次修复的 `api_shared_get` 同型）。
3. `GET /api/v1/wiki/{page_path}` — 404 detail 回显路径，存在性可枚举。
4. `POST /api/v1/shared/docs/{doc_id}/blocks` — 特权侧缺 404 可区分语义。
5. `POST /api/v1/hub-agent/audit/{request_id}` — 端点级门禁待审。

## 实施要点（对齐范式）

- 判定顺序：**先鉴权、后取内容**。`can_access` 对不存在的资源返回 False（如 `shared_workspace.py:304-326` 的 `if row is None: return False`）时，非特权主体的两种情形天然合并为同一个 403。
- 特权主体分支：鉴权失败且 `principal_is_privileged(principal)` 为真时，再探存在性以保留 404/403 可区分语义。
- 403/200 返回体结构与 detail 文案保持不变；读审计口径：成功路径落行不变，403/404 自 CD-059（T18，2026-09-20）起落 `denied` 行（`granted_level="denied"`，只记拒绝事实、不区分存在性）。
- 身份门 403（如「不能以 X 身份操作 Y」）属主体不匹配，不在本策略「资源存在性」范畴内。

## 门禁测试模板（CD-059 起；用户 2026-09-20 追认）+

读写审计类端点的新语义验收断言，一律用「**恰多 N 行 + 全字段核对**」写法；禁止只断言「行数不变/不为空」或只断言「不抛错」。

```python
before = len(_log_rows(env))
... 触发一次被拒读 ...
rows = _log_rows(env)
assert len(rows) == before + 1, f"403 拒绝必须恰好多 1 行 denied，实际 {len(rows) - before}"
row = rows[-1]
assert row["requester"] == "<调用主体>"
assert row["kind"] == "<端点既有 kind>"
assert row["target"] == "<目标资源>"
assert row["granted_level"] == "denied"
assert row["item_count"] == 0
```

四条要点：① **「恰多」而非「非空」**——多一行/少一行都算回归；② 字段逐一核对（requester / kind / target / granted_level / item_count）；
③ 成功路径同样「恰多 1 行 + 字段核对」；④ **语义反转时旧断言必须翻转并加严**（不是删除、不是放宽），旧名→新名与理由写进 commit message，
并在测试注释里标注「语义随 CD-xxx 变更」，避免日后被误读为回归。

先例：`tests/test_memory_read_audit.py::test_m5_403_logs_denied`、`tests/test_shared_read_audit.py::test_s4_forbidden_read_logs_denied`、
`tests/test_memory_versions_owner_scope.py::test_v5_read_audit`、`tests/test_403_policy_matrix.py::test_p5_read_audit_success_and_denied`
（4 条均于 2026-09-20 由 CD-059 翻转，经用户追认不回滚）。

