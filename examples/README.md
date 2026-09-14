# examples/ —— 演示 / 展示 / 示例资产

本目录收编星枢 Sync Hub 的**演示与展示资产**。它们**不是 Hub 运行必需**：
整目录删除不影响 Hub 启动与任何 API（唯一例外是 `showcase/`，见下）。

| 资产 | 说明 | 运行前提 |
|------|------|----------|
| `client.py` | Agent SDK Python 客户端（含 `--demo` 自导演示） | Hub 在线（默认 :3060）+ `pip install aiohttp` |
| `demo_workflow.py` | 一页纸端到端演示（7 幕：注册→记忆→披露→任务→通知…） | Hub 已在 3060 运行 + `pip install requests` |
| `seed_data.py` | 演示数据播种（向本地库写入示例 Agent/记忆/任务） | 无外部依赖（stdlib only） |
| `showcase/` | 销售落地页，由 Hub 的 `/showcase` 路由直出 `./examples/showcase/index.html`，**文件必须留在仓库内**（打包 datas 亦携带） | 随 Hub 启动即可访问 `/showcase` |
| `arch-site/` | 加法版架构展示站（React + Node 边车 :7101），独立于 Hub 运行 | `cd arch-site && npm install && npm run dev` |

## 快速体验

```bash
# 1. 启动 Hub（仓库根目录）
python main.py

# 2. 另开终端，运行端到端演示
python examples/demo_workflow.py

# 或用 SDK 客户端自导演示
python examples/client.py --demo
```

`client.py` / `demo_workflow.py` 均为自包含脚本（仅 stdlib + `aiohttp`/`requests`），
移动目录不影响其 import。
