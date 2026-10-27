<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { request, errorText } from '../../lib/api'
type Document = {
  document_id: string
  name: string
  kind: string
  topic: string
  status: string
  revision: number
  definition: Record<string, string | string[]>
}
const route = useRoute(),
  rows = ref<Document[]>([]),
  error = ref(''),
  busy = ref(false),
  name = ref(''),
  topic = ref('order'),
  sourceTicket = ref(String(route.query.ticket || '')),
  selected = ref<string[]>([]),
  kind = ref('case'),
  conditions = ref(''),
  exclusions = ref(''),
  steps = ref(''),
  summary = ref(''),
  handoff = ref(''),
  tools = ref<string[]>([]),
  chosen = ref<Document | null>(null),
  week = ref(''),
  snapshot = ref<{
    counts: { family: string; cells: { topic: string; count: number | null }[] }[]
  } | null>(null)
const familyLabels: Record<string, string> = {
  consultations: '咨询',
  feedback: '反馈',
  tickets: '工单',
  human_handoffs: '人工接管',
  tool_failures: '工具失败',
}
const toolNames = [
  'catalog.read',
  'orders.list',
  'orders.detail',
  'shipments.list',
  'wallet.summary',
  'knowledge.search',
  'sql.query',
]
const labels: Record<string, string> = {
  candidate: '待审核',
  approved: '已审核',
  published: '已发布',
  retired: '已停用',
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
async function load() {
  rows.value = (await request<{ items: Document[] }>('/staff/experience')).items
}
const lines = (value: string) =>
  value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
async function create() {
  await run(async () => {
    await request('/staff/experience', 'POST', {
      kind: kind.value,
      name: name.value,
      topic: topic.value,
      ...(kind.value === 'case'
        ? { source_ticket_id: sourceTicket.value }
        : { source_case_ids: selected.value }),
      definition: {
        conditions: lines(conditions.value),
        exclusions: lines(exclusions.value),
        verification_steps: lines(steps.value),
        resolution_summary: summary.value,
        allowed_tools: tools.value,
        handoff_conditions: lines(handoff.value),
      },
    })
    await load()
  })
}
async function govern(row: Document, action: string) {
  await run(async () => {
    await request(`/staff/experience/${row.document_id}/${action}`, 'POST')
    await load()
  })
}
async function generate() {
  await run(async () => {
    await request('/staff/experience/generate-skill', 'POST', { source_case_ids: selected.value })
    await load()
  })
}
async function generateCase() {
  await run(async () => {
    await request('/staff/experience/generate-case', 'POST', {
      source_ticket_id: sourceTicket.value,
    })
    await load()
  })
}
async function aggregate() {
  await run(async () => {
    snapshot.value = await request('/staff/support-summary', 'POST', {
      week_start: week.value + 'T00:00:00+08:00',
    })
  })
}
onMounted(() => run(load))
</script>
<template>
  <div class="page-heading">
    <h1>处置知识</h1>
    <RouterLink to="/workbench">返回工作台</RouterLink>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <div class="order-detail-grid">
    <section class="panel">
      <h2>案例与处置 Skill</h2>
      <p v-if="!rows.length">暂无案例</p>
      <article v-for="row in rows" :key="row.document_id" class="experience-item">
        <h3>
          {{ row.name }} <span class="badge">{{ labels[row.status] }}</span>
        </h3>
        <p>{{ row.kind === 'case' ? '案例' : '处置 Skill' }} · 第{{ row.revision }}版</p>
        <div class="actions">
          <button @click="chosen = row">查看</button
          ><button
            v-if="row.status === 'candidate'"
            :disabled="busy"
            @click="govern(row, 'review')"
          >
            审核通过</button
          ><button
            v-if="row.status === 'approved' && row.kind === 'skill'"
            :disabled="busy"
            @click="govern(row, 'regress')"
          >
            校验权限边界</button
          ><button
            v-if="row.status === 'approved'"
            :disabled="busy"
            @click="govern(row, 'publish')"
          >
            发布</button
          ><button
            v-if="row.status === 'published'"
            :disabled="busy"
            @click="govern(row, 'retire')"
          >
            停用</button
          ><button
            v-if="['published', 'retired'].includes(row.status)"
            :disabled="busy"
            @click="govern(row, 'rollback')"
          >
            恢复为新候选
          </button>
        </div>
      </article>
      <section v-if="chosen">
        <h3>{{ chosen.name }}</h3>
        <p v-for="(value, key) in chosen.definition" :key="key">
          {{ Array.isArray(value) ? value.join('；') : value }}
        </p>
      </section>
    </section>
    <section class="panel">
      <h2>新建候选</h2>
      <form @submit.prevent="create">
        <label class="field"
          >类型<select v-model="kind">
            <option value="case">处置案例</option>
            <option value="skill">处置 Skill</option>
          </select></label
        ><label class="field">名称<input v-model="name" maxlength="160" required /></label
        ><label class="field"
          >主题<select v-model="topic">
            <option value="order">订单</option>
            <option value="aftersale">售后</option>
            <option value="transport">运输</option>
            <option value="complaint">投诉</option>
            <option value="general">一般咨询</option>
            <option value="knowledge_gap">知识缺口</option>
          </select></label
        ><label v-if="kind === 'case'" class="field"
          >已关闭工单编号<input v-model="sourceTicket" required
        /></label>
        <button
          v-if="kind === 'case'"
          type="button"
          :disabled="busy || !sourceTicket"
          @click="generateCase"
        >
          从处理过程生成案例候选
        </button>
        <fieldset v-if="kind === 'skill'">
          <legend>来源案例</legend>
          <label
            v-for="row in rows.filter((v) => v.kind === 'case' && v.status === 'published')"
            :key="row.document_id"
            ><input v-model="selected" type="checkbox" :value="row.document_id" />{{
              row.name
            }}</label
          ><button type="button" :disabled="busy || !selected.length" @click="generate">
            从案例生成 Skill 候选
          </button>
        </fieldset>
        <label class="field">适用条件<textarea v-model="conditions" rows="2" required /></label
        ><label class="field">排除条件<textarea v-model="exclusions" rows="2" required /></label
        ><label class="field">核对步骤<textarea v-model="steps" rows="3" required /></label
        ><label class="field">处置过程<textarea v-model="summary" rows="3" required /></label
        ><label class="field">转人工条件<textarea v-model="handoff" rows="2" required /></label>
        <fieldset>
          <legend>可参考的查询工具</legend>
          <label v-for="tool in toolNames" :key="tool"
            ><input v-model="tools" type="checkbox" :value="tool" />{{ tool }}</label
          >
        </fieldset>
        <button class="primary" :disabled="busy">保存候选</button>
      </form>
    </section>
  </div>
  <section class="panel aggregate">
    <h2>周度服务摘要</h2>
    <div class="actions">
      <label>起始周一（北京时间） <input v-model="week" type="date" /></label
      ><button :disabled="busy || !week" @click="aggregate">生成已结束周摘要</button>
    </div>
    <div v-if="snapshot" class="aggregate-grid">
      <div v-for="family in snapshot.counts" :key="family.family">
        <h3>{{ familyLabels[family.family] }}</h3>
        <p v-for="cell in family.cells" :key="cell.topic">
          {{ cell.topic }}：{{ cell.count === null ? '样本不足，不展示' : cell.count }}
        </p>
      </div>
    </div>
  </section>
</template>
<style scoped>
.field {
  display: grid;
  gap: 8px;
  margin: 16px 0;
}
fieldset {
  border: 1px solid #ddd5c9;
  border-radius: 12px;
  margin: 16px 0;
  padding: 16px;
}
fieldset label {
  display: block;
  margin: 8px 0;
}
.experience-item {
  padding: 20px 0;
  border-bottom: 1px solid #e8e1d7;
}
.aggregate {
  margin-top: 24px;
}
.aggregate-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 24px;
  margin-top: 20px;
}
</style>
