// arch-site Node 边车壳配置加载
// 优先级：环境变量 > server/config.json（本地文件，gitignore，不落库、不进前端 bundle）
import fs from 'node:fs'

// 版本信息：读 package.json，供 /healthz 返回
function loadVersion() {
  try {
    const pkgUrl = new URL('../package.json', import.meta.url)
    const raw = fs.readFileSync(pkgUrl, 'utf8')
    const pkg = JSON.parse(raw)
    return { name: pkg.name, version: pkg.version }
  } catch {
    return { name: 'arch-site-server', version: '0.0.0' }
  }
}

// scoped key：ARCH_SITE_HUB_KEY 环境变量优先；否则读 server/config.json（本地文件，gitignore，不落库、不进前端 bundle）
// 支持 { "hubKey": "sk-..." } 单条，或 { "hubKeys": ["sk-...", ...] } 多把（B2+ 按页分发多把 key）
function loadKeys() {
  const keys = new Set()
  if (process.env.ARCH_SITE_HUB_KEY) keys.add(process.env.ARCH_SITE_HUB_KEY)

  const cfgUrl = new URL('./config.json', import.meta.url)
  try {
    if (fs.existsSync(cfgUrl)) {
      const raw = fs.readFileSync(cfgUrl, 'utf8')
      const cfg = JSON.parse(raw)
      const list = []
      if (typeof cfg.hubKey === 'string') list.push(cfg.hubKey)
      if (Array.isArray(cfg.hubKeys)) list.push(...cfg.hubKeys)
      list.filter(k => typeof k === 'string' && k).forEach(k => keys.add(k))
    }
  } catch (err) {
    console.warn('[arch-site-server] config.json 读取失败（忽略，继续启动）:', err.message)
  }
  return keys
}

const meta = loadVersion()

export const config = {
  port: Number(process.env.ARCH_SITE_PORT || 7101),
  hubBaseUrl: process.env.HUB_BASE_URL || 'http://127.0.0.1:3060',
  keys: loadKeys(),
  version: meta.version,
  name: meta.name,
}
