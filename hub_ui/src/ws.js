// WebSocket 客户端：首帧鉴权 + 心跳 + 自动重连 + 降级回调（§四 WS 接入）
// 服务端约定：连接后首帧 {"type":"auth","token"}（NO_AUTH 模式下该帧被忽略），
// 业务期 {"msg_type":"ping"} → {"msg_type":"pong"}。断线由调用方降级为 30s 轮询。
import { getToken } from './api'

export const WS_OPEN = 'open'
export const WS_CONNECTING = 'connecting'
export const WS_DEGRADED = 'degraded'

export function connectWs(path, { onMessage, onStatus, pingMs = 25000 } = {}) {
  let ws = null
  let pingTimer = null
  let retryTimer = null
  let retries = 0
  let closed = false

  function setStatus(s) {
    if (onStatus) onStatus(s)
  }

  function open() {
    if (closed) return
    setStatus(WS_CONNECTING)
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    try {
      ws = new WebSocket(`${proto}://${window.location.host}${path}`)
    } catch {
      scheduleRetry()
      return
    }
    ws.onopen = () => {
      const token = getToken()
      // 有 token 发首帧鉴权；无 token 等服务端 4401 → onclose → 降级轮询
      if (token) {
        try { ws.send(JSON.stringify({ type: 'auth', token })) } catch { /* ignore */ }
      }
      retries = 0
      setStatus(WS_OPEN)
      clearInterval(pingTimer)
      pingTimer = setInterval(() => {
        try { ws.send(JSON.stringify({ msg_type: 'ping' })) } catch { /* ignore */ }
      }, pingMs)
    }
    ws.onmessage = (ev) => {
      if (!onMessage) return
      try { onMessage(JSON.parse(ev.data)) } catch { /* 非 JSON 帧忽略 */ }
    }
    ws.onerror = () => {
      try { ws.close() } catch { /* ignore */ }
    }
    ws.onclose = () => {
      clearInterval(pingTimer)
      setStatus(WS_DEGRADED)
      scheduleRetry()
    }
  }

  function scheduleRetry() {
    if (closed) return
    retries += 1
    clearTimeout(retryTimer)
    retryTimer = setTimeout(open, Math.min(30000, 5000 * retries))
  }

  open()
  return {
    close() {
      closed = true
      clearTimeout(retryTimer)
      clearInterval(pingTimer)
      try { ws && ws.close() } catch { /* ignore */ }
    },
  }
}
