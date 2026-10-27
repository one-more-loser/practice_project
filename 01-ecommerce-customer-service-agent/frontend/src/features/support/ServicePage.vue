<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useAuth } from '../../stores/auth'
import { request, mutate, money, errorText } from '../../lib/api'
import type { Proposal } from '../../lib/types'
import ActionConfirm from '../../components/ActionConfirm.vue'
type Ticket = {
  ticket_id: string
  subject: string
  status: string
  version: number
  entries?: { entry_id: string; text: string }[]
}
type Claim = {
  claim_id: string
  order_id: string
  ticket_id: string
  status: string
  version: number
  goods_minor: string
  original_freight_minor: string
  items: { order_item_id: string; qty: number; goods_minor: string }[]
  return_shipment?: { carrier: string; tracking_no: string }
  reviews?: { decision: string; basis: string }[]
}
type Incident = {
  incident_id: string
  order_id: string
  ticket_id: string
  status: string
  version: number
  kind: string
  refund_claim?: Claim
  facts: { decision: string; basis: string }[]
}
type Review = {
  review_id: string
  order_id: string
  status: string
  decision: string | null
  error_code: string | null
}
const auth = useAuth(),
  tickets = ref<Ticket[]>([]),
  claims = ref<Claim[]>([]),
  incidents = ref<Incident[]>([]),
  reviews = ref<Review[]>([]),
  ticket = ref<Ticket | null>(null),
  claim = ref<Claim | null>(null),
  incident = ref<Incident | null>(null),
  error = ref(''),
  busy = ref(false),
  proposal = ref<Proposal | null>(null),
  subject = ref(''),
  description = ref(''),
  category = ref('general'),
  basis = ref(''),
  evidence = ref(''),
  carrier = ref(''),
  tracking = ref(''),
  requestFreight = ref(false),
  inspection = ref<Record<string, { received_qty: number; saleable_qty: number }>>({})
const labels: Record<string, string> = {
  open: '待处理',
  assigned: '已分派',
  in_progress: '处理中',
  waiting_customer: '等待补充',
  resolved: '已解决',
  closed: '已关闭',
  reopened: '已重开',
  reviewing: '待审核',
  awaiting_return: '待退回',
  return_in_transit: '退货运输中',
  needs_dispute: '主管处理',
  refund_reviewed: '待核验到账条件',
  refunded: '已退余额',
  rejected: '未通过',
  withdrawn: '已撤销',
  verifying: '核验中',
  awaiting_intercept: '等待拦截',
  intercept_failed: '拦截失败',
  awaiting_liability: '等待责任核验',
  verified_loss: '已核验丢件',
  queued: '待审核',
  running: '审核中',
  unavailable: '审核未就绪',
  needs_facts: '需补充事实',
  execution_rejected: '业务状态已变化',
  succeeded: '已取消并退余额',
}
const customer = () => auth.session?.active_role === 'customer'
async function run(work: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try {
    await work()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
async function load() {
  claims.value = (await request<{ items: Claim[] }>('/aftersales')).items
  incidents.value = (await request<{ items: Incident[] }>('/transport-incidents')).items
  if (customer()) {
    tickets.value = (await request<{ items: Ticket[] }>('/me/tickets')).items
    reviews.value = (await request<{ items: Review[] }>('/me/cancellation-reviews')).items
  }
}
async function createTicket() {
  await run(async () => {
    proposal.value = await request('/action-proposals', 'POST', {
      action: 'ticket.create',
      resource_ref: 'new',
      expected_version: 1,
      parameters: {
        subject: subject.value,
        description: description.value,
        category: category.value,
      },
    })
  })
}
async function done() {
  proposal.value = null
  await run(load)
}
async function selectClaim(row: Claim) {
  await run(async () => {
    claim.value = await request<Claim>('/aftersales/' + row.claim_id)
    incident.value = null
    inspection.value = Object.fromEntries(
      claim.value.items.map((line) => [
        line.order_item_id,
        { received_qty: line.qty, saleable_qty: 0 },
      ]),
    )
  })
}
async function selectIncident(row: Incident) {
  await run(async () => {
    incident.value = await request<Incident>('/transport-incidents/' + row.incident_id)
    claim.value = null
    const lines = incident.value.refund_claim?.items || []
    inspection.value = Object.fromEntries(
      lines.map((line) => [line.order_item_id, { received_qty: line.qty, saleable_qty: 0 }]),
    )
  })
}
async function refreshDetail() {
  await load()
  if (claim.value) claim.value = await request('/aftersales/' + claim.value.claim_id)
  if (incident.value)
    incident.value = await request('/transport-incidents/' + incident.value.incident_id)
}
const evidenceRefs = () =>
  evidence.value
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean)
async function review(decision: string) {
  await run(async () => {
    const row = claim.value || incident.value
    if (!row) return
    const path = claim.value
      ? `/aftersales/${claim.value.claim_id}`
      : `/transport-incidents/${incident.value!.incident_id}`
    await mutate(path + '/reviews', 'POST', {
      decision,
      basis: basis.value,
      evidence_refs: evidenceRefs(),
      expected_version: row.version,
    })
    await refreshDetail()
  })
}
async function refundRequest() {
  if (!incident.value) return
  await run(async () => {
    proposal.value = await request('/action-proposals', 'POST', {
      action: 'transport.refund.request',
      resource_ref: incident.value!.incident_id,
      expected_version: incident.value!.version,
      parameters: {
        description: description.value || '申请核验后退余额',
        request_original_freight: requestFreight.value,
      },
    })
  })
}
async function sendReturn() {
  await run(async () => {
    await mutate(`/aftersales/${claim.value!.claim_id}/return-shipment`, 'POST', {
      carrier: carrier.value,
      tracking_no: tracking.value,
      expected_version: claim.value!.version,
    })
    await refreshDetail()
  })
}
async function inspect() {
  await run(async () => {
    const row = claim.value || incident.value?.refund_claim
    if (!row) return
    await mutate(`/refund-claims/${row.claim_id}/inspections`, 'POST', {
      lines: inspection.value,
      evidence_refs: evidenceRefs(),
      expected_version: row.version,
    })
    await refreshDetail()
  })
}
async function withdraw() {
  await run(async () => {
    await mutate(`/aftersales/${claim.value!.claim_id}/withdraw`, 'POST', {
      expected_version: claim.value!.version,
    })
    await refreshDetail()
  })
}
async function selectTicket(row: Ticket) {
  await run(async () => {
    ticket.value = await request('/me/tickets/' + row.ticket_id)
  })
}
async function reopen() {
  await run(async () => {
    await request(`/me/tickets/${ticket.value!.ticket_id}/reopen`, 'POST', {
      reason: description.value,
      expected_version: ticket.value!.version,
    })
    ticket.value = null
    await load()
  })
}
onMounted(() => run(load))
</script>
<template>
  <div class="page-heading">
    <h1>{{ customer() ? '我的服务' : '售后与运输' }}</h1>
    <button :disabled="busy" @click="run(refreshDetail)">刷新</button>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <section v-if="customer()" class="panel service-section">
    <h2>提交工单</h2>
    <form @submit.prevent="createTicket">
      <div class="form-grid">
        <label class="field">主题<input v-model="subject" maxlength="160" required /></label
        ><label class="field"
          >类型<select v-model="category">
            <option value="general">一般咨询</option>
            <option value="order">订单</option>
            <option value="complaint">投诉</option>
          </select></label
        >
      </div>
      <label class="field"
        >说明<textarea v-model="description" rows="2" maxlength="1000" required /></label
      ><button class="primary" :disabled="busy">核对并提交</button>
    </form>
  </section>
  <section v-if="reviews.length" class="panel service-section">
    <h2>取消审核</h2>
    <div v-for="row in reviews" :key="row.review_id" class="list-row">
      <RouterLink :to="'/orders/' + row.order_id">查看订单</RouterLink
      ><span>{{ labels[row.status] || row.status }}</span>
    </div>
  </section>
  <div class="order-detail-grid">
    <div>
      <section class="panel service-section">
        <h2>退货退款</h2>
        <p v-if="!claims.length">暂无申请</p>
        <div v-for="row in claims" :key="row.claim_id" class="list-row">
          <div class="grow">
            <p>{{ labels[row.status] || row.status }}</p>
            <strong>¥{{ money(row.goods_minor) }}</strong>
          </div>
          <button :disabled="busy" @click="selectClaim(row)">查看申请</button>
        </div>
      </section>
      <section class="panel service-section">
        <h2>运输异常</h2>
        <p v-if="!incidents.length">暂无异常工单</p>
        <div v-for="row in incidents" :key="row.incident_id" class="list-row">
          <div class="grow">{{ labels[row.status] || row.status }}</div>
          <button :disabled="busy" @click="selectIncident(row)">查看运输工单</button>
        </div>
      </section>
      <section v-if="customer()" class="panel service-section">
        <h2>服务工单</h2>
        <p v-if="!tickets.length">暂无工单</p>
        <div v-for="row in tickets" :key="row.ticket_id" class="list-row">
          <div class="grow">{{ row.subject }} · {{ labels[row.status] || row.status }}</div>
          <button :disabled="busy" @click="selectTicket(row)">查看</button>
        </div>
        <template v-if="ticket"
          ><h3>{{ ticket.subject }}</h3>
          <p v-for="entry in ticket.entries" :key="entry.entry_id">{{ entry.text }}</p>
          <template v-if="['resolved', 'closed'].includes(ticket.status)"
            ><label class="field">重开原因<input v-model="description" maxlength="500" /></label
            ><button :disabled="busy || !description" @click="reopen">重开工单</button></template
          ></template
        >
      </section>
    </div>
    <section v-if="claim || incident" class="panel service-section">
      <h2>{{ claim ? '退货退款详情' : '运输异常详情' }}</h2>
      <p class="badge">{{ labels[(claim || incident)!.status] || (claim || incident)!.status }}</p>
      <p v-if="claim">
        申请商品 ¥{{ money(claim.goods_minor) }} · 原运费 ¥{{ money(claim.original_freight_minor) }}
      </p>
      <p v-for="(fact, index) in incident?.facts" :key="index">{{ fact.basis }}</p>
      <p v-for="(fact, index) in claim?.reviews" :key="index">{{ fact.basis }}</p>
      <template v-if="customer() && claim"
        ><div v-if="claim.status === 'awaiting_return'">
          <label class="field">退货承运商<input v-model="carrier" maxlength="60" /></label
          ><label class="field">退货单号<input v-model="tracking" maxlength="80" /></label
          ><button class="primary" :disabled="busy || !carrier || !tracking" @click="sendReturn">
            登记退货物流
          </button>
        </div>
        <button
          v-if="['reviewing', 'awaiting_return', 'needs_dispute'].includes(claim.status)"
          :disabled="busy"
          @click="withdraw"
        >
          撤销申请
        </button></template
      >
      <button
        v-if="
          customer() &&
          incident &&
          !incident.refund_claim &&
          !['closed', 'refunded'].includes(incident.status)
        "
        class="primary"
        :disabled="busy"
        @click="refundRequest"
      >
        申请未收到货退款
      </button>
      <label
        v-if="
          customer() &&
          incident &&
          !incident.refund_claim &&
          !['closed', 'refunded'].includes(incident.status)
        "
        ><input v-model="requestFreight" type="checkbox" />
        同时申请退原订单运费（按适用政策核验）</label
      >
      <template v-if="!customer()"
        ><label class="field">处理依据<textarea v-model="basis" rows="2" maxlength="1000" /></label
        ><label class="field"
          >证据引用<input v-model="evidence" placeholder="inspection:编号，多个引用用英文逗号分隔"
        /></label>
        <div v-if="auth.session?.active_role !== 'shop_admin'" class="actions">
          <button :disabled="busy || !basis || !evidence" @click="review('approve')">
            审核通过</button
          ><button :disabled="busy || !basis || !evidence" @click="review('reject')">
            拒绝申请</button
          ><template v-if="incident"
            ><button :disabled="busy || !basis || !evidence" @click="review('intercept_requested')">
              申请拦截</button
            ><button :disabled="busy || !basis || !evidence" @click="review('intercept_failed')">
              拦截失败</button
            ><button :disabled="busy || !basis || !evidence" @click="review('loss_verified')">
              核实丢件责任</button
            ><button :disabled="busy || !basis || !evidence" @click="review('dispute')">
              交主管处理
            </button></template
          >
        </div>
        <div v-else>
          <div v-for="(line, ref) in inspection" :key="ref" class="form-grid">
            <label class="field"
              >实收数量<input v-model.number="line.received_qty" type="number" min="0" /></label
            ><label class="field"
              >可售数量<input v-model.number="line.saleable_qty" type="number" min="0"
            /></label>
          </div>
          <button class="primary" :disabled="busy || !evidence" @click="inspect">
            确认实收并核对退款
          </button>
        </div></template
      >
    </section>
  </div>
  <ActionConfirm :proposal="proposal" title="提交服务申请" @done="done" @close="proposal = null" />
</template>
<style scoped>
.service-section {
  margin-bottom: 24px;
}
.field {
  display: grid;
  gap: 8px;
  margin: 16px 0;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
</style>
