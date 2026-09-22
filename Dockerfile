FROM python:3.14-slim

WORKDIR /app

# 先复制 requirements 装依赖，再 COPY 全量代码 —— 利用 Docker 层缓存，
# 代码变更不触发依赖重装；requirements.txt 是依赖单一事实源（T0-4 已钉版）。
# D-5 3-5a：依赖分层后镜像仍装全量（核心 + 可选向量栈），行为与分层前一致。
COPY requirements.txt requirements-vector.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-vector.txt

COPY . .

# CD-076（2026-09-22）构建期门禁：新控制台产物必须在构建上下文里（故本检查必须在 COPY . . 之后）。
# 缺失后果：镜像内只有旧 dashboard/（单页），交付出去的控制台缺 12 页含身份供给页
# —— 缺的就是交付物本体，故这里 exit 1，而非像 build_hub.py:58 那样只打 [WARN]
# （exe 场景缺 dashboard_dist 时至少还有旧首页可用，镜像场景没有替代品）。
# 前置：在 hub_ui/ 跑 `npm install && npm run build`（产物落 ../dashboard_dist）。
RUN test -f dashboard_dist/index.html \
    || (echo "[ERROR] 缺少 dashboard_dist/index.html —— 新控制台未构建。" >&2; \
        echo "        请先在 hub_ui/ 执行 npm install && npm run build，再重建镜像。" >&2; \
        exit 1)

EXPOSE 3060

# main.py 无模块级 app 导出，全部启动逻辑（含 init_replay_store 接线）在
# __main__ 内并自行 uvicorn.run(app) —— 必须走 python main.py 启动路径。
CMD ["python", "main.py"]
