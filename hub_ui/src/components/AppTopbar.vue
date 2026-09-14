<template>
  <header class="topbar">
    <div class="crumbs">
      <span class="crumb-root">星枢 Hub</span>
      <span class="crumb-sep">/</span>
      <span>{{ route.meta.group || '' }}</span>
      <span class="crumb-sep">/</span>
      <strong>{{ route.meta.title || '' }}</strong>
    </div>

    <button class="palette-btn" @click="ui.togglePalette()" title="命令面板">
      ⌘K <span class="palette-hint">搜索 / 跳转 / 动作</span>
    </button>

    <div class="topbar-right">
      <!-- 待办聚合铃铛：角标 = 审批 pending + 未恢复告警（§1.3） -->
      <button class="bell" @click="ui.openDrawer(todoDrawer)" title="待办">
        <svg class="ic" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M8 2a4.5 4.5 0 0 0-4.5 4.5v3l-1.2 2.4a.5.5 0 0 0 .45.7h10.5a.5.5 0 0 0 .45-.7L12.5 9.5v-3A4.5 4.5 0 0 0 8 2z"/>
          <path d="M6.4 13.5a1.6 1.6 0 0 0 3.2 0"/>
        </svg>
        <span v-if="todoCount" class="bell-count mono">{{ todoCount }}</span>
      </button>
      <span class="account" title="hub_token 占位登录（S1-SMB 账号体系就绪后切换）">admin</span>
    </div>
  </header>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useUiStore, useDashboardStore } from '../stores'

const route = useRoute()
const ui = useUiStore()
const dash = useDashboardStore()

const todoCount = computed(() => {
  const approvals = dash.data?.pending_disclosures?.length || 0
  const alerts = alertsList.value.length
  return approvals + alerts
})

const alertsList = computed(() => {
  const list = []
  if (dash.health.live === false) list.push('存活探针失败（/healthz）')
  if (dash.health.ready === false) list.push('就绪探针失败（/readyz）')
  const q = dash.buffer?.queue_depth || 0
  if (q > 100) list.push(`写入缓冲积压 ${q} 条`)
  const failed = dash.data?.tasks?.by_status?.failed || 0
  if (failed > 0) list.push(`${failed} 个任务处于 failed 状态`)
  return list
})

const todoDrawer = computed(() => ({
  title: '待办聚合',
  lines: [
    { k: '待审批披露', v: String(dash.data?.pending_disclosures?.length || 0) },
    { k: '未恢复告警', v: String(alertsList.value.length) },
    ...alertsList.value.map((a, i) => ({ k: `告警 ${i + 1}`, v: a })),
  ],
}))
</script>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 20px;
  background: var(--bg-1);
  box-shadow: var(--shadow-raised-sm);
  z-index: 10;
}
.crumbs { font-size: var(--fs-13); color: var(--text-1); flex: 0 0 auto; }
.crumb-root { color: var(--text-2); }
.crumb-sep { margin: 0 6px; color: var(--text-2); }
.palette-btn {
  flex: 1;
  max-width: 420px;
  margin: 0 auto;
  padding: 7px 14px;
  font-size: var(--fs-12);
  color: var(--text-2);
  text-align: left;
  background: var(--bg-1);
  border: none;
  border-radius: var(--radius-ctrl);
  box-shadow: var(--shadow-inset);   /* 搜索框=凹陷嵌板 */
  cursor: pointer;
}
.palette-hint { margin-left: 8px; }
.topbar-right { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
.bell {
  position: relative;
  background: var(--bg-1);
  border: none;
  border-radius: 50%;
  width: 34px; height: 34px;
  box-shadow: var(--shadow-raised-sm);
  cursor: pointer;
  font-size: 14px;
}
.bell:active { box-shadow: var(--shadow-inset); }
.bell-count {
  position: absolute;
  top: -4px; right: -4px;
  background: var(--danger);
  color: #fff;
  font-size: 10px;
  border-radius: 8px;
  padding: 1px 5px;
}
.account { font-size: var(--fs-13); color: var(--text-1); }
</style>
