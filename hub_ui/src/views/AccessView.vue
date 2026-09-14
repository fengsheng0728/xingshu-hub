<template>
  <div>
    <div class="page-title">访问与权限 Access</div>
    <div class="page-sub">
      账号 / API Key / 例外与租约
      <span v-if="acc.lastFetch" class="mono refresh-ts">更新于 {{ new Date(acc.lastFetch).toLocaleTimeString() }}</span>
    </div>

    <div v-if="acc.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> {{ acc.error }}（此页仅 manager/orchestrator 角色可访问）
    </div>

    <!-- 顶栏 KPI -->
    <div class="kpi-row">
      <div class="kpi-card">
        <div class="kpi-label">注册账号</div>
        <div class="kpi-value">{{ accounts.length }}</div>
        <div class="kpi-foot">离线 {{ offlineCount }} 台</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">活跃 KEY</div>
        <div class="kpi-value">{{ activeKeys.length }}</div>
        <div class="kpi-foot">已吊销 {{ revokedCount }} 个</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">7 天内到期租约</div>
        <div class="kpi-value" :style="{ color: expiring7d > 0 ? 'var(--warn)' : 'var(--text-0)' }">{{ expiring7d }}</div>
        <div class="kpi-foot">已过期 {{ expiredCount }} 个</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">披露例外</div>
        <div class="kpi-value">{{ exceptions.length }}</div>
        <div class="kpi-foot">已批准的披露申请</div>
      </div>
    </div>

    <!-- Tab 切换 -->
    <div class="toolbar">
      <button v-for="t in tabs" :key="t.key" class="btn chip" :class="{ active: tab === t.key }" @click="tab = t.key">
        {{ t.label }}
      </button>
    </div>

    <!-- Tab 1：账号 -->
    <div v-if="tab === 'accounts'" class="panel">
      <div class="panel-title">账号（{{ accounts.length }}）· 已注册 Agent 身份</div>
      <table v-if="accounts.length" class="table">
        <thead><tr><th>ID</th><th>名称</th><th>角色</th><th>部门</th><th>最近活跃</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="a in accounts" :key="a.agent_id" @click="open('账号详情 · ' + a.agent_id, a)">
            <td data-mono>{{ a.agent_id }}</td>
            <td>{{ a.agent_name || '—' }}</td>
            <td><span class="badge"><span class="lamp" :class="roleLamp(a.role)"></span>{{ a.role }}</span></td>
            <td>{{ a.department || '—' }}</td>
            <td data-mono>{{ fmtTime(a.last_heartbeat) }}</td>
            <td><span class="badge"><span class="lamp" :class="a.status === 'online' ? 'on-ok' : 'off'"></span>{{ a.status }}</span></td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <div class="empty-title">暂无账号</div>
        <div class="empty-desc">S1-SMB 账号体系（阶段 1e）落地前，此处展示已注册 Agent 身份</div>
      </div>
    </div>

    <!-- Tab 2：API Key -->
    <div v-if="tab === 'keys'" class="panel">
      <div class="panel-title">
        Scoped API Key（{{ keyList.length }}）
        <button class="btn primary" style="float: right" @click="showCreate = !showCreate">
          {{ showCreate ? '收起' : '新建 Key' }}
        </button>
      </div>

      <div v-if="showCreate" class="create-form">
        <div class="form-row">
          <label>归属 Agent</label>
          <select v-model="form.agent_id" class="input">
            <option value="">（创建者自己）</option>
            <option v-for="a in accounts" :key="a.agent_id" :value="a.agent_id">{{ a.agent_id }}</option>
          </select>
          <label>级别上限</label>
          <select v-model="form.level_cap" class="input">
            <option value="">（不限制）</option>
            <option value="metadata">metadata</option>
            <option value="summary">summary</option>
            <option value="full">full</option>
          </select>
          <label>到期时间</label>
          <input v-model="form.expires_at" class="input" type="datetime-local">
          <button class="btn primary" :disabled="creating" @click="doCreate">{{ creating ? '创建中…' : '创建' }}</button>
        </div>
        <div v-if="createdKey" class="alert-strip warn" style="margin-top: 10px">
          <span class="lamp on-warn"></span>
          新 Key（仅此一次显示，请立即保存）：<code data-mono>{{ createdKey }}</code>
        </div>
      </div>

      <table v-if="keyList.length" class="table" style="margin-top: 12px">
        <thead><tr><th>Key ID</th><th>归属</th><th>Scope</th><th>到期</th><th>最近调用</th><th>调用数</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="k in keyList" :key="k.key_id" @click="open('Key 详情 · ' + k.key_id, k)">
            <td data-mono>{{ k.key_id }}</td>
            <td data-mono>{{ k.agent_id }}</td>
            <td data-mono>{{ scopeText(k.scope) }}</td>
            <td data-mono :style="{ color: expSoon(k) ? 'var(--warn)' : 'inherit' }">{{ k.expires_at ? fmtTime(k.expires_at) : '永久' }}</td>
            <td data-mono>{{ k.last_used_at ? fmtTime(k.last_used_at) : '从未' }}</td>
            <td data-mono>{{ k.call_count }}</td>
            <td><span class="badge"><span class="lamp" :class="k.status === 'active' ? 'on-ok' : 'off'"></span>{{ k.status }}</span></td>
            <td>
              <button v-if="k.status === 'active'" class="btn" @click.stop="doRevoke(k)">收回</button>
            </td>
          </tr>
        </tbody>
      </table>
        <div v-else class="empty" style="margin-top: 12px">
        <div class="empty-title">暂无 scoped key</div>
        <div class="empty-desc">scoped key 支持 endpoint/data_domain/level_cap 三层scope，点「新建 Key」签发</div>
      </div>
    </div>

    <!-- Tab 3：例外与租约 -->
    <div v-if="tab === 'leases'">
      <div class="panel">
        <div class="panel-title">到期租约（scoped key，{{ expiringList.length }}）</div>
        <table v-if="expiringList.length" class="table">
          <thead><tr><th>Key ID</th><th>归属</th><th>到期时间</th><th>剩余</th><th>最近调用</th><th></th></tr></thead>
          <tbody>
            <tr v-for="k in expiringList" :key="k.key_id" @click="open('租约详情 · ' + k.key_id, k)">
              <td data-mono>{{ k.key_id }}</td>
              <td data-mono>{{ k.agent_id }}</td>
              <td data-mono>{{ fmtTime(k.expires_at) }}</td>
              <td>
                <span class="badge">
                  <span class="lamp" :class="k.expired ? 'on-danger' : k.days_left <= 2 ? 'on-warn' : 'on-ok'"></span>
                  {{ k.expired ? '已过期' : k.days_left + ' 天' }}
                </span>
              </td>
              <td data-mono>{{ k.last_used_at ? fmtTime(k.last_used_at) : '从未' }}</td>
              <td><button v-if="!k.expired" class="btn" @click.stop="doRevoke(k)">一键收回</button></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">无 7 天内到期的租约</div>
          <div class="empty-desc">带 expires_at 的 scoped key 到期前 7 天会出现在这里倒计时</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">披露例外（已批准的披露申请，{{ exceptions.length }}）</div>
        <table v-if="exceptions.length" class="table">
          <thead><tr><th>申请 ID</th><th>任务</th><th>申请人</th><th>升至</th><th>理由</th><th>批准时间</th></tr></thead>
          <tbody>
            <tr v-for="e in exceptions" :key="e.request_id" @click="open('例外详情 · ' + e.request_id, e)">
              <td data-mono>{{ e.request_id }}</td>
              <td data-mono>{{ e.task_id }}</td>
              <td data-mono>{{ e.agent_id }}</td>
              <td data-mono>P{{ e.new_phase }}</td>
              <td>{{ (e.reason || '').slice(0, 30) }}</td>
              <td data-mono>{{ fmtTime(e.created_at) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">无披露例外</div>
          <div class="empty-desc">例外必须被阳光晒到：所有批准的披露申请在此公示（收回语义随 1e 落地）</div>
        </div>
      </div>
    </div>

    <!-- Tab 4：员工账号（1e） -->
    <div v-if="tab === 'employees'" class="panel">
      <div class="panel-title">员工账号（{{ employees.length }}）· SMB 无 AD 的入场券</div>
      <div class="toolbar emp-toolbar">
        <textarea v-model="csvText" class="csv-input" rows="4"
          placeholder="CSV 三列：姓名,邮箱,模板（owner/dept_head/staff/external）&#10;例：张三,zhangsan@corp.cn,staff&#10;李四,lisi@corp.cn,dept_head"></textarea>
        <div class="emp-actions">
          <button class="btn primary" :disabled="importing" @click="doImportCsv">{{ importing ? '导入中…' : '导入 CSV' }}</button>
          <button class="btn" :disabled="empCreating" @click="doCreateEmp">{{ empCreating ? '创建中…' : '新建员工' }}</button>
        </div>
      </div>
      <div v-if="importResult" class="import-result">{{ importResult }}</div>
      <div v-if="issuedKey" class="key-once">新 Key（仅此一次显示，请立即保存）：<code data-mono>{{ issuedKey }}</code></div>
      <table v-if="employees.length" class="table">
        <thead>
          <tr><th>员工 ID</th><th>姓名</th><th>邮箱</th><th>模板</th><th>部门</th><th>Key</th><th>状态</th><th>租约到期</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in employees" :key="e.employee_id" @click="open('员工详情 · ' + e.employee_id, e)">
            <td data-mono>{{ e.employee_id }}</td>
            <td>{{ e.name }}</td>
            <td data-mono>{{ e.email }}</td>
            <td><span class="chip" :class="tplCls(e.role_template)">{{ e.role_template }}</span></td>
            <td>{{ e.department || '—' }}</td>
            <td data-mono>{{ e.has_key ? '已签发' : '未签发' }}</td>
            <td>{{ e.status }}</td>
            <td data-mono>{{ fmtTime(e.lease_expires_at) }}</td>
            <td class="row-actions">
              <button class="btn small" @click.stop="doIssueKey(e)">签发</button>
              <button class="btn small" @click.stop="doRevokeKey(e)">吊销</button>
              <button class="btn small" @click.stop="doDisable(e)">{{ e.status === 'active' ? '禁用' : '启用' }}</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <div class="empty-title">暂无员工账号</div>
        <div class="empty-desc">CSV 导入或新建员工后，可签发个人 key（SHA256 落库，明文仅显示一次）</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useAccessStore, useUiStore } from '../stores'
import { api } from '../api'

const acc = useAccessStore()
const ui = useUiStore()
const tab = ref('accounts')
const tabs = [
  { key: 'accounts', label: '账号' },
  { key: 'employees', label: '员工账号' },
  { key: 'keys', label: 'API Key' },
  { key: 'leases', label: '例外与租约' },
]

onMounted(() => {
  acc.refresh()
  loadEmployees()
})

// ── 1e 员工账号 ──
const employees = ref([])
const csvText = ref('')
const importing = ref(false)
const importResult = ref('')
const issuedKey = ref('')
const empCreating = ref(false)

async function loadEmployees() {
  try {
    const r = await api('/api/v1/access/accounts/employees')
    employees.value = r.accounts || []
  } catch (e) {
    // 401/403 静默（登录门/权限门已全局处理）
  }
}

async function doImportCsv() {
  if (!csvText.value.trim()) return
  importing.value = true
  importResult.value = ''
  try {
    const r = await api('/api/v1/access/accounts/import', { method: 'POST', body: { csv: csvText.value } })
    const rejected = (r.rejected || []).map((b) => `L${b.line}:${b.reason}`).join('；')
    importResult.value = `导入 ${r.imported} · 更新 ${r.updated} · 拒绝 ${r.rejected.length}${rejected ? '（' + rejected + '）' : ''}`
    csvText.value = ''
    await loadEmployees()
  } catch (e) {
    ui.openDrawer({ title: '导入失败', raw: { error: e.message } })
  } finally {
    importing.value = false
  }
}

async function doCreateEmp() {
  empCreating.value = true
  issuedKey.value = ''
  try {
    const r = await api('/api/v1/access/accounts', {
      method: 'POST',
      body: { name: '新员工', email: `new_${Date.now()}@corp.cn`, role_template: 'staff' },
    })
    issuedKey.value = r.key || ''
    await loadEmployees()
  } catch (e) {
    ui.openDrawer({ title: '创建失败', raw: { error: e.message } })
  } finally {
    empCreating.value = false
  }
}

async function doIssueKey(e) {
  issuedKey.value = ''
  try {
    const r = await api(`/api/v1/access/accounts/${e.employee_id}/key`, { method: 'POST', body: {} })
    issuedKey.value = r.key || ''
    await loadEmployees()
  } catch (err) {
    ui.openDrawer({ title: '签发失败', raw: { error: err.message } })
  }
}

async function doRevokeKey(e) {
  try {
    await api(`/api/v1/access/accounts/${e.employee_id}/revoke`, { method: 'POST', body: {} })
    issuedKey.value = ''
    await loadEmployees()
  } catch (err) {
    ui.openDrawer({ title: '吊销失败', raw: { error: err.message } })
  }
}

async function doDisable(e) {
  const next = e.status === 'active' ? 'disabled' : 'active'
  try {
    await api(`/api/v1/access/accounts/${e.employee_id}`, { method: 'PATCH', body: { status: next } })
    await loadEmployees()
  } catch (err) {
    ui.openDrawer({ title: '状态变更失败', raw: { error: err.message } })
  }
}

function tplCls(t) {
  return { owner: 'on-danger', dept_head: 'on-warn', staff: 'off', external: 'off' }[t] || 'off'
}

const accounts = computed(() => acc.accounts?.accounts || [])
const keyList = computed(() => acc.keys?.keys || [])
const exceptions = computed(() => acc.exceptions?.exceptions || [])
const expiringList = computed(() => acc.exceptions?.expiring_keys || [])
const activeKeys = computed(() => keyList.value.filter((k) => k.status === 'active'))
const offlineCount = computed(() => accounts.value.filter((a) => a.status !== 'online').length)
const revokedCount = computed(() => keyList.value.filter((k) => k.status !== 'active').length)
const expiring7d = computed(() => acc.exceptions?.counts?.expiring_7d ?? 0)
const expiredCount = computed(() => acc.exceptions?.counts?.expired ?? 0)

const showCreate = ref(false)
const creating = ref(false)
const createdKey = ref('')
const form = reactive({ agent_id: '', level_cap: '', expires_at: '' })

async function doCreate() {
  creating.value = true
  createdKey.value = ''
  try {
    const scope = form.level_cap ? { level_cap: form.level_cap } : {}
    const payload = { agent_id: form.agent_id, scope }
    if (form.expires_at) payload.expires_at = new Date(form.expires_at).toISOString()
    const r = await acc.createKey(payload)
    createdKey.value = r.key || ''
    await acc.refresh()
  } catch (e) {
    ui.openDrawer({ title: '创建失败', raw: { error: e.message } })
  } finally {
    creating.value = false
  }
}

async function doRevoke(k) {
  try {
    await acc.revokeKey(k.key_id)
    await acc.refresh()
  } catch (e) {
    ui.openDrawer({ title: '收回失败', raw: { error: e.message, key_id: k.key_id } })
  }
}

function scopeText(scope) {
  if (!scope) return '—'
  const s = typeof scope === 'string' ? JSON.parse(scope || '{}') : scope
  const parts = []
  if (s.level_cap) parts.push('≤' + s.level_cap)
  if ((s.endpoints || []).length) parts.push(s.endpoints.length + ' 端点')
  if ((s.data_domain || []).length) parts.push(s.data_domain.length + ' 域')
  return parts.join(' · ') || '全量'
}
function expSoon(k) {
  if (!k.expires_at) return false
  return (new Date(k.expires_at) - Date.now()) < 7 * 86400000
}
function roleLamp(role) {
  return role === 'orchestrator' ? 'on-danger' : role === 'manager' ? 'on-warn' : 'off'
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(0, 16) : d.toLocaleString()
}
function open(title, raw) {
  ui.openDrawer({ title, raw })
}
</script>

<style scoped>
.refresh-ts { margin-left: 12px; font-size: var(--fs-11); color: var(--text-2); }
.toolbar { display: flex; gap: 8px; margin-bottom: 16px; }
.chip { font-size: var(--fs-12); padding: 4px 12px; }
.chip.active { box-shadow: var(--shadow-inset); color: var(--text-0); }
.create-form { padding: 12px; background: var(--bg-2); border-radius: 8px; margin-bottom: 4px; }
.form-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: var(--fs-12); }
.input {
  padding: 5px 8px; border-radius: 6px; border: none; font-size: var(--fs-12);
  background: var(--bg-0); color: var(--text-0); box-shadow: var(--shadow-inset);
}
.emp-toolbar { align-items: flex-start; }
.csv-input {
  flex: 1; min-width: 320px; padding: 8px 10px; border-radius: 8px; border: none;
  font-family: var(--font-mono); font-size: var(--fs-12); line-height: 1.5;
  background: var(--bg-0); color: var(--text-0); box-shadow: var(--shadow-inset);
  resize: vertical;
}
.emp-actions { display: flex; flex-direction: column; gap: 8px; }
.import-result { margin-bottom: 10px; font-size: var(--fs-12); color: var(--ok); }
.key-once {
  margin-bottom: 10px; padding: 8px 12px; border-radius: 8px; font-size: var(--fs-12);
  background: var(--accent-weak); color: var(--text-0);
}
.key-once code { word-break: break-all; }
.row-actions { display: flex; gap: 4px; }
.btn.small { padding: 2px 8px; font-size: var(--fs-11); }
</style>
