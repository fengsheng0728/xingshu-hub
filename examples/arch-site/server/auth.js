// scoped key 鉴权中间件（B1 本地层）
// 契约：请求头 Authorization: Bearer sk-<token>
// B1 阶段只做本地壳——「有 key 则校验格式、无 key 拒绝」；转发 Python 由 forward.js 负责，真实 scope 判定在 Python 侧（key_scopes.py 三层 scope）。
// 本地配置 key（ARCH_SITE_HUB_KEY / server/config.json）用于校验请求携带的 key 是否为本层认可的 scoped key。
import { config } from './config.js'

// scoped key 本地格式：sk- 前缀 + 至少 16 位 token 体（Python 侧 ScopedKeyStore 按 SHA256 哈希匹配，明文不过 Node 日志）
const KEY_FORMAT = /^sk-[A-Za-z0-9_-]{16,}$/

// 说明：Node 自身的 /healthz、/readyz 探针在 index.js 中于本中间件挂载点之前注册，天然免鉴权；
// 本中间件挂载范围内（/api、/hub-api）一律要求鉴权，不做路径白名单（B2：/hub-api/healthz 也须带 key 透传）。

export function authMiddleware(req, res, next) {
  const header = req.headers.authorization
  if (!header || !header.startsWith('Bearer ')) {
    return res.status(401).json({ error: '缺少有效的 API Key' })
  }

  const token = header.slice('Bearer '.length).trim()
  if (!KEY_FORMAT.test(token)) {
    return res.status(401).json({ error: 'key 格式非法，期望 Authorization: Bearer sk-<token>' })
  }

  // 本地已配置 key 时要求精确匹配；未配置任何 key 时（开发态）只验格式、放行，由 Python 侧做真实 scope 判定
  if (config.keys.size > 0 && !config.keys.has(token)) {
    return res.status(401).json({ error: '未知或未授权的 scoped key' })
  }

  // 交给转发层/下游路由
  req.hubKey = token
  next()
}
