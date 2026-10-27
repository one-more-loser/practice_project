<script setup lang="ts">
import { onMounted, ref, onUnmounted } from 'vue'
import { useAuth } from '../../stores/auth'
import { request, errorText } from '../../lib/api'
import { subscribe } from './realtime'
type Ticket = {
  ticket_id: string
  summary: string
  category: string
  status: string
  version: number
  assigned_to_me: boolean
  sla_overdue: boolean
  conversation_id?: string
  assignment_id?: string
  assignment_version?: number
  entries?: { entry_id: string; text: string; visibility: string }[]
  case_kind?: string
  case_ref?: string
  related_order?: {
    display_no: string
    paid_minor: string
    shipping: { phone_masked: string; address_masked: string }
  }
}
type Conversation = {
  conversation_id: string
  mode: string
  mode_epoch: number
  version: number
  items: { message_id: string; source_role: string; text: string }[]
}
const auth = useAuth(),
  rows = ref<Ticket[]>([]),
  detail = ref<Ticket | null>(null),
  assignees = ref<{ staff_id: string; name: string }[]>([]),
  chosen = ref(''),
  text = ref(''),
  error = ref(''),
  busy = ref(false),
  chat = ref<Conversation | null>(null)
let closeSocket: (() => void) | undefined
const pendingReply = ref<{ id: string; text: string; epoch: number; assignment: number } | null>(
  null,
)
const labels: Record<string, string> = {
  open: '待分派',
  assigned: '已分派',
  in_progress: '处理中',
  waiting_customer: '等待顾客',
  resolved: '已解决',
  closed: '已关闭',
  reopened: '已重开',
  queued: '等待接入',
  human: '人工服务',
  agent: '店铺客服',
}
async function load() {
  rows.value = (await request<{ items: Ticket[] }>('/staff/queue')).items
}
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
async function select(row: Ticket) {
  if (pendingReply.value) {
    error.value = '请先核对上一条消息的发送结果，再切换工单'
    return
  }
  await run(async () => {
    closeSocket?.()
    chat.value = null
    detail.value = null
    detail.value = await request<Ticket>('/staff/tickets/' + row.ticket_id)
    if (detail.value.conversation_id) {
      await sync()
      closeSocket = subscribe(detail.value.conversation_id, chat.value!.mode_epoch, sync, (s) => {
        error.value = s
        detail.value = null
        chat.value = null
      })
    }
  })
}
async function sync() {
  if (detail.value?.conversation_id)
    chat.value = await request<Conversation>('/staff/conversations/' + detail.value.conversation_id)
}
async function assign() {
  await run(async () => {
    await request('/staff/tickets/' + detail.value!.ticket_id + '/assignments', 'POST', {
      staff_id: chosen.value,
      reason: text.value,
      expected_version: detail.value!.version,
    })
    await load()
    detail.value = await request('/staff/tickets/' + detail.value!.ticket_id)
  })
}
async function act(action: string) {
  await run(async () => {
    await request('/staff/tickets/' + detail.value!.ticket_id + '/actions', 'POST', {
      action,
      text: text.value,
      expected_version: detail.value!.version,
    })
    detail.value = await request('/staff/tickets/' + detail.value!.ticket_id)
    text.value = ''
    await load()
  })
}
async function human(end = false) {
  await run(async () => {
    await sync()
    await request(
      '/staff/conversations/' + chat.value!.conversation_id + (end ? '/end-service' : '/takeover'),
      'POST',
      {
        assignment_id: detail.value!.assignment_id,
        assignment_version: detail.value!.assignment_version,
        expected_epoch: chat.value!.mode_epoch,
        expected_version: chat.value!.version,
        ...(end ? { reason: text.value || '本次服务已完成' } : {}),
      },
    )
    await sync()
  })
}
async function reply() {
  await run(async () => {
    pendingReply.value ??= {
      id: 'staff_' + crypto.randomUUID(),
      text: text.value,
      epoch: chat.value!.mode_epoch,
      assignment: detail.value!.assignment_version!,
    }
    await request('/staff/conversations/' + chat.value!.conversation_id + '/messages', 'POST', {
      client_message_id: pendingReply.value.id,
      text: pendingReply.value.text,
      assignment_version: pendingReply.value.assignment,
      expected_epoch: pendingReply.value.epoch,
    })
    pendingReply.value = null
    text.value = ''
    await sync()
  })
}
function clearPending() {
  pendingReply.value = null
  text.value = ''
}
onMounted(() =>
  run(async () => {
    await load()
    if (auth.session?.active_role === 'supervisor')
      assignees.value = (await request<{ items: typeof assignees.value }>('/staff/assignees')).items
  }),
)
onUnmounted(() => closeSocket?.())
</script>
<template>
  <div class="page-heading">
    <h1>客服工作台</h1>
    <div class="actions">
      <RouterLink v-if="auth.session?.active_role === 'supervisor'" to="/experience"
        >处置知识</RouterLink
      ><RouterLink to="/service">售后与运输</RouterLink
      ><button :disabled="busy" @click="run(load)">刷新队列</button>
    </div>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <div class="order-detail-grid">
    <section class="panel">
      <h2>本店队列</h2>
      <p v-if="!rows.length">暂无工单</p>
      <div v-for="row in rows" :key="row.ticket_id" class="list-row">
        <div class="grow">
          <h3>{{ row.summary }}</h3>
          <span>{{ labels[row.status] || row.status }}</span>
          <span v-if="row.sla_overdue" class="error">已超时</span>
        </div>
        <button
          v-if="row.assigned_to_me || auth.session?.active_role === 'supervisor'"
          :disabled="busy"
          @click="select(row)"
        >
          处理
        </button>
      </div>
    </section>
    <section v-if="detail" class="panel">
      <h2>{{ detail.summary }}</h2>
      <p>{{ labels[detail.status] || detail.status }}</p>
      <RouterLink
        v-if="auth.session?.active_role === 'supervisor' && detail.status === 'closed'"
        :to="{ path: '/experience', query: { ticket: detail.ticket_id } }"
        >沉淀处置案例</RouterLink
      >
      <RouterLink v-if="detail.case_ref" to="/service">查看关联业务申请</RouterLink>
      <div v-for="entry in detail.entries" :key="entry.entry_id" class="list-row">
        <span v-if="entry.visibility === 'internal'" class="badge">内部</span>
        <p>{{ entry.text }}</p>
      </div>
      <template v-if="chat"
        ><h3>{{ labels[chat.mode] }}</h3>
        <div class="support-messages" role="log">
          <p v-for="item in chat.items" :key="item.message_id">
            <strong>{{ item.source_role === 'customer' ? '顾客' : '店铺客服' }}：</strong
            >{{ item.text }}
          </p>
        </div>
        <button
          v-if="chat.mode === 'queued' && auth.session?.active_role === 'support'"
          :disabled="busy"
          @click="human()"
        >
          接入服务</button
        ><button
          v-if="chat.mode === 'human' && auth.session?.active_role === 'support'"
          :disabled="busy"
          @click="human(true)"
        >
          结束服务
        </button></template
      >
      <section v-if="detail.related_order">
        <h3>订单 {{ detail.related_order.display_no }}</h3>
        <p>
          {{ detail.related_order.shipping.phone_masked }} ·
          {{ detail.related_order.shipping.address_masked }}
        </p>
      </section>
      <label class="field"
        >处理内容<textarea v-model="text" :readonly="!!pendingReply" rows="3" maxlength="1000" />
      </label>
      <p v-if="pendingReply" role="status">
        请使用原内容重试，或核对会话历史后放弃重试。<button @click="clearPending">
          已核对，放弃重试
        </button>
      </p>
      <div v-if="auth.session?.active_role === 'supervisor'" class="actions">
        <label class="field"
          >分派客服<select v-model="chosen" aria-label="分派客服">
            <option value="">请选择</option>
            <option v-for="staff in assignees" :key="staff.staff_id" :value="staff.staff_id">
              {{ staff.name }}
            </option>
          </select></label
        ><button :disabled="busy || !chosen || !text" @click="assign">确认分派</button>
      </div>
      <div class="actions">
        <button
          v-if="chat?.mode === 'human' && auth.session?.active_role === 'support'"
          class="primary"
          :disabled="busy || !text"
          @click="reply"
        >
          发送会话消息</button
        ><button :disabled="busy || !text" @click="act('reply')">回复工单</button
        ><button :disabled="busy || !text" @click="act('note')">内部备注</button
        ><button :disabled="busy || !text" @click="act('resolve')">解决</button
        ><button :disabled="busy || !text" @click="act('close')">关闭</button
        ><button :disabled="busy || !text" @click="act('escalate')">升级投诉</button>
      </div>
    </section>
  </div>
</template>
<style scoped>
.support-messages {
  max-height: 340px;
  overflow: auto;
  padding: 16px;
  background: var(--surface-soft, #faf7f2);
  margin: 16px 0;
}
.support-messages p {
  white-space: pre-wrap;
  margin: 12px 0;
}
.field {
  display: grid;
  gap: 8px;
  margin: 16px 0;
}
</style>
