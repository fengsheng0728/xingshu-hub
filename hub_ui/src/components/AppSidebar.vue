<template>
  <nav class="sidebar" :class="{ collapsed }">
    <div v-for="g in NAV" :key="g.group" class="nav-group">
      <div v-if="!collapsed" class="nav-group-title">{{ g.group }}</div>
      <router-link
        v-for="it in g.items"
        :key="it.path"
        :to="it.path"
        class="nav-item"
        :class="{ active: route.path === it.path }"
        :title="it.title"
      >
        <span class="nav-icon">{{ icons[it.name] || '·' }}</span>
        <span v-if="!collapsed" class="nav-text">{{ it.title.split(' ')[0] }}</span>
      </router-link>
    </div>
    <button class="collapse-btn" @click="collapsed = !collapsed" :title="collapsed ? '展开' : '折叠'">
      {{ collapsed ? '»' : '«' }}
    </button>
  </nav>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { NAV } from '../router'

const route = useRoute()
const collapsed = ref(false)

const icons = {
  overview: '◉', activity: '≋', access: '⚿', disclosure: '◐', audit: '✓',
  knowledge: '◈', wiki: '✎', memory: '▣', ops: '⚙', integrations: '⇄', settings: '☰',
}
</script>

<style scoped>
.sidebar {
  width: 200px;
  flex: 0 0 auto;
  background: var(--bg-1);
  box-shadow: var(--shadow-raised-sm);
  padding: 14px 10px 60px;
  overflow-y: auto;
  position: relative;
  transition: width var(--dur-ui) ease-out;
}
.sidebar.collapsed { width: 56px; }
.nav-group { margin-bottom: 18px; }
.nav-group-title {
  font-size: var(--fs-11);
  color: var(--text-2);
  letter-spacing: 0.08em;
  padding: 0 10px 6px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  margin-bottom: 4px;
  border-radius: var(--radius-ctrl);
  color: var(--text-1);
  text-decoration: none;
  font-size: var(--fs-13);
}
/* 侧栏选中项（★★ 凹陷）：选中="按下去"，未选=平 */
.nav-item.active {
  box-shadow: var(--shadow-inset);
  color: var(--accent);
  border-left: 3px solid var(--accent);
  padding-left: 7px;
  font-weight: 600;
}
.nav-icon { width: 18px; text-align: center; }
.collapse-btn {
  position: absolute;
  bottom: 14px;
  left: 10px; right: 10px;
  padding: 6px;
  border: none;
  border-radius: var(--radius-ctrl);
  background: var(--bg-1);
  box-shadow: var(--shadow-raised-sm);
  color: var(--text-2);
  cursor: pointer;
}
.collapse-btn:active { box-shadow: var(--shadow-inset); }
</style>
