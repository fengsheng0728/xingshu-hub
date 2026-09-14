<template>
  <div>
    <div class="page-title">审计中心 Audit</div>
    <div class="page-sub">hash chain 完整性 · 事件检索 · 导出（导出动作本身入审计）</div>

    <div v-if="audit.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> {{ audit.error }}（此页仅 manager/orchestrator 角色可访问）
    </div>

    <!-- 链完整性卡片（常驻顶部） -->
    <div class="panel chain-card">
      <div class="chain-left">
        <div class="kpi-label">链完整性</div>
        <div class="chain-status" :style="{ color: chainColor }">
          <span class="lamp" :class="chainLamp"></span>{{ chainText }}
        </div>
        <div class="kpi-foot" v-if="lv">
          上次校验 {{ fmtTime(lv.created_at) }} · 操作者 {{ lv.actor || '—' }} · 覆盖 {{ lv.checked_total }} 条
        </div>
        <div class="kpi-foot" v-else>尚未执行过校验</div>
        <div class="kpi-foot">
          主链 {{ coverage.audit_log }} 条 · 披露链 {{ coverage.disclosure_log }} 条 · jsonl 锚 {{ coverage.jsonl_anchors }} 个
        </div>
      </div>
      <div class="chain-right">
        <button class="btn primary" :disabled="audit.verifying" @click="doVerify">
          {{ audit.verifying ? '校验中…' : '立即校验' }}
        </button>
      </div>
    </div>

    <div v-if="verifyResult" class="alert-strip" :style="{ color: verifyResult.valid ? 'var(--ok)' : 'var(--danger)' }">
      <span class="lamp" :class="verifyResult.valid ? 'on-ok' : 'on-danger'"></span>
      校验完成：{{ verifyResult.valid ? '全链完整' : '发现断链！' }}
      （checked {{ verifyResult.checked_total }}<template v-if="!verifyResult.valid"> · first_bad {{ firstBad }}</template>）
    </div>

    <!-- 检索条 -->
    <div class="panel">
      <div class="search-bar">
        <label>从</label>
        <input v-model="filters.time_from" class="input" type="datetime-local">
        <label>到</label>
        <input v-model="filters.time_to" class="input" type="datetime-local">
        <label>动作类型</label>
        <select v-model="filters.entry_type" class="input">
          <option value="">全部</option>
          <option v-for="t in facets.entry_types" :key="t" :value="t">{{ t }}</option>
        </select>
        <label>来源表</label>
        <select v-model="filters.ref_table" class="input">
          <option value="">全部</option>
          <option v-for="t in facets.ref_tables" :key="t" :value="t">{{ t }}</option>
        </select>
        <label>principal/关键词</label>
        <input v-model="filters.q" class="input" style="width: 140px" placeholder="actor / 关键词" @keyup.enter="doSearch">
        <button class="btn primary" :disabled="audit.searching" @click="doSearch">
          {{ audit.searching ? '检索中…' : '检索' }}
        </button>
        <button class="btn" @click="doExport('csv')">导出 CSV</button>
        <button class="btn" @click="doExport('json')">导出 JSON</button>
      </div>

      <!-- 结果表格 -->
      <table v-if="rows.length" class="table" style="margin-top: 12px">
        <thead><tr><th style="width: 60px">ID</th><th style="width: 150px">时间</th><th style="width: 120px">动作</th><th style="width: 130px">来源</th><th>payload</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.log_id" @click="open(r)">
            <td data-mono>{{ r.log_id }}</td>
            <td data-mono>{{ fmtTime(r.created_at) }}</td>
            <td><span class="badge"><span class="lamp" :class="typeLamp(r.entry_type)"></span>{{ r.entry_type }}</span></td>
            <td data-mono>{{ r.ref_table }}<template v-if="r.ref_id">#{{ r.ref_id }}</template></td>
            <td data-mono class="payload-cell">{{ (r.payload || '').slice(0, 80) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty" style="margin-top: 12px">
        <div class="empty-title">{{ audit.result ? '无匹配记录' : '设置条件后点「检索」' }}</div>
        <div class="empty-desc">行点击展开原始 JSON；时间范围留空 = 不限</div>
      </div>
      <div v-if="audit.result" class="kpi-foot" style="margin-top: 8px">
        匹配 {{ audit.result.total }} 条 · 显示前 {{ rows.length }} 条（limit {{ audit.result.limit }}）
      </div>
    </div>

    <!-- 读审计 · 网关读取记录（阶段2/03） -->
    <div class="panel" style="margin-top: 16px">
      <div class="panel-title">读审计 · 网关读取记录</div>
      <div class="page-sub" style="margin: 4px 0 10px">谁（agent/员工）经网关看了什么 · 给到哪级 · 剥离了多少越权内容 —— 全部落 gateway_read_log</div>
      <div class="search-bar">
        <label>读取者</label>
        <input v-model="rq.requester" class="input" style="width: 150px" placeholder="agent_id / 员工">
        <label>类型</label>
        <select v-model="rq.kind" class="input" style="width: 110px">
          <option value="">全部</option>
          <option value="semantic">semantic</option>
          <option value="memory">memory</option>
          <option value="doc">doc</option>
        </select>
        <button class="btn primary" :disabled="reads.loading" @click="doLoadReads">
          {{ reads.loading ? '查询中…' : '查询' }}
        </button>
      </div>
      <div v-if="reads.error" class="alert-strip warn" style="margin-top: 10px">
        <span class="lamp on-warn"></span> 读审计查询失败：{{ reads.error }}（错误与空数据不同——空=确实无记录，失败=需要排查）
      </div>
      <table v-if="reads.rows.length" class="table" style="margin-top: 12px">
        <thead>
          <tr>
            <th style="width: 56px">ID</th><th style="width: 140px">时间</th>
            <th style="width: 130px">读取者</th><th style="width: 90px">类型</th>
            <th>query</th><th style="width: 90px">级别</th>
            <th style="width: 56px">条数</th><th style="width: 56px">剥离</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in reads.rows" :key="r.log_id">
            <td data-mono>{{ r.log_id }}</td>
            <td data-mono>{{ fmtTime(r.created_at) }}</td>
            <td data-mono>{{ r.requester }}</td>
            <td><span class="badge">{{ r.kind }}</span></td>
            <td data-mono class="payload-cell">{{ r.query || '—' }}</td>
            <td>{{ r.granted_level || '—' }}</td>
            <td>{{ r.item_count }}</td>
            <td><span v-if="r.stripped_chunks" class="badge on-danger">{{ r.stripped_chunks }}</span><span v-else>—</span></td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty" style="margin-top: 12px">
        <div class="empty-title">{{ reads.error ? '查询失败' : (reads.loaded ? '暂无读取记录' : '点「查询」查看网关读取审计') }}</div>
        <div class="empty-desc">网关读取 = Agent/员工经 /api/v1/gateway/read 的检索与文档读取，全部落读审计（scope 剥离数单独列示）</div>
      </div>
      <div v-if="reads.rows.length" class="kpi-foot" style="margin-top: 8px">
        共 {{ reads.total }} 条 · 显示前 {{ reads.rows.length }} 条
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useAuditStore, useUiStore } from '../stores'
import { getToken } from '../api'

const audit = useAuditStore()
const ui = useUiStore()
const filters = reactive({ time_from: '', time_to: '', entry_type: '', ref_table: '', q: '' })
const reads = reactive({ rows: [], total: 0, loading: false, loaded: false, error: '' })
const rq = reactive({ requester: '', kind: '' })

async function doLoadReads() {
  reads.loading = true
  reads.error = ''
  try {
    const qs = new URLSearchParams({ limit: '100' })
    if (rq.requester) qs.set('requester', rq.requester)
    if (rq.kind) qs.set('kind', rq.kind)
    const resp = await fetch(`/api/audit/reads?${qs}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    })
    if (!resp.ok) {
      // 401/403/500 与「暂无记录」必须可区分——审计页静默失败会让审计员误判「没人读过」
      let detail = ''
      try { detail = ((await resp.json()).detail) || '' } catch { /* ignore */ }
      throw new Error(`${resp.status}${detail ? '：' + detail : ''}`)
    }
    const d = await resp.json()
    reads.rows = d.rows || []
    reads.total = d.total || 0
    reads.loaded = true
  } catch (e) {
    reads.loaded = true
    reads.error = (e && e.message) ? e.message : '网络/未知错误'
  } finally {
    reads.loading = false
  }
}
const verifyResult = ref(null)

onMounted(() => {
  audit.refreshVerify()
  audit.search({})  // 首屏：最近 50 条
})

const lv = computed(() => audit.lastVerify?.last_verify)
const coverage = computed(() => audit.lastVerify?.coverage || { audit_log: 0, disclosure_log: 0, jsonl_anchors: 0 })
const rows = computed(() => audit.result?.rows || [])
const facets = computed(() => audit.result?.facets || { entry_types: [], ref_tables: [] })

const chainText = computed(() => {
  if (!lv.value) return '未校验'
  return lv.value.valid ? '完整' : '断链'
})
const chainColor = computed(() =>
  !lv.value ? 'var(--text-2)' : lv.value.valid ? 'var(--ok)' : 'var(--danger)')
const chainLamp = computed(() =>
  !lv.value ? 'off' : lv.value.valid ? 'on-ok' : 'on-danger')
const firstBad = computed(() => {
  const chains = verifyResult.value?.chains || {}
  for (const [name, r] of Object.entries(chains)) {
    if (r && r.first_bad_id) return `${name}#${r.first_bad_id}`
  }
  return ''
})

function queryFilters() {
  const f = { ...filters }
  if (f.time_from) f.time_from = new Date(f.time_from).toISOString()
  if (f.time_to) f.time_to = new Date(f.time_to).toISOString()
  return f
}

async function doVerify() {
  verifyResult.value = await audit.runVerify()
}
function doSearch() {
  audit.search(queryFilters())
}

async function doExport(format) {
  // 带 Bearer 的下载：<a href> 无法注入 header，走 fetch + blob
  const url = new URL('/api/audit/export', window.location.origin)
  url.searchParams.set('format', format)
  for (const [k, v] of Object.entries(queryFilters())) {
    if (v) url.searchParams.set(k, v)
  }
  try {
    const resp = await fetch(url, { headers: { Authorization: `Bearer ${getToken()}` } })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `audit-export.${format}`
    a.click()
    URL.revokeObjectURL(a.href)
  } catch (e) {
    ui.openDrawer({ title: '导出失败', raw: { error: e.message } })
  }
}

function typeLamp(t) {
  if (t === 'verify') return 'on-ok'
  if (t === 'audit_export') return 'on-warn'
  if (t === 'jsonl_anchor') return 'off'
  return 'off'
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(5, 16) : d.toLocaleString()
}
function open(r) {
  let payload = r.payload
  try { payload = JSON.parse(r.payload) } catch { /* 原文展示 */ }
  ui.openDrawer({ title: `审计记录 #${r.log_id} · ${r.entry_type}`, raw: { ...r, payload } })
}
</script>

<style scoped>
.chain-card { display: flex; align-items: center; justify-content: space-between; }
.chain-status { font-size: var(--fs-20); font-weight: 600; margin: 6px 0; display: flex; align-items: center; gap: 8px; }
.search-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--fs-12); }
.input {
  padding: 5px 8px; border-radius: 6px; border: none; font-size: var(--fs-12);
  background: var(--bg-0); color: var(--text-0); box-shadow: var(--shadow-inset);
}
.payload-cell { color: var(--text-2); font-size: var(--fs-11); }
</style>
