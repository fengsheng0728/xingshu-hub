<template>
  <div v-if="!needsLogin" class="shell">
    <AppTopbar />
    <div class="shell-body">
      <AppSidebar />
      <main class="content">
        <router-view />
      </main>
    </div>
    <DetailDrawer />
    <CommandPalette />
  </div>
  <LoginGate v-else />
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount } from 'vue'
import AppTopbar from './components/AppTopbar.vue'
import AppSidebar from './components/AppSidebar.vue'
import DetailDrawer from './components/DetailDrawer.vue'
import CommandPalette from './components/CommandPalette.vue'
import LoginGate from './components/LoginGate.vue'
import { useUiStore } from './stores'
import { getToken } from './api'

const ui = useUiStore()

// NO_AUTH 开发模式下后端全放行，无 token 也可直接用；
// 这里仅在「曾经 401 过」或显式登出时强制登录门
const needsLogin = computed(() => ui.loggedOut && !getToken())

function onUnauthorized() { ui.loggedOut = true }

function onKey(e) {
  // ⌘K / Ctrl+K 命令面板（§1.3 全局模式）
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    ui.togglePalette()
  }
  if (e.key === 'Escape') {
    ui.paletteOpen = false
    ui.closeDrawer()
  }
}

onMounted(() => {
  ui.setDensity(ui.density)
  window.addEventListener('keydown', onKey)
  window.addEventListener('hub:unauthorized', onUnauthorized)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('hub:unauthorized', onUnauthorized)
})
</script>
