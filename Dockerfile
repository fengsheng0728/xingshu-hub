FROM python:3.11-slim

WORKDIR /app

# 先复制 requirements 装依赖，再 COPY 全量代码 —— 利用 Docker 层缓存，
# 代码变更不触发依赖重装；requirements.txt 是依赖单一事实源（T0-4 已钉版）。
# D-5 3-5a：依赖分层后镜像仍装全量（核心 + 可选向量栈），行为与分层前一致。
COPY requirements.txt requirements-vector.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-vector.txt

COPY . .

EXPOSE 3060

# main.py 无模块级 app 导出，全部启动逻辑（含 init_replay_store 接线）在
# __main__ 内并自行 uvicorn.run(app) —— 必须走 python main.py 启动路径。
CMD ["python", "main.py"]
