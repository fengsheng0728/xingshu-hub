<template>
  <div>
    <div class="page-title">披露治理 Disclosure</div>
    <div class="page-sub">规则链 · 模拟器（逐规则命中轨迹）· shadow 差异报告</div>

    <div v-if="dis.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> {{ dis.error }}（仅 manager/orchestrator 可用）
    </div>

    <div class="grid-2">
      <!-- 左：规则链 -->
      <div class="panel">
        <div class="panel-title">规则链（{{ rules.length }} 条，按优先级短路求值）</div>
        <table v-if="rules.length" class="table">
          <thead><tr><th style="width: 40px">#</th><th>规则</th><th>语义</th></tr></thead>
          <tbody>
            <tr v-for="r in rules" :key="r.id"
                :class="{ 'row-hit': hitRule === r.id, 'row-skip': skipRule(r.id) }"
                @click="open('规则 · ' + r.name, r)">
              <td data-mono>{{ r.priority }}</td>
              <td>
                <span class="badge">
                  <span class="lamp" :class="traceLamp(r.id)"></span>{{ r.name }}
                </span>
                <span class="rule-id" data-mono>{{ r.id }}</span>
              </td>
              <td class="rule-desc">{{ r.desc }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">规则表未加载</div>
          <div class="empty-desc">/api/audit/disclosure/rules 返回为空</div>
        </div>
        <div class="trace-legend" v-if="dis.simResult">
          <span class="badge"><span class="lamp on-ok"></span>命中</span>
          <span class="badge"><span class="lamp off"></span>已评估未通过</span>
          <span class="badge"><span class="lamp" style="opacity:.3"></span>未到达</span>
        </div>
      </div>

      <!-- 右：模拟器 -->
      <div class="panel">
        <div class="panel-title">模拟器（输入 requester + target → 判定轨迹）</div>
        <div class="sim-form">
          <div class="form-row">
            <label>请求方</label>
            <select v-model="form.requester_agent_id" class="input">
              <option value="">— 选择 —</option>
              <option v-for="a in agents" :key="a.agent_id" :value="a.agent_id">
                {{ a.agent_id }}（{{ a.role }}）
              </option>
            </select>
            <label>目标属主</label>
            <select v-model="form.owner_agent_id" class="input" :disabled="!!form.memory_id">
              <option value="">— 选择 —</option>
              <option v-for="a in agents" :key="a.agent_id" :value="a.agent_id">
                {{ a.agent_id }}（{{ a.role }}）
              </option>
            </select>
          </div>
          <div class="form-row">
            <label>记忆 ID</label>
            <input v-model="form.memory_id" class="input" style="width: 170px"
                   placeholder="留空=合成记忆" @input="form.memory_id = form.memory_id.trim()">
            <label>存储级别</label>
            <select v-model="form.memory_level" class="input" :disabled="!!form.memory_id">
              <option value="summary">summary</option>
              <option value="full">full</option>
              <option value="metadata">metadata</option>
              <option value="none">none</option>
            </select>
            <label>请求级别</label>
            <select v-model="form.required_level" class="input">
              <option value="full">full</option>
              <option value="summary">summary</option>
              <option value="metadata">metadata</option>
            </select>
            <button class="btn primary" :disabled="!canRun || dis.simulating" @click="run">
              {{ dis.simulating ? '模拟中…' : '模拟' }}
            </button>
          </div>
        </div>

        <!-- 判定结果 -->
        <div v-if="dis.simResult" class="sim-result">
          <div class="verdict">
            <span class="lamp" :class="`dot-lv-${dis.simResult.level}`"></span>
            <span class="verdict-level" :class="`lv-${dis.simResult.level}`">
              {{ dis.simResult.level.toUpperCase() }}
            </span>
            <span class="kpi-foot" style="margin-left: 10px">
              命中规则 {{ hitRule }} · {{ dis.simResult.inputs.memory_source === 'real' ? '真实记忆' : '合成记忆' }}
            </span>
          </div>
          <div class="kpi-foot">
            {{ dis.simResult.inputs.requester_agent_id }}（{{ dis.simResult.inputs.requester_role }}）
            → {{ dis.simResult.inputs.owner_agent_id }}（{{ dis.simResult.inputs.owner_role }}）
            · 存储 {{ dis.simResult.inputs.memory_level }} · 请求 {{ dis.simResult.inputs.required_level }}
          </div>
          <button class="btn" style="margin-top: 8px" @click="open('模拟详情', dis.simResult)">原始 JSON</button>
        </div>
        <div v-else class="empty" style="margin-top: 12px">
          <div class="empty-title">尚未模拟</div>
          <div class="empty-desc">判定结果用四级语义色展示，命中规则在左侧规则链同步高亮</div>
        </div>
      </div>
    </div>

    <!-- shadow 差异报告 -->
    <div class="panel">
      <div class="panel-title">
        shadow 差异报告（enforce vs shadow 全量重放）
        <button class="btn primary" style="float: right" :disabled="dis.replaying" @click="dis.runReplay()">
          {{ dis.replaying ? '重放中…' : '运行重放' }}
        </button>
      </div>
      <template v-if="dis.replay">
        <div class="alert-strip" :style="{ color: dis.replay.mismatched === 0 ? 'var(--ok)' : 'var(--danger)' }">
          <span class="lamp" :class="dis.replay.mismatched === 0 ? 'on-ok' : 'on-danger'"></span>
          共 {{ dis.replay.total }} 条历史判定 · 一致 {{ dis.replay.matched }} · 差异 {{ dis.replay.mismatched }}
        </div>
        <table v-if="(dis.replay.mismatches || []).length" class="table">
          <thead><tr><th>log</th><th>记忆</th><th>from → to</th><th>enforce</th><th>shadow</th><th>shadow 规则</th></tr></thead>
          <tbody>
            <tr v-for="m in dis.replay.mismatches" :key="m.log_id" class="row-diff" @click="open('差异详情', m)">
              <td data-mono>{{ m.log_id }}</td>
              <td data-mono>{{ (m.memory_id || '').slice(0, 12) }}</td>
              <td data-mono>{{ m.from_agent }} → {{ m.to_agent }}</td>
              <td><span class="badge"><span class="lamp" :class="`dot-lv-${m.history_level}`"></span>{{ m.history_level }}</span></td>
              <td><span class="badge"><span class="lamp" :class="`dot-lv-${m.sim_level}`"></span>{{ m.sim_level }}</span></td>
              <td data-mono>{{ m.sim_rule }}</td>
            </tr>
          </tbody>
        </table>
      </template>
      <div v-else class="empty">
        <div class="empty-title">未运行</div>
        <div class="empty-desc">重放 disclosure_log 全量历史：100% 一致 = 规则表化未改变线上行为</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive } from 'vue'
import { useDisclosureStore, useDashboardStore, useUiStore } from '../stores'

const dis = useDisclosureStore()
const dash = useDashboardStore()
const ui = useUiStore()
const form = reactive({
  requester_agent_id: '', owner_agent_id: '', memory_id: '',
  memory_level: 'summary', required_level: 'full',
})

onMounted(() => {
  dis.loadRules()
  if (!dash.data) dash.refresh()
})

const rules = computed(() => dis.rules || [])
const agents = computed(() => dash.data?.agents?.list || [])
const hitRule = computed(() => dis.simResult?.hit_rule || '')
const traceStages = computed(() => {
  const m = {}
  for (const t of dis.simResult?.trace || []) m[t.id] = t.stage
  return m
})
const canRun = computed(() =>
  form.requester_agent_id && (form.memory_id || form.owner_agent_id))

function traceLamp(id) {
  const s = traceStages.value[id]
  if (!s) return 'off'
  return s === 'hit' ? 'on-ok' : s === 'info' ? 'on-warn' : 'off'
}
function skipRule(id) {
  return traceStages.value[id] === 'skip'
}
async function run() {
  const payload = {
    requester_agent_id: form.requester_agent_id,
    required_level: form.required_level,
  }
  if (form.memory_id) payload.memory_id = form.memory_id
  else {
    payload.owner_agent_id = form.owner_agent_id
    payload.memory_level = form.memory_level
  }
  await dis.simulate(payload)
}
function open(title, raw) {
  ui.openDrawer({ title, raw })
}
</script>

<style scoped>
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
@media (max-width: 1200px) { .grid-2 { grid-template-columns: 1fr; } }
.rule-id { margin-left: 8px; font-size: var(--fs-11); color: var(--text-2); }
.rule-desc { font-size: var(--fs-12); color: var(--text-1); }
.row-hit td { background: color-mix(in srgb, var(--ok) 10%, transparent); }
.row-skip td { opacity: 0.45; }
.row-diff td { background: color-mix(in srgb, var(--danger) 8%, transparent); }
.trace-legend { display: flex; gap: 16px; margin-top: 10px; font-size: var(--fs-12); }
.sim-form { display: flex; flex-direction: column; gap: 10px; }
.form-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--fs-12); }
.input {
  padding: 5px 8px; border-radius: 6px; border: none; font-size: var(--fs-12);
  background: var(--bg-0); color: var(--text-0); box-shadow: var(--shadow-inset);
}
.sim-result { margin-top: 14px; padding: 12px; background: var(--bg-2); border-radius: 8px; }
.verdict { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.verdict-level { font-size: var(--fs-20); font-weight: 700; }
</style>
