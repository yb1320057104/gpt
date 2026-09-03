<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const mailComUrl = 'http://127.0.0.1:3211/?embedded=1'
const online = ref(false)
const checking = ref(false)
const frameKey = ref(0)
const storage = ref('')
const accountTotal = ref(0)
const aliasTotal = ref(0)
const frameUrl = computed(() => `${mailComUrl}&reload=${frameKey.value}`)
let timer: ReturnType<typeof setInterval> | undefined

async function checkHealth(reloadWhenRecovered = false) {
  if (checking.value) return
  checking.value = true
  const wasOnline = online.value
  try {
    const [healthResponse, statsResponse] = await Promise.all([
      fetch('http://127.0.0.1:3211/api/health', { cache: 'no-store' }),
      fetch('http://127.0.0.1:3211/api/stats', { cache: 'no-store' }),
    ])
    if (!healthResponse.ok || !statsResponse.ok) throw new Error('MailCom API unavailable')
    const health = await healthResponse.json()
    const stats = await statsResponse.json()
    storage.value = health.storage === 'mongodb-dpapi' ? 'MongoDB' : 'SQLite'
    accountTotal.value = Number(stats.total || 0)
    aliasTotal.value = Number(stats.aliases || 0)
    online.value = true
    if (reloadWhenRecovered && !wasOnline) frameKey.value += 1
  } catch {
    online.value = false
  } finally {
    checking.value = false
  }
}

function reloadFrame() {
  frameKey.value += 1
  void checkHealth()
}

onMounted(() => {
  void checkHealth(true)
  timer = setInterval(() => void checkHealth(true), 5000)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <section class="mailcom-page">
    <header class="mailcom-toolbar">
      <div>
        <p class="eyebrow">MAILCOM / LOCAL MANAGER</p>
        <h2>MailCom 邮箱管理</h2>
      </div>
      <div class="mailcom-actions">
        <span class="mailcom-status" :class="online ? 'online' : 'offline'"><i />{{ online ? `服务在线 · ${storage} · ${accountTotal} 邮箱 / ${aliasTotal} 别名` : '等待服务启动' }}</span>
        <button type="button" @click="reloadFrame">刷新页面</button>
        <a :href="mailComUrl" target="_blank" rel="noreferrer">新窗口打开</a>
      </div>
    </header>

    <div v-if="!online" class="mailcom-notice">
      MailCom 正在随主项目启动；服务就绪后此页面会自动加载。
    </div>
    <iframe
      :key="frameKey"
      class="mailcom-frame"
      :src="frameUrl"
      title="MailCom 邮箱管理后台"
      allow="clipboard-read; clipboard-write"
    />
  </section>
</template>

<style scoped>
.mailcom-page{display:flex;min-height:calc(100vh - 132px);flex-direction:column;overflow:hidden;border:1px solid var(--border-subtle);border-radius:18px;background:#07100f;box-shadow:0 20px 70px #0005}.mailcom-toolbar{display:flex;min-height:70px;align-items:center;justify-content:space-between;gap:18px;padding:13px 18px;border-bottom:1px solid var(--border-subtle);background:#0d1716}.mailcom-toolbar h2{margin:2px 0 0;font-size:18px}.mailcom-actions{display:flex;align-items:center;gap:8px}.mailcom-actions button,.mailcom-actions a{display:inline-flex;min-height:34px;align-items:center;padding:0 11px;border:1px solid #2a403b;border-radius:8px;color:#b7c8c3;background:#101d1b;font-size:11px;text-decoration:none;cursor:pointer}.mailcom-actions button:hover,.mailcom-actions a:hover{border-color:#4b8978;color:#62e6a9}.mailcom-status{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border-radius:999px;font-size:11px}.mailcom-status i{width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 9px currentColor}.mailcom-status.online{color:#62e6a9;background:#173d31}.mailcom-status.offline{color:#f0b85a;background:#3d3117}.mailcom-notice{padding:9px 18px;border-bottom:1px solid #4e3f22;color:#f0c77d;background:#2a2112;font-size:11px}.mailcom-frame{width:100%;min-height:680px;flex:1;border:0;background:#07100f}@media(max-width:760px){.mailcom-toolbar{align-items:flex-start;flex-direction:column}.mailcom-actions{width:100%;flex-wrap:wrap}.mailcom-page{min-height:calc(100vh - 112px)}}
</style>
