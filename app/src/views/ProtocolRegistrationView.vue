<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { ElMessage } from 'element-plus'
const count = ref(1); const workers = ref(1); const loading = ref(false); const job = ref<any>(null)
let timer: ReturnType<typeof setInterval> | undefined
async function start() { loading.value = true; try { const r = await fetch('/api/protocol-registration/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ count: count.value, workers: workers.value }) }); if (!r.ok) throw new Error((await r.json()).detail || '协议注册启动失败'); job.value = await r.json(); if (timer) clearInterval(timer); timer = setInterval(refresh, 2000); ElMessage.success('已启动，将复用现有邮箱池和代理池') } catch (e) { ElMessage.error(e instanceof Error ? e.message : '协议注册启动失败') } finally { loading.value = false } }
async function refresh() { if (!job.value?.jobId) return; const r = await fetch(`/api/protocol-registration/${job.value.jobId}`); if (r.ok) { job.value = await r.json(); if (['completed', 'failed'].includes(job.value.status) && timer) { clearInterval(timer); timer = undefined } } }
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>
<template><section class="page-shell"><div class="page-heading"><div><h2>协议注册</h2><p>复用现有邮箱池、代理池，成功账号自动进入账号池。</p></div></div><div class="panel" style="max-width:720px"><el-form label-width="110px"><el-form-item label="注册数量"><el-input-number v-model="count" :min="1" :max="100" /></el-form-item><el-form-item label="并发数"><el-input-number v-model="workers" :min="1" :max="10" /></el-form-item><el-button type="primary" :loading="loading" @click="start">启动协议注册</el-button></el-form></div><div v-if="job" class="panel" style="max-width:720px;margin-top:16px"><p>任务：{{ job.jobId }}</p><p>状态：{{ job.status }}　导入账号：{{ job.importedAccounts ?? 0 }}</p><el-scrollbar height="260px"><pre>{{ (job.lines || []).join('\n') }}</pre></el-scrollbar></div></section></template>
