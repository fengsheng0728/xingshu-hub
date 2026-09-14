"""P2 通知多渠道出站 — 钉钉 webhook + 邮件 SMTP

设计（D3/D4/D5）：
- create_notification 主流程不变，其后异步 fan-out（asyncio.create_task）
- 每渠道独立 try/except + 超时 ≤5s；失败 → 日志 + 返回状态（调用方写 channel_status）
- 渠道故障零阻塞主链路
- 凭据只从 config.yaml 读（NOTIFY_CHANNELS），不进 git 不进日志
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

logger = logging.getLogger("xingshu.notify")


def _channel_config(channels: dict, name: str) -> dict:
    """取渠道配置（不存在/未启用返回空 dict）"""
    if not isinstance(channels, dict):
        return {}
    cfg = channels.get(name) or {}
    if not cfg.get("enabled"):
        return {}
    return cfg


# ── 钉钉自定义机器人（webhook + 加签） ──

def _dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    """钉钉加签：HMAC-SHA256(timestamp + '\n' + secret) → base64 → urlencode"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(hmac_code))


def _dingtalk_send(cfg: dict, title: str, body: str, extra: dict) -> dict:
    """发送钉钉 markdown 消息。成功返回 {"status": "ok"}，失败抛异常。"""
    webhook = cfg.get("webhook", "").strip()
    if not webhook:
        raise ValueError("dingtalk webhook 未配置")

    url = webhook
    secret = cfg.get("secret", "").strip()
    if secret:
        ts = int(round(time.time() * 1000))
        url = webhook + ("" if "?" in webhook else "?") + f"&timestamp={ts}&sign={_dingtalk_sign(secret, ts)}"

    md = f"### {title}\n\n{body}"
    if extra:
        md += "\n\n---\n" + "  \n".join(f"**{k}**: {v}" for k, v in extra.items())

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title[:64], "text": md[:8000]},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if resp_data.get("errcode") not in (0, None):
        raise RuntimeError(f"钉钉返回 errcode={resp_data.get('errcode')}: {resp_data.get('errmsg')}")
    return {"status": "ok"}


# ── SMTP 邮件 ──

def _smtp_send(cfg: dict, title: str, body: str, extra: dict) -> dict:
    """发送邮件。成功返回 {"status": "ok"}，失败抛异常。"""
    host = cfg.get("host", "").strip()
    port = int(cfg.get("port") or 465)
    user = cfg.get("user", "").strip()
    password = cfg.get("password", "")
    from_addr = cfg.get("from", user)
    to_addrs = cfg.get("to", [])
    if isinstance(to_addrs, str):
        to_addrs = [a.strip() for a in to_addrs.split(",") if a.strip()]
    if not host or not to_addrs:
        raise ValueError("smtp host/to 未配置")

    text = body
    if extra:
        text += "\n\n" + "\n".join(f"{k}: {v}" for k, v in extra.items())
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr((str(Header("星枢", "utf-8")), from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)

    use_ssl = (port == 465)
    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=5)
    else:
        server = smtplib.SMTP(host, port, timeout=5)
        server.ehlo()
        if port == 587:
            server.starttls()
    try:
        if user and password:
            try:
                server.login(user, password)
            except smtplib.SMTPNotSupportedError:
                # 服务器不支持 AUTH（如本地接收端/测试 SMTP）——降级直接发送
                logger.warning(f"[notify:smtp] 服务器不支持 AUTH，跳过登录直接发送")
            except smtplib.SMTPAuthenticationError:
                raise
        server.sendmail(from_addr, to_addrs, msg.as_string())
    finally:
        try:
            server.quit()
        except Exception as _exc:
            logger.debug("notify_channels silent-except @122: %s", _exc)
    return {"status": "ok"}


# ── 出站 dispatcher（异步 fan-out） ──

async def fan_out(channels: dict, notif: dict) -> dict:
    """异步发往全部 enabled 渠道，返回 {渠道名: ok|fail}。
    任何渠道失败不抛异常（D4：主链路零阻塞），状态由调用方写入 channel_status。"""
    results = {}
    tasks = []

    async def _run(name, fn, cfg):
        try:
            r = await asyncio.to_thread(fn, cfg, notif.get("title", ""),
                                        notif.get("body", ""),
                                        {"type": notif.get("type", ""),
                                         "agent": notif.get("agent_id", ""),
                                         "source": notif.get("source", "")})
            results[name] = "ok" if r.get("status") == "ok" else "fail"
        except Exception as e:
            logger.warning(f"[notify:{name}] 发送失败: {type(e).__name__}: {str(e)[:120]}")
            results[name] = "fail"

    d_cfg = _channel_config(channels, "dingtalk")
    if d_cfg:
        tasks.append(_run("dingtalk", _dingtalk_send, d_cfg))
    s_cfg = _channel_config(channels, "smtp")
    if s_cfg:
        tasks.append(_run("smtp", _smtp_send, s_cfg))

    if tasks:
        await asyncio.gather(*tasks)
    return results
