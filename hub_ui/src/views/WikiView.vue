<template>
  <div>
    <div class="page-title">Wiki</div>
    <div class="page-sub">已发布页面 · 收件箱审查（新知识发布前必须过审）</div>

    <div v-if="ws.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> {{ ws.error }}
    </div>

    <!-- 收件箱审查（优先：这是待办） -->
    <div class="panel">
      <div class="panel-title">
        收件箱待审（{{ inbox.length }}）
        <span v-if="inbox.length" class="lamp on-warn" style="margin-left: 8px"></span>
      </div>
      <table v-if="inbox.length" class="table">
        <thead><tr><th>标题</th><th>路径</th><th>来源</th><th>进入时间</th><th style="width: 150px">操作</th></tr></thead>
        <tbody>
          <tr v-for="i in inbox" :key="i.id">
            <td>{{ i.title }}</td>
            <td data-mono>{{ i.page_path }}</td>
            <td><span class="badge">{{ i.source || '—' }}</span></td>
            <td data-mono>{{ fmtTime(i.created_at) }}</td>
            <td>
              <button class="btn primary" style="margin-right: 6px" @click.stop="review(i, 'approve')">发布</button>
              <button class="btn" @click.stop="review(i, 'reject')">拒绝</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <div class="empty-title">收件箱已清空</div>
        <div class="empty-desc">外部/低信任来源的新知识会先进收件箱，发布后才可被检索</div>
      </div>
    </div>

    <!-- 页面检索 + 列表 -->
    <div class="panel">
      <div class="panel-title">已发布页面（{{ shownPages.length }}）</div>
      <div class="search-bar">
        <input v-model="q" class="input" style="width: 240px" placeholder="全文搜索 Wiki…" @keyup.enter="ws.search(q)">
        <button class="btn" :disabled="ws.searching" @click="ws.search(q)">{{ ws.searching ? '搜索中…' : '搜索' }}</button>
        <button v-if="ws.searchResult" class="btn" @click="clearSearch">清除</button>
      </div>
      <table v-if="shownPages.length" class="table" style="margin-top: 12px">
        <thead><tr><th>路径</th><th>标题</th><th v-if="!ws.searchResult">类型</th><th v-if="ws.searchResult">匹配</th></tr></thead>
        <tbody>
          <tr v-for="p in shownPages" :key="p.path || p.page_path" @click="preview(p)">
            <td data-mono>{{ p.path || p.page_path }}</td>
            <td>{{ p.title || '—' }}</td>
            <td v-if="!ws.searchResult"><span class="badge">{{ p.type || dirOf(p.path || p.page_path) }}</span></td>
            <td v-if="ws.searchResult" class="snippet">{{ (p.snippet || p.excerpt || '').slice(0, 60) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty" style="margin-top: 12px">
        <div class="empty-title">{{ ws.searchResult ? '无匹配页面' : '暂无已发布页面' }}</div>
        <div class="empty-desc">行点击预览页面内容</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useWikiStore, useUiStore } from '../stores'

const ws = useWikiStore()
const ui = useUiStore()
const q = ref('')

onMounted(() => ws.refresh())

const inbox = computed(() => ws.inbox || [])
const shownPages = computed(() => ws.searchResult ?? (ws.pages || []))

function clearSearch() {
  q.value = ''
  ws.searchResult = null
}
async function review(item, action) {
  try {
    await ws.review(item.id, action)
  } catch (e) {
    ui.openDrawer({ title: '审查操作失败', raw: { error: e.message, item } })
  }
}
async function preview(p) {
  const path = p.path || p.page_path
  try {
    const d = await ws.readPage(path)
    ui.openDrawer({ title: 'Wiki · ' + (p.title || path), raw: d })
  } catch (e) {
    ui.openDrawer({ title: '页面读取失败', raw: { error: e.message, path } })
  }
}
function dirOf(path) {
  return (path || '').split('/')[0] || '—'
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(5, 16) : d.toLocaleString()
}
</script>

<style scoped>
.search-bar { display: flex; gap: 8px; align-items: center; }
.input {
  padding: 5px 8px; border-radius: 6px; border: none; font-size: var(--fs-12);
  background: var(--bg-0); color: var(--text-0); box-shadow: var(--shadow-inset);
}
.snippet { font-size: var(--fs-11); color: var(--text-2); }
</style>
