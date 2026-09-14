<template>
  <div>
    <div class="page-title">记忆池 Memory</div>
    <div class="page-sub">按 Agent 隔离的记忆池（写入隔离 · 版本可回溯）</div>

    <div class="panel">
      <div class="form-row">
        <label>Agent</label>
        <select v-model="selected" class="input" style="min-width: 220px" @change="load">
          <option value="">— 选择要查看的 Agent —</option>
          <option v-for="a in agents" :key="a.agent_id" :value="a.agent_id">
            {{ a.agent_id }}（{{ a.role }}{{ a.status === 'online' ? ' · 在线' : '' }}）
          </option>
        </select>
        <label>类型</label>
        <select v-model="kind" class="input" @change="load">
          <option value="">全部</option>
          <option value="fact">fact</option>
          <option value="todo">todo</option>
        </select>
        <span v-if="ms.loading" class="kpi-foot">加载中…</span>
        <span v-else-if="ms.memories" class="kpi-foot">{{ ms.memories.length }} 条</span>
      </div>
    </div>

    <div v-if="ms.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> {{ ms.error }}（api_key 认证时只能查看自己的记忆；hub_token 可查看全部）
    </div>

    <div class="panel" v-if="selected">
      <div class="panel-title">{{ selected }} 的记忆（{{ (ms.memories || []).length }}）</div>
      <table v-if="(ms.memories || []).length" class="table">
        <thead><tr><th>Key</th><th>内容</th><th>类型</th><th>重要性</th><th>置信度</th><th>披露级别</th><th>创建</th></tr></thead>
        <tbody>
          <tr v-for="m in ms.memories" :key="m.memory_id || m.memory_key" @click="open(m)">
            <td data-mono>{{ m.memory_key }}</td>
            <td>{{ (m.content || '').slice(0, 40) }}</td>
            <td><span class="badge">{{ m.kind }}</span></td>
            <td data-mono :style="{ color: (m.importance ?? 0) >= 0.7 ? 'var(--warn)' : 'inherit' }">{{ m.importance ?? '—' }}</td>
            <td data-mono>{{ m.confidence ?? '—' }}</td>
            <td><span class="badge"><span class="lamp" :class="`dot-lv-${m.disclosure_level || 'summary'}`"></span>{{ m.disclosure_level || '—' }}</span></td>
            <td data-mono>{{ fmtTime(m.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else-if="!ms.loading" class="empty">
        <div class="empty-title">该 Agent 暂无记忆</div>
        <div class="empty-desc">记忆通过 /api/v1/memory/store 写入，三段去重后落池</div>
      </div>
    </div>
    <div v-else class="panel">
      <div class="empty">
        <div class="empty-title">选择一个 Agent 开始</div>
        <div class="empty-desc">记忆池按 Agent 隔离：每个 Agent 只能写自己的池，读他人需披露引擎判定</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useMemoryStore, useDashboardStore, useUiStore } from '../stores'

const ms = useMemoryStore()
const dash = useDashboardStore()
const ui = useUiStore()
const selected = ref('')
const kind = ref('')

onMounted(() => { if (!dash.data) dash.refresh() })

const agents = computed(() => dash.data?.agents?.list || [])

function load() {
  ms.load(selected.value, kind.value)
}
async function open(m) {
  let versions = null
  try {
    versions = await ms.versions(m.memory_key)
  } catch { /* 版本历史可选 */ }
  ui.openDrawer({ title: `记忆 · ${m.memory_key}`, raw: { ...m, versions: versions?.versions || versions } })
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(5, 16) : d.toLocaleString()
}
</script>

<style scoped>
.form-row { display: flex; align-items: center; gap: 10px; font-size: var(--fs-12); }
.input {
  padding: 5px 8px; border-radius: 6px; border: none; font-size: var(--fs-12);
  background: var(--bg-0); color: var(--text-0); box-shadow: var(--shadow-inset);
}
</style>
