---
title: 能不能先试用一下再决定？
category: faq
tags: [试用, 部署, 演示, 上手]
source: FAQ.md#L140-L160
---

# 能不能先试用一下再决定？

**可以。** 部署和试用流程：

```bash
# 1. 下载
git clone <项目地址>
cd sync-hub-case

# 2. 安装依赖
pip install fastapi uvicorn aiohttp numpy websockets

# 3. 启动 Hub
python main.py

# 4. 另开终端，启动演示
python examples/client.py --demo
```

全程不需要配数据库、不需要注册账号、不需要联网。
如果觉得好用再决定要不要正式用。
