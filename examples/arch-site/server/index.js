// arch-site Node 边车壳入口（B2）
// 职责：静态托管 dist/、/healthz、/readyz（探测 Python 网关，透传 degraded 语义）、/hub-api 反代 → Python :3060、scoped key 鉴权、统一错误处理 {error} JSON
import express from 'express'
import cors from 'cors'
import { config } from './config.js'
import { authMiddleware } from './auth.js'
import { probeHub, forwardToHub, HubUnreachableError } from './forward.js'

import { fileURLToPath } from 'node:url'

const distDir = fileURLToPath(new URL('../dist/', import.meta.url))
const indexFile = fileURLToPath(new URL('../dist/index.html', import.meta.url))

const app = express()

app.use(cors())
app.use(express.json())

// —— 免鉴权探针 ——
app.get('/healthz', (req, res) => {
  res.json({ ok: true, version: config.version })
})

// 就绪探针：探测 Python 网关 /readyz（含 ChromaDB degraded 状态，§6.5），5s 超时；
// 失败返回 {ready:false, reason:"hub_unreachable"}，不抛错；成功时透传 Python 侧 body（含 degraded 标记）
app.get('/readyz', async (req, res) => {
  try {
    const r = await probeHub('/readyz', 5000)
    if (!r.ok) {
      return res.json({ ready: false, reason: 'hub_unhealthy: HTTP ' + String(r.status) })
    }
    let hub = null
    try {
      hub = await r.json()
    } catch {
      hub = null
    }
    res.json({ ready: true, degraded: hub?.degraded === true, hub })
  } catch (err) {
    res.json({ ready: false, reason: 'hub_unreachable' })
  }
})

// —— /hub-api/* 反代 → Python 网关（B2，§2.1：浏览器绝不直连 Python，一律经 Node 转发） ——
// 鉴权中间件对本挂载全量生效；通过后将请求方携带的 scoped key 透传为 Python 侧 Authorization 头
app.use('/hub-api', authMiddleware)
app.use('/hub-api', async (req, res, next) => {
  try {
    const hasBody = req.method !== 'GET' && req.method !== 'HEAD' && req.body !== undefined
    const upstream = await forwardToHub({
      path: req.url, // 挂载点内相对路径 + querystring
      method: req.method,
      body: hasBody ? req.body : undefined,
      key: req.hubKey,
    })
    const text = await upstream.text()
    const contentType = upstream.headers.get('content-type')
    if (contentType) res.setHeader('content-type', contentType)
    res.status(upstream.status).send(text)
  } catch (err) {
    if (err instanceof HubUnreachableError) {
      // §6：网关不可达 → 502 包装 JSON（非静默、绝不白屏透传），调用方按降级语义处理
      return res.status(502).json({
        error: 'hub_unreachable',
        detail: err.message,
        degraded: true,
        hint: 'Python 治理引擎不可达，请检查 Hub(:3060) 是否在线',
      })
    }
    next(err)
  }
})

// —— /api/* 业务路由（B1：鉴权后仅骨架响应；B3+ 在此挂载 Inbox 等 Node 本地路由）
app.use('/api', authMiddleware)
app.use('/api', (req, res) => {
  res.status(501).json({ error: 'not_implemented: B1 阶段未挂载转发路由' })
})

// —— 静态托管 dist/ ——
app.use(express.static(distDir))

// SPA 回退：非 /api、非 /hub-api 路径回 index.html（静态站单页路由）
app.use((req, res, next) => {
  if (req.method !== 'GET') return next()
  if (req.path.startsWith('/api') || req.path.startsWith('/hub-api') || req.path === '/healthz' || req.path === '/readyz') return next()
  res.sendFile(indexFile, (err) => {
    if (err) next()
  })
})

// 404：统一 {error} JSON
app.use((req, res) => {
  res.status(404).json({ error: 'not_found' })
})

// 统一错误处理：一律返回 {error} JSON
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error('[arch-site-server] 未捕获错误:', err.message)
  res.status(err.status || 500).json({ error: err.message || 'internal_error' })
})

app.listen(config.port, () => {
  console.log('[arch-site-server] ' + config.name + '@' + config.version + ' 已启动 :' + config.port + '，Hub 基址 ' + config.hubBaseUrl)
})
