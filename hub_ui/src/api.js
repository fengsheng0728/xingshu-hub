// fetch 封装：Bearer 注入 + 401 跳登录（§四 技术路线）
// 登录占位：现阶段使用 hub_token（S1-SMB 账号体系就绪后接口层不变）
const TOKEN_KEY = 'hub_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(t) {
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`)
    this.status = status
  }
}

export async function api(path, { method = 'GET', body, query } = {}) {
  const url = new URL(path, window.location.origin)
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v)
    }
  }
  const headers = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const resp = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (resp.status === 401) {
    // 401 → 清 token 触发登录态（App 监听 storage/自定义事件）
    setToken('')
    window.dispatchEvent(new CustomEvent('hub:unauthorized'))
    throw new ApiError(401, 'Unauthorized')
  }
  if (!resp.ok) {
    let detail = ''
    try { detail = (await resp.json()).detail || '' } catch { /* ignore */ }
    throw new ApiError(resp.status, detail)
  }
  return resp.json()
}
