# 安全底线 P2 验收表 — 联邦 AES-GCM 加密信道

> 方案：《星枢-安全底线-执行方案.md》P2（80 分钟预算）
> 实际耗时：约 120 分钟（含 async 缺失 await 等 3 个迭代修复）
> commit：`8f899a2`

## 验收用例（双 Hub 实测 11/11 + 单测 5/5）

| 用例 | 方法 | 结果 | 证据 |
|------|------|------|------|
| T2-1 加密信道真实路径 | 双 Hub（生产 3060 + 独立 3064）配对实操 | ✅ | worker 机密不可见（规则7，disclosed_count=0）；白名单可见（规则2，count=1）；假 key 403 —— 7/7 原有用例在加密信道保持 |
| T2-2 抓包不可读 | 密文结构验证 | ✅ | 请求体仅 nonce+ciphertext（len=427，无任何明文）；`X-Hub-Crypto: v1` 头；shared_secret 不暴露于任何 API（DB 直读验证） |
| T2-3 防重放 | 原样重放攻击 | ✅ | 首次解密通过（200）→ 同 nonce 原样重放 → 403 + `proxy_disclose_rejected` 审计（5 条实测） |
| T2-4 未配对伪造 | 第三 Hub 直调 + 构造攻击 | ✅ | 未配对明文直调 403；未配对加密伪造（乱密文）403；错密钥构造 403；均有审计 |
| T2-5 回归 | pytest | ✅ | Hub 163（158+5）/ Agent 118 全绿 |

## 实现要点

1. **shared_secret 落库**：team_members 加列（DDL + 增量迁移）；配对握手双方（accept_pairing 接受方 + _handle_pair_exchange 响应方）各自持久化 HKDF session_key（64 hex），重配对时 UPDATE 刷新——解决「密钥只在握手时临时存在，不落库」的现状
2. **fed_crypto.py**：`encrypt_payload`（AESGCM + 12B 随机 nonce）、`decrypt_payload`（解密 + 防重放登记）、`check_replay`（nonce 缓存 5min 窗口）
3. **调用端 disclose/remote**：有 shared_secret → 加密信道（body=密文，requester_agent_id 走 query 仅作密钥路由，不泄露披露内容）；无（旧配对数据）→ 降级明文 Bearer（向后兼容）
4. **被调端 proxy/disclose 双模式**：
   - `X-Hub-Crypto: v1` → 解密；信任锚点=配对关系（requester 的 shared_secret 只在配对双方间存在）；解密后 requester 与 query 一致性校验防伪造
   - 无头 → 兼容旧 Bearer remote_api_key
   - 所有拒绝路径（解密失败/重放/未配对/错密钥）→ 403 + `proxy_disclose_rejected` 审计事件
5. **exchange 加入 allowlist**：6 位配对码自认证（跨 Hub 调用，无本地 api_key），同 register 引导端点语义

## 迭代修复记录（T2 实操抓出）

1. **exchange 被中间件 401**：配对握手用配对码当 Bearer，不在 allowlist → 加入
2. **Starlette body 只读一次**：`_locate_shared_secret` 内 `await request.body()` 消费了 body，外层解密拿空 → 移除冗余读取（requester 走 query）
3. **async 缺失 await**：`decrypt_payload(_locate_shared_secret(request), enc)` 传了 coroutine 对象 → TypeError → 403；补 await
4. **加密分支未定义虚拟身份变量**：解密成功后 remote_agent_id/local_role 未赋值 → 500 UnboundLocalError → 补配对表查询
5. **重复跑残留**：A 侧历史配对记录干扰（INSERT OR IGNORE 不更新旧记录）→ 验证脚本开头 DELETE 清理

## 台账更新

- CD-013（MCP 无认证）→ 仍待做（不在本轮范围）
- 新增：配对后 shared_secret 落库为明文 SQLite——内网信任域内可接受（与 api_key 同级），异地部署需整库加密（登记为后续债）

## 本阶段明确不做（已遵守）

- 不引入 TLS/证书/HTTPS；不做密钥轮换协议（重新配对即轮换）；不加密本机 loopback/Agent↔Hub WS；不做联邦自动同步
