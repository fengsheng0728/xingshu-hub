<template>
  <div>
    <div class="page-title">运行 Ops</div>
    <div class="page-sub">
      配额用量 / 缓冲积压 / 降级状态 / 备份状态 / Agent 在线
      <span class="mono refresh-ts">
        <span class="lamp" :class="wsLamp"></span>
        缓冲 {{ wsText }} · 其余 30s 轮询
        <template v-if="ops.lastFetch"> · 更新于 {{ new Date(ops.lastFetch).toLocaleTimeString() }}</template>
      </span>
    </div>

    <!-- 降级告警条 -->
    <div v-if="degraded" class="alert-strip danger">
      <span class="lamp on-danger"></span>
      <span style="margin-right: 18px">系统降级：{{ health?.status }}</span>
      <span v-for="(w, i) in warnings" :key="i" style="margin-right: 18px">{{ w }}</span>
    </div>
    <div v-else-if="health" class="alert-strip" style="color: var(--ok)">
      <span class="lamp on-ok"></span> 系统正常（{{ health.status }}）
    </div>
    <div v-if="ops.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> 数据加载异常：{{ ops.error }}（检查登录 token）
    </div>

    <!-- KPI 五卡（O1-O5） -->
    <div class="kpi-row">
      <div class="kpi-card">
        <div class="kpi-label">配额配置 O1</div>
        <div class="kpi-value">{{ quotaList.length }}<span class="unit">台</span></div>
        <div class="kpi-foot">{{ quotaModeSummary }}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">缓冲积压 O2</div>
        <div class="kpi-value" :style="{ color: backlog > 100 ? 'var(--warn)' : 'var(--text-0)' }">{{ backlog }}</div>
        <div class="kpi-foot">
          已落库 {{ buf?.total_flushed ?? '—' }}
          · 降级直写 <span :style="{ color: fallbackCount > 0 ? 'var(--warn)' : 'inherit' }">{{ fallbackCount }}</span>
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">降级状态 O3</div>
        <div class="kpi-value" :style="{ color: degraded ? 'var(--warn)' : 'var(--ok)' }">
          {{ health ? (degraded ? 'DEGRADED' : 'OK') : '—' }}
        </div>
        <div class="kpi-foot">向量库 {{ chromaText }}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">备份状态 O4</div>
        <div class="kpi-value" :style="{ color: backupOk ? 'var(--text-0)' : 'var(--warn)' }">{{ backupText }}</div>
        <div class="kpi-foot">共 {{ ops.backup?.count ?? 0 }} 份 · 保留 {{ ops.backup?.keep_days ?? '—' }} 天</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">AGENT 在线 O5</div>
        <div class="kpi-value">{{ dash.data?.agents?.online ?? '—' }}<span class="unit">/{{ dash.data?.agents?.total ?? '—' }}</span></div>
        <div class="kpi-foot">掉线 {{ offlineCount }} 台</div>
      </div>
    </div>

    <div class="grid-2">
      <!-- 配额用量表 -->
      <div class="panel">
        <div class="panel-title">Agent 配额（{{ quotaList.length }}）</div>
        <table v-if="quotaList.length" class="table">
          <thead><tr><th>Agent</th><th>QPS 上限</th><th>模式</th><th>突发</th><th>窗口</th></tr></thead>
          <tbody>
            <tr v-for="q in quotaList" :key="q.agent_id" @click="open('配额详情', q)">
              <td data-mono>{{ q.agent_id }}</td>
              <td data-mono>{{ q.qps_limit }}</td>
              <td><span class="badge"><span class="lamp" :class="modeLamp(q.mode)"></span>{{ q.mode }}</span></td>
              <td data-mono>{{ q.burst }}</td>
              <td data-mono>{{ q.window_sec }}s</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">未配置配额</div>
          <div class="empty-desc">POST /api/v1/agents/quota 可为 Agent 设置 QPS 限流（reject/throttle/alert_only）</div>
        </div>
      </div>

      <!-- 写入缓冲详情 -->
      <div class="panel">
        <div class="panel-title">
          写入缓冲
          <span class="badge" style="margin-left: 8px"><span class="lamp" :class="wsLamp"></span>{{ wsText }}</span>
        </div>
        <div class="stat-grid">
          <div class="stat"><div class="stat-k">队列深度</div><div class="stat-v" data-mono>{{ buf?.queue_depth ?? '—' }} / {{ buf?.queue_max ?? '—' }}</div></div>
          <div class="stat"><div class="stat-k">累计落库</div><div class="stat-v" data-mono>{{ buf?.total_flushed ?? '—' }}</div></div>
          <div class="stat"><div class="stat-k">批次数</div><div class="stat-v" data-mono>{{ buf?.flush_count ?? '—' }}</div></div>
          <div class="stat"><div class="stat-k">均批大小</div><div class="stat-v" data-mono>{{ buf?.avg_batch_size ?? '—' }}</div></div>
          <div class="stat"><div class="stat-k">均落库延迟</div><div class="stat-v" data-mono>{{ buf?.avg_flush_latency_ms ?? '—' }} ms</div></div>
          <div class="stat"><div class="stat-k">P99 落库延迟</div><div class="stat-v" data-mono>{{ buf?.p99_flush_latency_ms ?? '—' }} ms</div></div>
          <div class="stat"><div class="stat-k">Wiki 待同步</div><div class="stat-v" data-mono>{{ buf?.wiki_sync_pending ?? '—' }}</div></div>
          <div class="stat"><div class="stat-k">降级直写</div><div class="stat-v" data-mono :style="{ color: fallbackCount > 0 ? 'var(--warn)' : 'inherit' }">{{ fallbackCount }}</div></div>
        </div>
        <div class="kpi-foot" style="margin-top: 10px">
          最近落库 {{ fmtTime(buf?.last_flush_at) }} · 最近 Wiki 同步 {{ fmtTime(buf?.last_wiki_sync) }}
        </div>
      </div>
    </div>

    <div class="grid-2">
      <!-- 降级与健康 -->
      <div class="panel">
        <div class="panel-title">健康细节（/health）</div>
        <div v-if="health" class="stat-grid">
          <div class="stat"><div class="stat-k">版本</div><div class="stat-v" data-mono>{{ health.version }}</div></div>
          <div class="stat"><div class="stat-k">运行时长</div><div class="stat-v" data-mono>{{ uptimeText }}</div></div>
          <div class="stat"><div class="stat-k">数据库</div><div class="stat-v" data-mono>{{ health.database?.status }} · {{ health.database?.wal_mode }} · {{ health.database?.size_mb }}MB</div></div>
          <div class="stat"><div class="stat-k">向量库</div><div class="stat-v" data-mono>{{ chromaText }}</div></div>
          <div class="stat"><div class="stat-k">磁盘剩余</div><div class="stat-v" data-mono :style="{ color: (health.disk?.free_gb ?? 99) < 5 ? 'var(--danger)' : 'inherit' }">{{ health.disk?.free_gb ?? '—' }} GB</div></div>
          <div class="stat"><div class="stat-k">卡住任务</div><div class="stat-v" data-mono :style="{ color: (health.tasks?.stale_tasks ?? 0) > 0 ? 'var(--warn)' : 'inherit' }">{{ health.tasks?.stale_tasks ?? '—' }}</div></div>
          <div class="stat"><div class="stat-k">记忆池</div><div class="stat-v" data-mono>{{ health.memory_pool?.total ?? '—' }}（1h +{{ health.memory_pool?.recent_1h ?? 0 }}）</div></div>
          <div class="stat"><div class="stat-k">防火墙</div><div class="stat-v" data-mono>{{ firewallText }}</div></div>
        </div>
        <div v-if="warnings.length" class="kpi-foot" style="color: var(--warn); margin-top: 10px">
          <span class="lamp on-warn"></span> {{ warnings.join('；') }}
        </div>
        <div v-if="!health" class="empty">
          <div class="empty-title">健康数据未加载</div>
          <div class="empty-desc">/health 无需鉴权，检查 Hub 是否在线</div>
        </div>
      </div>

      <!-- 备份状态 -->
      <div class="panel">
        <div class="panel-title">数据库备份（O4）</div>
        <div v-if="ops.backup" class="stat-grid">
          <div class="stat"><div class="stat-k">自动备份</div><div class="stat-v">{{ ops.backup.enabled ? '启用' : '禁用' }}</div></div>
          <div class="stat"><div class="stat-k">保留策略</div><div class="stat-v" data-mono>{{ ops.backup.keep_days }} 天</div></div>
          <div class="stat"><div class="stat-k">备份份数</div><div class="stat-v" data-mono>{{ ops.backup.count }}</div></div>
          <div class="stat"><div class="stat-k">最近大小</div><div class="stat-v" data-mono>{{ ops.backup.latest ? ops.backup.latest.size_mb + ' MB' : '—' }}</div></div>
        </div>
        <div v-if="ops.backup?.latest" class="kpi-foot" style="margin-top: 10px">
          最近备份：<span data-mono>{{ ops.backup.latest.file }}</span><br>
          {{ fmtTime(ops.backup.latest.mtime) }}
        </div>
        <div v-else-if="ops.backup" class="empty" style="margin-top: 12px">
          <div class="empty-title">尚无备份文件</div>
          <div class="empty-desc">Hub 启动时自动备份（1 小时冷却），或重启 Hub 触发首次备份</div>
        </div>
        <div v-if="!ops.backup" class="empty">
          <div class="empty-title">备份状态未加载</div>
          <div class="empty-desc">/api/v1/maintenance/backup-status 返回异常</div>
        </div>
      </div>
    </div>

    <!-- Agent 在线表 -->
    <div class="panel">
      <div class="panel-title">Agent 在线状态（{{ agentList.length }}）</div>
      <table v-if="agentList.length" class="table">
        <thead><tr><th>Agent</th><th>名称</th><th>角色</th><th>部门</th><th>状态</th><th>托管数</th></tr></thead>
        <tbody>
          <tr v-for="a in agentList" :key="a.agent_id" @click="open('Agent 详情', a)">
            <td data-mono>{{ a.agent_id }}</td>
            <td>{{ a.agent_name || '—' }}</td>
            <td><span class="badge">{{ a.role }}</span></td>
            <td>{{ a.department || '—' }}</td>
            <td><span class="badge"><span class="lamp" :class="a.status === 'online' ? 'on-ok' : 'off'"></span>{{ a.status }}</span></td>
            <td data-mono>{{ (a.managed_agents || []).length }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <div class="empty-title">暂无 Agent</div>
        <div class="empty-desc">Agent 注册后此处显示在线/掉线状态（心跳超时自动标记）</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount } from 'vue'
import { useOpsStore, useDashboardStore, useUiStore } from '../stores'
import { connectWs, WS_OPEN, WS_CONNECTING } from '../ws'

const ops = useOpsStore()
const dash = useDashboardStore()
const ui = useUiStore()
let wsConn = null
let timer = null

onMounted(() => {
  ops.refresh()
  dash.refresh()
  // O2：缓冲积压走 /ws/buffer 实时帧（1s 推送）；断线 → 30s HTTP 轮询兜底
  wsConn = connectWs('/ws/buffer', {
    onMessage: (stats) => ops.setBufferLive(stats),
    onStatus: (s) => ops.setBufferWs(s),
  })
  timer = setInterval(() => { ops.refresh(); dash.refresh() }, 30000)
})
onBeforeUnmount(() => { clearInterval(timer); wsConn && wsConn.close() })

const health = computed(() => ops.healthFull)
const buf = computed(() => ops.bufferLive)
const backlog = computed(() => buf.value?.queue_depth ?? 0)
const fallbackCount = computed(() => buf.value?.sync_fallback_count ?? 0)
const degraded = computed(() => (health.value?.status || 'ok') !== 'ok')
const warnings = computed(() => health.value?.warnings || [])
const quotaList = computed(() => ops.quotas?.quotas || [])
const agentList = computed(() => dash.data?.agents?.list || [])
const offlineCount = computed(() => agentList.value.filter((a) => a.status !== 'online').length)

const wsLamp = computed(() =>
  ops.bufferWs === WS_OPEN ? 'on-ok' : ops.bufferWs === WS_CONNECTING ? 'on-warn' : 'off')
const wsText = computed(() =>
  ops.bufferWs === WS_OPEN ? '实时' : ops.bufferWs === WS_CONNECTING ? '连接中' : '轮询兜底')

const chromaText = computed(() => {
  const c = health.value?.chromadb
  if (!c) return '—'
  return c.status === 'ok' ? `ok · ${c.documents} docs` : c.status
})
const uptimeText = computed(() => {
  const s = health.value?.uptime_seconds
  if (s == null) return '—'
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m ${Math.floor(s % 60)}s`
})
const firewallText = computed(() => {
  const f = health.value?.firewall
  if (!f || typeof f !== 'object') return '—'
  return f.status || (f.checked === false ? '未检查' : 'ok')
})

const backupOk = computed(() => !!ops.backup?.latest)
const backupText = computed(() => {
  const latest = ops.backup?.latest
  if (!ops.backup) return '—'
  if (!latest) return '无备份'
  const ageH = (Date.now() - new Date(latest.mtime).getTime()) / 3600000
  if (ageH < 1) return `${Math.max(1, Math.round(ageH * 60))}分钟前`
  if (ageH < 48) return `${Math.round(ageH)}小时前`
  return `${Math.round(ageH / 24)}天前`
})

const quotaModeSummary = computed(() => {
  const cnt = {}
  for (const q of quotaList.value) cnt[q.mode] = (cnt[q.mode] || 0) + 1
  if (!Object.keys(cnt).length) return '未配置限流'
  return Object.entries(cnt).map(([m, n]) => `${m} ×${n}`).join(' · ')
})

function modeLamp(mode) {
  return mode === 'reject' ? 'on-danger' : mode === 'throttle' ? 'on-warn' : 'off'
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(0, 16) : d.toLocaleString()
}
function open(title, raw) {
  ui.openDrawer({ title, raw })
}
</script>

<style scoped>
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
@media (max-width: 1200px) { .grid-2 { grid-template-columns: 1fr; } }
.unit { font-size: var(--fs-13); color: var(--text-2); margin-left: 4px; }
.refresh-ts { margin-left: 12px; font-size: var(--fs-11); color: var(--text-2); }
.stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 16px; }
.stat-k { font-size: var(--fs-11); color: var(--text-2); margin-bottom: 2px; }
.stat-v { font-size: var(--fs-13); }
</style>
