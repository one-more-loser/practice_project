<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Context, Monitoring, PlanSummary, Plan } from '../types'
import { api, errorMessage } from '../api'
import { dateTime, statusName } from '../format'
import CapabilityAction from './CapabilityAction.vue'
const props = defineProps<{context: Context; initialPlanId?: string}>()
const plans = ref<PlanSummary[]>([]), planId = ref(props.initialPlanId ?? ''), activities = ref<Record<string, unknown>[]>([]), monitoring = ref<Monitoring | null>(null)
const error = ref(''), message = ref(''), busy = ref(''), summary = ref(''), actions = ref(''), preview = ref<unknown>(null)
const monitorBusy = ref(false), lastRefresh = ref(''), reason = ref(''), pollCount = ref(0)
const selectedPlan = ref<Plan | null>(null)
let timer: ReturnType<typeof setTimeout> | undefined, controller: AbortController | undefined, unmounted = false
let selectionSequence = 0
// 同步失效，不能等下一次请求成功才移除上一份可执行审批。
watch(planId, () => {
  selectionSequence += 1; controller?.abort(); clearTimer()
  selectedPlan.value = null; monitoring.value = null; preview.value = null
  lastRefresh.value = ''; error.value = ''; monitorBusy.value = false
}, { flush: 'sync' })
const refreshSeconds = computed(() => {
  const plan = monitoring.value?.plan
  const minutes = plan && 'draft' in plan ? plan.draft.monitoring.interval_minutes : 5
  return Math.min(900, Math.max(60, Number.isFinite(minutes) && minutes > 0 ? minutes * 60 : 300))
})
function clearTimer() { if (timer) clearTimeout(timer); timer = undefined }
function scheduleRefresh() {
  clearTimer()
  if (pollCount.value < 60 && !unmounted && document.visibilityState === 'visible' && planId.value && props.context.capabilities.includes('cost.read')) timer = setTimeout(() => { if (!busy.value && !monitorBusy.value) void loadMonitoring(); else scheduleRefresh() }, refreshSeconds.value * 1000)
}
function visibilityChanged() { if (document.visibilityState !== 'visible') clearTimer(); else scheduleRefresh() }
function activityPhase(activity: Record<string, unknown>) { return statusName(String(activity.phase ?? activity.status ?? 'unknown')) }
function deviationText(value: unknown) {
  if (!value || typeof value !== 'object') return String(value ?? '')
  const row = value as Record<string, unknown>
  return String(row.meaning ?? `${row.metric ?? '指标'}：${row.value ?? '未知'}（${row.status ?? '待核对'}）`)
}
const realActivity = computed(() => props.context.mode === 'integration' && props.context.dependencies.activity === 'configured')
const executionReason = computed(() => props.context.mode === 'demo' ? '演示不签发真实活动授权' : !realActivity.value ? '01 活动接口未接入真实执行器' : !planId.value ? '先选择已确认方案' : '')
async function refresh() {
  if (busy.value || monitorBusy.value) return
  busy.value = 'load'; error.value = ''; pollCount.value = 0
  try { const list = await api.request<{items: PlanSummary[]}>('/plans'); plans.value = list.items; const result = await api.request<{items: Record<string, unknown>[]}>('/activities', { shop: realActivity.value }); activities.value = result.items; if (planId.value) await loadMonitoring() }
  catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
async function loadMonitoring() {
  if (unmounted) return
  clearTimer()
  controller?.abort()
  const sequence = ++selectionSequence
  selectedPlan.value = null; monitoring.value = null
  if (!planId.value || !props.context.capabilities.includes('cost.read')) { monitorBusy.value = false; return }
  const selectedId = planId.value
  const requestController = new AbortController(); controller = requestController
  const current = () => !unmounted && sequence === selectionSequence && selectedId === planId.value
  monitorBusy.value = true; error.value = ''
  try {
    pollCount.value += 1
    const plan = await api.request<Plan>(`/plans/${encodeURIComponent(selectedId)}`, { signal: requestController.signal })
    if (!current()) return
    const result = await api.request<Monitoring>(`/monitoring?plan_id=${encodeURIComponent(selectedId)}`, { signal: requestController.signal })
    if (!current()) return
    if (plan.plan_id !== selectedId || result.plan.plan_id !== selectedId) throw new Error('方案身份不符，请重新读取')
    monitoring.value = result; lastRefresh.value = new Date().toISOString()
    // 后台状态可能在两次读取间推进：保留当前监控事实，但不允许混用审批发布。
    if (result.plan.version === plan.version && result.plan.status === plan.status) selectedPlan.value = plan
    else error.value = '方案版本或状态已变化，请重新读取后再发布'
    const latest = await api.request<{items: Record<string, unknown>[]}>('/activities', { shop: realActivity.value, signal: requestController.signal })
    if (current()) activities.value = latest.items
  } catch (e) { if (current()) { selectedPlan.value = null; error.value = errorMessage(e) } }
  finally { if (current()) { monitorBusy.value = false; scheduleRefresh() } }
}
async function preflight() {
  if (executionReason.value || busy.value) return
  const selectedId = planId.value, sequence = selectionSequence
  busy.value = 'preview'; error.value = ''; preview.value = null
  try { const result = await api.request(`/plans/${encodeURIComponent(selectedId)}/execution-preview`, { method: 'POST', body: {} }); if (!unmounted && sequence === selectionSequence && selectedId === planId.value) preview.value = result } catch (e) { if (!unmounted && sequence === selectionSequence) error.value = errorMessage(e) } finally { busy.value = '' }
}
function publishBinding(plan: Plan | null) {
  if (!plan || plan.plan_id !== planId.value || plan.store_id !== props.context.store_id || plan.status !== 'confirmed' || !Number.isInteger(plan.version) || plan.version < 1) return null
  const approval = plan.approval, approvedPreview = approval?.preview as Record<string, unknown> | undefined
  const text = (value: unknown) => typeof value === 'string' && value.trim().length > 0
  if (!approval || approval.plan_ref !== plan.plan_id || approval.plan_version !== plan.version ||
      approval.mode !== 'integration' || approval.evidence_kind !== 'integration' ||
      !text(approval.approval_ref) || !text(approval.manifest_digest) || !approvedPreview ||
      approvedPreview.contract_version !== 'XOPS-ACTIVITY-v0.1' || approvedPreview.store_id !== plan.store_id ||
      approvedPreview.plan_id !== plan.plan_id || approvedPreview.plan_version !== plan.version ||
      approvedPreview.status !== 'passed' || approvedPreview.evidence_kind !== 'integration' ||
      approvedPreview.manifest_digest !== approval.manifest_digest || !text(approvedPreview.preview_ref)) return null
  return { plan_id: plan.plan_id, plan_version: plan.version, approval_ref: approval.approval_ref, manifest_digest: approval.manifest_digest, preview_ref: approvedPreview.preview_ref }
}
function validPublishBinding() {
  if (executionReason.value || monitorBusy.value || error.value || !props.context.capabilities.includes('cost.read') || !props.context.capabilities.includes('activity.publish')) return null
  const binding = publishBinding(selectedPlan.value), observed = monitoring.value?.plan
  const monitoringBinding = observed && 'approval' in observed ? publishBinding(observed) : null
  return binding && monitoringBinding && JSON.stringify(binding) === JSON.stringify(monitoringBinding) ? binding : null
}
const publishReason = computed(() => executionReason.value || (monitorBusy.value ? '正在读取当前方案' : !validPublishBinding() ? '先读取并确认当前有效方案' : ''))
async function publishActivity() {
  // 函数内重新取绑定，不能只信任按钮上一次计算的 disabled。
  const binding = validPublishBinding()
  if (!binding || busy.value) return
  busy.value = 'publish'; error.value = ''; message.value = ''
  try {
    const result = await api.request<Record<string, unknown>>('/activities', { shop: true, method: 'POST', body: binding })
    message.value = result.status === 'committed' ? '01已提交活动，正在核对最新状态' : '发布结果待核对，请刷新记录或核对执行'
    await loadMonitoring()
  } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
async function changePhase(activity: Record<string, unknown>, action: 'pause' | 'resume' | 'end') {
  if (busy.value || !realActivity.value || !reason.value.trim() || !props.context.capabilities.includes('activity.manage')) return
  busy.value = action; error.value = ''; message.value = ''
  try {
    const result = await api.request<Record<string, unknown>>(`/activities/${encodeURIComponent(String(activity.activity_id))}/${action}`, { shop: true, method: 'POST', body: { expected_revision: activity.activity_revision ?? activity.revision, reason: reason.value.trim() } })
    message.value = result.status === 'unknown' || result.status === 'result_unknown' ? '操作结果待核对，请刷新活动' : '01已返回活动事实，正在读取最新状态'
    const latest = await api.request<{items: Record<string, unknown>[]}>('/activities', { shop: true }); activities.value = latest.items
    if (planId.value) await loadMonitoring()
  } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
async function saveReview() {
  if (!planId.value || !summary.value.trim() || !props.context.capabilities.includes('plan.edit') || busy.value) return
  busy.value = 'review'; error.value = ''; message.value = ''
  try { await api.request('/retrospectives', { method: 'POST', body: { plan_id: planId.value, summary: summary.value, actions: actions.value.split('\n').map(s => s.trim()).filter(Boolean) } }); message.value = '复盘记录已保存；缺失的活动实际仍保持未知。'; summary.value = ''; actions.value = '' } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
onMounted(() => { document.addEventListener('visibilitychange', visibilityChanged); void refresh() })
onBeforeUnmount(() => { unmounted = true; clearTimer(); controller?.abort(); document.removeEventListener('visibilitychange', visibilityChanged) })
</script>
<template><section><div class="page-heading"><div><p class="eyebrow">活动与复盘</p><h1>确认之后，核对真实执行。</h1></div><button :disabled="!!busy || monitorBusy" @click="refresh">刷新记录</button></div><p v-if="error" role="alert" class="feedback error">{{ error }}</p><p v-if="message" role="status" class="feedback success">{{ message }}</p><p v-if="busy === 'load'" role="status">正在读取活动记录…</p>
  <div class="section-head"><h2>店铺活动</h2><span class="badge">{{ context.dependencies.activity === 'not_connected' ? '01 未接入' : context.dependencies.activity === 'contract_stub' ? '契约替身 · 不可真实发布' : '以01回执为准' }}</span></div><div v-if="!activities.length" class="empty-state compact"><h3>暂无真实活动回执</h3><p>方案测算或演示确认不会生成活动记录。</p></div><div v-else class="table-scroll"><table><thead><tr><th>活动</th><th>状态</th><th>方案版本</th><th>最后核对</th><th>操作</th></tr></thead><tbody><tr v-for="(activity, i) in activities" :key="i"><td>{{ activity.activity_id }}</td><td>{{ activityPhase(activity) }}</td><td>{{ activity.plan_id }} · {{ activity.plan_version }}</td><td>{{ dateTime(String(activity.updated_at ?? '')) }}</td><td><button :disabled="!realActivity || !!busy || !reason.trim() || !context.capabilities.includes('activity.manage') || !['active','scheduled'].includes(String(activity.phase))" @click="changePhase(activity, 'pause')">暂停</button><button :disabled="!realActivity || !!busy || !reason.trim() || !context.capabilities.includes('activity.manage') || activity.phase !== 'paused'" @click="changePhase(activity, 'resume')">恢复</button><button :disabled="!realActivity || !!busy || !reason.trim() || !context.capabilities.includes('activity.manage') || activity.phase === 'ended'" @click="changePhase(activity, 'end')">结束</button></td></tr></tbody></table></div>
  <div class="monitor-workspace"><div><div class="section-head"><h2>执行核对</h2></div><label>关联方案<select v-model="planId" :disabled="!!busy || !context.capabilities.includes('cost.read')" @change="loadMonitoring"><option value="">选择方案</option><option v-for="p in plans" :key="p.plan_id" :value="p.plan_id">{{ p.title ?? p.plan_id }} · v{{ p.version }} · {{ statusName(p.status) }}</option></select></label><div class="workflow-toolbar"><CapabilityAction :capabilities="context.capabilities" capability="plan.confirm" label="运行01预检" :busy="busy === 'preview'" :blocked-reason="executionReason" @activate="preflight" /><CapabilityAction :capabilities="context.capabilities" capability="activity.publish" label="发布活动" :blocked-reason="publishReason" :busy="busy === 'publish'" @activate="publishActivity" /></div><label>活动操作理由<input v-model="reason" maxlength="1000" placeholder="暂停、恢复或结束的理由"></label><details v-if="preview"><summary>01预检回执</summary><pre>{{ JSON.stringify(preview, null, 2) }}</pre></details>
  <p v-if="!context.capabilities.includes('cost.read')" class="inline-note">监控方案详情需要成本查看权限。</p><p v-if="monitorBusy" role="status" class="loading-line">正在核对活动实际…</p><section v-if="monitoring" class="monitor-status"><div class="section-head"><h3>实际与差异</h3><button class="text-button" :disabled="monitorBusy || !!busy" @click="loadMonitoring">刷新实际</button><span class="badge warning">{{ statusName(monitoring.status) }}</span></div><p v-if="!monitoring.actual" class="inline-note">尚无活动实际；支付、退款、预算核销和增量效果均未证实。</p><pre v-else>{{ JSON.stringify(monitoring.actual, null, 2) }}</pre><p class="muted">数据时效：{{ monitoring.freshness == null ? '未提供' : typeof monitoring.freshness === 'object' ? JSON.stringify(monitoring.freshness) : monitoring.freshness }}</p><ul v-if="monitoring.deviations.length" class="issue-list"><li v-for="(d, i) in monitoring.deviations" :key="i">{{ deviationText(d) }}</li></ul><p class="muted">每 {{ refreshSeconds / 60 }} 分钟核对 · 页面隐藏时暂停 · 本次最多60轮<template v-if="lastRefresh"> · 最近读取 {{ dateTime(lastRefresh) }}</template></p></section></div>
  <form class="review-form" @submit.prevent="saveReview"><div class="section-head"><h2>记录复盘</h2></div><p class="muted">引用当前方案；无实际数据时记录缺口与后续动作。</p><label>复盘结论<textarea v-model="summary" rows="5" maxlength="4000" :disabled="!context.capabilities.includes('plan.edit')" required></textarea></label><label>后续动作 · 每行一项<textarea v-model="actions" rows="4" :disabled="!context.capabilities.includes('plan.edit')"></textarea></label><CapabilityAction :capabilities="context.capabilities" capability="plan.edit" label="保存复盘" :primary="true" :busy="busy === 'review'" :blocked-reason="!planId ? '先选择关联方案' : !summary.trim() ? '先填写复盘结论' : ''" @activate="saveReview" /></form></div>
</section></template>

