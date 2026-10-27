<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { ApiError, errorText, request, setGuestSession } from '../../lib/api'
import { useAuth } from '../../stores/auth'
import { subscribe } from './realtime'
import ActionConfirm from '../../components/ActionConfirm.vue'
import type { Proposal } from '../../lib/types'

type Citation = { kind: string; reference_id: string; title: string; revision: number }
type Message = { message_id: string; source_role: string; text: string; citations: Citation[] }
type Conversation = {
  conversation_id: string
  mode: string
  mode_epoch: number
  last_seq: number
  version: number
}
type EventView = {
  seq: number
  type: string
  mode_epoch: number
  data: { run_id?: string; code?: string }
}
const auth = useAuth()
const messages = ref<Message[]>([]),
  input = ref(''),
  error = ref(''),
  connection = ref('连接中')
const proposals = ref<Proposal[]>([]),
  selected = ref<Proposal | null>(null)
async function feedback(message_id: string, rating: string) {
  if (!current.value) return
  try {
    await request(`/conversations/${current.value.conversation_id}/feedback`, 'POST', {
      message_id,
      rating,
      topics: ['general'],
    })
    error.value = ''
    rated.value[message_id] = true
  } catch (e) {
    error.value = errorText(e)
  }
}
const rated = ref<Record<string, boolean>>({})
async function proposalDone() {
  selected.value = null
  await history(generation)
}
const current = ref<Conversation | null>(null),
  pendingRun = ref(''),
  sending = ref(false)
const source = ref<{
  title?: string
  question?: string
  body: string
  source_ref: string
  source_revision: string
} | null>(null)
const scrollArea = ref<HTMLElement | null>(null)
let events: EventSource | null = null,
  cursor = 0,
  generation = 0,
  mounted = true
let closeHuman: (() => void) | undefined
let historyRequest = 0
const retry = ref<{ id: string; text: string } | null>(null)
const enabled = computed(() => !auth.session || auth.session.active_role === 'customer')
const identity = computed(() =>
  auth.session
    ? `${auth.session.subject_id}:${auth.session.active_role}:${auth.session.auth_version}`
    : 'guest',
)
const storage = computed(() => 'cs.conversation.v1:' + identity.value)

async function history(token: number) {
  if (!current.value) return
  const ref = current.value.conversation_id,
    sequence = ++historyRequest
  const [response, pending] = await Promise.all([
    request<{ items: Message[] }>(`/conversations/${ref}/messages?limit=50`),
    request<{ items: Proposal[] }>(`/conversations/${ref}/action-proposals`),
  ])
  if (
    generation !== token ||
    !mounted ||
    sequence !== historyRequest ||
    current.value?.conversation_id !== ref
  )
    return
  messages.value = response.items
  proposals.value = pending.items
  await nextTick()
  scrollArea.value?.scrollTo({ top: scrollArea.value.scrollHeight, behavior: 'instant' })
}
function disconnect() {
  events?.close()
  events = null
  closeHuman?.()
  closeHuman = undefined
}
async function syncService() {
  if (!current.value) return
  const token = generation,
    ref = current.value.conversation_id
  const value = await request<Conversation>(`/conversations/${ref}`)
  if (token !== generation || !mounted || current.value?.conversation_id !== ref) return
  if (value.version >= current.value.version) current.value = value
  if (current.value.mode !== 'agent') pendingRun.value = ''
  await history(generation)
}
async function human() {
  if (!current.value) return
  sending.value = true
  error.value = ''
  try {
    await syncService()
    const row = current.value
    await request(
      `/conversations/${row.conversation_id}/${row.mode === 'queued' ? 'cancel-human-request' : 'human-requests'}`,
      'POST',
      {
        expected_epoch: row.mode_epoch,
        expected_version: row.version,
        ...(row.mode === 'agent' ? { reason: input.value.trim() || '请协助处理我的咨询' } : {}),
      },
    )
    pendingRun.value = ''
    await syncService()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    sending.value = false
  }
}
function connect(token: number) {
  if (!current.value) return
  disconnect()
  const ref = current.value.conversation_id
  const stream = new EventSource(`/api/v1/conversations/${ref}/events?after_seq=${cursor}`, {
    withCredentials: true,
  })
  events = stream
  stream.onopen = () => {
    if (token === generation) connection.value = '已连接'
  }
  stream.onerror = async () => {
    if (token !== generation || !mounted) return
    connection.value = '连接中断，正在重连；已接收的任务会继续处理'
    try {
      await request(`/conversations/${ref}`)
    } catch (e) {
      if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
        disconnect()
        error.value = '会话已失效，请重新打开客服窗口。'
        pendingRun.value = ''
      }
    }
  }
  const receive = async (event: MessageEvent) => {
    if (token !== generation || !mounted) return
    try {
      const row: EventView = JSON.parse(event.data)
      if (row.seq <= cursor) return
      cursor = row.seq
      if (current.value && row.mode_epoch < current.value.mode_epoch) return
      if (current.value) current.value.mode_epoch = row.mode_epoch
      if (row.type === 'run.started' || row.type === 'message.accepted')
        pendingRun.value = row.data.run_id || ''
      if (['run.completed', 'run.failed', 'run.cancelled', 'run.superseded'].includes(row.type)) {
        if (pendingRun.value === row.data.run_id) pendingRun.value = ''
        if (row.type === 'run.failed')
          error.value =
            row.data.code === 'model_unconfigured'
              ? '咨询服务尚未配置；您仍可从订单和余额页面查看本人记录。'
              : '本次咨询未完成，请稍后重试。'
        await history(token)
      }
      if (row.type === 'answer.completed') await history(token)
      if (row.type === 'message.accepted') await history(token)
      if (row.type === 'service.mode_changed') await syncService()
    } catch {
      error.value = '会话更新暂不可用，请重新打开窗口核对历史记录。'
    }
  }
  for (const type of [
    'message.accepted',
    'run.started',
    'answer.completed',
    'run.completed',
    'run.failed',
    'run.cancelled',
    'run.superseded',
    'service.mode_changed',
  ])
    stream.addEventListener(type, (event) => {
      void receive(event as MessageEvent)
    })
  stream.addEventListener('stream.reset', () => {
    if (token !== generation) return
    disconnect()
    error.value = '会话需要重新同步，请关闭后重新打开。'
  })
}
async function initialize() {
  const token = ++generation
  disconnect()
  messages.value = []
  proposals.value = []
  selected.value = null
  rated.value = {}
  current.value = null
  pendingRun.value = ''
  error.value = ''
  retry.value = null
  sending.value = false
  if (!enabled.value) return
  try {
    if (!auth.session) {
      let guest: { csrf_token: string }
      try {
        guest = await request('/guest-session')
      } catch (e) {
        if (!(e instanceof ApiError) || e.status !== 401) throw e
        guest = await request('/guest-session', 'POST')
      }
      if (token !== generation) return
      setGuestSession(guest.csrf_token)
    }
    let value: Conversation | null = null
    const saved = sessionStorage.getItem(storage.value)
    if (saved && /^[A-Za-z0-9_]+$/.test(saved)) {
      try {
        value = await request<Conversation>(`/conversations/${saved}`)
      } catch (e) {
        if (!(e instanceof ApiError) || ![404, 409].includes(e.status)) throw e
      }
    }
    if (!value) value = await request<Conversation>('/conversations', 'POST', {})
    if (token !== generation || !mounted) return
    current.value = value
    sessionStorage.setItem(storage.value, value.conversation_id)
    // 先读历史，再从0补偿事件；编号去重，关闭窗口不取消任务。
    cursor = 0
    await history(token)
    connect(token)
    if (auth.session)
      closeHuman = subscribe(value.conversation_id, value.mode_epoch, syncService, (s) => {
        error.value = s
        disconnect()
      })
  } catch (e) {
    if (token === generation) error.value = errorText(e)
  }
}
async function send() {
  if (!current.value || sending.value || pendingRun.value || (!retry.value && !input.value.trim()))
    return
  const token = generation,
    path = `/conversations/${current.value.conversation_id}/messages`
  retry.value ??= { id: 'web_' + crypto.randomUUID().replaceAll('-', ''), text: input.value }
  sending.value = true
  error.value = ''
  try {
    const result = await request<{ run_id: string | null }>(path, 'POST', {
      client_message_id: retry.value.id,
      text: retry.value.text,
    })
    if (token !== generation) return
    pendingRun.value = result.run_id || ''
    retry.value = null
    input.value = ''
    await history(token)
  } catch (e) {
    if (token !== generation) return
    error.value = errorText(e)
    if (e instanceof ApiError && e.status >= 400 && e.status < 500) retry.value = null
  } finally {
    if (token === generation) sending.value = false
  }
}
async function cancel() {
  if (!pendingRun.value) return
  try {
    await request(`/runs/${pendingRun.value}/cancel`, 'POST', {})
    pendingRun.value = ''
    error.value = ''
    await history(generation)
  } catch (e) {
    error.value = errorText(e)
  }
}
async function openSource(citation: Citation) {
  try {
    source.value = await request(`/knowledge/references/${citation.kind}/${citation.reference_id}`)
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(initialize)
watch(identity, initialize)
onUnmounted(() => {
  mounted = false
  generation++
  disconnect()
  retry.value = null
})
</script>

<template>
  <section class="chat-panel" aria-label="店铺客服会话">
    <p v-if="!enabled" class="muted">请切换至顾客身份使用咨询服务。</p>
    <template v-else>
      <div v-if="auth.session" class="actions">
        <span v-if="current?.mode === 'queued'" role="status"
          >等待客服接入，暂未接通；工单已保留，可取消等待后查看服务记录</span
        ><span v-if="current?.mode === 'human'" role="status">人工服务中</span
        ><button
          v-if="current?.mode !== 'human'"
          :disabled="sending || !current"
          class="text-button"
          @click="human"
        >
          {{ current?.mode === 'queued' ? '取消等待' : '联系人工' }}</button
        ><RouterLink to="/service">我的服务</RouterLink>
      </div>
      <div
        ref="scrollArea"
        class="chat-history"
        role="log"
        aria-live="polite"
        aria-label="咨询记录"
      >
        <p v-if="!messages.length" class="chat-welcome">您好，有什么可以帮您？</p>
        <article
          v-for="item in messages"
          :key="item.message_id"
          :class="['chat-message', item.source_role]"
        >
          <span class="visually-hidden"
            >{{ item.source_role === 'customer' ? '我' : '店铺客服' }}：</span
          >
          <p>{{ item.text }}</p>
          <div
            v-if="auth.session && ['assistant', 'support'].includes(item.source_role)"
            class="actions"
          >
            <button
              class="text-button"
              :disabled="rated[item.message_id]"
              @click="feedback(item.message_id, 'helpful')"
            >
              有帮助</button
            ><button
              class="text-button"
              :disabled="rated[item.message_id]"
              @click="feedback(item.message_id, 'unhelpful')"
            >
              未解决
            </button>
          </div>
          <div v-if="item.citations?.length" class="chat-citations">
            <button
              v-for="(citation, index) in item.citations"
              :key="citation.reference_id"
              class="text-button"
              @click="openSource(citation)"
            >
              [{{ index + 1 }}] {{ citation.title }}
            </button>
          </div>
        </article>
      </div>
      <p v-if="pendingRun" role="status">
        正在处理 <button class="text-button" @click="cancel">停止本次回答</button>
      </p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button v-for="p in proposals" :key="p.confirmation_id" @click="selected = p">
        核对操作摘要
      </button>
      <form class="chat-compose" @submit.prevent="send">
        <label for="chat-input" class="visually-hidden">咨询内容</label>
        <textarea
          id="chat-input"
          v-model="input"
          placeholder="输入商品、政策或本人订单问题"
          rows="2"
          maxlength="4000"
          :readonly="!!retry"
          :disabled="sending || !!pendingRun"
        />
        <button
          class="primary"
          :disabled="!current || sending || !!pendingRun || (!input.trim() && !retry)"
        >
          {{ sending ? '发送中' : retry ? '原内容重试' : '发送' }}
        </button>
      </form>
      <p class="chat-connection" role="status">{{ connection }}</p>
    </template>
    <ElDialog
      :model-value="!!source"
      title="资料依据"
      width="560"
      append-to-body
      @close="source = null"
    >
      <template v-if="source"
        ><h3>{{ source.title || source.question }}</h3>
        <p class="source-body">{{ source.body }}</p>
        <p class="muted">{{ source.source_ref }} · {{ source.source_revision }}</p></template
      >
    </ElDialog>
    <ActionConfirm
      :proposal="selected"
      title="提交操作"
      @done="proposalDone"
      @close="selected = null"
    />
  </section>
</template>

<style scoped>
.chat-history {
  height: 350px;
  overflow-y: auto;
  padding: 8px 4px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.chat-message {
  align-self: flex-start;
  padding: 12px 16px;
  background: #f6f2ec;
  border-radius: 4px 16px 16px;
  max-width: 94%;
}
.chat-message.customer {
  align-self: flex-end;
  background: #ffeadb;
  border-radius: 16px 4px 16px 16px;
}
.chat-message p,
.source-body {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 0;
  line-height: 1.65;
}
.chat-compose {
  display: flex;
  gap: 8px;
  margin-top: 16px;
  align-items: end;
}
.chat-compose textarea {
  flex: 1;
  min-width: 0;
  resize: vertical;
  padding: 12px;
  border: 1px solid #bfb4a8;
  border-radius: 12px;
  font: inherit;
}
.chat-compose button {
  min-height: 48px;
}
.chat-welcome {
  margin: auto;
  color: #706454;
}
.chat-connection {
  font-size: 12px;
  color: #706454;
  margin-bottom: 0;
}
.chat-citations {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
  align-items: start;
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}
</style>
