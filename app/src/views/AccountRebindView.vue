<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

const tasks = ref<any[]>([])
const pools = ref<any>({ availableRegistrationEmails: 0, availableRebindEmails: 0, reservedRebindEmails: 0, success: [] })
const loading = ref(false)
const proxies = ref<any[]>([])
const proxyId = ref('local7890')
const customProxy = ref('')
const logs = ref<any[]>([])
const concurrency = ref(2)
const flatRows = computed(() => tasks.value.flatMap((task) => (task.items || []).map((item: any) => ({ ...item, taskId: task.taskId, taskStatus: task.status, taskProgress: task.progress, proxy: task.proxy }))))
let timer: ReturnType<typeof setInterval> | undefined

async function load(manual = false) {
  loading.value = true
  try {
    const [taskResponse, poolResponse, proxyResponse, logResponse] = await Promise.all([fetch('/api/account-rebind/tasks'), fetch('/api/account-rebind/pools'), fetch('/api/account-rebind/proxies'), fetch('/api/account-rebind/logs')])
    if (taskResponse.ok) tasks.value = (await taskResponse.json()).items || []
    if (poolResponse.ok) pools.value = await poolResponse.json()
    if (proxyResponse.ok) proxies.value = (await proxyResponse.json()).items || []
    if (logResponse.ok) logs.value = (await logResponse.json()).items || []
    if (manual) ElMessage.success('数据已刷新')
  } finally { loading.value = false }
}
async function start(taskId: string) {
  const response = await fetch(`/api/account-rebind/tasks/${taskId}/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ proxyId: proxyId.value === 'custom' ? '' : proxyId.value, proxy: proxyId.value === 'custom' ? customProxy.value : '' }) })
  const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
  if (!response.ok) return ElMessage.error(body.detail || '鍚姩鎹㈢粦澶辫触')
  ElMessage.success('换绑任务已启动'); await load()
}
async function saveConcurrency() {
  const response = await fetch('/api/account-rebind/concurrency', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ concurrency: concurrency.value }) })
  if (!response.ok) return ElMessage.error('保存并发失败')
  ElMessage.success(`换绑并发已设置为 ${concurrency.value}`)
}
async function startAll() {
  await saveConcurrency()
  const response = await fetch('/api/account-rebind/tasks/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ proxyId: proxyId.value === 'custom' ? '' : proxyId.value, proxy: proxyId.value === 'custom' ? customProxy.value : '' }) })
  const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
  if (!response.ok) return ElMessage.error(body.detail || '批量启动失败')
  ElMessage.success(`已启动 ${body.started}/${body.requested} 个任务，并发 ${body.concurrency}`)
  await load()
}
async function cancelAll() {
  const response = await fetch('/api/account-rebind/tasks/cancel-all', { method: 'POST' })
  const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
  if (!response.ok) return ElMessage.error(body.detail || '取消任务失败')
  ElMessage.success(`已取消 ${body.cancelled || 0} 个任务，并释放占用邮箱`)
  await load()
}
async function release(taskId: string) {
  await fetch(`/api/account-rebind/tasks/${taskId}/release`, { method: 'POST' })
  await load()
  ElMessage.success('任务已释放，占用邮箱已归还')
}
onMounted(() => { void load(); timer = setInterval(() => void load(), 3000) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="rebind-console">
    <header class="hero"><div class="hero-brand"><div class="console-logo">AR</div><div><div class="eyebrow">LOCAL CONTROL PLANE</div><h2>Account Rebind Console</h2><p>Account pool / Mailbox pool / Rebind tasks</p></div></div><div class="hero-actions"><span class="online-pill"><i />Service online</span><button class="ghost-btn" :disabled="loading" @click="load(true)">Refresh</button></div></header>
    <section class="stat-grid"><article><span>Tasks</span><strong>{{ tasks.length }}</strong><small>Total queue</small></article><article><span>Pending</span><strong>{{ tasks.filter(t => t.status === 'pending').length }}</strong><small>Waiting to start</small></article><article><span>Running</span><strong>{{ tasks.filter(t => t.status === 'running').length }}</strong><small>Active tasks</small></article><article><span>Success</span><strong>{{ pools.success?.length || 0 }}</strong><small>Account pool updated</small></article><article><span>Mailboxes</span><strong>{{ pools.availableRebindEmails || 0 }}</strong><small>Available for rebind</small></article></section>
    <div class="console-layout"><main>
      <section class="console-panel"><div class="panel-head"><div><h3>代理、并发与启动</h3><p>选择代理并设置并发，然后统一开始所有待处理任务</p></div><button class="primary-btn" @click="$router.push('/accounts')">前往账号池选择</button></div><div class="proxy-toolbar"><el-select v-model="proxyId" filterable><el-option v-for="item in proxies" :key="item.id" :label="item.label" :value="item.id" /><el-option label="自定义代理" value="custom" /></el-select><el-input v-if="proxyId === 'custom'" v-model="customProxy" placeholder="http://user:pass@host:port" /><el-input-number v-model="concurrency" :min="1" :max="20" /><button class="primary-btn" :disabled="!tasks.some(t => t.status === 'pending')" @click="startAll">开始换绑</button><button class="danger-btn" @click="cancelAll">一键取消所有任务</button></div></section><section class="console-panel"><div class="panel-head"><div><h3>任务账号列表</h3><p>一行一个账号，独立显示进度和占用邮箱</p></div></div><div class="table-wrap"><table><thead><tr><th>任务/账号</th><th>进度</th><th>代理</th><th>占用邮箱</th><th>状态</th></tr></thead><tbody><tr v-for="row in flatRows" :key="`${row.taskId}-${row.accountId}`"><td><b>{{ row.email }}</b><small>{{ row.taskId.slice(0,12) }}</small></td><td><el-progress :percentage="row.progress || row.taskProgress || 0" /></td><td class="muted">{{ row.proxy || '尚未选择' }}</td><td><span class="badge" :class="row.status">{{ row.mailbox || '尚未分配' }}</span></td><td><span class="badge" :class="row.status">{{ row.status }}</span><button class="danger-btn" :disabled="['success','released'].includes(row.taskStatus)" @click="release(row.taskId)">取消任务</button></td></tr><tr v-if="!flatRows.length"><td colspan="5" class="empty">暂无任务，请前往账号池选择账号</td></tr></tbody></table></div></section>
      <section class="console-panel"><div class="panel-head"><div><h3>Successful accounts</h3><p>Original account, email and AT are updated after success</p></div></div><div class="table-wrap"><table><thead><tr><th>Current email</th><th>Rebound email</th><th>AT</th><th>Proxy</th></tr></thead><tbody><tr v-for="item in pools.success" :key="item.id"><td>{{ item.email }}</td><td>{{ item.reboundEmail || '-' }}</td><td><span class="badge success">{{ item.accessTokenConfigured ? 'Updated' : 'Missing' }}</span></td><td>{{ item.rebindProxy || '-' }}</td></tr><tr v-if="!pools.success?.length"><td colspan="4" class="empty">No successful accounts</td></tr></tbody></table></div></section>
    </main><aside><section class="console-panel side-panel"><div class="panel-head"><div><h3>邮箱隔离状态</h3><p>注册和换绑互不抢占</p></div></div><div class="pool-row"><span>注册可用邮箱</span><b>{{ pools.availableRegistrationEmails || 0 }}</b></div><div class="pool-row"><span>换绑可用邮箱</span><b>{{ pools.availableRebindEmails || 0 }}</b></div><div class="pool-row"><span>换绑占用邮箱</span><b class="amber">{{ pools.reservedRebindEmails || 0 }}</b></div></section><section class="console-panel side-panel"><div class="panel-head"><div><h3>换绑运行日志</h3><p>每 3 秒自动刷新</p></div></div><div class="log-box"><p v-for="(log,index) in logs.slice().reverse()" :key="index"><span class="log-time">{{ (log.time || '').replace('T',' ').slice(0,19) }}</span> <b :class="log.level === 'ERROR' ? 'log-error' : log.level === 'WARN' ? 'log-warn' : 'log-info'">[{{ log.level }}]</b> {{ log.message }}</p><p v-if="!logs.length">暂无运行日志</p></div></section></aside></div>
  </div>
</template>

<style scoped>
.proxy-toolbar{display:flex;gap:10px}.proxy-toolbar .el-select{width:360px}.proxy-toolbar .el-input{max-width:420px}.log-time{color:#6684a5}.log-info{color:#74b2ff}.log-error{color:#ff8799}.log-warn{color:#ffd278}
.rebind-console{--p:#111d2d;--p2:#16263a;--line:#263a52;--muted:#8fa5bf;min-height:calc(100vh - 150px);padding:24px;border-radius:20px;background:radial-gradient(900px 450px at 78% -12%,#21426b 0,#09111f 62%);color:#edf4ff}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:22px}.hero-brand{display:flex;gap:13px;align-items:center}.console-logo{display:grid;width:44px;height:44px;place-items:center;border-radius:13px;background:linear-gradient(135deg,#69a7ff,#7267ff);box-shadow:0 8px 25px #4c7bff55;font-size:18px;font-weight:800}.eyebrow{color:#78aaff;font-size:10px;font-weight:800;letter-spacing:.14em}.hero h2{margin:2px 0;font-size:25px}.hero p,.panel-head p{margin:0;color:var(--muted);font-size:12px}.hero-actions{display:flex;gap:9px;align-items:center}.online-pill{padding:7px 11px;border:1px solid var(--line);border-radius:999px;color:var(--muted);background:#0d1828}.online-pill i,.green-dot{display:inline-block;width:7px;height:7px;margin-right:6px;border-radius:50%;background:#35d399;box-shadow:0 0 12px #35d399}.ghost-btn,.primary-btn,.danger-btn{padding:8px 12px;border-radius:9px;border:1px solid var(--line);color:#c6d7ec;background:#15243a;cursor:pointer}.primary-btn{border-color:transparent;color:white;background:linear-gradient(135deg,#5d9eff,#477de9)}.danger-btn{color:#ffb2bd;background:#4b1f31;border-color:#713145}.stat-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:17px}.stat-grid article,.console-panel{border:1px solid var(--line);border-radius:15px;background:linear-gradient(150deg,#15263a,#101b2b);box-shadow:0 18px 45px #0005}.stat-grid article{padding:15px 16px}.stat-grid span{color:var(--muted);font-size:12px}.stat-grid strong{display:block;margin-top:4px;font-size:27px}.stat-grid small{color:#7189a6}.console-layout{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(330px,.7fr);gap:17px}.console-panel{padding:18px;margin-bottom:17px}.panel-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.panel-head h3{margin:0 0 3px;font-size:16px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:11px}table{width:100%;border-collapse:collapse;min-width:680px}th,td{padding:11px 12px;text-align:left;border-bottom:1px solid #20334a}th{color:#86a0bd;background:#0d1929;font-size:11px;text-transform:uppercase}td{font-size:12px}td small{display:block;color:var(--muted);margin-top:3px}.muted{color:var(--muted)}.mail-row{display:flex;justify-content:space-between;gap:8px;margin:4px 0}.badge{display:inline-flex;padding:4px 8px;border-radius:999px;background:#3a2e12;color:#ffd27a;font-size:10px;font-weight:700}.badge.success{background:#12392f;color:#76edbf}.badge.failed{background:#431e2b;color:#ff9baa}.badge.released{background:#172c4b;color:#8dbbff}.empty{text-align:center;color:var(--muted);padding:28px}.pool-row{display:flex;justify-content:space-between;padding:13px 0;border-bottom:1px solid #20334a}.pool-row b{color:#76edbf;font-size:18px}.pool-row b.amber{color:#ffd27a}.hint{color:var(--muted);font-size:12px;line-height:1.65}.log-box{min-height:180px;padding:12px;border:1px solid var(--line);border-radius:11px;background:#0a121e;color:var(--muted);font:12px/1.7 ui-monospace,Consolas,monospace}@media(max-width:1100px){.stat-grid{grid-template-columns:repeat(3,1fr)}.console-layout{grid-template-columns:1fr}}@media(max-width:650px){.rebind-console{padding:14px}.hero{display:block}.hero-actions{margin-top:12px}.stat-grid{grid-template-columns:repeat(2,1fr)}}
</style>
