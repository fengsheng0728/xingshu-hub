<template>
  <div>
    <div class="page-title">身份供给 Provision</div>
    <div class="page-sub">
      部门 → 员工凭据 → 内容级权限（披露上限 + 数据域）· 部门只做目录与默认值，权限判定仍走员工记录
    </div>

    <div v-if="err" class="alert-strip warn"><span class="lamp on-warn"></span> {{ err }}</div>
    <div v-if="notice" class="alert-strip ok"><span class="lamp on-ok"></span> {{ notice }}</div>

    <div class="prov-grid">
      <!-- ── 左：部门目录 ── -->
      <div class="panel">
        <div class="panel-title row-between">
          <span>部门（{{ depts.length }}）</span>
          <button class="btn-xs" @click="showNewDept = !showNewDept">
            {{ showNewDept ? '取消' : '+ 新建部门' }}
          </button>
        </div>

        <div v-if="showNewDept" class="mini-form">
          <input v-model="nd.name" class="input" placeholder="部门名称（唯一，如 客服部）" />
          <input v-model="nd.description" class="input" placeholder="描述（可选）" />
          <label class="lbl">建员工时的默认模板</label>
          <select v-model="nd.default_role_template" class="input">
            <option v-for="tp in TEMPLATES" :key="tp.k" :value="tp.k">{{ tp.label }}</option>
          </select>
          <button class="btn" :disabled="!nd.name.trim() || busy" @click="createDept">创建部门</button>
        </div>

        <table v-if="depts.length" class="table">
          <thead><tr><th>部门</th><th>员工</th><th>已签发</th><th>Agent</th></tr></thead>
          <tbody>
            <tr v-for="d in depts" :key="d.department_id"
                :class="{ sel: d.name === cur }" @click="select(d.name)">
              <td>
                {{ d.name }}
                <div class="dim">{{ d.description || '—' }} · 默认 {{ d.default_role_template }}</div>
              </td>
              <td data-mono>{{ d.employee_count }}</td>
              <td data-mono>{{ d.key_issued }}</td>
              <td data-mono>{{ d.agent_count }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">还没有部门</div>
          <div class="empty-desc">先建部门，再在部门下建员工并配权限</div>
        </div>

        <div v-if="unmanaged.length" class="unmanaged">
          <div class="panel-title">未纳管部门（{{ unmanaged.length }}）</div>
          <div class="page-sub">员工里出现、目录里没有同名条目（历史自由文本，或改名时没同步员工）</div>
          <div v-for="u in unmanaged" :key="u.department" class="un-row">
            <span>{{ u.department }} · {{ u.employee_count }} 人</span>
            <button class="btn-xs" @click="adopt(u.department)">纳入目录</button>
          </div>
        </div>
      </div>

      <!-- ── 右：部门详情 ── -->
      <div v-if="curDept" class="panel">
        <div class="panel-title row-between">
          <span>{{ curDept.name }} <span class="dim">· {{ curDept.employee_count }} 名员工 / {{ curDept.agent_count }} 台 Agent</span></span>
          <span class="btn-group">
            <button class="btn-xs" @click="toggleEditDept">编辑部门</button>
            <button class="btn-xs danger" @click="deleteDept">删除</button>
          </span>
        </div>

        <div v-if="showEditDept" class="mini-form">
          <label class="lbl">名称</label>
          <input v-model="ed.name" class="input" />
          <label class="lbl">描述</label>
          <input v-model="ed.description" class="input" />
          <label class="lbl">建员工默认模板</label>
          <select v-model="ed.default_role_template" class="input">
            <option v-for="tp in TEMPLATES" :key="tp.k" :value="tp.k">{{ tp.label }}</option>
          </select>
          <label class="chk">
            <input type="checkbox" v-model="ed.sync_employees" :disabled="ed.name.trim() === curDept.name" />
            改名同时同步 {{ curDept.employee_count }} 名员工的所属部门（不勾则旧名字会落到「未纳管」）
          </label>
          <button class="btn" :disabled="busy" @click="saveDept">保存</button>
        </div>

        <!-- 员工凭据 -->
        <div class="panel-title row-between" style="margin-top: 14px">
          <span>员工凭据（{{ employees.length }}）</span>
          <button class="btn-xs" @click="toggleNewEmp">{{ showNewEmp ? '取消' : '+ 新建员工' }}</button>
        </div>

        <div v-if="showNewEmp" class="mini-form">
          <div class="grid2">
            <input v-model="ne.name" class="input" placeholder="姓名" />
            <input v-model="ne.email" class="input" placeholder="邮箱（登录标识，唯一）" />
          </div>
          <label class="lbl">权限模板（决定披露上限与数据域）</label>
          <select v-model="ne.role_template" class="input">
            <option v-for="tp in TEMPLATES" :key="tp.k" :value="tp.k">{{ tp.label }}</option>
          </select>
          <div class="tpl-card">
            <div><b>{{ tplInfo(ne.role_template).label }}</b></div>
            <div>披露上限：<b>{{ tplInfo(ne.role_template).cap }}</b> ｜ 可见范围：{{ tplInfo(ne.role_template).see }}</div>
            <div class="dim">数据域 = 所属部门<b>「{{ curDept.name }}」</b>{{ ne.role_template === 'external' ? ' + 项目域' : '' }}</div>
          </div>
          <div class="grid2">
            <input v-if="ne.role_template === 'external'" v-model="ne.project_scope" class="input" placeholder="项目域（external 必填）" />
            <input v-model="ne.lease_expires_at" class="input" placeholder="租约到期（可选，如 2026-12-31）" />
            <input v-model="ne.label" class="input" placeholder="凭据标签（可选，如 小王的主钥匙）" />
          </div>
          <button class="btn" :disabled="!ne.name.trim() || !ne.email.trim() || busy" @click="createEmp">
            创建并签发凭据
          </button>
        </div>

        <table v-if="employees.length" class="table">
          <thead>
            <tr><th>姓名 / 邮箱</th><th>权限模板</th><th>项目域</th><th>状态</th><th>凭据</th><th>租约</th><th></th></tr>
          </thead>
          <tbody>
            <template v-for="e in employees" :key="e.employee_id">
              <tr>
                <td>{{ e.name }}<div class="dim">{{ e.email }}</div></td>
                <td>
                  <span class="badge">{{ e.role_template }}</span>
                  <div class="dim">上限 {{ tplInfo(e.role_template).cap }}</div>
                </td>
                <td data-mono>{{ e.project_scope || '—' }}</td>
                <td>
                  <span class="lamp" :class="e.status === 'active' ? 'on-ok' : 'on-off'"></span>
                  {{ e.status === 'active' ? '在职' : '已停用' }}
                </td>
                <td>
                  <span class="badge" :class="{ warn: !e.key_count }">{{ e.key_count }} 把</span>
                  <button class="btn-xs" @click="toggleKeys(e)">{{ expandedKeys === e.employee_id ? '收起' : '明细' }}</button>
                </td>
                <td data-mono>{{ e.lease_expires_at || '—' }}</td>
                <td class="act">
                  <button class="btn-xs" @click="toggleEditEmp(e)">改权限</button>
                  <button class="btn-xs" @click="issueKey(e)">签发新凭据</button>
                  <button v-if="e.key_count" class="btn-xs danger" @click="revokeAllKeys(e)">吊销全部</button>
                  <button v-if="e.status === 'active'" class="btn-xs danger" @click="setStatus(e, 'disabled')">停用</button>
                  <button v-else class="btn-xs" @click="setStatus(e, 'active')">恢复</button>
                </td>
              </tr>
              <tr v-if="expandedKeys === e.employee_id" class="edit-row">
                <td colspan="7">
                  <div class="panel-title" style="margin-bottom: 6px">
                    凭据明细（{{ (keysOf[e.employee_id] || []).length }} 把 · 明文只在签发时可见一次，库内只存 SHA256）
                  </div>
                  <table v-if="(keysOf[e.employee_id] || []).length" class="table">
                    <thead><tr><th>key_id</th><th>标签</th><th>状态</th><th>创建</th><th>到期</th><th>最近使用</th><th>调用次数</th><th></th></tr></thead>
                    <tbody>
                      <tr v-for="k in keysOf[e.employee_id]" :key="k.key_id">
                        <td data-mono>{{ k.key_id }}</td>
                        <td>{{ k.label || '—' }}</td>
                        <td>
                          <span class="lamp" :class="k.status === 'active' ? 'on-ok' : 'on-off'"></span>
                          {{ k.status === 'active' ? '有效' : '已吊销' }}
                        </td>
                        <td data-mono>{{ fmtTime(k.created_at) }}</td>
                        <td data-mono>{{ k.expires_at || '不过期' }}</td>
                        <td data-mono>{{ k.last_used_at || '从未使用' }}</td>
                        <td data-mono>{{ k.call_count }}</td>
                        <td class="act">
                          <button v-if="k.status === 'active'" class="btn-xs danger"
                                  @click="revokeOneKey(e, k)">吊销这把</button>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                  <div v-else class="page-sub">暂无凭据——点「签发新凭据」给这位员工发一把</div>
                </td>
              </tr>
              <tr v-if="editing === e.employee_id" class="edit-row">
                <td colspan="7">
                  <div class="mini-form inline">
                    <label class="lbl">模板</label>
                    <select v-model="ef.role_template" class="input">
                      <option v-for="tp in TEMPLATES" :key="tp.k" :value="tp.k">{{ tp.label }}</option>
                    </select>
                    <label class="lbl">项目域</label>
                    <input v-model="ef.project_scope" class="input" placeholder="external 用" />
                    <label class="lbl">所属部门</label>
                    <input v-model="ef.department" class="input" />
                    <label class="lbl">租约到期</label>
                    <input v-model="ef.lease_expires_at" class="input" placeholder="2026-12-31" />
                    <button class="btn" :disabled="busy" @click="saveEmp(e)">保存</button>
                  </div>
                  <div class="tpl-card">
                    改后上限：<b>{{ tplInfo(ef.role_template).cap }}</b> ｜ 可见：{{ tplInfo(ef.role_template).see }}
                    <span class="dim">（模板变更下次认证时现算生效，无需重签凭据）</span>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">该部门还没有员工</div>
          <div class="empty-desc">建员工会自动签发一把凭据（明文只显示一次）</div>
        </div>

        <!-- 该部门已注册 Agent（只读） -->
        <div class="panel-title" style="margin-top: 14px">本部门已注册 Agent（{{ deptAgents.length }}）</div>
        <table v-if="deptAgents.length" class="table">
          <thead><tr><th>Agent</th><th>角色</th><th>状态</th><th>最近心跳</th></tr></thead>
          <tbody>
            <tr v-for="a in deptAgents" :key="a.agent_id">
              <td>{{ a.agent_name || a.agent_id }}<div class="dim" data-mono>{{ a.agent_id }}</div></td>
              <td><span class="badge">{{ a.role }}</span></td>
              <td>
                <span class="lamp" :class="(a.status || '').includes('online') ? 'on-ok' : 'on-off'"></span>
                {{ a.status }}
              </td>
              <td data-mono>{{ fmtTime(a.last_heartbeat) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="page-sub">该部门暂无已注册 Agent（Agent 是机器身份，经注册接口登记，不在此页创建）</div>
      </div>

      <div v-else class="panel">
        <div class="empty">
          <div class="empty-title">先选一个部门</div>
          <div class="empty-desc">左侧点部门名进二级页：建员工、配权限、签发凭据</div>
        </div>
      </div>
    </div>

    <!-- 明文凭据一次性弹窗 -->
    <div v-if="plainKey" class="modal-mask" @click.self="plainKey = ''">
      <div class="modal">
        <div class="panel-title">凭据只显示这一次</div>
        <div class="page-sub">关掉就再也查不到（库内只存 SHA256）。请现在复制并用安全渠道交给本人。</div>
        <div class="key-box">{{ plainKey }}</div>
        <div class="btn-group" style="margin-top: 12px">
          <button class="btn" @click="copyKey">复制</button>
          <button class="btn-xs" @click="plainKey = ''">我已保存</button>
        </div>
        <div v-if="copied" class="page-sub" style="margin-top: 8px">已复制到剪贴板</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const TEMPLATES = [
  { k: 'owner', label: 'owner — 负责人', cap: 'FULL', see: '全域内容（不限部门域）' },
  { k: 'dept_head', label: 'dept_head — 部门主管', cap: 'FULL', see: '本部门全文' },
  { k: 'staff', label: 'staff — 员工', cap: 'SUMMARY', see: '本部门摘要 + 公司公共区' },
  { k: 'external', label: 'external — 外部协作', cap: 'METADATA', see: '指定项目，仅存在性/标签' },
]

const depts = ref([])
const unmanaged = ref([])
const agents = ref([])
const employees = ref([])
const cur = ref('')
const busy = ref(false)
const err = ref('')
const notice = ref('')
const plainKey = ref('')
const copied = ref(false)

const showNewDept = ref(false)
const nd = ref({ name: '', description: '', default_role_template: 'staff' })
const showEditDept = ref(false)
const ed = ref({ name: '', description: '', default_role_template: 'staff', sync_employees: false })
const showNewEmp = ref(false)
const ne = ref({ name: '', email: '', role_template: 'staff', project_scope: '', lease_expires_at: '', label: '' })
const editing = ref('')
const expandedKeys = ref('')
const keysOf = ref({})
const ef = ref({ role_template: 'staff', project_scope: '', department: '', lease_expires_at: '' })

const curDept = computed(() => depts.value.find((d) => d.name === cur.value) || null)
const deptAgents = computed(() => agents.value.filter((a) => (a.department || '') === cur.value))

function tplInfo(k) {
  return TEMPLATES.find((t) => t.k === k) || { label: k, cap: '?', see: '?' }
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(5, 16) : d.toLocaleString()
}
function fail(e) {
  err.value = e?.detail || e?.message || String(e)
}
function ok(msg) {
  notice.value = msg
  setTimeout(() => { if (notice.value === msg) notice.value = '' }, 4000)
}

async function loadDepts() {
  try {
    const r = await api('/api/v1/access/departments')
    depts.value = r.departments || []
    unmanaged.value = r.unmanaged || []
    if (cur.value && !depts.value.some((d) => d.name === cur.value)) cur.value = ''
  } catch (e) { fail(e) }
}
async function loadAgents() {
  try {
    const r = await api('/api/v1/access/accounts')
    agents.value = r.accounts || []
  } catch (e) { /* Agent 表是附加信息，失权时不阻塞主流程 */ }
}
async function loadEmployees() {
  employees.value = []
  if (!cur.value) return
  try {
    const r = await api('/api/v1/access/accounts/employees', { query: { department: cur.value } })
    employees.value = r.accounts || []
  } catch (e) { fail(e) }
}
function select(name) {
  cur.value = name
  showEditDept.value = false
  showNewEmp.value = false
  editing.value = ''
  err.value = ''
  loadEmployees()
}

async function createDept() {
  busy.value = true; err.value = ''
  try {
    const r = await api('/api/v1/access/departments', { method: 'POST', body: { ...nd.value } })
    nd.value = { name: '', description: '', default_role_template: 'staff' }
    showNewDept.value = false
    await loadDepts()
    ok(`部门已创建：${r.name}`)
  } catch (e) { fail(e) } finally { busy.value = false }
}
async function adopt(name) {
  busy.value = true; err.value = ''
  try {
    await api('/api/v1/access/departments', { method: 'POST', body: { name } })
    await loadDepts()
    ok(`已纳入目录：${name}`)
  } catch (e) { fail(e) } finally { busy.value = false }
}
function toggleEditDept() {
  if (!curDept.value) return
  ed.value = {
    name: curDept.value.name,
    description: curDept.value.description || '',
    default_role_template: curDept.value.default_role_template || 'staff',
    sync_employees: false,
  }
  showEditDept.value = !showEditDept.value
}
async function saveDept() {
  busy.value = true; err.value = ''
  const old = curDept.value.name
  try {
    const body = { ...ed.value }
    if (body.name === old) delete body.name
    const r = await api(`/api/v1/access/departments/${curDept.value.department_id}`,
      { method: 'PATCH', body })
    showEditDept.value = false
    cur.value = body.name || old
    await loadDepts(); await loadEmployees()
    ok(`已保存${r.moved_employees ? `，同步了 ${r.moved_employees} 名员工的所属部门` : ''}`)
  } catch (e) { fail(e) } finally { busy.value = false }
}
async function deleteDept() {
  if (!curDept.value) return
  const d = curDept.value
  if (!window.confirm(`删除部门「${d.name}」？（有员工时会被拒绝）`)) return
  busy.value = true; err.value = ''
  try {
    await api(`/api/v1/access/departments/${d.department_id}`, { method: 'DELETE' })
    cur.value = ''
    await loadDepts()
    ok(`部门已删除：${d.name}`)
  } catch (e) { fail(e) } finally { busy.value = false }
}

function toggleNewEmp() {
  showNewEmp.value = !showNewEmp.value
  if (showNewEmp.value) {
    ne.value = {
      name: '', email: '', role_template: curDept.value?.default_role_template || 'staff',
      project_scope: '', lease_expires_at: '', label: '',
    }
  }
}
async function createEmp() {
  busy.value = true; err.value = ''
  try {
    const r = await api('/api/v1/access/accounts', {
      method: 'POST',
      body: { ...ne.value, department: cur.value },
    })
    showNewEmp.value = false
    plainKey.value = r.key
    copied.value = false
    await loadDepts(); await loadEmployees()
    expandedKeys.value = r.employee_id
    await loadKeys(r.employee_id)
    ok(`员工已创建：${r.employee_id}`)
  } catch (e) { fail(e) } finally { busy.value = false }
}
function toggleEditEmp(e) {
  editing.value = editing.value === e.employee_id ? '' : e.employee_id
  ef.value = {
    role_template: e.role_template, project_scope: e.project_scope || '',
    department: e.department || cur.value, lease_expires_at: e.lease_expires_at || '',
  }
}
async function saveEmp(e) {
  busy.value = true; err.value = ''
  try {
    await api(`/api/v1/access/accounts/${e.employee_id}`, { method: 'PATCH', body: { ...ef.value } })
    editing.value = ''
    await loadDepts(); await loadEmployees()
    ok('权限已更新（下次认证时生效）')
  } catch (e2) { fail(e2) } finally { busy.value = false }
}
async function setStatus(e, status) {
  busy.value = true; err.value = ''
  try {
    await api(`/api/v1/access/accounts/${e.employee_id}`, { method: 'PATCH', body: { status } })
    await loadDepts(); await loadEmployees()
    ok(status === 'active' ? '已恢复' : '已停用（凭据仍在，如需立即失效请再吊销）')
  } catch (e2) { fail(e2) } finally { busy.value = false }
}
async function issueKey(e) {
  busy.value = true; err.value = ''
  try {
    const r = await api(`/api/v1/access/accounts/${e.employee_id}/key`, { method: 'POST' })
    plainKey.value = r.key
    copied.value = false
    await loadEmployees()
    if (expandedKeys.value === e.employee_id) await loadKeys(e.employee_id)
  } catch (e2) { fail(e2) } finally { busy.value = false }
}
async function revokeAllKeys(e) {
  if (!window.confirm(`吊销「${e.name}」的全部 ${e.key_count} 把凭据？立即失效，之后可再签新的。`)) return
  busy.value = true; err.value = ''
  try {
    const r = await api(`/api/v1/access/accounts/${e.employee_id}/revoke`, { method: 'POST' })
    if (expandedKeys.value === e.employee_id) await loadKeys(e.employee_id)
    await loadDepartmentsSafe()
    ok(`已吊销 ${r.revoked ?? 0} 把凭据`)
  } catch (e2) { fail(e2) } finally { busy.value = false }
}
async function toggleKeys(e) {
  if (expandedKeys.value === e.employee_id) { expandedKeys.value = ''; return }
  expandedKeys.value = e.employee_id
  await loadKeys(e.employee_id)
}
async function loadKeys(empId) {
  try {
    const r = await api(`/api/v1/access/accounts/${empId}/keys`)
    keysOf.value = { ...keysOf.value, [empId]: r.keys || [] }
  } catch (e) { fail(e) }
}
async function revokeOneKey(e, k) {
  if (!window.confirm(`吊销凭据 ${k.key_id}（${k.label || '无标签'}）？该员工其他凭据不受影响。`)) return
  busy.value = true; err.value = ''
  try {
    await api(`/api/v1/access/accounts/${e.employee_id}/keys/${k.key_id}/revoke`, { method: 'POST' })
    await loadKeys(e.employee_id)
    await loadDepartmentsSafe()
    ok('已吊销这一把')
  } catch (e2) { fail(e2) } finally { busy.value = false }
}
async function loadDepartmentsSafe() {
  await loadDepts(); await loadEmployees()
}
async function copyKey() {
  try {
    await navigator.clipboard.writeText(plainKey.value)
    copied.value = true
  } catch { /* 剪贴板不可用时让用户手动选中复制 */ }
}

onMounted(() => { loadDepts(); loadAgents() })
</script>

<style scoped>
.prov-grid { display: grid; grid-template-columns: minmax(300px, 360px) 1fr; gap: 14px; align-items: start; }
.row-between { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.dim { color: var(--text-2); font-size: var(--fs-11); }
.mini-form {
  display: flex; flex-direction: column; gap: 6px; margin: 8px 0 12px;
  padding: 10px; border-radius: var(--radius-ctrl); background: var(--bg-0);
  box-shadow: var(--shadow-inset);
}
.mini-form.inline { flex-direction: row; flex-wrap: wrap; align-items: center; gap: 8px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.input {
  padding: 5px 8px; border-radius: 6px; border: none; font-size: var(--fs-12);
  background: var(--bg-1); color: var(--text-0); box-shadow: var(--shadow-inset);
}
.lbl { font-size: var(--fs-11); color: var(--text-2); }
.chk { font-size: var(--fs-11); color: var(--text-1); display: flex; gap: 6px; align-items: center; }
.btn, .btn-xs {
  border: none; cursor: pointer; border-radius: var(--radius-ctrl);
  background: var(--accent); color: #fff; font-size: var(--fs-12); padding: 6px 12px;
}
.btn-xs { padding: 3px 8px; font-size: var(--fs-11); background: var(--bg-1); color: var(--text-0); box-shadow: var(--shadow-inset); }
.btn-xs.danger { color: var(--warn); }
.btn:disabled { opacity: 0.5; cursor: default; }
.btn-group { display: inline-flex; gap: 6px; }
.tpl-card {
  font-size: var(--fs-11); color: var(--text-1); background: var(--bg-1);
  border-radius: var(--radius-ctrl); padding: 8px 10px; line-height: 1.7;
}
tr.sel { background: var(--bg-0); box-shadow: var(--shadow-inset); }
tr.edit-row td { background: var(--bg-0); }
.act { white-space: nowrap; }
.act > * { margin-right: 4px; }
.unmanaged { margin-top: 14px; padding-top: 10px; border-top: 1px dashed var(--bg-0); }
.un-row { display: flex; align-items: center; justify-content: space-between; padding: 4px 0; font-size: var(--fs-12); }
.modal-mask {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 60;
}
.modal {
  background: var(--bg-1); border-radius: var(--radius-ctrl); padding: 18px;
  max-width: 520px; width: calc(100% - 40px); box-shadow: var(--shadow-raised-sm);
}
.key-box {
  margin-top: 10px; padding: 10px; border-radius: 6px; background: var(--bg-0);
  box-shadow: var(--shadow-inset); font-family: var(--font-mono, monospace);
  font-size: var(--fs-12); word-break: break-all; user-select: all;
}
</style>
