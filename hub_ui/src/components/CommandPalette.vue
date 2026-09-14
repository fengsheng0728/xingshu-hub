<template>
  <Transition name="fade">
    <div v-if="ui.paletteOpen" class="palette-mask" @click.self="ui.paletteOpen = false">
      <div class="palette">
        <input
          ref="inputEl"
          v-model="q"
          class="palette-input"
          placeholder="跳页 / 搜 Agent / 执行动作…"
          @keydown.down.prevent="move(1)"
          @keydown.up.prevent="move(-1)"
          @keydown.enter.prevent="run(results[sel])"
        />
        <div class="palette-list">
          <div
            v-for="(r, i) in results"
            :key="r.label"
            class="palette-item"
            :class="{ sel: i === sel }"
            @click="run(r)"
            @mouseenter="sel = i"
          >
            <span class="pi-kind">{{ r.kind }}</span>
            <span>{{ r.label }}</span>
          </div>
          <div v-if="!results.length" class="empty">
            <div class="empty-title">无匹配结果</div>
            <div class="empty-desc">试试页面名（如「审计」）或 Agent ID</div>
          </div>
        </div>
      </div>
    </div>
  </Transition>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NAV } from '../router'
import { useUiStore, useDashboardStore } from '../stores'

const ui = useUiStore()
const dash = useDashboardStore()
const router = useRouter()
const q = ref('')
const sel = ref(0)
const inputEl = ref(null)

// ⌘K：跳页 / 搜 Agent / 执行动作（带确认）
const pageIndex = NAV.flatMap((g) =>
  g.items.map((it) => ({ kind: '页面', label: it.title, path: it.path }))
)

const actions = [
  {
    kind: '动作', label: '刷新总览数据',
    run: () => dash.refresh(),
  },
  {
    kind: '动作', label: '触发数据清理（需确认）',
    confirm: true,
    run: async () => {
      const { api } = await import('../api')
      await api('/api/v1/maintenance/cleanup', { method: 'POST' })
      await dash.refresh()
    },
  },
]

const agentResults = computed(() => {
  const kw = q.value.trim().toLowerCase()
  if (!kw) return []
  const agents = dash.data?.agents?.list || []
  return agents
    .filter((a) =>
      a.agent_id.toLowerCase().includes(kw) ||
      (a.agent_name || '').toLowerCase().includes(kw))
    .slice(0, 5)
    .map((a) => ({
      kind: 'Agent',
      label: `${a.agent_name || a.agent_id}（${a.agent_id} · ${a.status}）`,
      run: () => ui.openDrawer({ title: `Agent ${a.agent_id}`, raw: a }),
    }))
})

const results = computed(() => {
  const kw = q.value.trim().toLowerCase()
  const pages = kw
    ? pageIndex.filter((p) => p.label.toLowerCase().includes(kw))
    : pageIndex
  const acts = kw ? actions.filter((a) => a.label.toLowerCase().includes(kw)) : []
  return [...pages.map((p) => ({ ...p, run: () => router.push(p.path) })),
          ...agentResults.value,
          ...acts].slice(0, 12)
})

watch(() => ui.paletteOpen, async (open) => {
  if (open) {
    q.value = ''
    sel.value = 0
    await nextTick()
    inputEl.value?.focus()
    if (!dash.data) dash.refresh()
  }
})
watch(q, () => { sel.value = 0 })

function move(d) {
  sel.value = Math.max(0, Math.min(results.value.length - 1, sel.value + d))
}

function run(r) {
  if (!r) return
  if (r.confirm && !window.confirm(`确认执行：${r.label}？`)) return
  ui.paletteOpen = false
  r.run?.()
}
</script>

<style scoped>
.palette-mask {
  position: fixed; inset: 0;
  background: rgba(42, 52, 66, 0.18);
  z-index: 200;
  display: flex;
  justify-content: center;
  padding-top: 12vh;
}
.palette {
  width: min(560px, 92vw);
  height: fit-content;
  background: var(--bg-2);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-raised);
  overflow: hidden;
}
.palette-input {
  width: 100%;
  border: none;
  outline: none;
  background: var(--bg-2);
  color: var(--text-0);
  font-family: var(--font-sans);
  font-size: var(--fs-16);
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
}
.palette-list { max-height: 50vh; overflow-y: auto; padding: 8px; }
.palette-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--radius-ctrl);
  font-size: var(--fs-13);
  color: var(--text-0);
  cursor: pointer;
}
.palette-item.sel { box-shadow: var(--shadow-inset); color: var(--accent); }
.pi-kind {
  flex: 0 0 auto;
  font-size: var(--fs-11);
  color: var(--text-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 1px 6px;
}
</style>
