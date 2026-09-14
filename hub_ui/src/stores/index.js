import { defineStore } from 'pinia'

// 全局 UI 状态：抽屉 / ⌘K / 密度 / 登录
export const useUiStore = defineStore('ui', {
  state: () => ({
    drawer: null,          // { title, lines: [{k, v}], raw } | null
    paletteOpen: false,
    density: localStorage.getItem('ui.density') || 'default',
    loggedOut: false,
  }),
  actions: {
    openDrawer(payload) { this.drawer = payload },
    closeDrawer() { this.drawer = null },
    togglePalette() { this.paletteOpen = !this.paletteOpen },
    setDensity(d) {
      this.density = d
      localStorage.setItem('ui.density', d)
      document.documentElement.dataset.density =
        d === 'compact' ? 'compact' : d === 'relaxed' ? 'relaxed' : ''
    },
  },
})

// 总览数据（真实 API，30s 轮询由视图驱动）
import { api } from '../api'

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    data: null,
    buffer: null,
    dbStats: null,
    health: { live: null, ready: null },
    loading: false,
    error: '',
    lastFetch: 0,
  }),
  actions: {
    async refresh() {
      this.loading = true
      this.error = ''
      try {
        const [dash, buf, dbs, live, ready] = await Promise.allSettled([
          api('/api/v1/dashboard'),
          api('/api/v1/buffer/stats'),
          api('/api/v1/maintenance/db-stats'),
          fetch('/healthz').then((r) => r.ok),
          fetch('/readyz').then((r) => r.ok),
        ])
        if (dash.status === 'fulfilled') this.data = dash.value
        else this.error = dash.reason?.message || '加载失败'
        if (buf.status === 'fulfilled') this.buffer = buf.value
        if (dbs.status === 'fulfilled') this.dbStats = dbs.value
        this.health.live = live.status === 'fulfilled' ? live.value : false
        this.health.ready = ready.status === 'fulfilled' ? ready.value : false
        this.lastFetch = Date.now()
      } finally {
        this.loading = false
      }
    },
  },
})

// ── U2：运行 Ops 数据（配额 / 缓冲 / 降级 / 备份） ──
export const useOpsStore = defineStore('ops', {
  state: () => ({
    quotas: null,        // /api/v1/agents/quota
    backup: null,        // /api/v1/maintenance/backup-status
    healthFull: null,    // /health（降级状态 / 磁盘 / ChromaDB / 告警）
    bufferLive: null,    // /ws/buffer 实时帧（断线时由 refresh 的 HTTP 值兜底）
    bufferWs: '',        // open | connecting | degraded
    error: '',
    lastFetch: 0,
  }),
  actions: {
    async refresh() {
      this.error = ''
      const [quota, backup, health, buf] = await Promise.allSettled([
        api('/api/v1/agents/quota'),
        api('/api/v1/maintenance/backup-status'),
        fetch('/health').then((r) => r.json()),
        api('/api/v1/buffer/stats'),
      ])
      if (quota.status === 'fulfilled') this.quotas = quota.value
      else this.error = quota.reason?.message || '配额加载失败'
      if (backup.status === 'fulfilled') this.backup = backup.value
      if (health.status === 'fulfilled') this.healthFull = health.value
      // WS 断开时才用 HTTP 值覆盖缓冲显示（WS 活着以实时帧为准）
      if (buf.status === 'fulfilled' && this.bufferWs !== 'open') this.bufferLive = buf.value
      this.lastFetch = Date.now()
    },
    setBufferWs(status) { this.bufferWs = status },
    setBufferLive(stats) { this.bufferLive = stats },
  },
})

// ── U2：活动 Activity 事件流（/ws/dashboard + 通知种子） ──
const EVENT_CAP = 200

function eventCategory(type) {
  if (!type) return 'other'
  if (type.startsWith('task_')) return 'task'
  if (type.startsWith('disclosure_')) return 'disclosure'
  if (type.startsWith('agent_')) return 'agent'
  if (type === 'security') return 'security'
  if (type === 'wiki_inbox') return 'wiki'
  return 'other'
}

function normalizeEvent(raw) {
  const now = new Date().toISOString()
  // 通知推送：{type:'push', event:'notification', data:{...}}
  if (raw && raw.type === 'push' && raw.event === 'notification') {
    const d = raw.data || {}
    return {
      key: `push-${d.id || now}-${Math.random()}`,
      cat: eventCategory(d.type),
      type: d.type || 'notification',
      summary: `${d.title || ''}${d.body ? ' — ' + d.body : ''}`.trim() || '通知',
      time: d.created_at || now,
      raw,
    }
  }
  // 瞬态广播：{type:'task_assigned'|...}
  const t = (raw && raw.type) || 'unknown'
  const S = {
    agent_online: () => `Agent 上线：${raw.agent_id}`,
    agent_offline: () => `Agent 掉线：${raw.agent_id}`,
    task_assigned: () => `任务指派：${raw.task_id} → ${raw.assigned_to || '?'}`,
    task_completed: () => `任务完成：${raw.task_id}（${raw.agent_id || '?'}）`,
    task_failed: () => `任务失败：${raw.task_id}（${raw.agent_id || '?'}）`,
    disclosure_request: () => `披露申请：${raw.agent_id} 申请任务 ${raw.task_id} 升至 P${raw.new_phase}`,
    disclosure_approved: () => `披露批准：${raw.request_id} 升至 P${raw.new_phase}`,
    disclosure_denied: () => `披露拒绝：${raw.request_id}`,
  }
  const build = S[t]
  return {
    key: `ev-${t}-${now}-${Math.random()}`,
    cat: eventCategory(t),
    type: t,
    summary: build ? build() : JSON.stringify(raw).slice(0, 120),
    time: raw.timestamp || now,
    raw,
  }
}

export const useActivityStore = defineStore('activity', {
  state: () => ({
    events: [],          // 新事件在头部
    wsStatus: '',        // open | connecting | degraded
    paused: false,
    seeded: false,
    error: '',
  }),
  actions: {
    pushRaw(raw) {
      if (this.paused) return
      if (raw && raw.msg_type === 'pong') return
      this.events.unshift(normalizeEvent(raw))
      if (this.events.length > EVENT_CAP) this.events.length = EVENT_CAP
    },
    setWsStatus(s) { this.wsStatus = s },
    togglePause() { this.paused = !this.paused },
    clear() { this.events = [] },
    async seed() {
      // 种子数据：__dashboard__ 通道的持久通知（瞬态任务事件不落库，仅实时可见）
      this.error = ''
      try {
        const d = await api('/api/v1/notifications', {
          query: { agent_id: '__dashboard__', limit: 50 },
        })
        const items = (d.notifications || []).map((n) => normalizeEvent({
          type: 'push', event: 'notification', data: n,
        }))
        // 与实时事件按时间合并去重（同 id 通知只留一份）
        const seen = new Set(this.events.map((e) => e.raw?.data?.id).filter(Boolean))
        for (const e of items) {
          const id = e.raw?.data?.id
          if (id && seen.has(id)) continue
          this.events.push(e)
        }
        this.events.sort((a, b) => (b.time || '').localeCompare(a.time || ''))
        if (this.events.length > EVENT_CAP) this.events.length = EVENT_CAP
        this.seeded = true
      } catch (e) {
        this.error = e.message || '种子数据加载失败'
        this.seeded = true
      }
    },
  },
})

// ── U3：访问与权限 ──
export const useAccessStore = defineStore('access', {
  state: () => ({
    accounts: null,      // /api/v1/access/accounts
    exceptions: null,    // /api/v1/access/exceptions
    keys: null,          // /api/v1/keys
    error: '',
    lastFetch: 0,
  }),
  actions: {
    async refresh() {
      this.error = ''
      const [acc, exc, keys] = await Promise.allSettled([
        api('/api/v1/access/accounts'),
        api('/api/v1/access/exceptions'),
        api('/api/v1/keys'),
      ])
      if (acc.status === 'fulfilled') this.accounts = acc.value
      else this.error = acc.reason?.message || '账号加载失败'
      if (exc.status === 'fulfilled') this.exceptions = exc.value
      if (keys.status === 'fulfilled') this.keys = keys.value
      this.lastFetch = Date.now()
    },
    async createKey(payload) {
      return api('/api/v1/keys', { method: 'POST', body: payload })
    },
    async revokeKey(keyId) {
      return api(`/api/v1/keys/${encodeURIComponent(keyId)}`, { method: 'DELETE' })
    },
  },
})

// ── U3：审计中心 ──
export const useAuditStore = defineStore('audit', {
  state: () => ({
    lastVerify: null,    // /api/audit/last-verify
    result: null,        // /api/audit/events 检索结果 {total, rows, facets}
    verifying: false,
    searching: false,
    error: '',
  }),
  actions: {
    async refreshVerify() {
      try {
        this.lastVerify = await api('/api/audit/last-verify')
      } catch (e) {
        this.error = e.message || '链状态加载失败'
      }
    },
    async runVerify() {
      this.verifying = true
      this.error = ''
      try {
        const r = await api('/api/audit/verify', { method: 'POST', body: {} })
        await this.refreshVerify()
        return r
      } catch (e) {
        this.error = e.message || '校验失败'
        return null
      } finally {
        this.verifying = false
      }
    },
    async search(filters = {}) {
      this.searching = true
      this.error = ''
      try {
        this.result = await api('/api/audit/events', { query: { ...filters, limit: filters.limit || 50 } })
      } catch (e) {
        this.error = e.message || '检索失败'
      } finally {
        this.searching = false
      }
    },
  },
})

// ── U4：披露治理（规则表 + 模拟器 + shadow 差异） ──
export const useDisclosureStore = defineStore('disclosure', {
  state: () => ({
    rules: null,         // /api/audit/disclosure/rules
    simResult: null,     // 模拟结果 {level, hit_rule, trace, inputs}
    simulating: false,
    replay: null,        // shadow 差异报告
    replaying: false,
    error: '',
  }),
  actions: {
    async loadRules() {
      try {
        this.rules = (await api('/api/audit/disclosure/rules')).rules || []
      } catch (e) {
        this.error = e.message || '规则表加载失败'
      }
    },
    async simulate(payload) {
      this.simulating = true
      this.error = ''
      try {
        this.simResult = await api('/api/audit/disclosure/simulate', { method: 'POST', body: payload })
        return this.simResult
      } catch (e) {
        this.error = e.message || '模拟失败'
        return null
      } finally {
        this.simulating = false
      }
    },
    async runReplay() {
      this.replaying = true
      this.error = ''
      try {
        this.replay = await api('/api/audit/disclosure/replay', { method: 'POST', body: {} })
      } catch (e) {
        this.error = e.message || '重放失败'
      } finally {
        this.replaying = false
      }
    },
  },
})

// ── U4：知识库 ──
export const useKnowledgeStore = defineStore('knowledge', {
  state: () => ({ entries: null, error: '', lastFetch: 0 }),
  actions: {
    async refresh() {
      this.error = ''
      try {
        const d = await api('/api/v1/knowledge')
        this.entries = d.entries || d.knowledge || d.items || (Array.isArray(d) ? d : [])
        this.lastFetch = Date.now()
      } catch (e) {
        this.error = e.message || '知识库加载失败'
      }
    },
  },
})

// ── U4：Wiki（页面 + 收件箱审查） ──
export const useWikiStore = defineStore('wiki', {
  state: () => ({ pages: null, inbox: null, searchResult: null, searching: false, error: '' }),
  actions: {
    async refresh() {
      this.error = ''
      const [pages, inbox] = await Promise.allSettled([
        api('/api/v1/wiki/pages'),
        api('/api/v1/wiki/inbox'),
      ])
      if (pages.status === 'fulfilled') this.pages = pages.value.pages || []
      else this.error = pages.reason?.message || 'Wiki 加载失败'
      if (inbox.status === 'fulfilled') this.inbox = inbox.value.items || inbox.value.inbox || []
    },
    async search(q) {
      if (!q) { this.searchResult = null; return }
      this.searching = true
      try {
        const d = await api('/api/v1/wiki/search', { query: { q } })
        this.searchResult = d.results || d.pages || []
      } catch (e) {
        this.error = e.message || '搜索失败'
      } finally {
        this.searching = false
      }
    },
    async readPage(path) {
      return api(`/api/v1/wiki/page/${encodeURIComponent(path)}`)
    },
    async review(inboxId, action) {
      const r = await api(`/api/v1/wiki/inbox/${inboxId}/${action}`, { method: 'POST' })
      await this.refresh()
      return r
    },
  },
})

// ── U4：记忆池（按 Agent 查看 + 搜索） ──
export const useMemoryStore = defineStore('memory', {
  state: () => ({ agentId: '', memories: null, loading: false, error: '' }),
  actions: {
    async load(agentId, kind = '') {
      this.agentId = agentId
      if (!agentId) { this.memories = null; return }
      this.loading = true
      this.error = ''
      try {
        const d = await api('/api/v1/memory', { query: { agent_id: agentId, kind } })
        this.memories = d.memories || d.items || (Array.isArray(d) ? d : [])
      } catch (e) {
        this.error = e.message || '记忆加载失败'
        this.memories = []
      } finally {
        this.loading = false
      }
    },
    async versions(memoryKey) {
      return api(`/api/v1/memory/${encodeURIComponent(memoryKey)}/versions`, {
        query: { agent_id: this.agentId },
      })
    },
  },
})

// ── 集成层（§七 门框）：连接器管理 ──
export const useIntegrationsStore = defineStore('integrations', {
  state: () => ({
    connectors: null, available: null, error: '',
    testing: '', pulling: '', pullResult: null, saving: false,
  }),
  actions: {
    async refresh() {
      this.error = ''
      const [list, avail] = await Promise.allSettled([
        api('/api/v1/integrations'),
        api('/api/v1/integrations/meta/available'),
      ])
      if (list.status === 'fulfilled') this.connectors = list.value.connectors || []
      else this.error = list.reason?.message || '连接器列表加载失败'
      if (avail.status === 'fulfilled') this.available = avail.value.available || []
    },
    async test(name) {
      this.testing = name
      try {
        return await api(`/api/v1/integrations/${name}/test`, { method: 'POST' })
      } finally { this.testing = '' }
    },
    async pull(name, full = false) {
      this.pulling = name
      this.pullResult = null
      try {
        const r = await api(`/api/v1/integrations/${name}/pull`, {
          method: 'POST', body: { full },
        })
        this.pullResult = { name, ...r }
        await this.refresh()
        return r
      } finally { this.pulling = '' }
    },
    async configure(name, payload) {
      this.saving = true
      try {
        const r = await api(`/api/v1/integrations/${name}/configure`, {
          method: 'POST', body: payload,
        })
        await this.refresh()
        return r
      } finally { this.saving = false }
    },
  },
})

// ── 设置页（⑪）：server config + 词库 + 联邦 + Hub Agent 调试 ──
export const useSettingsStore = defineStore('settings', {
  state: () => ({
    server: null, words: null, wordsSource: '', wordsCount: 0, wordsDenied: false,
    members: null, peers: null, pairCode: '', pairMsg: '',
    haConfig: null, haTest: null, chatLog: [], chatSending: false,
    saving: false, reloading: false, testing: false, error: '',
  }),
  actions: {
    async refreshServer() {
      try { this.server = await api('/api/v1/server/config') }
      catch (e) { this.error = e.message || 'server config 加载失败' }
    },
    async saveServer(payload) {
      this.saving = true
      try {
        const r = await api('/api/v1/server/config', { method: 'POST', body: payload })
        await this.refreshServer()
        return r
      } finally { this.saving = false }
    },
    async refreshWords(agentId) {
      this.wordsDenied = false
      try {
        const d = await api('/api/v1/sensitivity/words', { query: { agent_id: agentId } })
        this.words = d.words || []
        this.wordsSource = d.source || ''
        this.wordsCount = d.count || 0
      } catch (e) {
        if (String(e.message).includes('403')) { this.wordsDenied = true; this.words = [] }
        else this.error = e.message || '词库加载失败'
      }
    },
    async reloadWords(agentId, reclassify = true) {
      this.reloading = true
      try {
        return await api('/api/v1/sensitivity/words/reload', {
          method: 'POST', body: { reclassify }, query: { agent_id: agentId },
        })
      } finally { this.reloading = false }
    },
    async refreshTeam(agentId) {
      const [m, p] = await Promise.allSettled([
        api('/api/v1/team/members', { query: { agent_id: agentId } }),
        api('/api/v1/team/discover', { query: { agent_id: agentId } }),
      ])
      if (m.status === 'fulfilled') this.members = m.value.members || m.value || []
      if (p.status === 'fulfilled') this.peers = p.value.peers || []
    },
    async pairRequest(agentId) {
      const r = await api('/api/v1/team/pair/request', { method: 'POST', query: { agent_id: agentId } })
      this.pairCode = r.code || r.pair_code || ''
      return r
    },
    async pairAccept(agentId, code) {
      const r = await api('/api/v1/team/pair/accept', {
        method: 'POST', body: { code }, query: { agent_id: agentId },
      })
      await this.refreshTeam(agentId)
      return r
    },
    async removeMember(agentId, memberId) {
      await api(`/api/v1/team/members/${memberId}`, { method: 'DELETE', query: { agent_id: agentId } })
      await this.refreshTeam(agentId)
    },
    async refreshHubAgent() {
      try { this.haConfig = await api('/api/v1/hub-agent/config') }
      catch (e) { this.error = e.message || 'Hub Agent 配置加载失败' }
    },
    async testHubAgent() {
      this.testing = true
      try { this.haTest = await api('/api/v1/hub-agent/test', { method: 'POST' }); return this.haTest }
      finally { this.testing = false }
    },
    async chat(message) {
      this.chatSending = true
      this.chatLog.push({ role: 'user', content: message, ts: Date.now() })
      try {
        const r = await api('/api/v1/hub-agent/chat', { method: 'POST', body: { message } })
        this.chatLog.push({ role: 'assistant', content: r.reply || r.response || JSON.stringify(r), ts: Date.now() })
        return r
      } catch (e) {
        this.chatLog.push({ role: 'error', content: e.message || 'chat 失败', ts: Date.now() })
      } finally { this.chatSending = false }
    },
  },
})
