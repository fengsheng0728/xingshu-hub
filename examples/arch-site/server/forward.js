// 转发模块（B2 起由 index.js /hub-api 挂载使用）
// 语义对齐设计文档 §2.1：浏览器绝不直连 Python，一律经 Node 转发，鉴权头在此注入，错误统一归一化为 {error} JSON
import { config } from './config.js'

export class HubUnreachableError extends Error {
  constructor(message) {
    super(message)
    this.name = 'HubUnreachableError'
  }
}

// 带超时的 fetch（Node 18+ 内置 fetch + AbortController）
async function fetchWithTimeout(url, options = {}, timeoutMs = 5000) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    return await fetch(url, { ...options, signal: ctrl.signal })
  } finally {
    clearTimeout(timer)
  }
}

// 探测 Python 侧健康探针；失败抛 HubUnreachableError
export async function probeHub(path, timeoutMs = 5000) {
  try {
    return await fetchWithTimeout(`${config.hubBaseUrl}${path}`, {}, timeoutMs)
  } catch (err) {
    throw new HubUnreachableError(`hub_unreachable: ${err.message}`)
  }
}

// 向 Python 网关转发业务请求（B2 起由 index.js /hub-api 挂载调用）
// 约定：body 为对象时按 JSON 发送；网关不可达抛 HubUnreachableError，由调用方转成降级响应/502，禁止 5xx 透传白屏（§6）
// 状态码与响应体透传（§2.4 错误码约定：401/403/404/degraded/unreachable 由调用方处理）
export async function forwardToHub({ path, method = 'GET', body, headers = {}, key, timeoutMs = 10000 }) {
  if (!path.startsWith('/')) throw new Error('forwardToHub: path 需以 / 开头')
  const init = {
    method,
    headers: { ...headers },
  }
  if (key) init.headers.Authorization = `Bearer ${key}`
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = typeof body === 'string' ? body : JSON.stringify(body)
  }
  // 状态码透传；超时/连接失败归一化为 HubUnreachableError（§2.4）
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    return await fetch(`${config.hubBaseUrl}${path}`, { ...init, signal: ctrl.signal })
  } catch (err) {
    throw new HubUnreachableError(`hub_unreachable: ${err.message}`)
  } finally {
    clearTimeout(timer)
  }
}
