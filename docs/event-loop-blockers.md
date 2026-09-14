# 事件循环阻塞排查报告（batch4）

日期：2026-09-02
范围：星枢 Hub 主进程 Python 代码（不含 tests/、docs/、node_modules/）

## 1. 方法

- AST/grep 静态扫描，覆盖模式：`urllib.request.urlopen`、`requests`、`subprocess`、`os.system`、`time.sleep`、`socket`、`sqlite3`/`.execute(`、`open(`
- 2026-09-02 实测：修复前后各跑一次 `pytest tests/test_team_routes.py tests/test_team_dashboard.py`
- 分级标准：
  - A 类：async handler 链上的同步网络 I/O，对端不可达时冻结整个事件循环 → 本轮修复
  - B 类：async handler 链上的同步 sqlite3 读写 → 本轮文档化，不修
  - C 类：async handler 链上的同步本地文件 `open()` → 风险低，文档化
  - D 类：历史已修项（netsh / wmic / UDP 8.8.8.8）→ 复查确认

## 2. A 类：同步 HTTP（已修，3 个调用点 / 6 处相关行）

修复方式统一为：提取模块级同步辅助函数 `_fetch_url_sync(request, timeout) -> bytes`
（内部 `with urllib.request.urlopen(...) as resp: return resp.read()`），
async 函数内改为 `await asyncio.to_thread(_fetch_url_sync, req, timeout)`。
timeout 语义、异常捕获位置、返回结构、日志全部保持不变；不引入 aiohttp 等新依赖。

| 位置（修复前行号） | 函数 | 阻塞类型 | timeout | 修复方式 |
|---|---|---|---|---|
| hub_mixins/team.py:91/103/109 | `accept_pairing`（async） | urllib urlopen 同步 POST `/team/pair/exchange` | 8s | `await asyncio.to_thread(_fetch_url_sync, hr, 8)`，外层 `except Exception` 捕获不变 |
| hub_mixins/team.py:202/203/209 | `remove_team_member`（async） | urllib urlopen 同步 POST `/team/revoke`（202/203 为 import/Request 构造，实际 urlopen 在 209） | 5s | `await asyncio.to_thread(_fetch_url_sync, req, 5)`，`except Exception: pass` 语义不变 |
| routes_team.py:291/316 | `api_team_remote_disclose`（async） | urllib urlopen 同步 POST `/team/proxy/disclose`（291 为 import，实际 urlopen 在 316） | 12s | `await asyncio.to_thread(_fetch_url_sync, hr, 12)`，`except Exception` 返回错误 dict 不变 |

修复后 grep 实证：两个文件中 `urlopen` 仅存在于模块级同步辅助函数 `_fetch_url_sync` 内
（hub_mixins/team.py:23、routes_team.py:27），所有 async def 函数体内只有
`await asyncio.to_thread(...)`（hub_mixins/team.py:117/216、routes_team.py:323）。

风险说明：修复前对端 Hub 不可达时，单次调用最长冻结事件循环 5–12 秒，
期间所有请求（含 /health）全部排队——与历史两次事故（netsh/UDP）同一机理，本处为第三处实证。

## 3. B 类：sqlite3 同步调用（未修，文档化）

全量扫描（主代码，不含 tests/docs）：`.execute(` 共 473 处，分布在 30+ 文件。
其中 `db.py` 内部 70 处为 DB 帮助层本身；其余散布在各 async handler 与 mixin 中。
sqlite 单查询通常 <1ms，但慢查询/锁等待/大表扫描时会阻塞事件循环。

### 高频热点（建议 to_thread 或缓存）

| 位置 | 函数 | 同步查询数（本轮实测） | 代表行号 |
|---|---|---|---|
| hub_mixins/dashboard.py | `get_dashboard_data`（async） | 21 处 `.execute(` | 48, 50, 54, 59, 67, 69, 77, 79, 81, 83, 90, 95, 100, 105, 115, 117, 122, 127, 135, 137, 165 |
| routes_report.py | `api_daily_report`（async） | 6 处 `.execute(` | 33, 40, 44, 46, 54, 65 |
| hub_mixins/tasks.py | 任务 CRUD | 25 处（文件级） | — |
| hub_mixins/ingest.py | 汇入管道 | 25 处（文件级） | — |
| routes_automation.py | 自动化任务 | 22 处（文件级） | — |

注：任务书预估 dashboard 9 处 / report 8 处，本轮 grep 实测为 21 / 6
（统计口径差异：含分支内全部 `.execute(`），以实测为准。

### 低频单查（风险低，观察）

routes_team.py（7 处）、hub_mixins/team.py（9 处）、hub_mixins/notifications.py（9 处）、
routes_n1.py（9 处）、key_scopes.py（8 处）等——单条主键查询为主，阻塞窗口极小。

### 架构背景

项目写入侧已有缓冲设计（`hub_mixins/buffer.py`，写走队列异步落库），
但**读取直连 sqlite**：每个 dashboard/report/team 请求都在事件循环线程上同步执行 SQL。
读路径是 B 类风险的全部来源。

## 4. C 类：`open()` 同步读静态页（风险低，文档化）

本地文件读取，单次 <1ms，无网络等待，不构成事件循环冻结风险。清单：

- routes.py:672/674 — dashboard index（**每请求 2 次 open**：先试 `dashboard_dist/index.html`，回退 `dashboard/index.html`）→ 标注「可加缓存」
- routes.py:680（showcase）、896（knowledge）、903（chat）、910（report）、917（wiki）、924（team）— 静态页每请求 1 次 open
- routes.py:661、routes_maintenance.py:44、routes_server.py:33/66/74 — config.yaml 读写（低频管理端点）
- routes_wiki.py:33/55/57/90/156 — wiki 文件读写（低频）

合计静态页读约 8 处。建议（不属本轮）：静态页内容加内存缓存 + mtime 失效。

## 5. D 类：历史已修项复查（grep 实证）

- `wmic`：主代码 **0 残留**。
- `netsh` / UDP `8.8.8.8`：调用代码仍在 db.py:825–927（`get_lan_ips` / `check_windows_firewall`），
  但 2026-08-07 压测基线后已加 TTL 缓存（LAN IP 300s、firewall 60s），
  /health（routes.py:788–789、routes_maintenance.py:26–27、routes_server.py:48）稳态下不再每请求阻塞。
- **诚实标注的残留风险**：缓存过期瞬间仍会同步执行一次——netsh 最坏约 15s
  （db.py:902 timeout=5 + db.py:912 timeout=10）、UDP connect 最坏 1s（db.py:843）。
  即「每 60s 可能有一次最长 15s 的事件循环阻塞窗口」。彻底消除需把这两个调用也移入
  `asyncio.to_thread` 或后台定时刷新，建议列入下一轮。

## 6. 建议（后续轮次，不属本轮）

1. B 类热点优先：`get_dashboard_data`（21 查询/请求）与 `api_daily_report` 改为
   `asyncio.to_thread` 整体包裹或加短时缓存（dashboard 数据可容忍秒级陈旧）。
2. sqlite 读取统一异步封装（如 `hub._db_async()` / aiosqlite）是架构级议题，
   涉及 30+ 文件 473 处调用点，需单独排期与回归基线。
3. D 类缓存窗口残留（第 5 节）列入下一轮修复。
4. C 类静态页缓存可与 B 类 dashboard 缓存合并实施。

## 7. 验证结果

- 修复前基线：`pytest tests/test_team_routes.py tests/test_team_dashboard.py` → **7 passed**（5.52s）
- 修复后回归：同上 → **7 passed**（5.11s），行为不变
- grep 验收：async def 函数体内无直接 `urllib.request.urlopen`，仅 `await asyncio.to_thread`
- 变更范围：仅 `hub_mixins/team.py`、`routes_team.py` 两个文件 + 本文档（新建）
