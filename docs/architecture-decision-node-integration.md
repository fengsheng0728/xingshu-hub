# 架构决策：加法版 SaaS 的 Node 集成方式（边车 vs 移植）

日期：2026-09-02 · 决策者：用户授权 Hermes 决定 · 状态：**已定（边车）**

## 决策

加法版 SaaS（arch-site / Node 应用层）集成「原版资产库」5 个 Python 模块（sensitivity / key_scopes / disclosure / audit_chain / entity_extraction）的方式：**边车（sidecar）**——Python Hub（:3060）作为唯一治理引擎常驻，Node 应用层经网关工具层 HTTP 调用，不移植不重写。

## 理由

1. **治理链已验收**：网关工具层（阶段2）、披露/敏感度/审计链（BOUNDARY 22/22 + 468 测试护航）全部落地。重写 = 丢弃已验收资产，复刻成本高且必然引入新缺陷（幻影缺陷五连发的前车之鉴）。
2. **工作量差一个量级**：边车 = arch-site 加 HTTP 客户端 + 鉴权接线；移植 = 5 个模块 × Node 重写 + 重新验收。系统架构图的「上线顺序」也写明网关是咽喉——网关已存在，Node 侧接它即可。
3. **测试基线可复用**：Node 侧零测试负担，Python 侧 468 基线继续守护。
4. **失败模式清晰**：Node 层无状态，重启不丢治理；Python 边车挂了 Node 层显式降级（网关已支持降级链）。

## 影响与边界

- arch-site 应用层需要：HTTP 客户端封装 + scoped key 认证 + 错误降级（对标 routes_gateway.py 的语义）
- 不动的：Python 侧 API 契约（网关读取端点 /api/v1/gateway/read 三 kind 为唯一数据通道）
- 后续若 Node 侧性能/独立性需要，可再评估部分模块移植——但需独立的验收周期，不在首轮范围

## 关联

- 事件循环排查报告 docs/event-loop-blockers.md（网关/team 域已修）
- 系统架构图 SystemArchitecture.tsx（Node 应用层 status 按此决策更新）
