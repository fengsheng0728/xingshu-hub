# P0-2 内网组队协同 — 安全设计文档

> 状态：待评审 | 预估：10-12h | 依赖：无（独立模块）

---

## 一、目标

把星枢的信任模型从单 Hub 扩展为联邦：两台电脑各自的 Hub 互相认证后，跨 Hub 的信息披露对 Agent 端透明——查同事记忆就像查自己的一样，底层由披露引擎统一裁决。

---

## 二、威胁模型

| 威胁 | 攻击者 | 危害 | 对策 |
|------|--------|------|------|
| 伪造邀请 | 同 LAN 任意设备 | 伪装成"同事"，骗取用户接受配对 | 配对码（人眼确认） |
| 嗅探握手 | 同 LAN 抓包 | 截获明文 api_key | 配对码 + X25519 DH 派生 session_key，离线爆破不可行 |
| 恶意内部人提权 | 已配对的对方 Hub 操控者 | curl 声称 manager 角色查敏感信息 | 角色取自本地 team_members 表，不接受请求方声称 |
| SSRF 代理 | 已配对的恶意 Hub | 通过 remote_hub_url 攻击内网服务 | URL 写入前白名单校验（端口强制3060+拒绝域名+仅私网） |
| 离队不撤销 | 离职员工的电脑 | 旧 api_key 仍可查询 | 双向撤销 + key 30 天有效期 + 撤销队列重试 |
| 广播不到 | 企业 AP/VLAN | 发现不到对方 | 手动输入 IP 兜底 |

---

## 三、协议设计

### 3.1 发现（Discovery）

两种模式，用户可选：

**A. UDP 广播（同网段自动）**

- Hub 启动后每 5 秒向 `255.255.255.255:<port>` 发 JSON：`{"type":"xingshu_hello","hub_id":"xxx","hostname":"HOST-01","user":"用户","port":3060}`
- 收到广播的 Hub 显示在"可发现设备"列表里
- 30 秒无心跳自动下线
- 仅在 LAN 模式（host=0.0.0.0）时启用

**B. 手动输入 IP（兜底）**

- 用户直接输入 `192.168.1.100:3060`
- Hub 向该地址发 HTTP `GET /api/v1/team/ping` 验证可达性
- 跨 VLAN/客户端隔离场景的唯一通路

### 3.2 配对握手（Pairing Handshake）

> 核心防伪机制——攻击者必须在同一房间看到屏幕上的配对码，纯网络攻击无效。

```
用户的 Hub（发起方）                    同事的 Hub（接收方）
──────────────────────────────────────────────────
① 点"邀请组队"
② GET /api/v1/team/pair/request
   → 返回 pairing_code: "847291" (6位)
③ 显示在屏幕上："配对码 847291"
                                          ④ 收到邀请推送
                                          ⑤ 点"接受"
                                          ⑥ 输入用户告诉他的 6 位码
                                          ⑦ POST /api/v1/team/pair/accept
                                             body: {code:"847291"}
⑧ 校验 code 一致
⑨ 双方各生成临时 X25519 密钥对，交换公钥
⑩ session_key = HKDF(
       DH_shared_secret + pairing_code,
       salt = hub_id_a + hub_id_b,
       info = b"xingshu-team-pairing-v1"
   )
   离线爆破者没有 DH 私钥，6 位码不再可离线攻击
⑪ 用 session_key AES-256-GCM 加密 api_key
   发送给接收方
                                          ⑫ 解密 → 存入 team_members
                                          ⑬ 用自己的 session_key 加密自己的 api_key
                                             发回给发起方
⑭ 解密 → 存入 team_members → 组队完成
```

**配对码特性：**
- 6 位数字（1/1,000,000 盲猜概率）
- 5 分钟有效期
- 3 次错误尝试后作废
- 一次性使用（配对成功或过期即销毁）

**密钥交换的安全性：**
- 配对码本身不通过网络传输（人在电话/当面说）
- 攻击者即使嗅探到加密的 api_key 密文和 DH 公钥，没有配对码无法派生 session_key
- 离线爆破的 6 位码空间被 DH 私钥保护——攻击者必须同时知道 DH 私钥和配对码，这是计算上不可行的
- session_key 不持久化，仅用于握手阶段

### 3.3 URL 安全校验（防 SSRF）

`remote_hub_url` 写入 `team_members` 表前强制校验：

```python
def validate_remote_url(url: str) -> bool:
    """仅允许 RFC1918 私网地址 + 3060 端口"""
    parsed = urlparse(url)
    
    # 1. 仅 HTTP（内网无需 HTTPS）
    if parsed.scheme not in ('http',):
        return False
    
    # 2. 端口强制 3060（无端口默认 80 → 拒绝）
    port = parsed.port or 80
    if port != 3060:
        return False
    
    # 3. 解析 IP — 拒绝域名（防 DNS rebinding）
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return False
    
    # 4. 拒绝特殊地址
    if ip.is_loopback:      return False  # 127.x — 你已配对到自己
    if ip.is_link_local:    return False  # 169.254.x — metadata 服务
    if ip.is_multicast:     return False
    if ip.is_unspecified:   return False
    
    # 5. 仅允许私网
    if not ip.is_private:   return False  # 公网 IP 拒绝
    
    return True
```

**进安全回归集：** 新增 `test_team_url_validation.py`，参数化覆盖：
- 合法：`http://192.168.1.100:3060` / `http://10.0.0.5:3060`
- 非法：`http://169.254.169.254:3060` / `http://127.0.0.1:3060` / `http://8.8.8.8:3060` / `http://192.168.1.1:80` (非标准端口)

### 3.4 跨 Hub 身份代入协议

> 这是唯一可能动到披露引擎的地方。

**请求转发格式：**

```
用户的 Hub                    同事的 Hub
──────────────────────────────────────────
用户 Agent 请求查同事记忆
  → 本方 Hub 发现 target=同事（remote）
  → 构造转发请求:

POST http://192.168.1.100:3060/api/v1/memory/disclose
Authorization: Bearer <同事分配的 remote_api_key>
Body: {
  "requester_agent_id": "fengsheng",       # 请求方真实身份（仅审计用）
  "target_agent_id": "colleague-agent",    # 被查方
  "query": "...",
  "required_level": "summary",
  "_proxied_by": "HOST-01"           # 担保方 Hub ID（仅审计用）
}
                                        → 对方 Hub 收到请求
                                        → api_key 认证通过（确认是已配对 Hub）
                                        → 查 team_members 表取本地存 role
                                        → 用本地 role 构造虚拟身份
                                        → 代入 8 条披露规则裁决
                                        → 返回结果
```

**虚拟身份构造（对方 Hub 侧）——信任锚点在本地表：**

```python
def resolve_remote_identity(proxied_by, requester_agent_id):
    """用本地 team_members 存的 role 构造虚拟身份，不接受请求方声称"""
    member = get_team_member(proxied_by)  # 查本地 team_members 表
    if not member:
        raise 403("未配对的 Hub")
    
    # 虚拟 agent 的角色来自本地存储——curl 提权不可行
    return VirtualAgent(
        agent_id=f"{proxied_by}:{requester_agent_id}",  # 带命名空间的 ID
        role=member.role,            # ← 本地表存的 role，不信请求
        department=member.department,
        managed_agents=[],           # 跨 Hub 不暴露管理关系
    )
```

**铁律：**
- 对方 Hub **永不**接受请求中携带的角色声称——role 永远从本地 `team_members.role` 取
- 虚拟 agent 不参与任务分配（不能跨 Hub 分配任务，那是深度集成的事）
- `requester_agent_id` 和 `_proxied_by` 仅作审计字段，不进裁决逻辑
- 任何 curl 直接带 `_guaranteed_role: "manager"` 的尝试——对方 Hub 忽略此字段，只看本地表

### 3.5 双向撤销 + Key 轮换

**撤销协议：**

```
我方点"移出团队"
  → DELETE /api/v1/team/members/{member_id}  （本方 Hub）
  → POST {remote_hub_url}/api/v1/team/revoke  （通知对方 Hub）
     body: { revoked_agent_id: "xxx" }
  → 对方 Hub: 标记 team_member.revoked_at = now()
  → 对方 Hub: 该 remote_api_key 立即失效（拒绝后续请求）
```

**对方 Hub 收到撤销后的行为：**
- 该 remote_api_key 的所有后续请求返回 403
- 用户在该 Hub 的"团队"页看到该成员变灰 + "已离队"标签
- 30 天后物理删除行（给误操作留恢复窗口）

**撤销的离线场景（对方 Hub 关机）：**
- 撤销通知丢失 → 对方 Hub 重启后不会自动感知撤销
- 缓解：每条 team_member 记录带 `key_expires_at` 字段（配对时设 30 天有效期）
- 超期 key 自动报废——离线撤销最多有 30 天残余窗口
- 撤销队列重试：如果 revoke POST 失败，本方 Hub 每 10 分钟重试一次，直到成功或 key 自然过期

**Key 轮换（MVP 延后，预留接口）：**
- `POST /api/v1/team/rotate-key/{member_id}` —— 重新走一次握手协议换新 key
- cron 定期提醒（30 天未轮换的 key 标黄）

---

## 四、数据模型

```sql
CREATE TABLE team_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    local_agent_id  TEXT NOT NULL,           -- 我方 Agent ID（这条关系属于谁）
    remote_hub_id   TEXT NOT NULL,           -- 对方 Hub 的唯一 ID
    remote_hub_url  TEXT NOT NULL,           -- 对方 Hub 地址（经过 URL 校验）
    remote_agent_id TEXT NOT NULL,           -- 对方 Agent ID
    remote_api_key  TEXT NOT NULL,           -- 对方的认证 token（配对握手时交换）
    hostname        TEXT,                    -- 对方机器名
    user_name       TEXT,                    -- 对方用户名
    role            TEXT DEFAULT 'worker',   -- 对方在我方团队的角色
    department      TEXT,                    -- 对方部门
    paired_at       TEXT NOT NULL,           -- ISO timestamp
    key_expires_at  TEXT NOT NULL,           -- key 到期时间（配对时 +30天）
    last_heartbeat  TEXT,                    -- 最后心跳时间
    revoked_at      TEXT,                    -- 撤销时间（NULL = 有效）
    UNIQUE(local_agent_id, remote_hub_id)
);
```

---

## 五、审计与身份模型

**双身份主体：**

| 主体类型 | actor 字段值 | 示例 |
|----------|-------------|------|
| 本地 Agent | `agent:<agent_id>` | `agent:cs-wang` |
| 远程 Hub（担保） | `hub:<hub_id>` | `hub:HOST-01` |

**跨 Hub 请求的审计日志：**

```
// 在请求方 Hub（用户侧）：
{
  "action": "remote_disclose",
  "actor": "agent:fengsheng",
  "target_hub": "DESKTOP-COLLEAGUE",
  "target_agent": "colleague-agent",
  "required_level": "summary"
}

// 在接收方 Hub（同事侧）：
{
  "action": "disclose",
  "actor": "hub:HOST-01",      // ← 注意：不是 agent:fengsheng
  "proxied_agent": "fengsheng",     // 实际请求方记录在另字段
  "guaranteed_role": "worker",
  "result": "summary_3_entries"
}
```

`guard_agent_identity` 在转发端点不做校验（转发端点用自己的认证机制——`remote_api_key`），但所有操作记入审计。

**路由扫描注册：** 转发端点（`/api/v1/team/proxy/disclose` 等）必须加入 `test_all_memory_routes_declare_guard` 的白名单，声明"此端点使用 remote_api_key 认证而非 guard_agent_identity"，避免未来扫描器误报或遗忘认证。

---

## 六、离线与降级

| 场景 | 行为 |
|------|------|
| 对方 Hub 离线 | 查询返回 `{"error":"remote_hub_unreachable", "hub":"DESKTOP-COLLEAGUE"}`，5 秒超时 |
| 对方 Hub 拒绝（已撤销） | 返回 403 |
| 对方 Hub 超时 | 重试 1 次（总计 10s），仍失败返回离线错误 |
| 本方离线（对方在查） | 对方同样超时降级 |
| 心跳超时 60 秒 | 成员卡片标灰，30 分钟后标为离线 |

---

## 七、验收用例

| # | 用例 | 预期 |
|---|------|------|
| TC1 | 配对码不一致 → 接受应无效 | 发起方 code=847291，接收方输入 000000 → 拒绝 |
| TC2 | 配对码第 4 次错误尝试 → 拒绝 | 同 code 连续 3 次错误后第 4 次 → 该 code 作废 |
| TC3 | 过期配对码 → 拒绝 | code 生成 6 分钟后使用 → TTL 过期，拒绝 |
| TC4 | 恶意 remote_hub_url → 写入前拒绝 | `http://169.254.169.254:3060` 写入 team_members → 400 |
| TC5 | 无端口 URL → 拒绝 | `http://192.168.1.1` (parsed.port=None) → 400 |
| TC6 | 公网 IP URL → 拒绝 | `http://8.8.8.8:3060` → 400 |
| TC7 | 跨 Hub 身份代入：worker 角色查 summary | 对方 Hub 用本地 team_members.role=worker 代入规则 → 返回摘要级 |
| TC8 | curl 提权尝试 → 本地 role 裁决 | 请求方 curl 带 `_guaranteed_role: "manager"` → 对方 Hub 忽略，仍用本地 role=worker 裁决 |
| TC9 | 离队后旧 key 访问 → 403 | 发起撤销 → 对方 Hub 标记 revoked → 旧 key 请求被拒 |
| TC10 | 撤销时对方离线 → 重试队列 | 关掉对方 Hub → 本方撤销 → revoke POST 失败 → 10 分钟后重试 → 手动开启对方 Hub → 重试成功 → key 失效 |
| TC11 | 对方 Hub 离线 → 查询优雅降级 | 关掉对方 Hub → 查询返回 remote_hub_unreachable |
| TC12 | 手动 IP 输入 → 可达性验证 | 输入 `192.168.1.100:3060` → ping 成功 → 显示可配对 |
| TC13 | UDP 广播发现 → 同网段可见 | 两台 Hub 同网段 → 互相出现在"可发现"列表 |
| TC14 | 审计 actor 区分 | 跨 Hub 请求 → 对方侧审计 actor=hub:xxx，非 agent:xxx |

---

## 八、不做什么（MVP 明确边界）

- ✗ 不跨 Hub 分配任务（task 调度仅本地）
- ✗ 不跨 Hub 写记忆（只读查询）
- ✗ 不做 Key 自动轮换（UI 手动触发 + cron 提醒）
- ✗ 不做 3 台以上组队优化（按 2-5 台设计，不做一致性协议）
- ✓ 只做"成员查成员"（不做 Hub Agent 级别的跨 Hub 搜索）
