<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, errorMessage } from '../api'
import type { Context, Draft, Plan, PlanSummary, Snapshot } from '../types'
import { createDraft, dateTime, sourceName, statusName } from '../format'
import CapabilityAction from './CapabilityAction.vue'
import EvaluationPanel from './EvaluationPanel.vue'
import PlanEditor from './PlanEditor.vue'
const props = defineProps<{ context: Context; snapshots: Snapshot[]; snapshotId: string }>()
const emit = defineEmits<{ updated: [plan: Plan]; dirty: [value: boolean] }>()
const items = ref<PlanSummary[]>([]), plan = ref<Plan | null>(null), newDraft = ref<Draft | null>(null), error = ref(''), message = ref(''), busy = ref(''), dirty = ref(false), reason = ref('')
const draft = computed(() => plan.value?.draft ?? newDraft.value)
const snapshot = computed(() => props.snapshots.find(value => value.snapshot_id === draft.value?.snapshot_id))
const capabilities = computed(() => props.context.capabilities), canEdit = computed(() => capabilities.value.includes('plan.edit') && capabilities.value.includes('cost.read'))
const confirmBlock = computed(() => {
  if (dirty.value) return '先保存修改，再重新评估'
  if (!capabilities.value.includes('cost.read')) return '需要成本查看权限'
  if (plan.value?.status === 'invalidated') return plan.value.validity?.note ?? '快照已失效，须重新评估'
  if (!plan.value?.evaluation || plan.value.evaluation.feasibility !== 'checks_passed') return '02 必需检查尚未通过'
  if (plan.value.approval) return '当前版本已记录确认'
  if (!reason.value.trim()) return '填写确认依据后继续'
  if (props.context.mode !== 'demo' && !['configured', 'connected'].includes(props.context.dependencies.activity)) return '01 活动预检未接入'
  return ''
})
const cancelBlock = computed(() => {
  if (!capabilities.value.includes('cost.read')) return '需要成本查看权限'
  if (dirty.value) return '先保存或恢复当前修改'
  if (!plan.value?.approval) return '当前版本尚无审批记录'
  if (plan.value.status === 'cancel_pending') return '撤销待01终态核对；不能视为活动已停止'
  if (['cancelled', 'closed_no_effect'].includes(plan.value.status)) return '当前审批已关闭'
  if (!reason.value.trim()) return '填写撤销理由后继续'
  return ''
})
async function cancelApproval() {
  if (!plan.value || busy.value || cancelBlock.value || !capabilities.value.includes('plan.confirm')) return
  busy.value = 'cancel'; error.value = ''; message.value = ''
  try {
    const result = await api.request<Plan>(`/plans/${encodeURIComponent(plan.value.plan_id)}/cancel-approval`, { method: 'POST', body: { expected_version: plan.value.version, reason: reason.value } })
    plan.value = result; emit('updated', result)
    message.value = '撤销请求已记录；以执行终态为准，已提交活动需在01暂停/结束。'
    await refresh()
  } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
async function refresh() { try { items.value = (await api.request<{ items: PlanSummary[] }>('/plans')).items } catch (e) { error.value = errorMessage(e) } }
function setDirty(value: boolean) { dirty.value = value; emit('dirty', value) }
function newPlan() {
  if (!canEdit.value || dirty.value || busy.value) return
  const selected = props.snapshots.find(value => value.snapshot_id === props.snapshotId)
  if (!selected) { error.value = '先选择一个已接收的经营快照。'; return }
  plan.value = null; newDraft.value = createDraft(selected, props.context.clock); error.value = ''; message.value = ''; reason.value = ''
}
async function open(id: string) {
  if (dirty.value || busy.value || !capabilities.value.includes('cost.read')) return
  busy.value = 'load'; error.value = ''; message.value = ''
  try { const result = await api.request<Plan>(`/plans/${encodeURIComponent(id)}`); plan.value = result; newDraft.value = null; reason.value = ''; emit('updated', result) } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
async function save(value: Draft) {
  if (!canEdit.value || busy.value) return
  busy.value = 'save'; error.value = ''; message.value = ''
  try { const result = plan.value ? await api.request<Plan>(`/plans/${encodeURIComponent(plan.value.plan_id)}`, { method: 'PUT', body: { expected_version: plan.value.version, draft: value } }) : await api.request<Plan>('/plans', { method: 'POST', body: value }); plan.value = result; newDraft.value = null; setDirty(false); message.value = `方案版本 v${result.version} 已保存，需重新评估。`; emit('updated', result); await refresh() } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
async function operate(action: 'evaluate' | 'confirm') {
  if (!plan.value || busy.value || dirty.value || (action === 'confirm' && confirmBlock.value)) return
  busy.value = action; error.value = ''; message.value = ''
  try { const result = await api.request<Plan>(`/plans/${encodeURIComponent(plan.value.plan_id)}/${action}`, { method: 'POST', body: { expected_version: plan.value.version, ...(action === 'confirm' ? { reason: reason.value } : {}) } }); plan.value = result; emit('updated', result); message.value = action === 'confirm' ? (props.context.mode === 'demo' ? '演示确认已记录，仍需接入 01 预检与真实发布。' : '方案确认已记录；发布活动是独立操作。') : '评估完成，请查看约束与数据缺口。'; await refresh() } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
async function exportPlan() {
  if (!plan.value || busy.value) return
  busy.value = 'export'; error.value = ''
  try { const markdown = await api.request<string>(`/plans/${encodeURIComponent(plan.value.plan_id)}/export`, { text: true }); const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' })); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `经营方案-${plan.value.plan_id}-v${plan.value.version}.md`; anchor.click(); URL.revokeObjectURL(url); message.value = '方案 Markdown 已导出。' } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
async function reconcileExecution() {
  if (!plan.value || busy.value || dirty.value || !capabilities.value.includes('plan.confirm')) return
  busy.value = 'reconcile'; error.value = ''; message.value = ''
  try { const result = await api.request<Plan>(`/plans/${encodeURIComponent(plan.value.plan_id)}/reconcile-execution`, { method: 'POST', body: { expected_version: plan.value.version } }); plan.value = result; emit('updated', result); message.value = '已核对执行结果；仍在待核对状态时，不表示撤销成功。'; await refresh() } catch (e) { error.value = errorMessage(e) } finally { busy.value = '' }
}
onMounted(refresh)
</script>
<template>
  <section><div class="page-heading"><div><p class="eyebrow">运营方案</p><h1>把判断，落成可核对的方案。</h1></div><CapabilityAction :capabilities="capabilities" capability="plan.edit" label="新建方案" :primary="true" :blocked-reason="dirty ? '先保存或恢复当前修改' : !snapshotId ? '先选择数据快照' : ''" :busy="!!busy" @activate="newPlan" /></div>
    <p v-if="error" class="feedback error" role="alert">{{ error }}</p><p v-if="message" class="feedback success" role="status">{{ message }}</p>
    <div class="plan-workspace"><aside class="plan-index"><div class="section-head"><h2>方案列表</h2><button class="text-button" :disabled="!!busy" @click="refresh">刷新</button></div><div v-if="!items.length" class="empty-list">从当前快照新建方案。</div><button v-for="item in items" :key="item.plan_id" class="plan-list-item" :class="{ active: plan?.plan_id === item.plan_id }" :disabled="dirty || !!busy || !capabilities.includes('cost.read')" @click="open(item.plan_id)"><strong>{{ item.title ?? item.plan_id }}</strong><span>v{{ item.version }} · {{ statusName(item.status) }}</span><small>{{ sourceName(item.source_kind) }} · {{ dateTime(item.updated_at) }}</small></button><p v-if="!capabilities.includes('cost.read')" class="inline-note">当前仅可查看脱敏方案索引；详情需要成本权限。</p></aside>
      <div class="plan-detail"><p v-if="busy === 'load'" role="status" class="loading-line"><span class="spinner"></span>正在读取方案…</p>
        <template v-if="draft"><div class="detail-heading"><div><h2>{{ plan ? draft.title : '编制新方案' }}</h2><p class="source-line">{{ draft.snapshot_id }} · 数据版本 {{ draft.expected_snapshot_revision }}<template v-if="plan"> · 方案 v{{ plan.version }}</template></p></div><span v-if="plan" class="badge">{{ statusName(plan.status) }}</span></div>
          <p v-if="plan?.validity && (!plan.validity.snapshot_current || plan.validity.execution_frozen)" class="feedback" role="status">{{ plan.validity.note }}</p>
          <PlanEditor :draft="draft" :snapshot="snapshot" :can-edit="canEdit && !plan?.validity?.execution_frozen" :saving="!!busy" @save="save" @dirty="setDirty" />
          <template v-if="plan"><div v-if="plan.validity?.execution_frozen" class="workflow-toolbar"><CapabilityAction :capabilities="capabilities" capability="plan.confirm" label="核对未决执行" :busy="busy === 'reconcile'" :blocked-reason="dirty || !!busy ? '等待当前操作完成' : ''" @activate="reconcileExecution" /></div><div class="workflow-toolbar"><CapabilityAction :capabilities="capabilities" capability="plan.edit" label="重新评估" :busy="busy === 'evaluate'" :blocked-reason="dirty ? '先保存当前修改' : busy ? '等待当前操作完成' : ''" @activate="operate('evaluate')" /><button :disabled="!!busy || dirty" @click="open(plan.plan_id)">重新读取方案</button><CapabilityAction :capabilities="capabilities" capability="cost.read" label="导出完整方案" :busy="busy === 'export'" :blocked-reason="dirty ? '先保存当前修改' : busy ? '等待当前操作完成' : ''" @activate="exportPlan" /></div><EvaluationPanel v-if="plan.evaluation" :evaluation="plan.evaluation" /><div v-else class="inline-note">保存后的方案尚未评估。先运行评估，核对金额、资源与执行条件。</div>
            <section class="confirmation-section"><div class="section-head"><h2>方案确认</h2><span v-if="context.mode === 'demo'" class="badge">仅演示记录</span></div><p class="muted">{{ context.mode === 'demo' ? '演示确认不签发真实活动授权；01 接入后仍需预检及明确发布。' : '确认绑定当前版本。后续修改会产生新版本，需要重新评估与确认。' }}</p><label>确认 / 撤销依据<textarea v-model="reason" rows="2" :disabled="!!busy || !capabilities.includes('plan.confirm')" placeholder="确认时说明核对依据；撤销时说明原因"></textarea></label><CapabilityAction :capabilities="capabilities" capability="plan.confirm" :label="context.mode === 'demo' ? '演示确认方案' : '确认方案'" :primary="true" :busy="busy === 'confirm'" :blocked-reason="busy && busy !== 'confirm' ? '等待当前操作完成' : confirmBlock" @activate="operate('confirm')" /><div v-if="plan.approval" class="workflow-toolbar"><CapabilityAction :capabilities="capabilities" capability="plan.confirm" label="申请撤销审批" :busy="busy === 'cancel'" :blocked-reason="busy && busy !== 'cancel' ? '等待当前操作完成' : cancelBlock" @activate="cancelApproval" /><p v-if="plan.status === 'cancel_pending'" role="status" class="inline-note">撤销请求待01闭合执行槽位；尚不能确认无执行效果。已提交活动须前往01暂停或结束。</p></div><details v-if="plan.approval"><summary>查看确认记录</summary><pre>{{ JSON.stringify(plan.approval, null, 2) }}</pre></details></section>
          </template>
        </template><div v-else-if="!busy" class="empty-state"><h2>从经营目标开始</h2><p>新建方案，编辑价格、情景数量、预算与岗位任务，再保存和评估。</p></div>
      </div>
    </div>
  </section>
</template>
