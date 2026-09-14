// hub-client：浏览器 → Node 边车壳（:7101）→ Python 网关（:3060）的统一 fetch 封装（B2）
// 契约对齐设计文档 §2.1 / §2.4：
// - 浏览器绝不直连 Python；一律经 Node（dev 下经 Vite proxy /api → :7101）
// - 基址可配置：import.meta.env.VITE_HUB_BASE（默认空串 = 同源，走 proxy/静态托管）
// - 鉴权头注入：Authorization: Bearer sk-<token>
// - 错误归一化：{ code: 401|403|404|degraded|unreachable, ... }；绝不静默吞错
// - Hub 状态源（B2）：模块级 store + useSyncExternalStore，驱动 HubStatusBanner 升起/撤下（§6）
import { useSyncExternalStore } from 'react'

/** 统一错误码（§2.4 错误码约定的前端侧映射） */
export type HubErrorCode = 'unauthorized' | 'forbidden' | 'not_found' | 'bad_request' | 'degraded' | 'unreachable' | 'unknown'

export interface HubError {
  code: HubErrorCode
  status?: number
  message: string
}

export class HubClientError extends Error {
  readonly code: HubErrorCode
  readonly status?: number

  constructor(err: HubError) {
    super(err.message)
    this.name = 'HubClientError'
    this.code = err.code
    this.status = err.status
  }
}

/** 网关语义检索降级标记（§2.4：HTTP 200 + degraded:true 是可用结果，不是错误） */
export interface DegradedResult<T> {
  data: T
  degraded: boolean
}

export interface HubRequestOptions {
  method?: 'GET' | 'POST' | 'DELETE'
  /** body 为对象时按 JSON 发送 */
  body?: unknown
  /** scoped key（sk-...）；缺省时回退 localStorage 中 401 引导保存的 key，仍无则不注入鉴权头 */
  key?: string
  /** 超时毫秒，默认 10s（§4） */
  timeoutMs?: number
  /** 额外请求头 */
  headers?: Record<string, string>
}

// —— scoped key 本地存储（401 引导入口写入；仅存浏览器 localStorage，不进代码库、不过日志） ——
const KEY_STORAGE = 'arch_site_hub_key'

export function getStoredKey(): string | null {
  try {
    return window.localStorage.getItem(KEY_STORAGE)
  } catch {
    return null
  }
}

export function setStoredKey(key: string | null): void {
  try {
    if (key) window.localStorage.setItem(KEY_STORAGE, key)
    else window.localStorage.removeItem(KEY_STORAGE)
  } catch {
    // localStorage 不可用（隐私模式等）时静默降级为「仅本次会话内存态」——不清除既有状态
  }
}

// —— Hub 状态源（§6：驱动全局横幅的升起/撤下；模块级 store + 订阅，最简实现，不引状态库） ——
export interface HubStatus {
  /** null = 尚未探测；true = 网关就绪；false = 不可达/不就绪 */
  reachable: boolean | null
  /** 业务降级（Python readyz/响应体 degraded:true，CD-016 语义：可用结果，与不可达分开标注） */
  degraded: boolean
  /** 不可达原因（hub_unreachable / hub_unhealthy: HTTP xxx 等） */
  reason?: string
  /** 最近一次探测时间戳（ms） */
  checkedAt?: number
  /** 最近一次 401 信息（非空时前端显示 key 配置引导，§4 统一错误处理） */
  authError: string | null
}

let hubStatus: HubStatus = { reachable: null, degraded: false, authError: null }
const listeners = new Set<() => void>()

export function getHubStatus(): HubStatus {
  return hubStatus
}

export function subscribeHubStatus(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function patchHubStatus(patch: Partial<HubStatus>): void {
  hubStatus = { ...hubStatus, ...patch }
  listeners.forEach((l) => l())
}

/** React 侧订阅 Hub 状态（HubStatusBanner 等组件使用） */
export function useHubStatus(): HubStatus {
  return useSyncExternalStore(subscribeHubStatus, getHubStatus)
}

/**
 * 探测 Node /readyz（免鉴权）并更新状态源（§6.5）。
 * 不抛错：任何失败都落为 reachable:false + reason，由横幅显式呈现（不静默吞错）。
 */
export async function checkHubReadiness(timeoutMs = 6000): Promise<HubStatus> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(resolveBase() + '/readyz', { signal: ctrl.signal })
    const body = (await res.json().catch(() => null)) as { ready?: boolean; reason?: string; degraded?: boolean } | null
    if (body?.ready === true) {
      patchHubStatus({ reachable: true, degraded: body.degraded === true, reason: undefined, checkedAt: Date.now() })
    } else {
      patchHubStatus({ reachable: false, reason: body?.reason ?? 'readyz_not_ready', checkedAt: Date.now() })
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    patchHubStatus({ reachable: false, reason: `node_unreachable: ${message}`, checkedAt: Date.now() })
  } finally {
    clearTimeout(timer)
  }
  return hubStatus
}

function resolveBase(): string {
  // Vite 注入；缺省空串 = 同源（dev 走 proxy，生产走 Node 静态托管同端口）
  return import.meta.env.VITE_HUB_BASE ?? ''
}

function mapStatusToCode(status: number): HubErrorCode {
  if (status === 401) return 'unauthorized'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 400) return 'bad_request'
  return 'unknown'
}

/**
 * 统一 fetch 封装。
 * 抛 HubClientError（绝不静默吞错）；
 * 返回 DegradedResult 以透传 Python 侧 degraded:true（调用方据此渲染降级横幅）。
 */
export async function hubRequest<T = unknown>(path: string, options: HubRequestOptions = {}): Promise<DegradedResult<T>> {
  const { method = 'GET', body, timeoutMs = 10000, headers = {} } = options
  const key = options.key ?? getStoredKey() ?? undefined

  const init: RequestInit = {
    method,
    headers: { ...headers },
  }
  if (key) {
    ;(init.headers as Record<string, string>).Authorization = `Bearer ${key}`
  }
  if (body !== undefined) {
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
    init.body = typeof body === 'string' ? body : JSON.stringify(body)
  }

  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)

  let res: Response
  try {
    res = await fetch(resolveBase() + path, { ...init, signal: ctrl.signal })
  } catch (err) {
    // 连接失败/超时/中断 → unreachable（§2.4：网关不可达，Node/前端降级，绝不白屏）
    const message = err instanceof Error ? err.message : String(err)
    patchHubStatus({ reachable: false, reason: `unreachable: ${message}`, checkedAt: Date.now() })
    throw new HubClientError({ code: 'unreachable', message: `hub_unreachable: ${message}` })
  } finally {
    clearTimeout(timer)
  }

  let payload: unknown = null
  const text = await res.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!res.ok) {
    const message =
      typeof payload === 'object' && payload !== null && 'error' in payload
        ? String((payload as { error: unknown }).error)
        : `HTTP ${res.status}`
    if (res.status === 401) {
      // §4 统一错误处理：401 → 状态源记账，前端渲染 key 配置引导（不复用错误页、不静默）
      patchHubStatus({ authError: message })
    }
    throw new HubClientError({ code: mapStatusToCode(res.status), status: res.status, message })
  }

  // 成功路径：清除 401 引导、确认可达；degraded:true 透传给调用方渲染业务降级提示（§2.4/§6.4）
  if (hubStatus.authError) patchHubStatus({ authError: null })
  if (hubStatus.reachable === false) patchHubStatus({ reachable: true, reason: undefined })

  const degraded =
    typeof payload === 'object' && payload !== null && (payload as { degraded?: boolean }).degraded === true
  return { data: payload as T, degraded }
}

// —— B3+ 路由类型约定（B2 不接业务数据流，仅类型入库） ——

/** 网关读取（唯一数据通道）：POST /api/v1/gateway/read，kind ∈ semantic/memory/doc */
export type GatewayKind = 'semantic' | 'memory' | 'doc'

export interface GatewayReadRequest {
  kind: GatewayKind
  query?: string
  target_agent_id?: string
  doc_id?: string
  n_results?: number
  required_level?: string
}

export interface GatewayReadResponse {
  items: unknown[]
  degraded?: boolean
  [key: string]: unknown
}

/** Inbox ingest：POST /api/v1/chunks/ingest */
export interface IngestRequest {
  doc_id: string
  content: string
  kind?: string
  source_agent_id?: string
  trust_level?: string
}

export interface IngestResponse {
  chunks?: number
  inserted?: number
  skipped_hash?: number
  locked?: number
  locked_none?: number
  disclosure_level?: string
  [key: string]: unknown
}
