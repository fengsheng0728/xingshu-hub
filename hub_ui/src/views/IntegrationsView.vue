<template>
  <div>
    <div class="page-title">集成 Integrations</div>
    <div class="page-sub">
      外部系统连接器（ERP/CRM/财务 · §七 门框期：数据走附录 E 汇入管道，taint=external）
    </div>

    <div v-if="st.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> {{ st.error }}
    </div>

    <div class="kpi-row">
      <div class="kpi-card">
        <div class="kpi-label">已发现连接器</div>
        <div class="kpi-value">{{ connectors.length }}</div>
        <div class="kpi-foot">目录扫描自动注册</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">已配置</div>
        <div class="kpi-value">{{ configuredCount }}</div>
        <div class="kpi-foot">{{ enabledCount }} 个启用中</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">累计入汇记录</div>
        <div class="kpi-value">{{ totalRecords }}</div>
        <div class="kpi-foot">经附录 E 管道分级落库</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">连接器（{{ connectors.length }}）</div>
      <table v-if="connectors.length" class="table">
        <thead><tr>
          <th>连接器</th><th>分类</th><th>状态</th><th>最近同步</th>
          <th>记录数</th><th>拉取间隔</th><th>操作</th>
        </tr></thead>
        <tbody>
          <tr v-for="c in connectors" :key="c.name">
            <td>
              <div>{{ c.display_name }}</div>
              <div class="mono name-sub">{{ c.name }}</div>
            </td>
            <td><span class="badge">{{ c.category }}</span></td>
            <td>
              <span class="dot" :class="statusDot(c)"></span>
              {{ statusText(c) }}
            </td>
            <td data-mono>{{ fmtTime(c.last_sync_at) }}</td>
            <td data-mono>{{ c.record_count }}</td>
            <td data-mono>{{ c.pull_interval_min ? c.pull_interval_min + ' min' : '—' }}</td>
            <td>
              <button class="btn sm" :disabled="!c.configured || st.testing === c.name"
                      @click="doTest(c)">
                {{ st.testing === c.name ? '测试中…' : '测试连接' }}
              </button>
              <button class="btn sm" :disabled="!c.configured || st.pulling === c.name"
                      @click="doPull(c)">
                {{ st.pulling === c.name ? '拉取中…' : '手动拉取' }}
              </button>
              <button class="btn sm ghost" @click="openConfig(c)">
                {{ c.configured ? '配置' : '添加' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        <div class="empty-title">未发现连接器</div>
        <div class="empty-desc">integrations/connectors/ 目录下的适配器会被自动注册（见《适配器开发指南》）</div>
      </div>
    </div>

    <div v-if="st.pullResult" class="alert-strip ok">
      <span class="lamp on-ok"></span>
      {{ st.pullResult.name }} 拉取完成：收到 {{ st.pullResult.received }} 条，
      入汇 {{ st.pullResult.ingested }}（SUMMARY 封顶 {{ st.pullResult.summary_capped }} ·
      NONE 锁定 {{ st.pullResult.locked_none }} · 错误 {{ st.pullResult.errors }}）
    </div>

    <div v-if="unconfigured.length" class="panel">
      <div class="panel-title">添加连接器</div>
      <div class="card-row">
        <div v-for="a in unconfigured" :key="a.name" class="add-card" @click="openConfig(a)">
          <div class="add-card-cat">{{ catLabel(a.category) }}</div>
          <div class="add-card-name">{{ a.display_name }}</div>
          <div class="add-card-state"><span class="dot dot-off"></span> 未配置</div>
        </div>
      </div>
    </div>

    <div v-if="editing" class="panel">
      <div class="panel-title">配置 · {{ editing.display_name }}</div>
      <div class="form-grid">
        <label>服务地址 server_url
          <input v-model="form.server_url" class="input mono" placeholder="http://erp.internal:8080" />
        </label>
        <label>API 凭证 api_token（加密落库）
          <input v-model="form.api_token" class="input mono" type="password"
                 :placeholder="editing.configured ? '留空保留旧值' : ''" />
        </label>
        <label>拉取间隔（分钟，0=仅手动）
          <input v-model.number="form.pull_interval_min" class="input mono" type="number" min="0" />
        </label>
      </div>
      <div class="panel-title" style="margin-top: 14px">字段映射（规范字段 → 外部字段）</div>
      <div v-for="(row, i) in form.mappingRows" :key="i" class="map-row">
        <input v-model="row.k" class="input mono" placeholder="name" />
        <span class="map-arrow">→</span>
        <input v-model="row.v" class="input mono" placeholder="外部字段名" />
        <button class="btn sm ghost" @click="form.mappingRows.splice(i, 1)">删</button>
      </div>
      <button class="btn sm ghost" @click="form.mappingRows.push({ k: '', v: '' })">+ 加映射</button>
      <div class="form-actions">
        <button class="btn primary" :disabled="st.saving" @click="doSave">
          {{ st.saving ? '保存中…' : '保存并启用' }}
        </button>
        <button class="btn ghost" @click="editing = null">取消</button>
        <span v-if="saveMsg" class="save-msg">{{ saveMsg }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useIntegrationsStore } from '../stores'

const st = useIntegrationsStore()
onMounted(() => st.refresh())

const connectors = computed(() => st.connectors || [])
const configuredCount = computed(() => connectors.value.filter((c) => c.configured).length)
const enabledCount = computed(() => connectors.value.filter((c) => c.enabled).length)
const totalRecords = computed(() => connectors.value.reduce((s, c) => s + (c.record_count || 0), 0))
const unconfigured = computed(() => connectors.value.filter((c) => !c.configured))

const editing = ref(null)
const saveMsg = ref('')
const form = reactive({ server_url: '', api_token: '', pull_interval_min: 0, mappingRows: [] })

function statusDot(c) {
  if (!c.configured) return 'dot-off'
  if (!c.enabled) return 'dot-off'
  if (c.last_status === 'ok') return 'dot-ok'
  if (c.last_status === 'error' || c.last_status === 'partial') return 'dot-err'
  return 'dot-warn'
}
function statusText(c) {
  if (!c.configured) return '未配置'
  if (!c.enabled) return '已禁用'
  return { ok: '正常', never: '待同步', error: '异常', partial: '部分失败' }[c.last_status] || c.last_status
}
function catLabel(cat) {
  return { erp: 'ERP', crm: 'CRM', finance: '财务', oa: 'OA' }[cat] || cat
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(5, 16) : d.toLocaleString()
}

async function doTest(c) {
  const r = await st.test(c.name)
  saveMsg.value = ''
  alert(r.ok ? `连接成功：${r.detail}（${r.latency_ms}ms）` : `连接失败：${r.detail}`)
}
async function doPull(c) {
  try { await st.pull(c, true) } catch (e) { alert('拉取失败：' + (e.message || e)) }
}
function openConfig(c) {
  editing.value = c
  saveMsg.value = ''
  form.server_url = c.config?.server_url || ''
  form.api_token = ''
  form.pull_interval_min = c.pull_interval_min || 0
  const m = c.field_mapping || {}
  form.mappingRows = Object.keys(m).length
    ? Object.entries(m).map(([k, v]) => ({ k, v }))
    : [{ k: 'name', v: '' }, { k: 'note', v: '' }]
}
async function doSave() {
  const mapping = {}
  for (const r of form.mappingRows) if (r.k && r.v) mapping[r.k] = r.v
  const cfg = { server_url: form.server_url }
  if (form.api_token) cfg.api_token = form.api_token
  try {
    await st.configure(editing.value.name, {
      config: cfg, field_mapping: mapping,
      enabled: true, pull_interval_min: form.pull_interval_min,
    })
    saveMsg.value = '已保存（密钥字段已加密落库）'
    editing.value = null
  } catch (e) {
    saveMsg.value = '保存失败：' + (e.message || e)
  }
}
</script>

<style scoped>
.name-sub { font-size: var(--fs-11); color: var(--text-2); }
.card-row { display: flex; gap: 12px; flex-wrap: wrap; }
.add-card {
  min-width: 180px; padding: 14px; border: 1px dashed var(--line);
  border-radius: var(--r-md); cursor: pointer;
}
.add-card:hover { border-color: var(--accent); }
.add-card-cat { font-size: var(--fs-11); color: var(--text-2); }
.add-card-name { font-weight: 600; margin: 4px 0 8px; }
.add-card-state { font-size: var(--fs-11); color: var(--text-2); }
.form-grid { display: grid; grid-template-columns: 1fr 1fr 200px; gap: 12px; }
.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-12); color: var(--text-2); }
.map-row { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
.map-arrow { color: var(--text-2); }
.form-actions { margin-top: 14px; display: flex; gap: 10px; align-items: center; }
.save-msg { font-size: var(--fs-12); color: var(--text-2); }
</style>
