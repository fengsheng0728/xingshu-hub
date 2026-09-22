# CD-076 部署面收口 —— 验收记录

> 台账项：`carried_debts.md` CD-076（对照 Tencent/WeKnora 复盘 2026-09-22，A 档·部署，严重度=高）
> **轮次名：升龙**（用户命名，2026-09-22；用于口头指代「升龙那条」）
> 状态名：**已收口**（2026-09-22 下午：本机 Docker engine 已可用，CI 的三项判据全部在本地取到真证据，见 §2/§3/§6/§8）
> 执行：Hermes（**无外部执行方**——kimi 月度配额 403 整周期废，claude/codex 未安装，
> opencode 为按量 API 故未派；任务书留存 `.hermes/T-CD076-任务书.md` 与
> `E:\星枢-待办\星枢任务书-2026-09-22\T-CD076-部署面收口-任务书.md`，本记录逐条对任务书核验）
> 日期：2026-09-22

## 0. 台账原文的两处不实（本轮实测修正，均已反映到代码/文档）

| 台账原文 | 实测 | 处置 |
|---|---|---|
| 「`.dockerignore` 已把 `chroma_db/` `wiki/` `audit/` `ystore.db` **`config/`** 排除在镜像外」 | 前四项属实；`config/` **不在** `.dockerignore`（`grep -n config .dockerignore` 零命中），而 Dockerfile 是 `COPY . .` → 生产 `config/config.yaml`（含真实 `auth.hub_token`，部署级全权凭据）会被烤进镜像 | `.dockerignore` 补 `config/`（T1-d） |
| 「版本号 `routes_server.py:106/252` 硬编码 2.0.0」 | 还有**第三处** `routes.py` 的 FastAPI `app(version=...)` | 三处统一改为 `models.HUB_VERSION` 单一来源（T4） |

## 1. T1 compose 数据卷收口

**现状（修改前实测）**：`volumes` 仅 `hub-data:/app/data` + `./main.py:/app/main.py` + `./dashboard:/app/dashboard`，**无 `environment` 段**；而 `DB_PATH="./sync_hub.db"`、`CHROMA_PATH="./chroma_db"`、`AUDIT_DIR/WIKI_ROOT/WORKSPACE_YSTORE_PATH` 空（= 仓库内路径）、`SYNC_HUB_CONFIG_DIR` 默认 `./config`，容器 `WORKDIR /app` → 数据实际落 `/app/*`，**不在任何卷里**，且多被 `.dockerignore` 排除（也不在镜像里）。

**改法**：六类运行期数据经 `SYNC_HUB_*` env 全部指到 `/app/data`（该 env 覆盖通道已存在、优先级高于 config.yaml，见 `models.py` 的覆盖段，**未改业务代码**）；删除两个宿主源码 bind mount；删除废弃的 `version: '3.8'`；`.dockerignore` 补 `config/`；`HUB_PORT` / `HUB_CONTAINER_NAME` 可覆盖（演练不占生产 3060）。

**验收证据（本机真跑，`docker compose config` 不需要 daemon）**

先红（改前）——存档 `E:\星枢-待办\_sync\cd076_prefix_compose_config.txt`：

```
level=warning msg="...docker-compose.yml: the attribute `version` is obsolete..."
    volumes:
      - type: volume
        source: hub-data
        target: /app/data
      - type: bind
        source: E:\sync-hub-case\main.py
        target: /app/main.py
      - type: bind
        source: E:\sync-hub-case\dashboard
        target: /app/dashboard
（无 environment 段）
```

后绿（改后）——存档 `..._sync\cd076_postfix_compose_config.txt`，断言全部命中：

```
docker compose config  → rc=0
environment:  SYNC_HUB_DB / SYNC_HUB_CHROMA_PATH / SYNC_HUB_AUDIT_DIR
              SYNC_HUB_WIKI_ROOT / SYNC_HUB_YSTORE_PATH / SYNC_HUB_CONFIG_DIR   （各 1 命中，值均为 /app/data/...）
volumes:      仅 1 条 —— hub-data → /app/data
宿主 bind:    0（main.py / dashboard 均无）
version obsolete 警告: 0
```

## 2. T2 持久化演练（重建容器后数据仍在）

新增 `scripts/deploy_persistence_drill.sh`：起容器（独立 project `xingshu-drill` + 端口 3077）→ 卷内自造测试 `config.yaml` → 写 agent/memory/knowledge/wiki → `compose down`（**严禁 `-v`**）→ 再起 → 读回断言（memory 读回同时证明 agents 行与凭据仍在；knowledge 读回用卷内 `hub_token`，同时证明 config 在卷里）→ 反向断言 `/app` 根下**不得**有 `sync_hub.db/chroma_db/wiki/audit/ystore.db/config`。
`.github/workflows/ci.yml` 的 docker job 增加：先 `npm ci && npm run build`（构建新控制台产物）→ `docker build` → `compose config` → **真跑本脚本**（`DRILL_IMAGE=xingshu-hub:ci` 复用镜像，不二次 build）→ 失败时导出容器日志。

**已实测**：`bash -n` 语法通过；本机试跑按设计走「daemon 不可用 → exit 2（不是 PASS，也不是被测代码失败），判据在 CI」分支（原始输出见提交说明）。

**已实测（2026-09-22 下午，真跑 PASS）**：`DRILL_IMAGE=xingshu-hub:cd076 bash scripts/deploy_persistence_drill.sh`
→ **`DRILL_EXIT=0`**，13 项 `[OK]` / 0 项 `[FAIL]`：

```
0 前置：docker daemon 可用
1 卷内 config.yaml 写入成功（不是镜像内、不是宿主 bind）
2 容器已起（project=xingshu-drill, 宿主端口 3077）
3 /health 就绪（第 2 次轮询）
4 agent 注册拿到 api_key → memory 写入 → knowledge 写入 → wiki sync 触发
5 docker compose down（不带 -v）：容器已销毁 / 卷仍在 xingshu-drill_hub-data
6 重建容器后 /health 就绪
7 读回：memory 仍在（证明 agents 行 + 凭据也在）· knowledge 仍在（证明卷内 config 的 hub_token 生效）
8 反向断言：/app 根下无数据落点；卷内实见 /app/data/sync_hub.db + /app/data/chroma_db
结论 PASS：容器销毁重建后数据仍在（CD-076 核心判据成立）
```

原始日志：`E:\星枢-待办\_sync\cd076\drill-run-r4.log`（末行 `DRILL_EXIT=0`）。

**本次真跑暴露了 4 处脚本缺陷**（脚本此前从未跑出 PASS，缺陷被「daemon 不可用」整段掩盖；
逐条与修法见 §8）——这正是「判据必须真跑一次、不能只审读」的实证。

## 3. T3 基础镜像对齐

`Dockerfile`：`python:3.11-slim` → `python:3.14-slim`（`requirements.txt` 头部自订钉版基准为 Python 3.14）；`requirements.txt` 那条「Dockerfile 中的旧 pin（fastapi==0.104.1 等）需另行对齐」的**过期注释**更正（现状 Dockerfile 无任何 pip pin）。新增构建期门禁：缺 `dashboard_dist/index.html` → `[ERROR]` + `exit 1`（缺的是交付物本体；与 `build_hub.py` 只打 `[WARN]` 的区别及理由写在 Dockerfile 注释里）。

**已实测（2026-09-22 下午）：3.14 装通性成立，`docker build` rc=0。** 本机 Docker engine 可用后
真跑 `docker build -t xingshu-hub:cd076 .`：

```
DOCKER_BUILD_EXIT=0      （日志 E:\星枢-待办\_sync\cd076\docker-build.log）
pip install -r requirements.txt -r requirements-vector.txt → Successfully installed …（447.3s）
  关键传递依赖拿到 cp314 wheel：torch-2.14.0 / chromadb-1.5.9 / tokenizers-0.23.2 /
  regex-2026.9.10 / xxhash-4.0.1 / httptools-0.8.0 / safetensors-0.8.0(abi3)
构建期门禁（dashboard_dist/index.html）通过 → #10 DONE
容器内实测：python 3.14.7 / torch 2.14.0+cu130 / chromadb 1.5.9
  （docker run --rm --entrypoint python xingshu-hub:cd076 -c "import sys,chromadb,torch;…"）
```

故**不回退** 3.12-slim：Python 3.14 与 `requirements.txt` 的自订钉版基准一致，装通性有实测。
退路条款保留（若 CI 环境装不通再回退并登记，不退化成「假装对齐」）。

## 4. T4 版本号统一 + exe 重打

`models.py` 新增单一来源 `HUB_VERSION = "2.1.0"`；`routes.py`（FastAPI app）、`routes_server.py`（`/health`、`/healthz`）三处改引用。

**验收证据（本机全跑通）**

- `python -c "from routes import app"` → `app.version = 2.1.0`（CI 同款 import 红线）
- 起本地 Hub（**隔离库/隔离数据目录**，不碰生产 `./sync_hub.db` 的 29 Agent）：
  `GET /health` → `{"status": "ok", "version": "2.1.0", ...}`；`GET /healthz` → `{"status":"alive","version":"2.1.0",...}`
- `python build_hub.py` 重打 exe → `dist/星枢 Hub.exe`（307 MiB，13:34）**起 exe 实测**：
  `GET /health` / `GET /healthz` 均返回 `version = 2.1.0`
  （起 exe 需先绕开打包版防火墙硬门：`dist/config/config.yaml` 写 `server.host: 127.0.0.1` —— 该硬门是既有设计，见《使用手册》§1.3，非本次引入）
- 包内旧 Agent 兼容分支已随重打消除（exe 时间戳晚于 `5a13133`）

## 5. 全量回归

口径同《使用手册》§11 的离线全量集：

```
python -m pytest tests/ -q -k "not test_cross_agent_403 and not test_memory_search_self_access
  and not test_memory_list_self_access" --ignore=tests/test_team_integration.py
```

- 第一轮：`1 failed, 1026 passed, 60 skipped, 5 deselected, 1 xfailed`（333.58s）
- 失败项 = `tests/test_code_hygiene.py::test_h3_no_stale_line_refs`，**由本次改动引入**：`models.py` 的新注释里写了
  `routes.py:77` / `routes_server.py:106` 形式的行号引用，被 H-3 门禁拦下（「全仓无 `.py:行号` 形式引用」）——门禁口径正确，按其要求改成符号名引用
- 第二轮（修注释后）：**`1027 passed, 60 skipped, 5 deselected, 1 xfailed, 0 failed`（329.75s，PYTEST_RC=0）**
- 计数差说明：手册「全量回归 1034 passed」是 CD-075 之前的口径；CD-075 删了 `tests/test_l1_dedup.py`（9 例）并把
  `TestL0T3` 由 3 例改 5 例 → 1034 − 9 + 2 = **1027**，与本次实测一致。
- 日志：`E:\星枢-待办\_sync\cd076-full-regression.log`（第一轮）、`...-2.log`（第二轮）

## 6. 判据清单（2026-09-22 下午：全部已实测）

| 项 | 状态 | 证据 |
|---|---|---|
| 容器内持久化演练（T2 核心） | **PASS（`DRILL_EXIT=0`，13 [OK] / 0 [FAIL]）** | `E:\星枢-待办\_sync\cd076\drill-run-r4.log` |
| `docker build` | **rc=0** | `...\cd076\docker-build.log`（末行 `DOCKER_BUILD_EXIT=0`） |
| 演练脚本在真实容器里的行为 | **真跑过**——r1/r2/r3 连续失败、暴露并修掉 4 处缺陷（§8），r4 PASS | `...\drill-run.log` / `-r2` / `-r3` / `-r4` |
| `python:3.14-slim` 装通性 | **成立** | 容器内 `python 3.14.7` / `torch 2.14.0+cu130` / `chromadb 1.5.9` |
| `docker compose config`（CI 第 2 步） | **rc=0** | 本机真跑 |

**本机 Docker 从「不可用」到「可用」的处置（2026-09-22 下午）**：原根因（已查实，非猜测）是
Windows「虚拟机平台」组件未启用。**用户启用该组件并重启后**：`wsl -l -v` 显示 `Ubuntu` /
`docker-desktop` 两个 WSL2 发行版（均 Stopped）；启动 Docker Desktop → `docker info` 返回
Docker Desktop 4.72.0（Engine 29.4.2）→ 本机自此具备 docker build / 演练的实跑能力。
故 CI 的 docker job 从「唯一实跑点」变成「第二实跑点 + 回归保障」（`ci.yml` 注释已同步更正）。

## 7. 遗留（未在本轮动，登记备查）

- **`replay_nonce.db` 落点**：`main.py` 按 `dirname(CONFIG.DB_PATH)` 派生 → 配了 `SYNC_HUB_DB=/app/data/sync_hub.db` 后它会进卷，方向正确；但代码里仍是「cwd 相对」的隐含假设（本轮隔离实例实测落在 WORKDIR 一侧）。联邦防重放 nonce 库若落容器可写层，重建即丢。演练脚本第 8 步的反向断言会抓这种落点；建议另立台账条目（改 `main.py` 派生逻辑或补显式 env）。
- **前端产物与构建耦合**：`dashboard_dist/` 被 `.gitignore` 忽略，新控制台的唯一来源是构建产物 → 换机/CI 构建镜像前必须先构建前端（已在 Dockerfile 门禁 + README + 使用手册三处写明）。彻底解耦（如镜像内多阶段构建前端）属另一轮。
- **镜像体积 10.1 GB（本轮实测新增，已登记 CD-090）**：`docker images` 实测 `xingshu-hub:cd076` disk=10.1 GB / content=3.14 GB。原因是 `requirements-vector.txt` 的 sentence-transformers → torch 拉的是**默认带 CUDA 的 manylinux 轮**（`nvidia-cublas` / `cudnn-cu13` / `cufft` / `nccl-cu13` / `triton` 等十余个 `nvidia-*` 包，单个 cudnn 轮就 553 MB），而 Hub 的嵌入在容器里只跑 **CPU**。修法方向：装 torch 时给 CPU 索引（`--index-url https://download.pytorch.org/whl/cpu` 或 `--extra-index-url` + CPU 轮）→ 预计降到数百 MB 级。属部署面成本项，本轮不动（改依赖钉版会同时影响本机与 CI 两条安装路径，须单独立项验证）。
- **`ystore.db` 未在演练覆盖**：第 8 步反向断言的 `INVOLUME` 实测只见到 `sync_hub.db` 与 `chroma_db`——`ystore.db` 要在协作房间（YRoom）真正起过之后才落盘，本次演练未起房间。`/app/ystore.db` 的「不许落在可写层」断言仍然有效（真出现即 FAIL），只是本轮没被触发。同族遗留见上文 `replay_nonce.db` 那条。

## 8. 演练脚本的四处缺陷（本机首次真跑才暴露，均已修 + 已实证）

脚本此前**从未跑出过 PASS**，缺陷被「daemon 不可用 → exit 2」整段掩盖：先做的那轮审读（语法 + 依赖的 API
契约核对 + 逻辑走查）**一处都没抓到**。四处按暴露顺序逐个定位、逐个修，修完 r4 才 PASS：

| # | 症状（实测原文） | 根因 | 修法 |
|---|---|---|---|
| 1 | `unknown flag: --no-build` → 第 1 步即 FAIL | `docker compose run` 在 Compose v5.1.3 **没有** `--no-build`（只有 `up` 有） | run 那处去掉 `NO_BUILD` 数组 + 注释说明差异 |
| 2 | `open E:\tmp\drill-override-qPRLj7.yml: The system cannot find the file specified` | `mktemp -t` 返回 MSYS 的 `/tmp/...`，当**参数**交给原生 `docker.exe` 被路径转成 `E:\tmp\...`（文件实际不在那儿） | 改用**仓库根下相对文件名**（脚本已 `cd` 到仓库根）——bash 与 docker.exe 双方都成立 |
| 3 | 注册步 `curl: (22) … 400` | register 调用**没带引导凭据**；而 `routes.py:221-224` 明写「配了 `auth.hub_token` 后 register/bootstrap 必须带 hub_token」，否则 401 | register 补 `-H "Authorization: Bearer ${TOKEN}"`（用脚本自己写进卷的那把 token） |
| 4 | 同上 400，正文 `{"detail":"There was an error parsing the body"}` | **MSYS 的 curl 把内联 `-d` 里的中文转成 GBK 字节**发出 → FastAPI 收到非法 UTF-8 JSON。探针三连各自实证：同内容 ASCII 内联 = 200 / 中文走 UTF-8 文件 = 200 / 中文内联 = 400 | step 4 整块重写：请求体经 `body()` 以 UTF-8 字节落盘 + `--data-binary @file`（CI 的 Linux 下同样成立） |

**误判纠错（一并记档）**：修 #3 之前，我一度把脚本里 7 处 `Bearer ${TOKEN}` / `${AGENT_KEY}` 读成
`Bearer ***`，差点当作「占位符残留」去批量改。**那是 Hermes 工具输出侧的凭据掩码，不是文件内容**——
改用 `raw.count("${TOKEN}")` / `ord()` 这类**不可被掩码的取证方式**核对后，确认原文件本来就正确；
带着错误前提的修改脚本连同其断言一起废弃、**未落盘**。结论：**判定凭据类缺陷时，「读出来的样子」不算证据。**
