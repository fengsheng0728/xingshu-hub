# CD-034 R4 验收：审计链防重写 —— TSA 令牌离线验签 + 远端锚取回比对

- 项目：星枢 Sync Hub｜仓库 `E:\sync-hub-case`
- 日期：2026-09-21｜判定：负责人拍板两件（① 钉指纹；② cryptography 仅验签路径延迟导入）
- 前序：CD-034 R2（synchronous=FULL）、R3（tsa_stamp/verify_tsa 盖章 + 回拉比对，无真验签）

## 一、修的是什么

R3 的 `tsa_stamp` 只做了「盖章 + 廉价自洽检查（imprint 字节是否出现在 token 里）」，
**没有验签**——不验签的盖章防不了伪造令牌；验签不钉指纹也防不了「任何人自签一张
合法令牌」。`verify_anchor()` 只比同机同目录文件，防不了整体重写（锚文件能被顺手重写）。

本轮补齐两个能力：

1. `verify_tsa_token()`：RFC 3161 TimeStampResp **离线真验签**——解析 → 签名验证 →
   证书链校验 → **指纹钉扎比对** → 消息摘要与链头 hash 比对。
2. `fetch_and_verify_anchors()`：从 `AUDIT_ANCHOR_URLS` **取回**远端锚比对本地链，
   补上 XS-004 只有「外发」没有「取回」的半环。

## 二、验签口径（判定语义）

返回三态，**fail-closed 不假绿**：

| status | 含义 | 触发条件 |
|---|---|---|
| `verified` | 验签通过 | 签名有效 + imprint == SHA256(chain_head) + 指纹命中钉扎 |
| `failed` | 能验但不过（安全事件） | 摘要被篡改 `imprint_mismatch` / 签名无效 `signature_invalid` / 指纹不匹配 `fingerprint_not_pinned` / TSA 拒绝 `tsa_rejected` / 证书在 genTime 时刻不在有效期 |
| `unverified` | 无法验（不是通过！） | 缺 `cryptography`（`cryptography_unavailable`）/ 未配钉扎指纹（`no_trusted_fingerprints`） |

指纹钉扎两种命中方式（拍板①）：

- **钉 TSA 签名证书指纹**：叶子证书 SHA-256(DER) 直接命中即通过——私有/自签 TSA
  不要求成链；
- **钉可信 CA 指纹**：要求证书链（token 内附带证书逐级验签）闭合到已钉扎 CA。

指纹列表**空 = 未配置 = 一律 unverified**（不钉则验签等于白验，故拒绝出绿）。

`fetch_and_verify_anchors` 判据（与 `verify_tsa` 相同）：链头随写入变化，判据不是
「远端锚 == 当前链头」，而是「**远端锚节点仍存在于链中**」（`match_head`/`match_chain`
均为一致；`mismatch` = 整段重写/截断嫌疑，`valid=False`）。远端**取不回**记
`unverified` 计数、不判 `valid=False`——取不回不是篡改证据，但也不构成确认，
运维应按 `unverified > 0` 告警。

远端锚应答格式：JSON `{"anchor": "<hex>"}`（XS-004 webhook 外发同构），或纯文本
「`... <hex>`」末词（兼容 anchor.txt 快照格式）。**锚接收方需暴露 GET 取回口**。

## 三、依赖与配置方式

### cryptography（拍板②）

- 只在验签路径内**函数级延迟导入**（`_load_crypto()`），`ImportError` → 返回
  `unverified`，不 crash；
- 核心哈希链维持零依赖手写 DER 现状——模块顶层无任何 cryptography import，
  本轮只新增了同样零依赖的 DER **解码**器（`_der_read`/`_der_children`/`_der_oid_str`）。

### 指纹配置（不碰 models.py——别人在改，故走 env + audit/ 下 JSON）

优先级：显式入参 > 环境变量 > JSON 配置文件。

1. 环境变量 `SYNC_HUB_TSA_TRUSTED_FP`：逗号/空白分隔的 SHA-256 hex（带冒号、大小写
   均可，加载时归一）；
2. JSON 文件 `audit/tsa_trust.json`（跟随 `SYNC_HUB_AUDIT_DIR`/config audit.dir）：
   ```json
   {"trusted_fingerprints": ["<tsa_cert_sha256_hex>", "<ca_sha256_hex>"]}
   ```
   模板见 `audit/tsa_trust.example.json`（**example 不生效**，复制改名填真值）。

获取证书指纹：

```bash
python scripts/verify_tsa_anchor.py fingerprint tsa.crt        # PEM/DER 均可
openssl x509 -in tsa.crt -outform DER | openssl sha256         # 或 openssl
```

### 远端锚 SSRF 防护（`_validate_anchor_url`）

- 仅允许 http/https，禁 userinfo（防凭据泄漏/混淆）；
- **内网保留段默认放宽**：本产品是内网部署，锚接收方典型就是同网段审计服务器
  （如 `http://192.168.x.x/anchor`），一刀切拒绝内网段会让默认部署不可用；且 URL
  来自管理员配置（`AUDIT_ANCHOR_URLS`）而非用户输入，SSRF 攻击面有限；
- 部署面变化（URL 来自低权角色）时置 `SYNC_HUB_ANCHOR_ALLOW_PRIVATE=0` 收紧：
  拒绝私网/环回/链路本地/保留/组播/未指定地址（域名放行——离线无法判定解析结果，
  收紧到解析后校验的代价不值得）。

## 四、运维操作

```bash
# 1) 离线验签单个盖章令牌（--head 缺省取 --db 当前链头）
python scripts/verify_tsa_anchor.py token audit/tsa/<ts>_<head8>.tsr --db sync_hub.db

# 2) 对 audit/tsa 全部 ok 盖章记录逐个验签 + 链中比对（审计巡检用）
python scripts/verify_tsa_anchor.py tsa-dir --db sync_hub.db

# 3) 远端锚取回比对（缺省读 AUDIT_ANCHOR_URLS 配置）
python scripts/verify_tsa_anchor.py anchors --db sync_hub.db
python scripts/verify_tsa_anchor.py anchors --db sync_hub.db --url http://192.168.1.10:9000/anchor
```

退出码：`0` 全绿；`1` 存在 failed/valid=False（安全事件，告警）；`2` 存在
unverified（未配指纹/缺依赖/取不回，需要处置但不等于篡改证据）。

日常节奏建议：`tsa_stamp` 周期盖章（既有 `audit.tsa` 配置不变）+ 巡检时跑
`tsa-dir` 全量验签 + `anchors` 取回比对。

## 五、测试证据（2026-09-21 复跑，串行，PYTHONUTF8=1）

**新增** `tests/test_audit_tsa_verify.py`（17 用例，离线夹具：cryptography 现场生成
自签 CA + TSA 证书，手工拼 RFC3161 TimeStampResp DER + ECDSA 真签名，不出网）：

- 有效令牌通过：钉叶子指纹 / 钉 CA 指纹（两级链闭合）；
- 摘要被篡改拒绝（`imprint_mismatch`）、签名被篡改拒绝（`signature_invalid`）；
- 指纹不匹配拒绝（`fingerprint_not_pinned`）；
- 无指纹配置 unverified（显式空列表 / env 与文件全缺两条路径）；
- cryptography 缺失 unverified（monkeypatch `sys.modules` 模拟 ImportError）；
- env / JSON 文件两种配置加载、指纹归一（冒号/大写）、.tsr 文件路径入参；
- 远端锚：`match_head` / 链增长后 `match_chain` 不误报 / `mismatch` 判 valid=False /
  不可达记 unverified 不判 invalid / 纯文本锚格式兼容；
- SSRF：file://、ftp://、userinfo、无主机名拒绝；内网段默认放行 + 收紧档拒绝。

```
python -m pytest tests/test_audit_tsa_verify.py -q   → 17 passed
```

**既有 audit 子集回归**（串行）：

```
tests/test_audit_tsa.py + test_xs004_anchor_export.py + test_s2_audit_chain.py
+ test_audit_declared_gap.py + test_chain_head_binding.py
+ test_outbox_audit_atomicity.py                        → 46 passed
tests/test_l7_audit.py                                  →  6 passed
```

合计 **69 passed**；`python -m ruff check .` → All checks passed!（零告警）。

**脚本冒烟**：`token audit/tsa/20260920T090752Z_ea9dc498.tsr`（真实公网 TSA 令牌）
→ 未配指纹时正确报 `unverified / no_trusted_fingerprints`，exit=2；`anchors` 未配置
→ 休眠 note，exit=0。

## 六、改动清单（全部在白名单内）

| 文件 | 改动 |
|---|---|
| `audit_chain.py` | 新增 CD-034 R4 段：`_der_read`/`_der_children`/`_der_oid_str` 零依赖 DER 解码器；`_load_crypto`（延迟导入）；`_normalize_fingerprints`/`_load_trusted_fingerprints`；`_parse_timestamp_resp`/`_parse_tstinfo`；`_verify_signature`/`_cert_signed_by`；`verify_tsa_token`；`_validate_anchor_url`/`fetch_and_verify_anchors`。既有函数一行未动 |
| `tests/test_audit_tsa_verify.py` | 新建，17 用例（离线夹具现场生成证书/令牌） |
| `scripts/verify_tsa_anchor.py` | 新建运维 CLI（token / anchors / tsa-dir / fingerprint 四子命令） |
| `audit/tsa_trust.example.json` | 新建配置模板（example 不生效） |
| `docs/cd034-tsa-anchor-acceptance.md` | 本文档 |

未碰 `models.py` / `routes.py` / `main.py`；未 git commit。

## 七、遗留 / 边界

1. **验签未接 HTTP 端点**：本轮只交付库函数 + CLI；`/api/audit/anchor/*` 端点接
   `verify_tsa_token` 需动 routes_audit.py（不在本轮白名单），留待下一轮。
2. **证书有效期以 genTime 为判据**（正确口径：验「盖章时刻」而非「验签时刻」有效），
   未做 CRL/OCSP 吊销检查——内网私有 TSA 场景吊销由指纹钉扎替代承担。
3. **签名算法面**：实现覆盖 RSA PKCS#1 v1.5 + ECDSA（SHA-256）；Ed25519 TSA 罕见，
   遇到会报 `signature_invalid`（fail-closed 方向安全）。
4. **锚 GET 取回口**需锚接收方实现（XS-004 只定义了 POST 外发）；取回格式约定见
   第二节。
5. `verify_tsa_token` 的 `failed` 结果目前只返回给调用方，未自动落
   `anchor_mismatch` 审计/通知（同 1，端点接线下一轮）。
