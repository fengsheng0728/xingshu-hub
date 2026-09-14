<template>
  <div>
    <div class="page-title">活动 Activity</div>
    <div class="page-sub">
      实时事件流（任务 / 披露 / 审批 / 安全 / Agent 上下线）
      <span class="mono refresh-ts">
        <span class="lamp" :class="wsLamp"></span>{{ wsText }}
        · 共 {{ filtered.length }} 条
      </span>
    </div>

    <!-- WS 降级提示 -->
    <div v-if="act.wsStatus === 'degraded'" class="alert-strip warn">
      <span class="lamp on-warn"></span>
      WS 实时通道断开，已降级为 30s 轮询（事件可能延迟），后台持续重连中
    </div>
    <div v-if="act.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> 种子数据加载异常：{{ act.error }}
    </div>

    <!-- 工具条：类别过滤 + 暂停 + 清空 -->
    <div class="toolbar">
      <button
        v-for="c in cats" :key="c.key"
        class="btn chip" :class="{ active: filter === c.key }"
        @click="filter = c.key"
      >
        <span class="lamp" :style="chipStyle(c.color)"></span>{{ c.label }}
        <span class="chip-count" data-mono>{{ countOf(c.key) }}</span>
      </button>
      <span style="flex: 1"></span>
      <button class="btn" @click="act.togglePause()">
        {{ act.paused ? '继续' : '暂停' }}
      </button>
      <button class="btn" :disabled="!act.events.length" @click="act.clear()">清空</button>
    </div>
    <div v-if="act.paused" class="alert-strip">
      <span class="lamp off"></span> 已暂停接收新事件（WS 仍在连接，恢复后只显示最新 {{ 200 }} 条）
    </div>

    <!-- 事件流 -->
    <div class="panel">
      <table v-if="filtered.length" class="table">
        <thead><tr><th style="width: 150px">时间</th><th style="width: 110px">类别</th><th>事件</th></tr></thead>
        <tbody>
          <tr v-for="e in filtered" :key="e.key" @click="open(e)">
            <td data-mono>{{ fmtTime(e.time) }}</td>
            <td>
              <span class="badge">
                <span class="lamp" :style="chipStyle(catColor(e.cat))"></span>{{ catLabel(e.cat) }}
              </span>
            </td>
            <td>{{ e.summary }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <div class="empty-title">{{ act.events.length ? '该类别暂无事件' : '暂无事件' }}</div>
        <div class="empty-desc">
          任务指派/完成、披露申请与审批、安全熔断、Wiki 待审、Agent 上下线会实时出现在这里
        </div>
        <button class="btn primary" @click="act.seed()">刷新历史通知</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { useActivityStore, useUiStore } from '../stores'
import { connectWs, WS_OPEN, WS_CONNECTING } from '../ws'

const act = useActivityStore()
const ui = useUiStore()
const filter = ref('all')
let wsConn = null
let pollTimer = null

onMounted(() => {
  act.seed()
  // 实时通道：/ws/dashboard（首帧鉴权在 ws.js 内完成）
  wsConn = connectWs('/ws/dashboard', {
    onMessage: (raw) => act.pushRaw(raw),
    onStatus: (s) => act.setWsStatus(s),
  })
  // 断线降级：仅 WS 非 open 时 30s 轮询历史通知补齐（§四 降级模式）
  pollTimer = setInterval(() => {
    if (act.wsStatus !== WS_OPEN) act.seed()
  }, 30000)
})
onBeforeUnmount(() => { clearInterval(pollTimer); wsConn && wsConn.close() })

const CATS = [
  { key: 'all', label: '全部', color: '#93a0b3' },
  { key: 'task', label: '任务', color: '#3b6fd4' },
  { key: 'disclosure', label: '披露/审批', color: '#d99114' },
  { key: 'agent', label: 'Agent', color: '#2fa36b' },
  { key: 'security', label: '安全', color: '#d0453e' },
  { key: 'wiki', label: 'Wiki', color: '#7a5cbe' },
  { key: 'other', label: '其他', color: '#c9cfda' },
]
const cats = CATS

const filtered = computed(() =>
  filter.value === 'all' ? act.events : act.events.filter((e) => e.cat === filter.value))

const wsLamp = computed(() =>
  act.wsStatus === WS_OPEN ? 'on-ok' : act.wsStatus === WS_CONNECTING ? 'on-warn' : 'off')
const wsText = computed(() =>
  act.wsStatus === WS_OPEN ? 'WS 实时' : act.wsStatus === WS_CONNECTING ? 'WS 连接中' : '轮询兜底')

function catColor(cat) {
  return (CATS.find((c) => c.key === cat) || {}).color || '#93a0b3'
}
function catLabel(cat) {
  return (CATS.find((c) => c.key === cat) || {}).label || cat
}
function countOf(key) {
  return key === 'all' ? act.events.length : act.events.filter((e) => e.cat === key).length
}
function chipStyle(color) {
  return { background: color, boxShadow: `0 0 6px ${color}` }
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d)) return String(iso).slice(5, 16)
  const today = new Date().toDateString() === d.toDateString()
  return today ? d.toLocaleTimeString() : d.toLocaleString()
}
function open(e) {
  ui.openDrawer({ title: `事件详情 · ${e.type}`, raw: e.raw })
}
</script>

<style scoped>
.refresh-ts { margin-left: 12px; font-size: var(--fs-11); color: var(--text-2); }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.chip { font-size: var(--fs-12); padding: 4px 10px; }
.chip.active { box-shadow: var(--shadow-inset); color: var(--text-0); }
.chip-count { margin-left: 4px; color: var(--text-2); }
</style>
