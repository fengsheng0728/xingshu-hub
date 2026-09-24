# -*- coding: utf-8 -*-
"""CD-098：WS 帧交错窗口收口 —— 回归测试（2026-09 审查修复轮）

routes_automation / disclosure_ops / routes_sessions 对 hub.active_ws[agent_id]
的直发全部统一走 notifications.safe_send / safe_send_text（per-agent 发送锁）。
本文件用假 WS 记录帧序验证：两协程并发发送不抛 RuntimeError、帧不交错。
"""
import asyncio
import json

from notifications import NotificationManager


class _FakeWS:
    """记录帧序的假 WS：每次 send 拆 begin/end 两段、中间让出事件循环——
    无锁时另一协程的帧必然插进来（begin/begin/end/end 交错），持锁则严格成对。"""

    def __init__(self):
        self.events = []  # [("begin"|"end", payload)] 帧序记录
        self.frames = []  # 完整送达的帧

    async def send_text(self, text):
        self.events.append(("begin", text))
        await asyncio.sleep(0)  # 让出点：无锁时这里就是交错窗口
        self.events.append(("end", text))
        self.frames.append(text)

    async def send_json(self, data):
        await self.send_text(json.dumps(data, ensure_ascii=False))


def _assert_no_interleave(ws):
    """begin/end 严格交替成对 = 无帧交错；任何嵌套 begin 即交错。"""
    depth, current = 0, None
    for ev, payload in ws.events:
        if ev == "begin":
            assert depth == 0, f"帧交错：{payload!r} 插入未完成帧 {current!r}"
            depth, current = 1, payload
        else:
            assert depth == 1 and payload == current, "end 与 begin 不配对（帧交错）"
            depth, current = 0, None
    assert depth == 0, "存在未完成的帧"


def test_concurrent_safe_send_json_and_text_no_interleave():
    """JSON 帧与文本帧两协程并发（scheduler 文本帧 × notify JSON 帧同构）：
    不抛异常、全部送达、帧序不交错。"""
    mgr = NotificationManager()
    ws = _FakeWS()

    async def run():
        await mgr.connect("agent-x", ws)

        async def writer_json():
            for i in range(20):
                await mgr.safe_send("agent-x", {"type": "json-frame", "seq": i})

        async def writer_text():
            for i in range(20):
                await mgr.safe_send_text("agent-x", f"text-frame-{i}")

        await asyncio.gather(writer_json(), writer_text())

    asyncio.run(run())  # 不抛 RuntimeError 即过第一关
    assert len(ws.frames) == 40, f"帧丢失: {len(ws.frames)}/40"
    _assert_no_interleave(ws)


def test_external_writer_reusing_send_lock_no_interleave():
    """外部写入方复用同一把 send_lock（routes_ws handler 直发口径）时，
    与 safe_send_text 写入方并发同样不交错。"""
    mgr = NotificationManager()
    ws = _FakeWS()

    async def run():
        await mgr.connect("agent-y", ws)

        async def writer_locked():
            for i in range(20):
                async with mgr.send_lock("agent-y"):
                    await ws.send_text(f"raw-{i}")

        async def writer_safe():
            for i in range(20):
                await mgr.safe_send_text("agent-y", f"safe-{i}")

        await asyncio.gather(writer_locked(), writer_safe())

    asyncio.run(run())
    assert len(ws.frames) == 40, f"帧丢失: {len(ws.frames)}/40"
    _assert_no_interleave(ws)
