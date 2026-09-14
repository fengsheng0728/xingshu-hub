"""
星枢已知限制登记 (known_limitations)

覆盖范围：当前覆盖 L0 / L1 / L5（共 4 条），L2 / L3 / L4 / L6-L8 尚未登记。
新增限制按既有 6 字段格式（id/phase/description/impact/mitigation/status）追加；
可选扩展字段（如 compat_deadline / note）按条目需要在既有 6 字段之外补充。

格式: {id, phase, description, impact, mitigation, status}
"""

KNOWN_LIMITATIONS = [
    {
        "id": "L1-EVICT-001",
        "phase": "L1",
        "description": "LRU 淘汰后旧 dispatch_id 重放会重新执行。缓存上限 10000/24h TTL，超出上限最旧的 entry 被淘汰。淘汰后相同 dispatch_id 再次到达时被视为新派单。",
        "impact": "极低频。正常工况下 dispatch_id 在 TTL 内不会超过 10000 容量。仅在高频派单+超长 TTL+重放延迟三条件同时满足时可能触发。",
        "mitigation": "1) LRU 保最近 10000 条 2) 24h TTL 自然过期 3) 后端重放窗口有限（200条/会话） 4) ToolExecutor 幂等验证兜底",
        "status": "accepted"
    },
    {
        "id": "L1-EVICT-002",
        "phase": "L1",
        "description": "进程重启后缓存清空，所有历史 dispatch_id 视为新派单。",
        "impact": "Agent 重启后首次重放的派单会重新执行。",
        "mitigation": "1) L3 checkpoint replay 保障重启后上下文恢复 2) EXTERNAL 工具始终过审批门 3) 非 EXTERNAL 工具重复执行通常幂等（读操作无副作用）",
        "status": "accepted"
    },
    {
        "id": "L0-COMPAT-001",
        "phase": "L0",
        "description": "旧 Agent 使用平铺消息格式（version<2），新 Hub 兼容检测 is_legacy_flat() 后以旧格式处理。兼容期间两套消息并行。",
        "impact": "旧 Agent 无法享受 envelope 正交性保护（payload 可能含 type 字段碰撞）。",
        "mitigation": "1) is_legacy_flat 在 WS handler 明确分支 2) 旧 Agent 逐步升级后下线兼容分支 3) 兼容窗口建议 30 天",
        "status": "accepted",
        "compat_deadline": "2026-08-28",
        "note": "compat_deadline 2026-08-28 已过期：envelope.is_legacy_flat 旧格式兼容分支当前仍在 routes.py:646 生效（两套消息并行）。下线决策未定——旧 Agent 端已冻结、待新 harness 替换，故 status 保持 accepted 不变。（2026-09-10 标注，台账 CD-032）"
    },
    {
        "id": "L5-TIMEOUT-001",
        "phase": "L5",
        "description": "_PONG_TIMEOUT=90s 是 pong 响应超时。心跳间隔 30s，需要 3 个连续周期无响应才判定半开。选择 3x 而非 1x 是为容忍偶发丢包和 GC 暂停。",
        "impact": "真断开后最长 90s 才触发重连，非即时。",
        "mitigation": "1) 30s 心跳间隔已较快 2) 业务帧到达自动刷新 last_pong 3) L3 退避重连在检测后半开后触发",
        "status": "accepted"
    },
]
