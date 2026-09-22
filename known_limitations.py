"""
星枢已知限制登记 (known_limitations)

覆盖范围：当前覆盖 L2 / L5（共 3 条）。L3 / L4 / L6-L8 尚未登记。

已退役（2026-09-21 用户方向裁决「自研 Agent 端砍掉、只放通用 API」，台账 CD-032 / CD-075）：
  - L0-COMPAT-001 旧 Agent 平铺消息兼容分支 —— 分支已下线（envelope.is_legacy_flat 删除 + routes_ws 分支删除），
    协议层口径改为「平铺旧格式（version<2）一律不接受」。
  - L1-EVICT-001 / L1-EVICT-002 派单幂等缓存 —— 属旧 Agent 端执行层实现，我们不再交付该实现；
    协议层口径改为「Hub 在 hello 时按 checkpoint 重放未确认派单，幂等由接入方自行保证」（见《使用手册》§12）。
新增限制按既有 6 字段格式（id/phase/description/impact/mitigation/status）追加；
可选扩展字段（如 compat_deadline / note）按条目需要在既有 6 字段之外补充。

格式: {id, phase, description, impact, mitigation, status}
"""

KNOWN_LIMITATIONS = [
    {
        "id": "L5-TIMEOUT-001",
        "phase": "L5",
        "description": "_PONG_TIMEOUT=90s 是 pong 响应超时。心跳间隔 30s，需要 3 个连续周期无响应才判定半开。选择 3x 而非 1x 是为容忍偶发丢包和 GC 暂停。",
        "impact": "真断开后最长 90s 才触发重连，非即时。",
        "mitigation": "1) 30s 心跳间隔已较快 2) 业务帧到达自动刷新 last_pong 3) L3 退避重连在检测后半开后触发",
        "status": "accepted"
    },
    {
        "id": "L2-ANCHOR-001",
        "phase": "L2",
        "description": "审计链的外部锚默认未配（AUDIT_ANCHOR_URLS 空 = 不联网），且本地锚文件 audit/anchor.txt "
                      "与链同机同目录、verify_anchor() 只比它 → 有本机写权限者可同时改链与锚，属循环论证；"
                      "全仓亦无「把远端锚取回比对本地链头」的机制（export_anchor 仅由手动端点触发）。"
                      "配套已修：审计连接 PRAGMA synchronous NORMAL→FULL（CD-034 R2），断电/内核崩溃不再丢尾。",
        "impact": "仅影响「对外举证防篡改」场景：本地自校验仍能查出行级篡改，但无法证明链未被整段重写。"
                  "日常运行、断网重连、进程崩溃均不受影响。",
        "mitigation": "1) 本地锚仅供防误删，**不作为防篡改证据**——对客户的举证结论必须写明这一点 "
                      "2) CD-034 R3 已落地（2026-09-20）：链头交公共 RFC3161 TSA 盖章（.tsq/.tsr "
                      "+ index.jsonl），校验时回拉比对——判据为「被盖章的链头节点是否仍在链中」，"
                      "链正常增长不误报、整段重写/截断必告警（events anchor_mismatch + dashboard 通知）；"
                      "默认关，配置 audit.tsa.enabled 开启（url 默认公共 TSA，interval 默认 86400s） "
                      "3) 仍建议同时配 AUDIT_ANCHOR_URLS 由外部保管方留档（R1，需外部落点，可复用它把 "
                      ".tsr 一并外发）",
        "status": "accepted",
        "note": "CD-034（2026-09-17 用户拍板：先做 R2 + 落 R1 文档声明）。"
                "2026-09-20：R3 落地（ef11166）——原「无远端锚回拉比对机制」一条已不成立；"
                "剩余边界＝本地 .tsr 仍与链同机（可被一并删改），对外举证需把 .tsr/盖章记录发到外部保管方"
                "（AUDIT_ANCHOR_URLS）方为完整闭环。评估记录 docs/audit-chain-durability-assessment.md",
    },
    {
        "id": "L2-ANCHOR-002",
        "phase": "L2",
        "description": "**jsonl 运行产物曾被纳入 git 跟踪**（audit/transport.jsonl、audit/memory_pool.jsonl）："
                      "运行期累积的行从未提交，而项目长期用 `git checkout -- audit` 收尾测试产物 → 文件被回退成"
                      "旧提交版本，运行期增长的那一段永久丢失。主链里的 jsonl_anchor 于是指向已不存在的内容"
                      "（实测 2026-09-21：transport.jsonl 窗口 w-5609-6608 丢失，segment_missing）。"
                      "**主链本身未受损**（audit_log 6603 条 prev/entry hash 完整），受影响的是那段 jsonl"
                      "自身无法再自证。",
        "impact": "审计中心校验会报「断链」——如实反映该段内容已不存在。丢的那段传输日志不可恢复，"
                  "因此其覆盖期间（2026-09-01 之前的 ws 收发窗口）不能用于举证。",
        "mitigation": "1) CD-073（2026-09-21）：jsonl 移出 git 跟踪（`.gitignore` 加 `audit/*.jsonl` + "
                      "`git rm --cached`），从此 `git checkout -- audit` 不再能抹掉运行产物 "
                      "2) 已丢失的窗口用 `scripts/audit_declare_gap.py` **如实上链声明**"
                      "（entry_type=anchor_gap，写窗口/原因/登记人）——校验把它标为「已声明缺口」而非「未解释断链」，"
                      "但内容不会回来，也不改历史哈希 3) 声明纪律：能用备份恢复就先恢复，声明是最后手段，不是消红工具",
        "status": "accepted",
        "note": "CD-073（2026-09-21 用户拍板 A+B）。声明脚本幂等；测试 tests/test_audit_declared_gap.py",
    },
]
