<template>
  <div>
    <div class="page-title">知识库 Knowledge</div>
    <div class="page-sub">
      企业知识条目（写入走缓冲管道 · 仅 manager/orchestrator 可写）
      <span v-if="ks.lastFetch" class="mono refresh-ts">更新于 {{ new Date(ks.lastFetch).toLocaleTimeString() }}</span>
    </div>

    <div v-if="ks.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> {{ ks.error }}
    </div>

    <div class="kpi-row">
      <div class="kpi-card">
        <div class="kpi-label">知识条目</div>
        <div class="kpi-value">{{ entries.length }}</div>
        <div class="kpi-foot">{{ catCount }} 个分类</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">高重要性</div>
        <div class="kpi-value">{{ highImportance }}</div>
        <div class="kpi-foot">importance ≥ 0.7</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">标签覆盖</div>
        <div class="kpi-value">{{ tagCount }}</div>
        <div class="kpi-foot">去重标签数</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">条目（{{ entries.length }}）</div>
      <table v-if="entries.length" class="table">
        <thead><tr><th>标题</th><th>分类</th><th>重要性</th><th>标签</th><th>创建者</th><th>更新</th></tr></thead>
        <tbody>
          <tr v-for="e in entries" :key="e.entry_id || e.id || e.title" @click="open(e)">
            <td>{{ e.title }}</td>
            <td><span class="badge">{{ e.category || '—' }}</span></td>
            <td data-mono :style="{ color: (e.importance ?? 0) >= 0.7 ? 'var(--warn)' : 'inherit' }">{{ e.importance ?? '—' }}</td>
            <td data-mono>{{ tagText(e.tags) }}</td>
            <td data-mono>{{ e.created_by || '—' }}</td>
            <td data-mono>{{ fmtTime(e.updated_at || e.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <div class="empty-title">暂无知识条目</div>
        <div class="empty-desc">POST /api/v1/knowledge 创建；写入经 asyncio.Queue 缓冲批量落库</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useKnowledgeStore, useUiStore } from '../stores'

const ks = useKnowledgeStore()
const ui = useUiStore()
onMounted(() => ks.refresh())

const entries = computed(() => ks.entries || [])
const catCount = computed(() => new Set(entries.value.map((e) => e.category).filter(Boolean)).size)
const tagCount = computed(() => {
  const s = new Set()
  for (const e of entries.value) for (const t of parseTags(e.tags)) s.add(t)
  return s.size
})
const highImportance = computed(() => entries.value.filter((e) => (e.importance ?? 0) >= 0.7).length)

function parseTags(tags) {
  if (Array.isArray(tags)) return tags
  try { return JSON.parse(tags || '[]') } catch { return [] }
}
function tagText(tags) {
  const t = parseTags(tags)
  return t.length ? t.slice(0, 3).join(', ') + (t.length > 3 ? ` +${t.length - 3}` : '') : '—'
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(5, 16) : d.toLocaleString()
}
function open(e) {
  ui.openDrawer({ title: '知识条目 · ' + (e.title || ''), raw: e })
}
</script>

<style scoped>
.refresh-ts { margin-left: 12px; font-size: var(--fs-11); color: var(--text-2); }
</style>
