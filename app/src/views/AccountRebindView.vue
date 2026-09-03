<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { countryLabel } from '@/services/countries'
import type { ProxyCountrySummary, ProxyGroupSummary } from '@/types'

const tasks = ref<any[]>([])
const pools = ref<any>({ availableRegistrationEmails: 0, availableRebindEmails: 0, availableStandardEmails: 0, availableAliasEmails: 0, reservedRebindEmails: 0, concurrency: 2, success: [] })
const loading = ref(false)
type RebindProxyCountry = ProxyCountrySummary & { rebindAvailable?: number }
type RebindProxyGroup = ProxyGroupSummary & { rebindAvailable?: number }
const proxyCountries = ref<RebindProxyCountry[]>([])
const proxyGroups = ref<RebindProxyGroup[]>([])
const proxyMode = ref<'pool' | 'local' | 'custom'>('pool')
const proxyCountry = ref('')
const proxyGroup = ref('')
const customProxy = ref('')
const emailSource = ref<'standard' | 'mailcom_alias'>('standard')
const logs = ref<any[]>([])
const logBox = ref<HTMLElement>()
const concurrency = ref(2)
const concurrencyLoaded = ref(false)
const retryingFailed = ref(false)
const startingAll = ref(false)
const flatRows = computed(() => tasks.value.flatMap((task) => (task.items || []).map((item: any) => ({ ...item, taskId: task.taskId, taskStatus: task.status, taskProgress: task.progress, proxy: item.proxy || task.proxy }))))
const pendingItemCount = computed(() => flatRows.value.filter((item: any) => item.status === 'pending').length)
const countryOptions = computed(() => proxyCountries.value
  .filter((item) => item.country !== 'ZZ' && (item.rebindAvailable || 0) > 0)
  .map((item) => ({ value: item.country, label: `${countryLabel(item.country)} · ${item.rebindAvailable || 0} 条可用于换绑` })))
const groupOptions = computed(() => proxyGroups.value
  .filter((item) => item.country === proxyCountry.value && (item.rebindAvailable || 0) > 0)
  .map((item) => ({ value: item.group, label: `${item.group} · ${item.rebindAvailable || 0} 条已通过 ChatGPT 验证` })))
const proxySelectionReady = computed(() => {
  if (proxyMode.value === 'pool') return Boolean(proxyCountry.value && proxyGroup.value)
  if (proxyMode.value === 'custom') return /^https?:\/\//i.test(customProxy.value.trim())
  return true
})
let timer: ReturnType<typeof setInterval> | undefined

watch(proxyCountry, () => {
  if (!groupOptions.value.some((item) => item.value === proxyGroup.value)) {
    proxyGroup.value = groupOptions.value[0]?.value || ''
  }
})

watch(logs, async () => {
  await nextTick()
  if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
})

function proxyPayload() {
  const mailbox = { emailSource: emailSource.value }
  if (proxyMode.value === 'pool') return { ...mailbox, proxyMode: 'pool', country: proxyCountry.value, group: proxyGroup.value }
  if (proxyMode.value === 'custom') return { ...mailbox, proxyMode: 'custom', proxy: customProxy.value }
  return { ...mailbox, proxyMode: 'local', proxyId: 'local7890' }
}

const statusLabels: Record<string, string> = {
  pending: '等待启动', queued: '排队中', running: '执行中', success: '换绑成功',
  failed: '换绑失败', released: '已释放', cancelled: '已取消', partial_success: '部分成功',
}
const levelLabels: Record<string, string> = { INFO: '信息', WARN: '警告', ERROR: '错误' }
function statusLabel(value: string) { return statusLabels[value] || value || '未知' }
function levelLabel(value: string) { return levelLabels[value] || value || '信息' }
function proxyDetail(row: any) {
  const scope = [row.proxyCountry, row.proxyGroup].filter(Boolean).join(' / ')
  return [row.proxy || '尚未领取', scope].filter(Boolean).join(' · ')
}

function responseMessage(body: any, fallback: string) {
  return typeof body?.detail === 'string' ? body.detail : body?.detail?.message || fallback
}

async function load(manual = false) {
  loading.value = true
  try {
    const [taskResponse, poolResponse, proxyResponse, logResponse] = await Promise.all([fetch('/api/account-rebind/tasks'), fetch('/api/account-rebind/pools'), fetch('/api/account-rebind/proxies'), fetch('/api/account-rebind/logs')])
    if (taskResponse.ok) tasks.value = (await taskResponse.json()).items || []
    if (poolResponse.ok) {
      pools.value = await poolResponse.json()
      if (!concurrencyLoaded.value) {
        concurrency.value = Number(pools.value.concurrency || 2)
        concurrencyLoaded.value = true
      }
    }
    if (proxyResponse.ok) {
      const payload = await proxyResponse.json()
      proxyCountries.value = payload.countries || []
      proxyGroups.value = payload.groups || []
      if (!countryOptions.value.some((item) => item.value === proxyCountry.value)) {
        proxyCountry.value = countryOptions.value[0]?.value || ''
      }
      if (!groupOptions.value.some((item) => item.value === proxyGroup.value)) {
        proxyGroup.value = groupOptions.value[0]?.value || ''
      }
    }
    if (logResponse.ok) logs.value = (await logResponse.json()).items || []
    if (manual) ElMessage.success('数据已刷新')
  } finally { loading.value = false }
}
async function start(taskId: string) {
  const response = await fetch(`/api/account-rebind/tasks/${taskId}/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(proxyPayload()) })
  const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
  if (!response.ok) return ElMessage.error(responseMessage(body, '启动换绑失败'))
  ElMessage.success('换绑任务已启动'); await load()
}
async function saveConcurrency() {
  const response = await fetch('/api/account-rebind/concurrency', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ concurrency: concurrency.value }) })
  const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
  if (!response.ok) { ElMessage.error(responseMessage(body, '保存全局并发失败')); return false }
  concurrency.value = Number(body.concurrency || concurrency.value)
  ElMessage.success(`全局换绑并发已设置为 ${concurrency.value}`)
  return true
}
async function startAll() {
  if (!pendingItemCount.value) return ElMessage.info('当前没有等待启动的账号')
  startingAll.value = true
  try {
    if (!await saveConcurrency()) return
    const response = await fetch('/api/account-rebind/tasks/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(proxyPayload()) })
    const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
    if (!response.ok) return ElMessage.error(responseMessage(body, '批量启动失败'))
    if (!body.requested) ElMessage.info('当前没有等待启动的任务')
    else if (body.failed) ElMessage.warning(`已启动 ${body.started}/${body.requested} 个任务，${body.failed} 个启动失败`)
    else ElMessage.success(`已启动 ${body.started}/${body.requested} 个任务，并发 ${body.concurrency}`)
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '批量启动失败')
  } finally {
    startingAll.value = false
  }
}
async function retryAllFailed() {
  if (!await saveConcurrency()) return
  retryingFailed.value = true
  try {
    const response = await fetch('/api/account-rebind/tasks/retry-failed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...proxyPayload(), retryFailedOnly: true }),
    })
    const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
    if (!response.ok) return ElMessage.error(responseMessage(body, '重试失败任务失败'))
    if (!body.requested) return ElMessage.info('当前没有可重试的失败任务')
    if (body.failed) ElMessage.warning(`已启动 ${body.started}/${body.requested} 个失败任务，${body.failed} 个启动失败`)
    else ElMessage.success(`已一键重试 ${body.started} 个失败任务，继续复用原预留邮箱`)
    await load()
  } finally { retryingFailed.value = false }
}
async function cancelAll() {
  const response = await fetch('/api/account-rebind/tasks/cancel-all', { method: 'POST' })
  const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
  if (!response.ok) return ElMessage.error(body.detail || '取消任务失败')
  ElMessage.success(`已取消 ${body.cancelled || 0} 个任务，移除 ${body.removed || 0} 个，释放邮箱 ${body.released || 0} 个${body.stopping ? `；${body.stopping} 个等待当前账号结束` : ''}`)
  await load()
}
async function release(taskId: string) {
  const response = await fetch(`/api/account-rebind/tasks/${taskId}/release`, { method: 'POST' })
  const raw = await response.text(); let body: any = {}; try { body = raw ? JSON.parse(raw) : {} } catch { body = { detail: raw } }
  if (!response.ok) return ElMessage.error(body.detail || '释放任务失败')
  await load()
  ElMessage.success(body.removed ? `任务已移除，释放邮箱 ${body.released || 0} 个` : '取消请求已提交，当前账号结束后会自动移除')
}
onMounted(() => { void load(); timer = setInterval(() => void load(), 3000) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="rebind-console">
    <header class="hero">
      <div class="hero-brand"><div class="console-logo">换</div><div><div class="eyebrow">账号自动换绑中心</div><h2>账号邮箱换绑</h2><p>统一使用项目账号池、邮箱池和代理池</p></div></div>
      <div class="hero-actions"><span class="online-pill"><i />服务在线</span><button class="ghost-btn" :disabled="loading" @click="load(true)">刷新数据</button></div>
    </header>
    <section class="stat-grid">
      <article><span>任务总数</span><strong>{{ tasks.length }}</strong><small>当前任务队列</small></article>
      <article><span>等待启动</span><strong>{{ pendingItemCount }}</strong><small>尚未开始执行的账号</small></article>
      <article><span>正在执行</span><strong>{{ tasks.filter(t => t.status === 'running').length }}</strong><small>受全局并发限制</small></article>
      <article><span>换绑已写回</span><strong>{{ pools.success?.length || 0 }}</strong><small>含等待刷新 AT</small></article>
      <article><span>可用邮箱</span><strong>{{ pools.availableRebindEmails || 0 }}</strong><small>项目邮箱池</small></article>
    </section>
    <div class="console-layout"><main>
      <section class="console-panel">
        <div class="panel-head"><div><h3>代理、邮箱与并发设置</h3><p>实际执行代理会先验证；连接失败会在所选国家和分组内自动轮换</p></div><button class="primary-btn" @click="$router.push('/accounts')">前往账号池选择账号</button></div>
        <div class="proxy-toolbar">
          <el-select v-model="proxyMode" placeholder="代理来源"><el-option label="项目代理池（推荐）" value="pool" /><el-option label="本地代理 127.0.0.1:7890（调试）" value="local" /><el-option label="自定义代理" value="custom" /></el-select>
          <el-select v-if="proxyMode === 'pool'" v-model="proxyCountry" filterable placeholder="选择代理国家"><el-option v-for="item in countryOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select>
          <el-select v-if="proxyMode === 'pool'" v-model="proxyGroup" filterable placeholder="选择代理分组" :disabled="!proxyCountry"><el-option v-for="item in groupOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select>
          <el-input v-if="proxyMode === 'custom'" v-model="customProxy" placeholder="请输入完整代理地址，例如 http://账号:密码@主机:端口" />
          <el-select v-model="emailSource" placeholder="换绑邮箱类型"><el-option :label="`普通邮箱（可用 ${pools.availableStandardEmails || 0}）`" value="standard" /><el-option :label="`分裂邮箱（可用 ${pools.availableAliasEmails || 0}）`" value="mailcom_alias" /></el-select>
          <div class="concurrency-control"><span>全局并发数</span><el-input-number v-model="concurrency" :min="1" :max="20" /></div>
          <button class="primary-btn" :disabled="startingAll || !pendingItemCount || !proxySelectionReady" @click="startAll">{{ startingAll ? '正在启动…' : `按当前设置开始换绑（${pendingItemCount}）` }}</button>
          <button class="retry-btn" :disabled="retryingFailed || !tasks.some(t => (t.items || []).some((item: any) => item.status === 'failed')) || !proxySelectionReady" @click="retryAllFailed">{{ retryingFailed ? '正在重试…' : '一键重试失败任务' }}</button>
          <button class="danger-btn" @click="cancelAll">取消所有未执行任务</button>
        </div>
        <p v-if="proxyMode === 'pool' && !countryOptions.length" class="proxy-empty">项目代理池中没有已启用的国家分组，请先在配置页面导入并启用代理。</p>
      </section>
      <section class="console-panel">
        <div class="panel-head"><div><h3>账号执行进度</h3><p>每个账号分别显示当前步骤、百分比、实际代理和占用邮箱</p></div></div>
        <div class="table-wrap"><table><thead><tr><th>任务与账号</th><th class="progress-column">当前进度</th><th>实际执行代理</th><th>换绑邮箱</th><th>状态与操作</th></tr></thead><tbody>
          <tr v-for="row in flatRows" :key="`${row.taskId}-${row.accountId}`">
            <td><b>{{ row.email }}</b><small>任务 {{ row.taskId.slice(0, 12) }}</small></td>
            <td class="progress-cell"><div class="stage-line"><b>{{ row.stepLabel || '等待启动' }}</b><span>{{ row.progress ?? row.taskProgress ?? 0 }}%</span></div><el-progress :percentage="row.progress ?? row.taskProgress ?? 0" :show-text="false" /><small>{{ row.message || '等待任务开始' }}</small></td>
            <td class="muted"><b>{{ proxyDetail(row) }}</b><small v-if="row.proxyAttempt">第 {{ row.proxyAttempt }} 条代理验证成功</small></td>
            <td><span class="badge" :class="row.status">{{ row.mailbox || '尚未分配' }}</span><small>{{ row.mailboxSourceLabel || (row.emailSource === 'mailcom_alias' ? '分裂邮箱' : '普通邮箱') }}</small></td>
            <td><span class="badge" :class="row.status">{{ statusLabel(row.status) }}</span><button class="danger-btn row-action" :disabled="row.taskStatus === 'success'" @click="release(row.taskId)">取消并移除</button></td>
          </tr>
          <tr v-if="!flatRows.length"><td colspan="5" class="empty">暂无换绑任务，请先前往账号池选择账号并提交换绑队列</td></tr>
        </tbody></table></div>
      </section>
      <section class="console-panel"><div class="panel-head"><div><h3>换绑已写回账号</h3><p>验证码确认后立即更新邮箱；新登录失败时保留“AT 待刷新”状态</p></div></div><div class="table-wrap"><table><thead><tr><th>原邮箱</th><th>当前邮箱</th><th>访问令牌</th><th>使用代理</th></tr></thead><tbody><tr v-for="item in pools.success" :key="item.id"><td>{{ item.previousEmail || '-' }}</td><td>{{ item.email }}</td><td><span class="badge" :class="item.rebindStatus === 'success' ? 'success' : 'failed'">{{ item.rebindStatus === 'email_changed_token_pending' ? '邮箱已换 · AT 待刷新' : item.accessTokenConfigured ? '已更新' : '缺失' }}</span></td><td>{{ item.rebindProxy || '-' }}</td></tr><tr v-if="!pools.success?.length"><td colspan="4" class="empty">暂无已写回的换绑账号</td></tr></tbody></table></div></section>
    </main><aside>
      <section class="console-panel side-panel"><div class="panel-head"><div><h3>项目邮箱池</h3><p>换绑邮箱直接从现有邮箱池领取</p></div></div><div class="pool-row"><span>普通邮箱可用</span><b>{{ pools.availableStandardEmails || 0 }}</b></div><div class="pool-row"><span>分裂邮箱可用</span><b>{{ pools.availableAliasEmails || 0 }}</b></div><div class="pool-row"><span>换绑已占用</span><b class="amber">{{ pools.reservedRebindEmails || 0 }}</b></div><div class="pool-row"><span>当前全局并发</span><b>{{ pools.concurrency || concurrency }}</b></div></section>
      <section class="console-panel side-panel"><div class="panel-head"><div><h3>换绑运行日志</h3><p>固定窗口显示，每 3 秒刷新并自动滚动到最新日志</p></div></div><div ref="logBox" class="log-box"><p v-for="(log,index) in logs" :key="index"><span class="log-time">{{ (log.time || '').replace('T',' ').slice(0,19) }}</span> <b :class="log.level === 'ERROR' ? 'log-error' : log.level === 'WARN' ? 'log-warn' : 'log-info'">[{{ levelLabel(log.level) }}]</b> <span v-if="log.stepLabel" class="log-step">[{{ log.stepLabel }}<template v-if="log.percent !== undefined"> · {{ log.percent }}%</template>]</span> {{ log.message }}</p><p v-if="!logs.length">暂无运行日志</p></div></section>
    </aside></div>
  </div>
</template>

<style scoped>
.proxy-toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.proxy-toolbar .el-select{width:260px}.proxy-toolbar .el-input{max-width:420px}.proxy-empty{margin:10px 0 0;color:#ffd278;font-size:12px}.concurrency-control{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}.log-time{color:#6684a5}.log-info{color:#74b2ff}.log-error{color:#ff8799}.log-warn{color:#ffd278}.log-step{color:#a9c7e8}.progress-column{min-width:270px}.progress-cell{min-width:270px}.stage-line{display:flex;justify-content:space-between;gap:10px;margin-bottom:7px}.stage-line b{color:#dceaff}.stage-line span{color:#78aaff}.progress-cell small{margin-top:7px;white-space:normal;line-height:1.45}.row-action{display:block;margin-top:8px;padding:5px 8px}.muted b{display:block;color:#a9bdd4;font-weight:500}
.rebind-console{--p:#111d2d;--p2:#16263a;--line:#263a52;--muted:#8fa5bf;min-height:calc(100vh - 150px);padding:24px;border-radius:20px;background:radial-gradient(900px 450px at 78% -12%,#21426b 0,#09111f 62%);color:#edf4ff}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:22px}.hero-brand{display:flex;gap:13px;align-items:center}.console-logo{display:grid;width:44px;height:44px;place-items:center;border-radius:13px;background:linear-gradient(135deg,#69a7ff,#7267ff);box-shadow:0 8px 25px #4c7bff55;font-size:18px;font-weight:800}.eyebrow{color:#78aaff;font-size:10px;font-weight:800;letter-spacing:.14em}.hero h2{margin:2px 0;font-size:25px}.hero p,.panel-head p{margin:0;color:var(--muted);font-size:12px}.hero-actions{display:flex;gap:9px;align-items:center}.online-pill{padding:7px 11px;border:1px solid var(--line);border-radius:999px;color:var(--muted);background:#0d1828}.online-pill i,.green-dot{display:inline-block;width:7px;height:7px;margin-right:6px;border-radius:50%;background:#35d399;box-shadow:0 0 12px #35d399}.ghost-btn,.primary-btn,.danger-btn{padding:8px 12px;border-radius:9px;border:1px solid var(--line);color:#c6d7ec;background:#15243a;cursor:pointer}.primary-btn{border-color:transparent;color:white;background:linear-gradient(135deg,#5d9eff,#477de9)}.danger-btn{color:#ffb2bd;background:#4b1f31;border-color:#713145}.stat-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:17px}.stat-grid article,.console-panel{border:1px solid var(--line);border-radius:15px;background:linear-gradient(150deg,#15263a,#101b2b);box-shadow:0 18px 45px #0005}.stat-grid article{padding:15px 16px}.stat-grid span{color:var(--muted);font-size:12px}.stat-grid strong{display:block;margin-top:4px;font-size:27px}.stat-grid small{color:#7189a6}.console-layout{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(330px,.7fr);gap:17px}.console-panel{padding:18px;margin-bottom:17px}.panel-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.panel-head h3{margin:0 0 3px;font-size:16px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:11px}table{width:100%;border-collapse:collapse;min-width:680px}th,td{padding:11px 12px;text-align:left;border-bottom:1px solid #20334a}th{color:#86a0bd;background:#0d1929;font-size:11px;text-transform:uppercase}td{font-size:12px}td small{display:block;color:var(--muted);margin-top:3px}.muted{color:var(--muted)}.mail-row{display:flex;justify-content:space-between;gap:8px;margin:4px 0}.badge{display:inline-flex;padding:4px 8px;border-radius:999px;background:#3a2e12;color:#ffd27a;font-size:10px;font-weight:700}.badge.success{background:#12392f;color:#76edbf}.badge.failed{background:#431e2b;color:#ff9baa}.badge.released{background:#172c4b;color:#8dbbff}.empty{text-align:center;color:var(--muted);padding:28px}.pool-row{display:flex;justify-content:space-between;padding:13px 0;border-bottom:1px solid #20334a}.pool-row b{color:#76edbf;font-size:18px}.pool-row b.amber{color:#ffd27a}.hint{color:var(--muted);font-size:12px;line-height:1.65}.log-box{height:360px;overflow-y:auto;overscroll-behavior:contain;padding:12px;border:1px solid var(--line);border-radius:11px;background:#0a121e;color:var(--muted);font:12px/1.7 ui-monospace,Consolas,monospace}.log-box p{margin:0 0 6px;overflow-wrap:anywhere}@media(max-width:1100px){.stat-grid{grid-template-columns:repeat(3,1fr)}.console-layout{grid-template-columns:1fr}}@media(max-width:650px){.rebind-console{padding:14px}.hero{display:block}.hero-actions{margin-top:12px}.stat-grid{grid-template-columns:repeat(2,1fr)}}
.retry-btn{padding:8px 12px;border:1px solid #26745c;border-radius:9px;color:#d8fff1;background:#174638;cursor:pointer}.retry-btn:disabled{opacity:.5;cursor:not-allowed}
</style>
