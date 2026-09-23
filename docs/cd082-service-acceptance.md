# CD-082 服务化与自愈 + CD-088 FAQ 措辞 · 验收记录（运维轮 · 2026-09-23）

## 执行方与中断记档

- 基线：`6e81fa9`（worktree `E:\xingshu-wt-cd082`，分支 `ops-cd082`）
- 派单：`E:\星枢-待办\星枢任务书-2026-09-23\T3-CD082-服务化与自愈-任务书.md`
- **执行方 = kimi-code（前半）+ Hermes 自实现（接管）**：kimi 写出 `scripts/install_service_windows.ps1` 并做完本机非管理员实测（明确失败、exit 1）后，被 **5 小时配额 403 中断**（`You've reached your 5-hour usage limit`），未输出完成报告。
  → 按「有产物就地接管」纪律：**脚本产物保留并由验收方复核**，启动自动迁移、FAQ 措辞、验收证据、全部用例由 **Hermes 自实现并独立取证**。

## 一、基线缺陷

- **CD-082（高）**：全仓无 systemd / Windows 服务 / 任务计划 / 开机自启（grep 实测唯一命中就是 `FAQ.md` 把它当**已有**部署方式写着）；exe 形态靠双击 `启动 Hub.bat` → **机房重启后要人工拉起**；迁移也只有手动 `alembic upgrade head`（忘了跑 = 运行期撞缺列，CD-030 / CD-055 同族）。
- **CD-088（中）**：`FAQ.md:76` 把「服务注册 ★★★ 写入 systemd/Windows 服务自动启动」列为已有部署方式 —— **不实承诺**。

**终端用户可感知**：重启后系统自己回来 vs 要人工拉；按 FAQ 去配服务发现配不了。

## 二、改动（3 文件）

| 文件 | 改动 |
|---|---|
| `scripts/install_service_windows.ps1`（新增，172 行） | Windows 任务计划安装/卸载：`-Mode source\|exe`、`-ConfigDir`、`-WorkDir`、`-TaskName`（默认 `XingshuSyncHub`）、`-Uninstall`；`AtStartup` 触发器 + `SYSTEM` 身份 + `RestartCount 5 / RestartInterval 1min`（失败重启=自愈）+ `ExecutionTimeLimit 0`（常驻）；包一层 PowerShell 透传 `SYNC_HUB_CONFIG_DIR`、日志落 `<WorkDir>\logs\hub-service.log`、子进程退出码透传为任务退出码（失败重启语义依赖非零退出）；**幂等**（同名先注销再注册）；**权限诚实**（非管理员 → 打印确切修复命令 + `exit 1`）；脚本末尾**回读任务**确认注册结果；不动防火墙/注册表/UAC、不删既有任务 |
| `main.py`（+113） | CD-082 启动迁移检查：`_alembic_head()` / `_db_alembic_version()` / `_run_alembic_upgrade()` / `run_startup_migration()`，在 `uvicorn.run` 之前调用 |
| `config.example.yaml`（+8） | 新增 `migrations.auto_upgrade: true`（含语义注释：false=只告警不迁移；失败一律 fail-closed） |
| `FAQ.md`（+9/−2） | 部署方式表「服务注册」行改为**实际可用**的安装命令 + 「需管理员执行一次」+「Linux 未提供脚本」；「不需要专职 IT」改为「基本不需要」并补两条如实说明（CD-088 收口） |

### 启动迁移的判定表（设计取舍，逐条对应任务书判据）

| 库内状态 | 开关（默认 `true`） | 行为 |
|---|---|---|
| 无 `alembic_version` 表（全新库 / 内联 DDL 建的库） | — | **不猜、不迁移**，打印说明继续启动（避免对已由内联 DDL 建好表的库跑 0001 重复建表） |
| 已等于 head | — | 无动作 |
| 落后 | `true` | 执行 `python -m alembic upgrade head`（子进程 + `SYNC_HUB_DB` 注入，env.py 靠它定位目标库），执行后**复核**版本确已到 head |
| 落后 | `false` | 打 `[WARN]` + 给手动命令，继续启动（存量部署行为不变） |
| 迁移失败 / 执行后版本仍不符 | — | **fail-closed：抛错 → `[FATAL]` + 修复指引 + `sys.exit(1)`**（与 TLS / 防火墙硬门同风格） |

测试态（`models._in_test_context()`：`SYNC_HUB_DB_GUARD` / `PYTEST_*` / `NO_AUTH`）在调用点直接跳过，**测试库不会被迁移**。

## 三、验收证据

### 3.1 先红（形态与替代证据，诚实登记）

本条属「**新增能力**」，基线不存在对应符号与文件，故先红天然是 **AttributeError / 文件缺失**的退化红（任务书 §T3-0 预告的形态）。替代证据 = 基线能力缺失的**事实断言**：

```
$ git show 6e81fa9:main.py | grep -c "alembic"            → 0
$ git show 6e81fa9:main.py | grep -c "migrations"         → 0
$ git show 6e81fa9:config.example.yaml | grep -c "^migrations:"  → 0
$ git ls-tree 6e81fa9 scripts/ | grep -c install_service  → 0
```

### 3.2 转绿（新增用例 9 例）

```
$ python -m pytest tests/test_startup_migration.py -q
9 passed, 1 warning in 1.68s
```

覆盖：已是最新无动作 / 落后+开→执行并推进到 head（断言库内版本真的变了）/ 落后+关→告警不执行不退出 /
失败 rc≠0 → fail-closed / rc=0 但版本未推进 → fail-closed / 无 `alembic_version` 表 → 不迁移 /
真子进程命令形状（`["-m","alembic","upgrade","head"]` + `cwd` + `SYNC_HUB_DB`/`SYNC_HUB_CONFIG_DIR` 注入）/
head revision 必须存在于 `migrations/alembic/versions/` / **生产库副本走真实读路径判「已是最新」且文件字节数不变**。

### 3.3 PowerShell 脚本实测（非管理员环境，验收方亲自复跑）

```
① 语法解析： [Parser]::ParseFile(...) → PARSE_OK
② 非管理员直跑：
   $ powershell -NoProfile -ExecutionPolicy Bypass -File ...\install_service_windows.ps1 -Mode source
   [FAIL] 需要管理员权限才能注册开机自启(AtStartup)的系统级计划任务。
   [FAIL] 修复: 右键开始菜单 -> Windows PowerShell(管理员) / 终端(管理员)，然后执行:
   [FAIL]   powershell -ExecutionPolicy Bypass -File "E:\xingshu-wt-cd082\scripts\install_service_windows.ps1" -Mode source
   rc=1
```

判据：**不假装成功**（非管理员 → 非零退出 + 确切修复命令），符合任务书「权限诚实」条款。

### 3.4 回归

```
$ python -m pytest tests/test_db_path_guard.py tests/test_startup_migration.py -q   # 见下方合并后复跑记录
```

## 四、未实测项（判据落点写清）

| 未实测 | 原因 | 判据落点 |
|---|---|---|
| **管理员环境下真实注册任务**（`Register-ScheduledTask` → `Get-ScheduledTask` 回读 → `-Uninstall` 清理） | 本机 shell **非提权**（zero 在 Admins 组但当前会话未提权），脚本按要求**不自行提权**、不动 UAC | 用户在管理员 PowerShell 执行一次即可取得；命令已在脚本输出与 FAQ 表中给出。**这是本条目唯一的收口缺口** |
| 「失败重启」自愈的真实验证（杀进程 → 1 分钟内被拉起） | 同上（需先注册成功） | 注册成功后：`taskkill /F /PID <hub pid>` → 观察 `logs\hub-service.log` 与任务历史 |
| Linux systemd 单元文件 | 本轮范围只做 Windows（任务书如此） | 已如实写进 FAQ「Linux 未提供脚本」；需要时另立条目 |
| Docker 原生服务编排（`deploy/compose` 里带 restart 策略） | 本机 Docker daemon 未运行 | 现有 compose 已含 healthcheck；重启策略另有条目 |

## 五、结论

- **CD-082 主体落地**：Windows 开机自启 + 失败重启（脚本 + 权限诚实 + 幂等 + 回读确认）、启动自动迁移（默认开、失败 fail-closed、测试态跳过）。**收口缺口 = 管理员环境的注册实证**（本轮取不到，判据已写明落点）。
- **CD-088 收口**：FAQ 不再承诺不存在的服务化能力；改为实际命令 + 「需管理员执行一次」+ Linux 未提供；顺带把「不需要专职 IT」改成「基本不需要」并补两条如实说明。
