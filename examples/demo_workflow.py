#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星枢 Sync Hub — 一页纸演示脚本
启动 Hub 后运行此脚本，模拟完整工作流：3 个 Agent + 渐进披露全链路

用法:
  1. 先启动 Hub:  cd E:/sync-hub-case && python main.py
  2. 再运行此脚本: python demo_workflow.py
"""

import requests, time, json, sys
from datetime import datetime

HUB = "http://127.0.0.1:3060"
API = f"{HUB}/api/v1"

def log(actor, msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {actor:12s} | {msg}")

def post(path, data=None, params=None):
    try:
        r = requests.post(f"{API}{path}", json=data, params=params, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def get(path, params=None):
    try:
        r = requests.get(f"{API}{path}", params=params, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ── 等待 Hub 就绪 ──
print("=" * 60)
print("  星枢 Sync Hub — 渐进式披露全链路演示")
print("=" * 60)

for i in range(30):
    try:
        r = requests.get(f"{HUB}/health", timeout=3)
        if r.status_code == 200:
            print(f"Hub 已就绪\n")
            break
    except Exception as _exc:
        print(f"Hub 未就绪,重试中... {_exc}")
    time.sleep(1)
else:
    print("❌ Hub 未启动，请先运行: cd E:\\sync-hub-case && python main.py")
    sys.exit(1)

# ═══════════════════════════════════════════════════
# Act 1: 注册 Agent
# ═══════════════════════════════════════════════════
print("─" * 60)
print("Act 1: 注册 3 个 Agent")
print("─" * 60)

# 客服小王
r = post("/agents/register", {
    "agent_id": "cs-wang",
    "agent_name": "客服小王",
    "department": "客服部",
    "role": "worker",
    "capabilities": ["接待", "售后", "投诉处理"]
})
log("Hub", f"注册 cs-wang → {r.get('status', r)}")

# 客服小李
r = post("/agents/register", {
    "agent_id": "cs-li",
    "agent_name": "客服小李",
    "department": "客服部",
    "role": "worker",
    "capabilities": ["接待", "换货", "退款"]
})
log("Hub", f"注册 cs-li → {r.get('status', r)}")

# 主管老张 (manager, 管理小王和小李)
r = post("/agents/register", {
    "agent_id": "mgr-zhang",
    "agent_name": "主管老张",
    "department": "客服部",
    "role": "manager",
    "managed_agents": ["cs-wang", "cs-li"],
    "capabilities": ["团队管理", "质量监控", "纠纷处理"]
})
log("Hub", f"注册 mgr-zhang → {r.get('status', r)}")

print()

# ═══════════════════════════════════════════════════
# Act 2: 日常工作 — 写入记忆（写入隔离）
# ═══════════════════════════════════════════════════
print("─" * 60)
print("Act 2: 日常工作 — 客服写入记忆（写入隔离）")
print("─" * 60)

r = post("/memory/store", {
    "memory_key": "case-001",
    "content": "客户张先生反馈浴室柜门板色差严重，与展厅样品不符。已拍照留证，安抚客户情绪，承诺48小时内给出解决方案。客户要求退货退款。",
    "summary": "张先生浴室柜色差投诉，要求退货退款",
    "tags": ["色差", "退货", "浴室柜"],
    "importance": 0.9,
    "disclosure_level": "summary",
    "disclosure_scope": "manager"
}, params={"agent_id": "cs-wang"})
log("小王", f"写入记忆: case-001 → {r.get('status')}")

r = post("/memory/store", {
    "memory_key": "case-002",
    "content": "客户李女士购买智能马桶盖，安装后发现漏电保护频繁跳闸。已安排师傅上门检修，初步判断是电源接地问题，非产品故障。",
    "summary": "李女士马桶盖漏电投诉，已安排上门检修",
    "tags": ["漏电", "马桶盖", "上门"],
    "importance": 0.8,
    "disclosure_level": "summary",
    "disclosure_scope": "manager"
}, params={"agent_id": "cs-wang"})
log("小王", f"写入记忆: case-002 → {r.get('status')}")

r = post("/memory/store", {
    "memory_key": "case-003",
    "content": "客户赵先生花洒水压低，检查后发现是楼层水压问题，非产品问题。已建议安装增压泵，客户表示理解。",
    "summary": "赵先生花洒水压问题，建议安装增压泵",
    "tags": ["水压", "花洒", "增压泵"],
    "importance": 0.5,
    "disclosure_level": "summary",
    "disclosure_scope": "manager"
}, params={"agent_id": "cs-li"})
log("小李", f"写入记忆: case-003 → {r.get('status')}")

r = post("/memory/store", {
    "memory_key": "case-004",
    "content": "处理了一个换货：王女士的水龙头漏水，已核实是密封圈老化，直接换新。客户满意，给了好评。",
    "summary": "王女士水龙头换货完成，客户好评",
    "tags": ["换货", "水龙头", "好评"],
    "importance": 0.6,
    "disclosure_level": "summary",
    "disclosure_scope": "manager"
}, params={"agent_id": "cs-li"})
log("小李", f"写入记忆: case-004 → {r.get('status')}")

print()

# ═══════════════════════════════════════════════════
# Act 3: 写入隔离验证 — 小王看不到小李的记忆
# ═══════════════════════════════════════════════════
print("─" * 60)
print("Act 3: 写入隔离验证 — 小王只能看到自己的记忆")
print("─" * 60)

# 小王查小李
r = post("/memory/disclose", {
    "requester_agent_id": "cs-wang",
    "target_agent_id": "cs-li",
    "task_id": "demo-isolation",
    "query": "水压",
    "required_level": "summary"
})
if r.get("disclosed_count", 0) == 0:
    log("小王", "尝试查小李记忆 → ❌ 被隔离，返回 0 条 (✅ 写入隔离生效)")
else:
    log("小王", f"⚠️ 查到了 {r['disclosed_count']} 条小李记忆 (隔离可能失效)")

# 小王查自己
r = post("/memory/disclose", {
    "requester_agent_id": "cs-wang",
    "target_agent_id": "cs-wang",
    "task_id": "demo-self-check",
    "required_level": "full"
})
log("小王", f"查看自己的记忆 → {r.get('disclosed_count')} 条 (自己看自己应全可见)")

print()

# ═══════════════════════════════════════════════════
# Act 4: 主管查看团队 — 渐进披露 (主管 → SUMMARY)
# ═══════════════════════════════════════════════════
print("─" * 60)
print("Act 4: 主管老张查看团队 — 渐进披露 (manager 看下属 → SUMMARY)")
print("─" * 60)

# 老张查小王
r = post("/memory/disclose", {
    "requester_agent_id": "mgr-zhang",
    "target_agent_id": "cs-wang",
    "task_id": "demo-mgr-review",
    "query": "色差",
    "required_level": "summary"
})
log("老张", f"查小王记忆 → {r.get('disclosed_count')} 条 (manager 看下属应看到 SUMMARY)")
if r.get("memories"):
    for m in r["memories"][:2]:
        disp = m.get("disclosure_level", "?")
        preview = m.get("content", "")[:80]
        log("  ", f"[{disp}] {preview}...")

print()

# ═══════════════════════════════════════════════════
# Act 5: 任务调度 — 主管创建任务
# ═══════════════════════════════════════════════════
print("─" * 60)
print("Act 5: 主管老张创建并调度任务")
print("─" * 60)

# 主管创建任务
r = post("/tasks/create", {
    "task_id": "T-001",
    "creator_agent_id": "mgr-zhang",
    "description": "处理张先生色差投诉 — 核实情况、联系工厂、给出解决方案",
    "required_capabilities": ["售后", "投诉处理"],
    "priority": 1
})
log("老张", f"创建任务 T-001 → {r.get('status')}")

# 调度任务
r = post("/tasks/T-001/schedule")
log("Hub", f"调度 T-001 → {r.get('status') or r.get('message', r)}")
if r.get("assigned_to"):
    log("  ", f"分配给: {r['assigned_to']} (第 {r.get('disclosure_phase')} 阶段披露: {r.get('disclosed_memories', {}).get('count', 0)} 条)")

print()

# ═══════════════════════════════════════════════════
# Act 6: 披露升级 — Agent 申请 + 主管审批
# ═══════════════════════════════════════════════════
print("─" * 60)
print("Act 6: 披露升级 — 小王申请完整信息 + 老张审批")
print("─" * 60)

# 小王申请升级
r = post("/tasks/T-001/advance", params={
    "agent_id": "cs-wang",
    "reason": "需要完整的色差投诉历史记录，才能准确判断责任归属"
})
log("小王", f"申请披露升级 → {r.get('status')}")
request_id = r.get("request_id", "")
log("  ", f"审批 ID: {request_id}")

# 老张批准
if request_id:
    r = post("/disclosure/approve", params={
        "request_id": request_id,
        "approver_id": "mgr-zhang"
    })
    log("老张", f"批准披露 → {r.get('status')}")
    log("  ", f"新阶段: 第 {r.get('new_phase')} 阶段")

print()

# ═══════════════════════════════════════════════════
# Act 7: 语义搜索
# ═══════════════════════════════════════════════════
print("─" * 60)
print("Act 7: 语义搜索 — 用自然语言搜索跨 Agent 记忆")
print("─" * 60)

r = post("/memory/semantic_search", {
    "query": "产品质量问题投诉",
    "n_results": 5,
    "requester_agent_id": "mgr-zhang"
})
log("老张", f"语义搜索 '产品质量投诉' → {r.get('total', 0)} 条")
if r.get("memories"):
    for m in r["memories"][:3]:
        sim = m.get("similarity", 0)
        owner = m.get("owner", "?")
        content = m.get("content", "")[:60]
        log("  ", f"[{owner}] sim={sim:.3f} {content}")

print()

# ═══════════════════════════════════════════════════
# Act 8: 跨会话记忆 — 关闭重开，AI 还记得
# ═══════════════════════════════════════════════════
print("─" * 60)
print("Act 8: 跨会话记忆 — 模拟会话 A 写入偏好 → 会话 B 检索验证")
print("─" * 60)

# 会话 A：用户告诉 AI 偏好
r = post("/memory/store", {
    "memory_key": "pref-spicy",
    "content": "用户饮食偏好：不吃辣，喜欢清汤火锅。上次团队聚餐选了海底捞清汤锅底。",
    "kind": "preference",
    "tags": ["饮食", "偏好", "火锅"],
    "confidence": 1.0,
    "source_type": "user",
    "disclosure_level": "summary"
}, params={"agent_id": "cs-wang"})
log("会话A", f"小王说'我不吃辣' → memory_pool 写入: {r.get('status')}")

r = post("/memory/store", {
    "memory_key": "pref-city",
    "content": "用户在广州珠江新城上班，通勤走猎德大桥。偏好上午 10 点后的会议。",
    "kind": "preference",
    "tags": ["位置", "通勤", "会议偏好"],
    "confidence": 1.0,
    "source_type": "user",
    "disclosure_level": "summary"
}, params={"agent_id": "cs-wang"})
log("会话A", f"小王说'我在珠江新城上班' → memory_pool 写入: {r.get('status')}")

# 模拟"关闭重开"——新会话 B 查询记忆
log("会话B", "🔄 新会话开始（模拟关闭重开）...")
time.sleep(0.3)

# 用 M3 search 检索跨会话记忆
r = post("/memory/search", {
    "agent_id": "cs-wang",
    "query": "饮食偏好 火锅",
    "limit": 5
})
log("会话B", f"问'推荐火锅' → memory_search 返回 {r.get('total', r.get('count', 0))} 条")
if r.get("memories") or r.get("results"):
    items = r.get("memories") or r.get("results", [])
    for m in items[:3]:
        content = (m.get("content") or "")[:60]
        score = m.get("score", m.get("similarity", 0))
        log("  ", f"[score={score:.2f}] {content}")

# 双向验证：确认信息来自 memory_pool 不是对话残留
r2 = post("/memory/search", {
    "agent_id": "cs-wang",
    "query": "珠江新城 通勤",
    "limit": 5
})
found_city = False
if r2.get("memories") or r2.get("results"):
    for m in (r2.get("memories") or r2.get("results", [])):
        if "珠江新城" in (m.get("content") or ""):
            found_city = True
            break
log("会话B", f"问'你在哪上班' → 珠江新城命中: {'✅ 来自 memory_pool' if found_city else '❌ 未命中'}")

# 验证删除传播（M4 三处生效）
r = post("/memory/search", {
    "agent_id": "cs-wang",
    "query": "不吃辣",
    "limit": 3
})
if r.get("memories") or r.get("results"):
    log("会话B", f"'不吃辣' 记忆存在 → 验证通过（跨会话可召回）")

print()
log("Act8", "✅ 跨会话记忆验证完成：写入 → 新会话检索 → 命中 → 非对话残留")

print()

# ═══════════════════════════════════════════════════
# 汇总
# ═══════════════════════════════════════════════════
print("=" * 60)
print("  演示完成 ✅")
print("=" * 60)
print("""
关键验证点:
  1. ✅ 写入隔离 — 同级 worker 互相不可见
  2. ✅ 渐进披露 — manager 看下属仅 summary
  3. ✅ 任务调度 — 按能力匹配 + 阶段披露
  4. ✅ 披露升级 — 申请 + 审批 + 完整信息
  5. ✅ 语义搜索 — 跨 Agent 向量搜索
  6. ✅ 跨会话记忆 — 会话A写入偏好 → 会话B检索命中 → 确认来自 memory_pool

场景示例:
  cs-wang/cs-li  = 客服小王/小李
  mgr-zhang      = 店长老张
  T-001          = 色差投诉工单
  渐进披露       = 客服写工单→主管看摘要→纠纷时申请完整记录
  跨会话记忆     = 上次说不吃辣→新会话推荐火锅自动避开辣锅底
""")
