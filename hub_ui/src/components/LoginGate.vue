<template>
  <div class="login-gate">
    <div class="login-card">
      <div class="login-title">星枢 Hub 控制台</div>
      <div class="login-sub">输入 hub_token 登录（占位——S1-SMB 账号体系就绪后切换，接口层不变）</div>
      <input
        v-model="token"
        class="login-input"
        type="password"
        placeholder="hub_token / api_key"
        @keydown.enter="submit"
      />
      <button class="btn primary login-btn" @click="submit">登 录</button>
      <div v-if="err" class="login-err">{{ err }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { setToken, api } from '../api'
import { useUiStore } from '../stores'

const token = ref('')
const err = ref('')
const ui = useUiStore()

async function submit() {
  err.value = ''
  if (!token.value.trim()) { err.value = '请输入 token'; return }
  setToken(token.value.trim())
  try {
    await api('/api/v1/dashboard')
    ui.loggedOut = false
  } catch (e) {
    err.value = e.status === 401 ? 'token 无效' : `连接失败：${e.message}`
    if (e.status !== 401) ui.loggedOut = false  // 网络问题不拦门
  }
}
</script>

<style scoped>
.login-gate {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.login-card {
  width: min(380px, 90vw);
  background: var(--bg-1);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-raised);
  padding: 32px 28px;
  text-align: center;
}
.login-title { font-size: var(--fs-20); font-weight: 600; margin-bottom: 8px; }
.login-sub { font-size: var(--fs-12); color: var(--text-2); margin-bottom: 20px; }
.login-input {
  width: 100%;
  border: none;
  outline: none;
  font-family: var(--font-mono);
  font-size: var(--fs-13);
  color: var(--text-0);
  background: var(--bg-1);
  border-radius: var(--radius-ctrl);
  box-shadow: var(--shadow-inset);
  padding: 10px 14px;
  margin-bottom: 16px;
}
.login-btn { width: 100%; }
.login-err { margin-top: 12px; font-size: var(--fs-12); color: var(--danger); }
</style>
