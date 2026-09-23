# CD-090 镜像瘦身（torch CPU 轮）· 验收记录（运维轮 · 2026-09-23）

## 执行方与背景

- 基线：`6e81fa9`（worktree `E:\xingshu-wt-cd090`，分支 `ops-cd090`）
- 派单：`E:\星枢-待办\星枢任务书-2026-09-23\T2-CD090-镜像瘦身-任务书.md`
- **执行方 = Hermes 自实现（无外部执行方）**：第一次派 kimi-code 在取证阶段被 **5 小时配额 403** 打断，**零产物**（worktree 干净、HEAD 未动）；按纪律保留原任务书，本轮由 Hermes 直接接管执行（用户指令「C90你做」）。
- **注意口径**：本条目的**改前/改后判据不是 pip 的「Would install」列表**——pip 在 Windows 宿主上**不按 Linux 平台标记求值**，所以 nvidia-* 在改前也不会出现在本机解析结果里。判据改为：**torch 轮的 Requires-Dist 声明 + 各 CUDA 依赖轮的真实体积 + 实际解析到的 torch 轮变体**（三条都是可复跑的硬事实）。

## 一、基线缺陷（台账 CD-090）

`docker images` 实测 `xingshu-hub:cd076` **disk 10.1 GB / content 3.14 GB**。原因链 `requirements-vector.txt` → `sentence-transformers` → `torch`：**torch 的默认（PyPI）Linux 轮声明了 5 个 `platform_system=="Linux"` 的 CUDA 依赖**，而 Hub 的向量嵌入在本部署里走 **CPU 路径**，这些字节纯属白付（交付包体积 / 首次 pull 时间 / 落盘 / 冷启动）。

## 二、改动（1 文件，`Dockerfile` 与 CI **无需改**）

`requirements-vector.txt`：在「向量数据库」段前插入一段注释 + 一行索引指令

```
--extra-index-url https://download.pytorch.org/whl/cpu
```

- pip 会读取 requirements 文件内的索引指令（**实测已生效**，见 §3.3），因此 `Dockerfile` 的 `pip install -r requirements.txt -r requirements-vector.txt` 与 CI 两条 job 的同一命令**都不用动**——**Dockerfile 未改、ci.yml 未改**。
- 回退：删掉该行即回到 CUDA 轮（注释里已写明）。
- 该指令在同一 pip 调用里是全局的（pip 语义），实测对核心依赖解析**无影响**：全量解析包数 135、`pycrdt 0.14.6` / `chromadb 1.5.9` / `fastapi 0.136.3` 与改前一致（§3.3 D）。

## 三、验收证据（全部可复跑）

### 3.1 torch 轮的依赖声明对照（权威来源：PyPI JSON / CPU 索引 METADATA）

```
$ curl -sL https://pypi.org/pypi/torch/2.14.0/json        # PyPI（CUDA）轮
torch 2.14.0 requires_dist 共 17 条；其中 nvidia/triton: 5
   - nvidia-cudnn-cu13==9.24.0.43; platform_system == "Linux"
   - nvidia-cusparselt-cu13==0.8.1; platform_system == "Linux"
   - nvidia-nccl-cu13==2.30.7; platform_system == "Linux"
   - nvidia-nvshmem-cu13==3.4.5; platform_system == "Linux"
   - triton~=3.8.0; platform_system == "Linux" and python_version < "3.15"
   torch-2.14.0-cp314-cp314-manylinux_2_28_x86_64.whl   size = 528.9 MB

$ curl -sL https://download.pytorch.org/whl/cpu/torch/ && <取其 PEP 658 metadata>
torch-2.14.0+cpu-cp314-cp314-manylinux_2_28_x86_64.whl  size = 187.2 MB
   Requires-Dist 共 10 条；含 nvidia/triton/cuXX 的: 0
```

**差异 = 一条 `--extra-index-url` 换来：torch 本体 −341.7 MB，外加 5 个 CUDA 依赖整包不再安装。**

### 3.2 CUDA 依赖轮的真实体积（PyPI JSON `size` 字段）

| 包 | 版本 | 轮体积 |
|---|---|---|
| nvidia-cudnn-cu13 | 9.24.0.43 | **527.5 MB** |
| triton | 3.8.0 | **236.6 MB** |
| nvidia-nccl-cu13 | 2.30.7 | **206.0 MB** |
| nvidia-cusparselt-cu13 | 0.8.1 | **162.3 MB** |
| nvidia-nvshmem-cu13 | 3.4.5 | **57.6 MB** |
| **合计** | | **≈ 1190.0 MB（1.16 GB）** |

**改动收益（可核对的硬数字）**：wheel 下载量 **1190.0 + 341.7 ≈ 1531.7 MB ≈ 1.50 GB** 不再进入 Linux 镜像；
（对照台账 CD-076 的实测：镜像 disk 10.1 GB / content 3.14 GB，其中 nvidia 段约 3 GB 属**更早版本 torch** 的声明集合——本轮按当前 torch 2.14.0 的实测声明重算。）

### 3.3 改动后解析（pip dry-run，`--report` 结构化输出）

```
A) 原生口径：python -m pip install --dry-run --ignore-installed --report after-native.json -r requirements-vector.txt
   → after-native:   包数=120  nvidia/triton=0  torch=2.14.0+cpu  chromadb=1.5.9
B) Linux 平台模拟（--platform manylinux_2_28/2_27/2_17/2014_x86_64 + cp314）：
   → after-linux-emu: 包数=120  nvidia/triton=0  torch=2.14.0+cpu  chromadb=1.5.9
C) 改前同口径：baseline-native:  包数=120  nvidia/triton=0  torch=2.14.0   ← 注意：torch 是 CUDA 版
D) 核心 + 向量全量（验证修法不破完整安装路径）：
   → 包数=135  torch=2.14.0+cpu  pycrdt=0.14.6  chromadb=1.5.9  fastapi=0.136.3  nvidia/triton=0
```

**关键读法**：改前/改后在**包数上一致（120）**，唯一差异是 **torch 从 `2.14.0`（CUDA 轮）变成 `2.14.0+cpu`** —— 说明修法只换了 torch 的 wheel 变体，没有改变其它任何依赖（装通性未变）；而 CUDA 字节的**存在性**由 §3.1 的依赖声明证明（pip 在 Windows 宿主不按 Linux 标记求值时不会把它列出来，这是 pip 的口径限制，不是「没有」）。

### 3.4 Dockerfile / CI 一致性

- `Dockerfile` **未改**：`RUN pip install --no-cache-dir -r requirements.txt -r requirements-vector.txt` 会读取 requirements 文件内的索引指令（§3.3 已证生效）。
- `.github/workflows/ci.yml` **未改**：两条 job 装的是同一组 `-r` 文件，自动继承本修法。
- 旧前提勘误（任务书 §2 曾写「本机 torch 2.12.0+cpu 说明 CPU 索引有 cp314 轮」）：本机那版是历史安装态，**不能当证据**；本轮用 CPU 索引页 + PEP 658 METADATA 实测确认 `torch-2.14.0+cpu-cp314` 轮确实存在。

## 四、未实测项（判据落点写清）

| 未实测 | 原因 | 判据落点 |
|---|---|---|
| **修后镜像的真实 disk/content 体积** | 本机 Docker daemon 未运行（`npipe:////./pipe/dockerDesktopLinuxEngine` 不存在）；未擅自启动 Docker Desktop | ① Docker 可用时本机跑 `docker build -t xingshu-hub:cd090 .` + `docker images`；② CI `docker-build` job **首次运行**（GitHub 443 不可达、本地 17 个 commit 未推送 → 取不到） |
| 镜像内 `pip list \| grep -i nvidia` 归零 | 同上 | 同①（构建后进容器查一遍即可闭环） |
| 真实安装（非 dry-run）验证 torch 是 CPU 版 | 任务书禁止污染本机 site-packages；隔离 `--target` 实装需下载 ~1GB 且本机 PyPI 连接不稳（实测多次 `ConnectionReset 10054`） | 镜像构建后 `python -c "import torch;print(torch.__version__)"` 应含 `+cpu` |

## 五、方法论备注（不是缺陷，供下次省时间）

1. **pip 的 `--platform` 是精确标签匹配**：只写 `--platform manylinux_2_28_x86_64` 会导致纯 Python 轮之外的包全部「找不到」（实测 chromadb/pycrdt 全灭）；必须给一串平台（`manylinux_2_28/2_27/2_17/2014_x86_64` + `linux_x86_64`）。
2. **pip 不按目标平台求值环境标记**：`platform_system == "Linux"` 的依赖在 Windows 宿主上永远不出现 → 想证明「Linux 镜像会拉 CUDA」，**只能读轮自身的 Requires-Dist 声明**，不能靠本机解析结果。
3. **镜像站会 403 掉裸 urllib**（清华 403）；取证统一走 `curl` + 原生 Windows 临时路径（MSYS `/tmp` 给 Windows python 不认）。

## 六、结论

- **CD-090 主体落地**：一行 `--extra-index-url https://download.pytorch.org/whl/cpu` 让 Linux 镜像里的 torch 从 CUDA 轮（528.9 MB + 5 个 CUDA 依赖 ≈ 1.19 GB）换成 CPU 轮（187.2 MB，零 CUDA 依赖），**收益 ≈ 1.50 GB 的 wheel 体积**；`Dockerfile` 与 CI 零改动；解析包数不变（装通性不受影响）。
- **收口缺口 = 修后镜像体积未实测**（Docker daemon 未运行 / CI 首跑取不到），判据落点已写明。
