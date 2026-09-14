<template>
  <div>
    <div class="page-title">总览 Overview</div>
    <div class="page-sub">
      健康 · 告警 · 关键指标 · 待办（本质：我今天要点哪三件事）
      <span v-if="dash.lastFetch" class="mono refresh-ts">
        更新于 {{ new Date(dash.lastFetch).toLocaleTimeString() }} · 30s 轮询
      </span>
    </div>

    <!-- 告警条（未恢复告警） -->
    <div v-if="alerts.length" class="alert-strip danger">
      <span class="lamp on-danger"></span>
      <span v-for="(a, i) in alerts" :key="i" style="margin-right: 18px">{{ a }}</span>
    </div>
    <div v-else-if="dash.data" class="alert-strip" style="color: var(--ok)">
      <span class="lamp on-ok"></span> 无未恢复告警
    </div>

    <div v-if="dash.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> 数据加载异常：{{ dash.error }}（检查登录 token）
    </div>

    <!-- 第一行：健康四卡（★★★ 全拟物） -->
    <div class="kpi-row">
      <div class="kpi-card">
        <div class="kpi-label">HUB 服务</div>
        <div class="kpi-value" :style="{ color: hubOk ? 'var(--ok)' : 'var(--danger)' }">
          {{ hubOk ? 'UP' : 'DOWN' }}
        </div>
        <div class="kpi-foot">
          <span class="lamp" :class="dash.health.live ? 'on-ok' : 'off'"></span>存活
          <span class="lamp" :class="dash.health.ready ? 'on-ok' : 'off'" style="margin-left:10px"></span>就绪
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">数据库</div>
        <div class="kpi-value">{{ dash.dbStats?.db_size_mb ?? '—' }}<span class="unit">MB</span></div>
        <div class="kpi-foot">memory {{ dash.dbStats?.memory_pool ?? '—' }} · tasks {{ dash.dbStats?.tasks ?? '—' }} · events {{ dash.dbStats?.events ?? '—' }}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">写入缓冲</div>
        <div class="kpi-value" :style="{ color: backlog > 100 ? 'var(--warn)' : 'var(--text-0)' }">{{ backlog }}</div>
        <div class="kpi-foot">已落库 {{ dash.buffer?.total_flushed ?? 0 }} · 均延 {{ dash.buffer?.avg_flush_latency_ms ?? 0 }}ms</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">AGENT 在线</div>
        <div class="kpi-value">{{ dash.data?.agents?.online ?? '—' }}<span class="unit">/{{ dash.data?.agents?.total ?? '—' }}</span></div>
        <div class="kpi-foot">记忆池 {{ dash.data?.memories?.total ?? '—' }} 条</div>
      </div>
    </div>

    <div class="grid-2">
      <!-- 任务状态分布（堆叠条，数据即现无动画） -->
      <div class="panel">
        <div class="panel-title">任务状态分布（共 {{ dash.data?.tasks?.total ?? 0 }}）</div>
        <div v-if="taskSegs.length" class="stack-bar">
          <div v-for="s in taskSegs" :key="s.name" :style="{ width: s.pct + '%', background: s.color }" :title="`${s.name}: ${s.count}`"></div>
        </div>
        <div class="stack-legend">
          <span v-for="s in taskSegs" :key="s.name" class="badge">
            <span class="lamp" :style="{ background: s.color, boxShadow: `0 0 6px ${s.color}` }"></span>
            {{ s.name }} {{ s.count }}
          </span>
        </div>
      </div>

      <!-- 披露四级判定分布（四级语义色=产品语言） -->
      <div class="panel">
        <div class="panel-title">最近披露判定（四级语义色 · 共 {{ dash.data?.disclosures?.total ?? 0 }}）</div>
        <div v-if="lvSegs.length" class="stack-bar">
          <div v-for="s in lvSegs" :key="s.name" :style="{ width: s.pct + '%', background: s.color }" :title="`${s.name}: ${s.count}`"></div>
        </div>
        <div v-else class="empty">
          <div class="empty-title">暂无披露记录</div>
          <div class="empty-desc">任务调度触发渐进式披露后，此处显示 full/summary/metadata 分布</div>
        </div>
        <div class="stack-legend">
          <span v-for="s in lvSegs" :key="s.name" class="badge">
            <span class="lamp" :class="`dot-${s.cls}`"></span>{{ s.name }} {{ s.count }}
          </span>
        </div>
      </div>
    </div>

    <!-- 第三行：待办三列 -->
    <div class="grid-3">
      <div class="panel">
        <div class="panel-title">待审批披露（{{ pendings.length }}）</div>
        <table v-if="pendings.length" class="table">
          <thead><tr><th>任务</th><th>申请人</th><th>阶段</th></tr></thead>
          <tbody>
            <tr v-for="p in pendings" :key="p.request_id" @click="open('审批详情', p)">
              <td data-mono>{{ p.task_id }}</td>
              <td data-mono>{{ p.agent_id }}</td>
              <td data-mono>P{{ p.new_phase }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">无待审批</div>
          <div class="empty-desc">披露升级申请会出现在这里</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">失败 / 阻塞任务（{{ problemTasks.length }}）</div>
        <table v-if="problemTasks.length" class="table">
          <thead><tr><th>任务</th><th>状态</th><th>阻塞于</th></tr></thead>
          <tbody>
            <tr v-for="t in problemTasks" :key="t.task_id" @click="open('任务详情', t)">
              <td data-mono>{{ t.task_id }}</td>
              <td><span class="badge"><span class="lamp" :class="t.status === 'failed' ? 'on-danger' : 'on-warn'"></span>{{ t.status }}</span></td>
              <td data-mono>{{ (t.blocked_by || []).join(', ') || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">无失败或阻塞</div>
          <div class="empty-desc">DAG 依赖未完成的任务会带 blocked_by 标记</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">最近披露事件（{{ recents.length }}）</div>
        <table v-if="recents.length" class="table">
          <thead><tr><th>从 → 到</th><th>级别</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="(d, i) in recents" :key="i" @click="open('披露事件', d)">
              <td data-mono>{{ d.from }} → {{ d.to }}</td>
              <td><span class="badge"><span class="lamp" :class="`dot-lv-${d.level}`"></span><span :class="`lv-${d.level}`">{{ d.level }}</span></span></td>
              <td data-mono>{{ (d.time || '').slice(5, 16) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">暂无披露事件</div>
          <div class="empty-desc">披露日志会实时出现在这里</div>
        </div>
      </div>
    </div>

    <!-- 最近任务（表格第一公民：行点击 → 抽屉详情） -->
    <div class="panel">
      <div class="panel-title">最近任务（{{ tasks.length }}）</div>
      <table v-if="tasks.length" class="table">
        <thead><tr><th>ID</th><th>描述</th><th>状态</th><th>执行者</th><th>更新</th></tr></thead>
        <tbody>
          <tr v-for="t in tasks" :key="t.task_id" @click="open('任务详情', t)">
            <td data-mono>{{ t.task_id }}</td>
            <td>{{ (t.description || '').slice(0, 40) }}</td>
            <td><span class="badge"><span class="lamp" :class="statusLamp(t.status)"></span>{{ t.status }}</span></td>
            <td data-mono>{{ t.assigned_agent_id || '—' }}</td>
            <td data-mono>{{ (t.updated_at || '').slice(5, 16) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <div class="empty-title">暂无任务</div>
        <div class="empty-desc">通过 API 创建任务后此处可见调度全过程</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount } from 'vue'
import { useDashboardStore, useUiStore } from '../stores'

const dash = useDashboardStore()
const ui = useUiStore()
let timer = null

onMounted(() => {
  dash.refresh()
  timer = setInterval(() => dash.refresh(), 30000)  // 30s 轮询（U2 接 WS 实时流后降级保留）
})
onBeforeUnmount(() => clearInterval(timer))

const hubOk = computed(() => dash.health.live !== false && dash.health.ready !== false)
const backlog = computed(() => dash.buffer?.queue_depth ?? 0)
const pendings = computed(() => dash.data?.pending_disclosures || [])
const recents = computed(() => (dash.data?.disclosures?.recent || []).slice(0, 8))
const tasks = computed(() => (dash.data?.tasks?.list || []).slice(0, 12))
const problemTasks = computed(() =>
  (dash.data?.tasks?.list || []).filter((t) => t.status === 'failed' || (t.blocked_by || []).length).slice(0, 8))

const alerts = computed(() => {
  const list = []
  if (dash.health.live === false) list.push('存活探针失败')
  if (dash.health.ready === false) list.push('就绪探针失败')
  if (backlog.value > 100) list.push(`写入缓冲积压 ${backlog.value} 条`)
  return list
})

const TASK_COLORS = {
  pending: '#93a0b3', assigned: '#3b6fd4', in_progress: '#d99114',
  completed: '#2fa36b', failed: '#d0453e', cancelled: '#c9cfda',
}
const taskSegs = computed(() => {
  const bs = dash.data?.tasks?.by_status || {}
  const total = Object.values(bs).reduce((a, b) => a + b, 0) || 1
  return Object.entries(bs).map(([name, count]) => ({
    name, count, pct: (count / total) * 100, color: TASK_COLORS[name] || '#93a0b3',
  }))
})

const LV_COLORS = {
  full: { color: 'var(--lv-full)', cls: 'lv-full' },
  summary: { color: 'var(--lv-summary)', cls: 'lv-summary' },
  metadata: { color: 'var(--lv-metadata)', cls: 'lv-metadata' },
}
const lvSegs = computed(() => {
  const recent = dash.data?.disclosures?.recent || []
  const cnt = {}
  for (const d of recent) cnt[d.level] = (cnt[d.level] || 0) + 1
  const total = recent.length || 1
  return Object.entries(cnt).map(([name, count]) => ({
    name, count, pct: (count / total) * 100,
    color: LV_COLORS[name]?.color || '#93a0b3', cls: LV_COLORS[name]?.cls || '',
  }))
})

function statusLamp(s) {
  return s === 'completed' ? 'on-ok' : s === 'failed' ? 'on-danger'
    : s === 'in_progress' ? 'on-warn' : 'off'
}

function open(title, raw) {
  ui.openDrawer({ title, raw })
}
</script>

<style scoped>
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
@media (max-width: 1200px) { .grid-3 { grid-template-columns: 1fr; } .grid-2 { grid-template-columns: 1fr; } }
.unit { font-size: var(--fs-13); color: var(--text-2); margin-left: 4px; }
.refresh-ts { margin-left: 12px; color: var(--text-2); font-size: var(--fs-11); }
</style>
