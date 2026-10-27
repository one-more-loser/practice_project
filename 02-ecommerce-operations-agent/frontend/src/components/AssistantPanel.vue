<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api, errorMessage } from '../api'
import type { AssistantResult } from '../types'
const props = defineProps<{ snapshotId: string; planId?: string }>()
const question = ref(''), result = ref<AssistantResult | null>(null), busy = ref(false), error = ref(''), sku = ref(''), channel = ref('')
let controller: AbortController | undefined
watch(() => [props.snapshotId, props.planId], () => {
  controller?.abort(); controller = undefined
  result.value = null; error.value = ''; busy.value = false
}, { flush: 'sync' })
onBeforeUnmount(() => controller?.abort())
async function ask(value?: string) {
  if (value) question.value = value
  if (!question.value.trim() || !props.snapshotId || busy.value) return
  controller = new AbortController(); const active = controller
  busy.value = true; error.value = ''; result.value = null
  try { const data = await api.request<AssistantResult>('/assistant', { method: 'POST', body: { question: question.value, snapshot_id: props.snapshotId, ...(sku.value ? {sku_id: sku.value} : {}), ...(channel.value ? {channel: channel.value} : {}), ...(props.planId ? { plan_id: props.planId } : {}) }, signal: active.signal }); if (!active.signal.aborted) result.value = data }
  catch (e) { if (!active.signal.aborted) error.value = errorMessage(e) }
  finally { if (active === controller) busy.value = false }
}
function cancel() { controller?.abort(); busy.value = false; error.value = '已取消问数，可修改问题后重试。' }
</script>
<template>
  <aside class="assistant-panel" aria-labelledby="assistant-title">
    <div class="section-head"><h2 id="assistant-title">问经营 Agent</h2><span class="badge">规则工具</span></div>
    <form @submit.prevent="ask()"><label class="sr-only" for="question">经营问题</label><textarea id="question" v-model="question" rows="4" maxlength="2000" placeholder="这批数据的净实付、退款和库存情况如何？" :disabled="busy"></textarea><label>SKU<input v-model="sku" placeholder="可选SKU ID" :disabled="busy"></label><label>渠道<input v-model="channel" placeholder="可选渠道" :disabled="busy"></label><div class="row-between"><span class="muted">基于当前快照</span><button v-if="busy" type="button" @click="cancel">取消</button><button v-else class="primary" :disabled="!question.trim() || !snapshotId" type="submit">开始分析 <span aria-hidden="true">↗</span></button></div></form>
    <div class="suggestions"><button type="button" :disabled="busy || !snapshotId" @click="ask('当前快照的支付、退款和净实付如何？')">查看经营概况</button><button type="button" :disabled="busy || !snapshotId" @click="ask('当前库存和成本还有哪些缺口？')">检查数据缺口</button></div>
    <p v-if="busy" role="status" class="feedback"><span class="spinner" aria-hidden="true"></span>正在读取获准指标…</p><p v-if="error" role="alert" class="feedback error">{{ error }}</p>
    <div v-if="result" class="assistant-answer" aria-live="polite"><p class="answer-text">{{ result.answer }}</p><dl class="evidence-list"><template v-for="(evidence, i) in result.evidence" :key="i"><dt>{{ evidence.title }}</dt><dd>{{ typeof evidence.value === 'object' ? JSON.stringify(evidence.value) : evidence.value }}</dd></template></dl><ul v-if="result.missing.length" class="issue-list"><li v-for="missing in result.missing" :key="missing">{{ missing }}</li></ul><small class="muted">确定性工具结果 · 不调用外部模型</small></div>
  </aside>
</template>
