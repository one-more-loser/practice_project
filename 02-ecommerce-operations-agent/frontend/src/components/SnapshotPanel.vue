<script setup lang="ts">
import { computed, ref } from 'vue'
import { api, errorMessage } from '../api'
import type { Context, Snapshot } from '../types'
import { dateTime, sourceName } from '../format'
import CapabilityAction from './CapabilityAction.vue'
const props = defineProps<{ context: Context; snapshots: Snapshot[]; selectedId: string }>()
const emit = defineEmits<{ select: [id: string]; imported: [] }>()
const json = ref(''), error = ref(''), message = ref(''), busy = ref(false), filename = ref(''), importMode = ref('snapshot')
const canImport = computed(() => props.context.capabilities.includes('data.import') && props.context.capabilities.includes('cost.read'))
async function readFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  if (file.size > 5 * 1024 * 1024) { error.value = '选择 5 MB 以内的 JSON；大批次请先分批整理。'; return }
  try { json.value = await file.text(); filename.value = file.name; error.value = ''; message.value = '' } catch { error.value = '无法读取文件，请重新选择。' }
}
async function importSnapshot() {
  if (!canImport.value || busy.value || !json.value.trim()) return
  let body: unknown
  try { body = JSON.parse(json.value) } catch { error.value = 'JSON 解析失败，请检查引号、逗号和括号。'; return }
  busy.value = true; error.value = ''; message.value = ''
  try { await api.request<Snapshot>(importMode.value === 'csv' ? '/snapshots/import-csv' : '/snapshots', { method: 'POST', body }); message.value = '快照已接收。可从列表选择后分析。'; emit('imported') } catch (e) { error.value = errorMessage(e) } finally { busy.value = false }
}
async function pullSnapshot() {
  if (!canImport.value || busy.value) return
  busy.value = true; error.value = ''; message.value = ''
  try { await api.request('/snapshots/pull', { method: 'POST', body: {} }); message.value = '01经营快照已归档'; emit('imported') } catch (e) { error.value = errorMessage(e) } finally { busy.value = false }
}
</script>
<template>
  <section><div class="page-heading"><div><p class="eyebrow">数据快照</p><h1>保留来源，按版本分析。</h1></div><span class="muted">{{ snapshots.length }} 个快照</span></div>
    <div v-if="snapshots.length" class="table-scroll"><table><thead><tr><th>快照</th><th>来源</th><th>数据时点</th><th class="number">SKU</th><th>分析来源</th></tr></thead><tbody><tr v-for="snapshot in snapshots" :key="`${snapshot.snapshot_id}-${snapshot.revision}`" :class="{ selected: snapshot.snapshot_id === selectedId }"><td><strong>{{ snapshot.snapshot_id }}</strong><small>版本 {{ snapshot.revision }}</small></td><td><span class="badge">{{ sourceName(snapshot.source_kind) }}</span></td><td>{{ dateTime(snapshot.as_of) }}</td><td class="number">{{ snapshot.skus.length }}</td><td><button type="button" :disabled="snapshot.snapshot_id === selectedId" @click="emit('select', snapshot.snapshot_id)">{{ snapshot.snapshot_id === selectedId ? '已选中' : '设为分析来源' }}</button></td></tr></tbody></table></div>
    <div v-else class="empty-state"><h2>还没有经营快照</h2><p>导入包含商品、订单和退款的 JSON，来源标记会保留在方案中。</p></div>
    <div class="section-head import-heading"><h2>导入快照</h2><button v-if="context.mode === 'integration'" :disabled="!canImport || busy" @click="pullSnapshot">从01拉取经营快照</button><span class="muted">整批校验 · 不含个人信息</span></div>
    <p v-if="!canImport" class="inline-note">当前身份没有数据导入权限。</p>
    <form class="import-form" @submit.prevent="importSnapshot"><fieldset :disabled="!canImport || busy"><label class="import-mode">导入格式<select v-model="importMode" name="import-mode"><option value="snapshot">Snapshot JSON</option><option value="csv">CSV 文本包 JSON</option></select></label><div class="row-between"><label class="file-picker">选择 JSON 文件<input type="file" accept="application/json,.json" @change="readFile"></label><span class="muted">{{ filename }}</span></div><label for="snapshot-json">{{ importMode === 'csv' ? 'CSV 文本包 JSON' : '快照 JSON' }}</label><textarea id="snapshot-json" v-model="json" class="code-input" rows="12" spellcheck="false" :placeholder="importMode === 'csv' ? '粘贴包含 manifest、skus_csv、orders_csv、refunds_csv 的 JSON' : '粘贴包含商品、订单与退款的完整 Snapshot JSON'"></textarea><details v-if="importMode === 'csv'"><summary>查看 CSV 文本包格式</summary><pre>skus_csv: sku_id,title,category,base_price_minor,cost_minor,variable_fee_minor,available_qty,price_version
orders_csv: order_id,paid_at,status,channel,order_paid_minor,sku_id,qty,item_paid_minor
refunds_csv: refund_id,order_id,occurred_at,status,amount_minor</pre><p class="muted">manifest：snapshot_id、revision、store_id、source_kind、as_of、currency。skus_csv、orders_csv、refunds_csv 分别保存含表头的 CSV 文本，以 JSON 字符串提交；换行写为 \n，字符串内引号需转义。服务端按完整批次校验。</p><pre>{ "manifest": { "snapshot_id": "batch_01", "revision": 1, "store_id": "当前获准店铺", "source_kind": "business_import", "as_of": "2026-10-23T12:00:00+08:00", "currency": "CNY" }, "skus_csv": "含表头的商品 CSV", "orders_csv": "含表头的订单 CSV", "refunds_csv": "含表头的退款 CSV" }</pre></details></fieldset><p v-if="error" class="feedback error" role="alert">{{ error }}</p><p v-if="message" class="feedback success" role="status">{{ message }}</p><CapabilityAction :capabilities="context.capabilities" capability="data.import" label="校验并导入" :primary="true" :busy="busy" :blocked-reason="!json.trim() ? '先选择文件或粘贴 JSON' : ''" @activate="importSnapshot" /></form>
  </section>
</template>
