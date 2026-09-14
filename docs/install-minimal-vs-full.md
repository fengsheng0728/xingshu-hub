# 安装方式：最小（核心） vs 全量（含向量栈）

D-5 3-5a（2026-09-10）起，依赖分为两层：

- `requirements.txt` — 核心依赖（Web 框架 / 数据模型 / SQLite / MCP / CRDT 等）
- `requirements-vector.txt` — 可选向量栈（chromadb / sentence-transformers / langchain，合计约 2GB）

## 最小安装（核心）

```bash
pip install -r requirements.txt
```

Hub 可正常启动（chromadb 缺失不再导致 `import hub_core` 失败）。能力差异：

| 能力 | 最小安装行为 |
|---|---|
| 语义检索（ChromaDB） | 不可用，自动降级为 SQLite 关键词检索（CD-016 既有降级路径，消费点全部判 `_chroma_collection is None`） |
| 真语义嵌入（`EMBEDDING_PROVIDER=sentence`） | 不可用；默认 `hasher` provider 为零依赖词袋嵌入，不受影响 |
| Hub Agent LLM 对话（`/api/v1/hub-agent/chat`、`/chat/clear`、`/chat/history`） | 返回 `{"status":"degraded","error":"LLM 栈未安装（langchain 缺失），请 pip install -r requirements-vector.txt"}`，不再是 500 堆栈 |

其余功能（注册/心跳/任务/披露/通知/审计/团队/Wiki/MCP 等）与全量安装一致。

## 全量安装（核心 + 向量栈）

```bash
pip install -r requirements.txt -r requirements-vector.txt
```

恢复全部能力：ChromaDB 语义检索、`sentence` provider 真语义嵌入、Hub Agent LLM 对话链。

## CI / Docker

CI（`.github/workflows/ci.yml`）与 Docker 镜像（`Dockerfile`）均安装两套依赖，行为与分层前一致。
