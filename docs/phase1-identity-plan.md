# 阶段 1：身份补全（1e 员工账号 + 2b 角色 fail-closed + 1c N1 Hub 侧）

> 版本 v1.0（2026-08-30）｜依据 arch-site 目标架构「身份制分发」+ Kimi Code 交接文档 §3 + 实证
> 前置：阶段 0（hub_ui 砍 dark 主题）已完成 commit 87ade9a

## 0. 实证结论（2026-08-30 grep 全入口）

| 项 | 现状 | 证据 |
|---|---|---|
| 角色 | Principal **无 role 字段**；role 来自 agents/team_members 表；判定入口唯一 | auth_provider.py Principal / disclosure.py:110-112 |
| 存量角色 | 28 agents 全有 role（manager 4 / orchestrator 2 / worker 22），**空 role = 0** | sync_hub.db 实测 |
| 账号 | 无 employee 表、无创建端点；routes_access 只读 agents 当占位 | routes_access.py:6 注释自认 |
| N1 | full_access 概念**零命中**（Hub 侧） | 全仓 grep |
| 删除/破坏端点 | 8 处（N1 审批门挂载点） | automation:113 / integrations:56 / keys:50(已有审批) / knowledge:62 / memory:72 / shared:120 / team:126 / chat-clear:149 |
| Hub 模式 | 当前 NO_AUTH=1 跑（开发），阶段 1 安全测试需鉴权实例（独立端口 + env.pop，防 NO_AUTH 假绿） | main.py / routes.py:133 |

## 1. 1e 员工账号（SMB 无 AD 的入场券）

**表 employee_accounts**（db.py 手写 DDL + IF NOT EXISTS，agent_keys 同款规范）：
```
employee_id TEXT PK          -- emp-{uuid8}
name TEXT NOT NULL
email TEXT UNIQUE
role_template TEXT NOT NULL  -- owner | dept_head | staff | external
department TEXT DEFAULT ''
project_scope TEXT DEFAULT ''  -- external 模板指定项目(METADATA 域)
key_hash TEXT DEFAULT ''       -- 员工个人 key SHA256(同 S1K 规范, 一员工一 key)
status TEXT DEFAULT 'active'   -- active | disabled
created_at TEXT DEFAULT (datetime('now'))
lease_expires_at TEXT DEFAULT ''  -- external 强制租约
```

**模板→scope 映射**（权限语义全走 S1K scope，D5 三方取 min 生效）：

| 模板 | level_cap | data_domain | 租约 |
|---|---|---|---|
| owner 老板 | full | [] 全域 | 无 |
| dept_head 部门负责人 | full | [本部门]（越域→METADATA） | 无 |
| staff 员工 | summary | [本部门, 公共区] | 无 |
| external 外部协作 | metadata | [project_scope] | 强制 |

**key 生命周期**：员工 key 独立存 employee_accounts.key_hash（不污染 agent_keys 语义）；吊销 = 清 key_hash + status=disabled；到期 = lease_expires_at 校验。

**认证扩展**（auth_provider.LocalProvider.authenticate）：
- api_key 路径 `_lookup_agent` 未命中 → `_lookup_employee_by_key`（key_hash SHA256 匹配 + status + 租约）→ `Principal(subject_type="user", subject_id=employee_id, auth_mode="api_key", scope=模板scope)`

**披露链改造（关键存量缺口）**：
- 现状实证：`request_disclosure` 调 `_calculate_disclosure_level`（**无 scope**）——S1K scope 的 level_cap/data_domain 实际没在主披露链生效（disclose_for_principal 无 routes 调用方）
- 1e 改法：新增 `get_current_principal` 依赖（完整 Principal）→ `request_disclosure(req, scope=None)` 内部复用 disclose_for_principal 的 scope 叠加逻辑（min level_cap + 越域降 METADATA）→ 员工模板权限 + Agent S1K scope **同时在主链生效**

**端点**（routes_access.py 扩展，全部 manager+ 门）：
- `POST /api/v1/access/accounts/import`：CSV 三列（姓名,邮箱,模板）批量导入；幂等（email 存在则更新模板）、坏行拒绝并回报告
- `POST /api/v1/access/accounts`：单建（自动签 key 返回明文一次）
- `POST /api/v1/access/accounts/{id}/key`：补签 key（明文仅返回一次）
- `POST /api/v1/access/accounts/{id}/revoke`：吊销（清 key_hash）
- `PATCH /api/v1/access/accounts/{id}`：改模板/禁用/改租约
- GET 已有（AccessView 前端加账号表 + 导入 + 签发）

**前端**：AccessView.vue 加「员工账号」区（表 + CSV 导入 + 签发/吊销按钮），沿用 light 白灰风格。

**测试**：CSV 幂等/坏行拒绝；模板→scope 映射正确；员工 key 认证 → user principal；披露链 scope 生效（level_cap min / 越域降 METADATA / external 租约过期拒登）；存量 Agent 披露不受影响。

## 2. 2b 角色 fail-closed

- disclosure.py:112 `requester_role = requester_info.get("role", "worker")` → 改为：role 缺失/空 → **该 principal 一律 METADATA 封顶**（fail-closed），不再默认 worker 权限
- 影响面：存量零影响（空 role = 0）；新测试：空 role → METADATA / 模板级别正确叠加 / user 与 service 双路径
- 健康报告（未映射/孤儿/超期未变周推管理员）：**后置**（独立小迭代，本阶段不做）

## 3. 1c N1 Hub 侧（全访问授权制）

- agents 表加列 `full_access INTEGER DEFAULT 0`（手写 DDL + 迁移）
- `POST /api/v1/agents/full-access`：manager+ 显式授权/收回，**授权动作入 events 审计**（谁/何时/给谁/理由）
- **审批门**：full_access=1 的 Agent 执行删除/覆盖操作 → 拦截 pending → manager 审批 → 放行/拒绝。挂载点 = 8 处删除端点
- 复用 D6 已交付的 **review_queue 通用组件**（item_type 扩展：n1_delete/n1_overwrite），禁仿制品
- 默认关：full_access=0 时全访问能力不生效（fail-closed）

## 4. 实现顺序与验证

1. **2b fail-closed 先行**（最小改动，独立 commit）——disclosure.py 判定 + 测试
2. **1e 员工账号**（表 + 端点 + Principal 扩展 + AccessView 前端）——最大块
3. **1c N1**（agents 列 + 授权端点 + 审批门挂 8 处 + 审计）

每子项：独立 commit + 单测 + 回归（345 基线只增不减）+ 鉴权实例验证（禁 NO_AUTH 假绿）。

## 决策点（默认已选，有异议拍我）

- A. 员工登录：本阶段占位（hub_token + 账号映射），真口令体系后置
- B. N1 审批门复用 review_queue（D6 拍板「禁仿制品」）
- C. 健康报告后置，不进本阶段
