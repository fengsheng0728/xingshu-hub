// HubStatusBanner：全局 Hub 状态横幅（B2，设计文档 §6 错误降级）
// 三种显式状态，绝不静默吞错：
// 1. 网关不可达（reachable:false）→ 红色横幅「Hub 离线，数据为缓存/本地态」+ 原因 + 重试
// 2. 业务降级（degraded:true，CD-016 语义：可用结果）→ 黄色横幅，与「不可达」分开标注
// 3. 401 鉴权失败（authError 非空）→ key 配置引导（设置入口：localStorage 保存，供 hubRequest 注入）
// 探测：挂载时 + 每 30s 轮询 Node /readyz（免鉴权，含 Python /readyz 透传的 degraded 标记）
import { useEffect, useState } from 'react'
import { AlertTriangle, KeyRound, RefreshCw, WifiOff } from 'lucide-react'
import { checkHubReadiness, setStoredKey, useHubStatus } from '../lib/hub-client'

const POLL_INTERVAL_MS = 30_000

export default function HubStatusBanner() {
  const status = useHubStatus()
  const [checking, setChecking] = useState(false)
  const [keyInput, setKeyInput] = useState('')

  useEffect(() => {
    let cancelled = false
    const probe = async () => {
      setChecking(true)
      try {
        await checkHubReadiness()
      } finally {
        if (!cancelled) setChecking(false)
      }
    }
    probe()
    const timer = setInterval(probe, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  const retry = () => {
    setChecking(true)
    checkHubReadiness().finally(() => setChecking(false))
  }

  // —— 401 引导（§4 统一错误处理：401 → key 配置引导，不复用错误页） ——
  if (status.authError) {
    return (
      <div className="border-b border-amber-300 bg-amber-50">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-3 px-4 py-2 text-sm text-amber-900">
          <KeyRound className="h-4 w-4 shrink-0" />
          <span>
            鉴权失败（401）：{status.authError}。请配置 scoped key（由管理员经 Python 侧 POST /api/v1/keys 签发）。
          </span>
          <input
            type="password"
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            placeholder="sk-..."
            className="w-56 rounded-md border border-amber-300 bg-white px-2 py-1 text-xs text-zinc-900 placeholder:text-zinc-400"
          />
          <button
            onClick={() => {
              setStoredKey(keyInput.trim() || null)
              setKeyInput('')
              retry()
            }}
            className="rounded-md border border-amber-400 bg-white px-2 py-1 text-xs hover:bg-amber-100"
          >
            保存并重试
          </button>
        </div>
      </div>
    )
  }

  // —— 网关不可达 ——
  if (status.reachable === false) {
    return (
      <div className="border-b border-red-300 bg-red-50">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-3 px-4 py-2 text-sm text-red-900">
          <WifiOff className="h-4 w-4 shrink-0" />
          <span>
            Hub 离线，数据为缓存/本地态
            {status.reason ? <span className="ml-1 text-red-700/70">（{status.reason}）</span> : null}
          </span>
          <button
            onClick={retry}
            disabled={checking}
            className="inline-flex items-center gap-1 rounded-md border border-red-300 bg-white px-2 py-1 text-xs hover:bg-red-100 disabled:opacity-50"
          >
            <RefreshCw className={checking ? 'h-3 w-3 animate-spin' : 'h-3 w-3'} />
            重试
          </button>
        </div>
      </div>
    )
  }

  // —— 业务降级（可用结果，与不可达分开标注，§6.4） ——
  if (status.degraded) {
    return (
      <div className="border-b border-amber-300 bg-amber-50">
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-4 py-2 text-sm text-amber-900">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>治理引擎降级运行：语义检索已降级为关键词检索（degraded），结果可用但精度受限。</span>
        </div>
      </div>
    )
  }

  return null
}
