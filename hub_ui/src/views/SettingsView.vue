<template>
  <div>
    <div class="page-title">设置 Settings</div>
    <div class="page-sub">server config · 机密词库 · 联邦节点 · Hub Agent 调试</div>

    <div v-if="st.error" class="alert-strip warn">
      <span class="lamp on-warn"></span> {{ st.error }}
    </div>

    <div class="tab-row">
      <button v-for="t in tabs" :key="t.key" class="btn chip" :class="{ active: tab === t.key }" @click="tab = t.key">
        {{ t.label }}
      </button>
    </div>

    <!-- ══ 服务器 ══ -->
    <div v-if="tab === 'server'">
      <div class="panel">
        <div class="panel-title">服务器配置</div>
        <table v-if="st.server" class="table">
          <tbody>
            <tr><td>监听地址</td><td data-mono>{{ st.server.host }}:{{ st.server.port }}</td></tr>
            <tr>
              <td>局域网开放</td>
              <td>
                <span class="dot" :class="st.server.lan_enabled ? 'dot-ok' : 'dot-off'"></span>
                {{ st.server.lan_enabled ? '已开放（0.0.0.0）' : '仅本机（127.0.0.1）' }}
              </td>
            </tr>
            <tr><td>认证</td><td>{{ st.server.auth_enabled ? '启用' : '关闭' }}</td></tr>
            <tr>
              <td>新控制台（ui.new 灰度）</td>
              <td>
                <span class="dot" :class="st.server.ui_new ? 'dot-ok' : 'dot-off'"></span>
                {{ st.server.ui_new ? '已启用' : '回退旧页' }}
                <span v-if="!st.server.ui_new_available" class="badge" style="margin-left: 8px">产物缺失</span>
              </td>
            </tr>
            <tr>
              <td>本机 IP</td>
              <td data-mono>{{ (st.server.network || []).join(' · ') || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="form-actions">
          <button class="btn sm" :disabled="st.saving" @click="toggleLan">
            {{ st.server?.lan_enabled ? '切为仅本机' : '开放局域网' }}
          </button>
          <button class="btn sm" :disabled="st.saving || !st.server?.ui_new_available" @click="toggleUiNew">
            {{ st.server?.ui_new ? '回退旧控制台' : '启用新控制台' }}
          </button>
          <span v-if="serverMsg" class="save-msg">{{ serverMsg }}</span>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">通知通道</div>
        <table class="table">
          <tbody>
            <tr><td>管理员事件流</td><td data-mono>WS /ws/dashboard（首帧 auth，活动流/健康卡实时）</td></tr>
            <tr><td>个人通知</td><td data-mono>WS /ws/{agent_id}（notify fan-out，断线 25s ping + 退避重连）</td></tr>
            <tr><td>缓冲推送</td><td data-mono>WS /ws/buffer（1s 批量）</td></tr>
            <tr><td>实时状态</td><td>通道在线数与降级态见「运行 Ops」页 O2/O5 卡</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ══ 机密词库 ══ -->
    <div v-if="tab === 'words'">
      <div class="panel">
        <div class="panel-title">
          机密词库（{{ st.wordsCount }} 词 · 来源 {{ sourceLabel }}）
        </div>
        <div v-if="st.wordsDenied" class="empty">
          <div class="empty-title">仅主管/店长可查看</div>
          <div class="empty-desc">词库本身是敏感信息：查看/重载走 manager/orchestrator 审批门（动作入审计链）</div>
        </div>
        <template v-else>
          <div v-if="st.words?.length" class="word-cloud">
            <span v-for="w in st.words" :key="w" class="badge word-tag">{{ w }}</span>
          </div>
          <div v-else class="empty"><div class="empty-title">词库为空</div></div>
          <div class="form-actions">
            <button class="btn sm" :disabled="st.reloading || st.wordsDenied" @click="doReload(false)">
              {{ st.reloading ? '重载中…' : '重载词库' }}
            </button>
            <button class="btn sm" :disabled="st.reloading || st.wordsDenied" @click="doReload(true)">
              重载 + 全量重判定（E.7）
            </button>
            <span v-if="wordsMsg" class="save-msg">{{ wordsMsg }}</span>
          </div>
          <div class="hint">词库维护：config/secret-words/ 目录（base.txt 必载 + industry-*.txt 行业包），改文件后点此重载生效</div>
        </template>
      </div>
    </div>

    <!-- ══ 联邦节点 ══ -->
    <div v-if="tab === 'team'">
      <div class="panel">
        <div class="panel-title">团队成员（{{ members.length }}）</div>
        <table v-if="members.length" class="table">
          <thead><tr><th>节点</th><th>地址</th><th>状态</th><th>配对时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="m in members" :key="m.member_id || m.id || m.hub_id">
              <td>{{ m.hub_name || m.name || m.hub_id || '—' }}</td>
              <td data-mono>{{ m.host || m.address || m.url || '—' }}</td>
              <td>
                <span class="dot" :class="(m.status === 'active' || m.status === 'paired') ? 'dot-ok' : 'dot-warn'"></span>
                {{ m.status || '—' }}
              </td>
              <td data-mono>{{ fmtTime(m.paired_at || m.created_at) }}</td>
              <td><button class="btn sm ghost" @click="doRemove(m)">移除</button></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">
          <div class="empty-title">暂无联邦节点</div>
          <div class="empty-desc">用下方配对码与其他 Hub 配对；UDP 发现的设备在下方列表</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">配对</div>
        <div class="form-actions">
          <button class="btn sm" @click="doPairRequest">生成配对码</button>
          <span v-if="st.pairCode" class="pair-code mono">{{ st.pairCode }}</span>
          <input v-model="acceptCode" class="input mono pair-input" placeholder="输入对方 6 位码" maxlength="6" />
          <button class="btn sm" :disabled="!acceptCode" @click="doPairAccept">接受配对</button>
        </div>
        <div v-if="st.pairMsg" class="save-msg">{{ st.pairMsg }}</div>
      </div>

      <div class="panel">
        <div class="panel-title">UDP 发现（{{ peers.length }}）</div>
        <table v-if="peers.length" class="table">
          <thead><tr><th>Hub</th><th>地址</th><th>最近发现</th></tr></thead>
          <tbody>
            <tr v-for="(p, i) in peers" :key="p.hub_id || i">
              <td>{{ p.hub_id || p.name || '—' }}</td>
              <td data-mono>{{ p.host || p.address || p.ip || '—' }}{{ p.port ? ':' + p.port : '' }}</td>
              <td data-mono>{{ fmtTime(p.last_seen || p.seen_at) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty"><div class="empty-title">未发现设备</div>
          <div class="empty-desc">同一局域网的 Hub 会通过 UDP 广播自动出现</div></div>
      </div>
    </div>

    <!-- ══ Hub Agent 调试 ══ -->
    <div v-if="tab === 'hubagent'">
      <div class="panel">
        <div class="panel-title">LLM 配置（api_key 已脱敏）</div>
        <table v-if="st.haConfig" class="table">
          <tbody>
            <tr v-for="(v, k) in st.haConfig" :key="k">
              <td data-mono>{{ k }}</td>
              <td data-mono>{{ v === '' || v == null ? '—' : String(v) }}</td>
            </tr>
          </tbody>
        </table>
        <div class="form-actions">
          <button class="btn sm" :disabled="st.testing" @click="doTestHa">
            {{ st.testing ? '测试中…' : '测试 LLM 连通性' }}
          </button>
          <span v-if="st.haTest" class="save-msg">
            {{ st.haTest.ok || st.haTest.status === 'ok' ? '✓ 连通正常' : '✗ ' + (st.haTest.error || st.haTest.detail || '失败') }}
            {{ st.haTest.latency_ms ? `（${st.haTest.latency_ms}ms）` : '' }}
          </span>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">调试对话（聊天页降级入口 · 仅调试用）</div>
        <div class="chat-log">
          <div v-for="(m, i) in st.chatLog" :key="i" class="chat-msg" :class="m.role">
            <span class="chat-role">{{ { user: '我', assistant: 'Hub Agent', error: '错误' }[m.role] }}</span>
            <span class="chat-content">{{ m.content }}</span>
          </div>
          <div v-if="!st.chatLog.length" class="empty" style="padding: 20px">
            <div class="empty-desc">对 Hub Agent（LLM 披露审计引擎）发消息调试；配置好 provider 后可用</div>
          </div>
        </div>
        <div class="chat-input-row">
          <input v-model="chatDraft" class="input" placeholder="输入调试消息，回车发送"
                 @keyup.enter="doChat" :disabled="st.chatSending" />
          <button class="btn primary" :disabled="!chatDraft || st.chatSending" @click="doChat">
            {{ st.chatSending ? '发送中…' : '发送' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useSettingsStore, useUiStore } from '../stores'

const st = useSettingsStore()
const ui = useUiStore()
const tab = ref('server')
const tabs = [
  { key: 'server', label: '服务器' },
  { key: 'words', label: '机密词库' },
  { key: 'team', label: '联邦节点' },
  { key: 'hubagent', label: 'Hub Agent 调试' },
]

// 词库/联邦端点走 manager 角色门：用当前登录身份（localStorage 记录的 agent_id）
const agentId = localStorage.getItem('hub_agent_id') || 'admin'

const serverMsg = ref('')
const wordsMsg = ref('')
const acceptCode = ref('')
const chatDraft = ref('')

const members = computed(() => Array.isArray(st.members) ? st.members : [])
const peers = computed(() => Array.isArray(st.peers) ? st.peers : [])
const sourceLabel = computed(() =>
  ({ dir: '目录 config/secret-words/', file: '单文件', default: '内置默认' }[st.wordsSource] || st.wordsSource || '—'))

onMounted(() => {
  st.refreshServer()
  st.refreshWords(agentId)
  st.refreshTeam(agentId)
  st.refreshHubAgent()
})

function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? String(iso).slice(5, 16) : d.toLocaleString()
}

async function toggleLan() {
  const r = await st.saveServer({ lan_enabled: !st.server.lan_enabled })
  serverMsg.value = r.restart_required ? '已写入 config.yaml，重启 Hub 生效' : '已保存'
}
async function toggleUiNew() {
  const r = await st.saveServer({ lan_enabled: st.server.lan_enabled, ui_new: !st.server.ui_new })
  serverMsg.value = '灰度开关已切换（刷新 / 生效）'
}
async function doReload(reclassify) {
  try {
    const r = await st.reloadWords(agentId, reclassify)
    wordsMsg.value = `重载完成：${r.words} 词` + (reclassify ? `，重判定 ${r.reclassified?.changed ?? '已触发'}` : '')
    await st.refreshWords(agentId)
  } catch (e) { wordsMsg.value = '重载失败：' + (e.message || e) }
}
async function doPairRequest() {
  try { await st.pairRequest(agentId); st.pairMsg = '配对码 5 分钟内有效，告知对方 Hub 输入' }
  catch (e) { st.pairMsg = '失败：' + (e.message || e) }
}
async function doPairAccept() {
  try { await st.pairAccept(agentId, acceptCode.value); st.pairMsg = '配对成功'; acceptCode.value = '' }
  catch (e) { st.pairMsg = '配对失败：' + (e.message || e) }
}
async function doRemove(m) {
  if (!confirm(`移除节点 ${m.hub_name || m.hub_id || ''}？`)) return
  await st.removeMember(agentId, m.member_id || m.id)
}
async function doTestHa() { await st.testHubAgent() }
async function doChat() {
  const msg = chatDraft.value
  chatDraft.value = ''
  await st.chat(msg)
}
</script>

<style scoped>
.tab-row { display: flex; gap: 8px; margin-bottom: 14px; }
.form-actions { margin-top: 12px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.save-msg { font-size: var(--fs-12); color: var(--text-2); }
.hint { margin-top: 10px; font-size: var(--fs-11); color: var(--text-2); }
.word-cloud { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.word-tag { font-size: var(--fs-12); }
.pair-code { font-size: 22px; letter-spacing: 4px; font-weight: 700; color: var(--accent); }
.pair-input { width: 140px; }
.chat-log { max-height: 320px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; }
.chat-msg { display: flex; gap: 8px; font-size: var(--fs-13); }
.chat-msg .chat-role { flex: 0 0 72px; color: var(--text-2); font-size: var(--fs-11); padding-top: 2px; }
.chat-msg.user .chat-role { color: var(--accent); }
.chat-msg.error .chat-content { color: var(--err); }
.chat-input-row { display: flex; gap: 8px; }
.chat-input-row .input { flex: 1; }
</style>
