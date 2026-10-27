<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { Draft, Snapshot } from '../types'
import { cloneDraft, fromLocal, localInput } from '../format'
const props = defineProps<{ draft: Draft; snapshot?: Snapshot; canEdit: boolean; saving: boolean }>()
const emit = defineEmits<{ save: [draft: Draft]; dirty: [dirty: boolean] }>()
const form = ref(cloneDraft(props.draft)), validation = ref(''), newSku = ref('')
watch(() => props.draft, value => { form.value = cloneDraft(value); validation.value = '' }, { deep: true })
const dirty = computed(() => JSON.stringify(form.value) !== JSON.stringify(props.draft))
watch(dirty, value => emit('dirty', value))
const stopText = computed({ get: () => form.value.monitoring.stop_conditions.join('\n'), set: value => { form.value.monitoring.stop_conditions = value.split('\n') } })
const availableSkus = computed(() => props.snapshot?.skus.filter(sku => !form.value.lines.some(line => line.sku_id === sku.sku_id)) ?? [])
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value) { event.preventDefault(); event.returnValue = '' } }
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => { window.removeEventListener('beforeunload', beforeUnload); emit('dirty', false) })
function reset() { form.value = cloneDraft(props.draft); validation.value = '' }
function addSku() {
  const sku = availableSkus.value.find(value => value.sku_id === newSku.value)
  if (!sku) return
  form.value.lines.push({ sku_id: sku.sku_id, quantities: { low: 0, base: 0, high: 0 }, direct_price_minor: sku.base_price_minor, basket_qty: 1, coupon_minor: '0', coupon_threshold_minor: sku.base_price_minor }); newSku.value = ''
}
function submit() {
  if (!props.canEdit || props.saving) return
  const d = form.value
  const amounts = [d.fixed_fee_minor, d.goal.min_revenue_minor, d.goal.min_profit_minor, d.goal.budget_minor, ...d.lines.flatMap(line => [line.direct_price_minor, line.coupon_minor, line.coupon_threshold_minor])]
  if (amounts.some(value => typeof value !== 'string' || !/^-?\d+$/.test(value))) { validation.value = '金额请输入整数分，不使用小数、千分号或科学计数法。'; return }
  if (!d.title.trim() || d.lines.length === 0) { validation.value = '填写方案名称，并选择至少一个 SKU。'; return }
  if (!d.start_at || !d.end_at || Date.parse(d.end_at) <= Date.parse(d.start_at)) { validation.value = '结束时间应晚于开始时间。'; return }
  validation.value = ''; emit('save', cloneDraft(d))
}
</script>
<template>
  <form class="plan-form" @submit.prevent="submit">
    <p v-if="validation" role="alert" class="feedback error">{{ validation }}</p>
    <fieldset :disabled="!canEdit || saving">
      <div class="form-grid">
        <label class="wide">方案名称<input v-model="form.title" name="title" maxlength="120" required></label>
        <label>开始时间 · 上海<input type="datetime-local" :value="localInput(form.start_at)" @input="form.start_at = fromLocal(($event.target as HTMLInputElement).value)" required></label>
        <label>结束时间 · 上海<input type="datetime-local" :value="localInput(form.end_at)" @input="form.end_at = fromLocal(($event.target as HTMLInputElement).value)" required></label>
      </div>
      <div class="section-head"><h3>目标与预算</h3><span class="muted">金额输入单位：分</span></div>
      <div class="form-grid four">
        <label>收入底线<input v-model="form.goal.min_revenue_minor" inputmode="numeric" name="min-revenue" required></label>
        <label>贡献利润底线<input v-model="form.goal.min_profit_minor" inputmode="numeric" name="min-profit" required></label>
        <label>让利与固定费用预算<input v-model="form.goal.budget_minor" inputmode="numeric" name="budget" required></label>
        <label>固定费用<input v-model="form.fixed_fee_minor" inputmode="numeric" name="fixed-fee" required></label>
        <label>选定规则<select v-model="form.selected_candidate"><option value="direct">直降</option><option value="baseline">原价（仅比较）</option><option value="coupon">满额券（仅探索）</option></select></label>
        <label>约束情景<select v-model="form.goal.scenario_policy"><option value="base">基准</option><option value="all">全部情景</option></select></label>
      </div>
      <div class="section-head"><h3>商品与数量</h3><span class="muted">低 / 基准 / 高情景</span></div>
      <div class="table-scroll"><table class="editable"><thead><tr><th>SKU</th><th>活动单价 / 分</th><th>低</th><th>基准</th><th>高</th><th>操作</th></tr></thead><tbody>
        <tr v-for="(line, i) in form.lines" :key="line.sku_id"><td><strong>{{ snapshot?.skus.find(sku => sku.sku_id === line.sku_id)?.title ?? line.sku_id }}</strong><small>{{ line.sku_id }}</small></td><td><input v-model="line.direct_price_minor" :name="`line-${i}-price`" :aria-label="`${line.sku_id} 活动单价（分）`" inputmode="numeric" required></td><td v-for="scenario in (['low','base','high'] as const)" :key="scenario"><input v-model.number="line.quantities[scenario]" type="number" min="0" step="1" :aria-label="`${line.sku_id} ${scenario} 数量`" required></td><td><button class="text-button" type="button" @click="form.lines.splice(i, 1)">移除</button></td></tr>
      </tbody></table></div>
      <div v-if="availableSkus.length" class="inline-fields"><select v-model="newSku" aria-label="添加SKU"><option value="">选择 SKU</option><option v-for="sku in availableSkus" :key="sku.sku_id" :value="sku.sku_id">{{ sku.title }} · 库存 {{ sku.available_qty ?? '未知' }}</option></select><button type="button" :disabled="!newSku" @click="addSku">添加商品</button></div>
      <details v-if="form.selected_candidate === 'coupon'" open><summary>满额券参数</summary><div v-for="line in form.lines" :key="line.sku_id" class="form-grid"><label>{{ line.sku_id }} 每篮件数<input v-model.number="line.basket_qty" type="number" min="1"></label><label>券额 / 分<input v-model="line.coupon_minor" inputmode="numeric"></label><label>门槛 / 分<input v-model="line.coupon_threshold_minor" inputmode="numeric"></label></div></details>
      <div class="section-head"><h3>岗位容量</h3><button type="button" class="text-button" @click="form.resources.push({ role: '', volume_per_unit: '1', productivity_per_hour: '20', people: 1, shift_hours: '8', attendance: '1', efficiency: '0.75', source: '', confirmed: false })">添加岗位</button></div>
      <div v-for="(resource, i) in form.resources" :key="i" class="resource-row">
        <div class="form-grid four"><label>岗位<input v-model="resource.role" required></label><label>每件工作量<input v-model="resource.volume_per_unit" inputmode="decimal" required></label><label>每小时产能<input v-model="resource.productivity_per_hour" inputmode="decimal" required></label><label>人数<input v-model.number="resource.people" type="number" min="0" required></label><label>每人在活动窗内可用工时<input v-model="resource.shift_hours" inputmode="decimal" required></label><label>出勤比例 0–1<input v-model="resource.attendance" inputmode="decimal" required></label><label>效率比例 0–1<input v-model="resource.efficiency" inputmode="decimal" required></label><label>参数来源<input v-model="resource.source" required></label></div>
        <div class="row-between"><label class="check-label"><input v-model="resource.confirmed" type="checkbox">已核对岗位容量及来源</label><button type="button" class="text-button" @click="form.resources.splice(i, 1)">移除岗位</button></div>
      </div>
      <div class="section-head"><h3>执行任务</h3><button type="button" class="text-button" @click="form.tasks.push({ title: '', owner: '', stage: 'during', due_at: form.start_at, done: false })">添加任务</button></div>
      <div v-for="(task, i) in form.tasks" :key="i" class="task-row"><div class="form-grid four"><label>任务<input v-model="task.title" required></label><label>负责人<input v-model="task.owner" required></label><label>阶段<select v-model="task.stage"><option value="before_publish">发布前</option><option value="during">活动中</option><option value="after">结束后</option></select></label><label>截止时间 · 上海<input type="datetime-local" :value="localInput(task.due_at)" @input="task.due_at = fromLocal(($event.target as HTMLInputElement).value)" required></label></div><div class="row-between"><label class="check-label"><input v-model="task.done" type="checkbox">已完成</label><button type="button" class="text-button" @click="form.tasks.splice(i, 1)">移除任务</button></div></div>
      <div class="section-head"><h3>监控与依据</h3></div>
      <div class="form-grid"><label>间隔 / 分钟<input v-model.number="form.monitoring.interval_minutes" type="number" min="1" required></label><label>实际数据源<input v-model="form.monitoring.data_source" required></label><label class="wide">停止条件 · 每行一项<textarea v-model="stopText" rows="3" required></textarea></label><label class="wide">依据与假设<textarea v-model="form.notes" rows="3"></textarea></label></div>
    </fieldset>
    <div class="form-footer"><span class="muted">{{ dirty ? '有未保存修改' : '修改后保存为新版本，确认记录不覆盖' }}</span><div class="button-row"><button type="button" :disabled="!dirty || saving" @click="reset">恢复已保存内容</button><button type="submit" class="primary" :disabled="!canEdit || saving" :aria-busy="saving"><span v-if="saving" class="spinner" aria-hidden="true"></span>{{ saving ? '保存中' : '保存方案版本' }}</button></div></div>
  </form>
</template>
