<template>
  <Transition name="fade">
    <div v-if="ui.drawer" class="drawer-mask" @click.self="ui.closeDrawer()">
      <aside class="drawer">
        <header class="drawer-head">
          <strong>{{ ui.drawer.title }}</strong>
          <button class="btn close" @click="ui.closeDrawer()">✕</button>
        </header>
        <div class="drawer-body">
          <template v-if="ui.drawer.lines?.length">
            <div v-for="(l, i) in ui.drawer.lines" :key="i" class="kv">
              <span class="kv-k">{{ l.k }}</span>
              <span class="kv-v mono">{{ l.v }}</span>
            </div>
          </template>
          <pre v-if="ui.drawer.raw" class="raw mono">{{ prettyRaw }}</pre>
          <div v-if="!ui.drawer.lines?.length && !ui.drawer.raw" class="empty">
            <div class="empty-title">没有可展示的细节</div>
          </div>
        </div>
      </aside>
    </div>
  </Transition>
</template>

<script setup>
import { computed } from 'vue'
import { useUiStore } from '../stores'

const ui = useUiStore()
const prettyRaw = computed(() => {
  try { return JSON.stringify(ui.drawer?.raw, null, 2) } catch { return String(ui.drawer?.raw) }
})
</script>

<style scoped>
.drawer-mask {
  position: fixed; inset: 0;
  background: rgba(42, 52, 66, 0.18);
  z-index: 100;
}
.drawer {
  position: absolute;
  top: 0; right: 0; bottom: 0;
  width: min(440px, 90vw);
  background: var(--bg-2);
  box-shadow: var(--shadow-raised);
  border-radius: var(--radius-card) 0 0 var(--radius-card);
  display: flex;
  flex-direction: column;
  animation: slide-in var(--dur-ui) ease-out;
}
@keyframes slide-in {
  from { transform: translateX(40px); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
.drawer-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 18px;
  font-size: var(--fs-16);
}
.close { padding: 4px 10px; }
.drawer-body { padding: 0 18px 18px; overflow-y: auto; }
.kv {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-13);
}
.kv-k { color: var(--text-1); flex: 0 0 auto; }
.kv-v { color: var(--text-0); text-align: right; word-break: break-all; }
.raw {
  margin-top: 12px;
  font-size: var(--fs-12);
  color: var(--text-1);
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--bg-1);
  border-radius: var(--radius-ctrl);
  box-shadow: var(--shadow-inset);
  padding: 12px;
}
</style>
