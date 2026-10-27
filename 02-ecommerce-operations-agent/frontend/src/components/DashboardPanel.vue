<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api, errorMessage } from '../api'
import type { Context, Dashboard } from '../types'
import { dateTime, fromLocal, localInput, sourceName } from '../format'
import MoneyValue from './MoneyValue.vue'
import AssistantPanel from './AssistantPanel.vue'
const props = defineProps<{ context: Context; snapshotId: string }>()
const data = ref<Dashboard | null>(null), error = ref(''), busy = ref(false), start = ref(''), end = ref(''), sku = ref(''), channel = ref('')
let controller: AbortController | undefined
async function load() {
  controller?.abort(); if (!props.snapshotId) { data.value = null; return }
  controller = new AbortController(); const active = controller
  error.value = ''; busy.value = true
  try { const query = new URLSearchParams({ snapshot_id: props.snapshotId }); if (sku.value) query.set('sku_id', sku.value); if (channel.value) query.set('channel', channel.value); if (start.value) query.set('start_at', fromLocal(start.value)); if (end.value) query.set('end_at', fromLocal(end.value)); const value = await api.request<Dashboard>(`/dashboard?${query}`, { signal: active.signal }); if (!active.signal.aborted) data.value = value }
  catch (e) { if (!active.signal.aborted) { data.value = null; error.value = errorMessage(e) } }
  finally { if (active === controller) busy.value = false }
}
watch(() => props.snapshotId, () => { data.value = null; start.value = ''; end.value = ''; load() }, { immediate: true })
onBeforeUnmount(() => controller?.abort())
</script>
<template>
  <section aria-label="经营概览">
    <div class="page-heading"><div><p class="eyebrow">经营概览</p><h1>每个决策，都有依据。</h1></div><button :disabled="busy || !snapshotId" @click="load"><span aria-hidden="true">↻</span> 刷新指标</button></div>
    <form class="date-filter" @submit.prevent="load"><label>统计开始<input v-model="start" type="datetime-local"></label><label>统计结束<input v-model="end" type="datetime-local"></label><label>SKU<input v-model="sku" placeholder="SKU ID"></label><label>渠道<input v-model="channel" placeholder="渠道或unknown"></label><button type="submit" :disabled="busy || !snapshotId">应用时间范围</button><button v-if="start || end" type="button" class="text-button" @click="start = ''; end = ''; load()">清除</button></form>
    <p v-if="error" role="alert" class="feedback error">{{ error }} <button class="text-button" @click="load">重试</button></p>
    <p v-if="busy" role="status" class="loading-line"><span class="spinner" aria-hidden="true"></span>正在读取经营指标… <button class="text-button" @click="controller?.abort(); busy = false">取消读取</button></p>
    <p v-if="data?.filter_missing?.length" role="status">{{ data.filter_missing.join('；') }}</p><div v-if="data" class="metric-strip"><div><span>支付金额</span><strong><MoneyValue :value="data.metrics.payment_minor" /></strong></div><div><span>成功退款</span><strong><MoneyValue :value="data.metrics.refund_minor" /></strong></div><div class="metric-emphasis"><span>现金期净实付</span><strong><MoneyValue :value="data.metrics.net_paid_minor" /></strong></div><div><span>已付订单 / 件数</span><strong>{{ data.metrics.paid_order_count }} <small>/ {{ data.metrics.paid_units }}</small></strong></div></div>
    <div v-else-if="!busy && !error" class="empty-state"><h2>选择快照，开始经营分析</h2><p>在“数据快照”导入获准数据，再选择分析来源。</p></div>
    <div class="overview-layout"><div class="overview-main">
      <template v-if="data">
        <div class="section-head"><h2>商品与库存</h2><span class="badge">{{ sourceName(data.snapshot.source_kind) }}</span></div>
        <p class="source-line">{{ data.snapshot.snapshot_id }} · v{{ data.snapshot.revision }} · 数据时点 {{ dateTime(data.snapshot.as_of) }}</p>
        <div class="table-scroll"><table><thead><tr><th>商品</th><th class="number">常规售价</th><th v-if="context.capabilities.includes('cost.read')" class="number">商品成本</th><th class="number">可用库存</th></tr></thead><tbody><tr v-for="sku in data.skus" :key="sku.sku_id"><td><strong>{{ sku.title }}</strong><small>{{ sku.sku_id }} · {{ sku.category }}</small></td><td class="number"><MoneyValue :value="sku.base_price_minor" /></td><td v-if="context.capabilities.includes('cost.read')" class="number"><MoneyValue :value="sku.cost_minor" /></td><td class="number">{{ sku.available_qty ?? '未知' }}</td></tr></tbody></table></div>
        <div class="facts-row"><div><span>客单价</span><strong><MoneyValue :value="data.metrics.average_order_minor" /></strong></div><div v-if="data.cost_coverage && context.capabilities.includes('cost.read')"><span>成本覆盖 SKU</span><strong>{{ data.cost_coverage.known }} / {{ data.cost_coverage.total }}</strong></div><div v-if="context.capabilities.includes('cost.read')"><span>贡献利润</span><strong><MoneyValue :value="data.contribution_profit_minor" /></strong></div></div>
        <p v-if="data.profit_missing_reason && context.capabilities.includes('cost.read')" class="inline-note">{{ data.profit_missing_reason }}</p>
        <details v-if="data.history.length"><summary>每日支付与退款</summary><div class="table-scroll"><table><thead><tr><th>日期</th><th class="number">支付金额</th><th class="number">退款金额</th></tr></thead><tbody><tr v-for="row in data.history" :key="row.date"><td>{{ row.date }}</td><td class="number"><MoneyValue :value="row.payment_minor" /></td><td class="number"><MoneyValue :value="row.refund_minor" /><details v-if="row.refund_missing?.length"><summary>退款分摊依据缺失</summary><ul><li v-for="source in row.refund_missing" :key="source.refund_id">{{ source.refund_id }} · {{ source.order_id }} · {{ dateTime(source.occurred_at) }}</li></ul></details></td></tr></tbody></table></div></details>
      </template>
    </div><AssistantPanel :snapshot-id="snapshotId" /></div>
  </section>
</template>
