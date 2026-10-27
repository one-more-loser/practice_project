<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElDialog } from 'element-plus'
import { errorText, request } from '../../lib/api'
type Unit = {
  unit_id: string
  title: string
  status: string
  visibility: string
  product_id: string | null
}
type Revision = {
  revision_id: string
  unit_id: string
  revision: number
  status: string
  source_ref: string
  source_revision: string
}
type Faq = {
  faq_id: string
  question: string
  revision: number
  status: string
  synthetic: boolean
}
const content = ref<{ units: Unit[]; revisions: Revision[]; faqs: Faq[] }>({
  units: [],
  revisions: [],
  faqs: [],
})
const error = ref(''),
  notice = ref(''),
  busy = ref(false),
  creating = ref(false),
  kind = ref('knowledge')
const versionUnit = ref('')
const preview = ref<{
  title?: string
  question?: string
  body?: string
  answer?: string
  source_ref: string
  source_revision: string
} | null>(null)
async function inspect(kind: string, ref: string) {
  try {
    preview.value = await request(`/knowledge/admin/${kind}/${ref}`)
  } catch (e) {
    error.value = errorText(e)
  }
}
const form = reactive({
  title: '',
  topic: '店铺政策',
  body: '',
  question: '',
  variants: '',
  source_ref: 'PRD:7.4',
  source_revision: 'REQ-CS-v0.2',
  visibility: 'public',
  product_id: '',
  valid_from: '',
  valid_until: '',
  knowledge_revision_id: '',
})
async function load() {
  content.value = await request('/knowledge')
}
async function execute(action: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
    await load()
    notice.value = '操作已完成'
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
function act(path: string) {
  return execute(() => request('/knowledge/' + path, 'POST', {}))
}
function draft(unit?: Unit) {
  versionUnit.value = unit?.unit_id || ''
  kind.value = 'knowledge'
  form.title = unit?.title || ''
  form.body = ''
  form.product_id = unit?.product_id || ''
  form.visibility = unit?.visibility || 'public'
  form.question = ''
  form.variants = ''
  creating.value = true
  const local = new Date(Date.now() - new Date().getTimezoneOffset() * 60000)
  form.valid_from = local.toISOString().slice(0, 16)
  form.valid_until = ''
}
function faqDraft() {
  draft()
  kind.value = 'faq'
}
async function save() {
  await execute(async () => {
    const validity = {
      valid_from: new Date(form.valid_from).toISOString(),
      valid_until: form.valid_until ? new Date(form.valid_until).toISOString() : null,
    }
    const source = { source_ref: form.source_ref, source_revision: form.source_revision }
    if (kind.value === 'faq') {
      await request('/knowledge/faqs', 'POST', {
        question: form.question,
        variants: form.variants
          .split('\n')
          .map((v) => v.trim())
          .filter(Boolean),
        answer: form.body,
        topic: form.topic,
        ...source,
        ...validity,
        visibility: form.visibility,
        product_id: form.product_id || null,
        knowledge_revision_id: form.knowledge_revision_id || null,
      })
    } else if (versionUnit.value) {
      await request(`/knowledge/units/${versionUnit.value}/revisions`, 'POST', {
        body: form.body,
        ...source,
        ...validity,
      })
    } else {
      await request('/knowledge/units', 'POST', {
        title: form.title,
        topic: form.topic,
        body: form.body,
        ...source,
        ...validity,
        visibility: form.visibility,
        product_id: form.product_id || null,
      })
    }
    creating.value = false
  })
}
onMounted(() => execute(load))
</script>

<template>
  <section class="page knowledge-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">KNOWLEDGE</p>
        <h1>知识与常见问答</h1>
      </div>
      <div class="knowledge-actions">
        <button @click="faqDraft">新增 FAQ</button
        ><button class="primary" @click="draft()">新增知识</button>
      </div>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <h2>知识资料</h2>
    <table>
      <thead>
        <tr>
          <th>名称</th>
          <th>可见范围</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="unit in content.units" :key="unit.unit_id">
          <td>{{ unit.title }}</td>
          <td>{{ unit.visibility === 'public' ? '公开' : '登录顾客' }}</td>
          <td>{{ unit.status }}</td>
          <td>
            <button :disabled="busy" @click="draft(unit)">新建版本</button>
            <button
              :disabled="busy || unit.status !== 'published'"
              @click="act(`units/${unit.unit_id}/retire`)"
            >
              停用
            </button>
          </td>
        </tr>
        <tr v-if="!content.units.length">
          <td colspan="4">暂无知识资料</td>
        </tr>
      </tbody>
    </table>
    <h2>待审与已发布版本</h2>
    <table>
      <thead>
        <tr>
          <th>知识</th>
          <th>版本与来源</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in content.revisions" :key="item.revision_id">
          <td>{{ content.units.find((u) => u.unit_id === item.unit_id)?.title }}</td>
          <td>v{{ item.revision }} · {{ item.source_ref }} / {{ item.source_revision }}</td>
          <td>{{ item.status }}</td>
          <td>
            <button @click="inspect('knowledge', item.revision_id)">查看内容</button>
            <button
              v-if="item.status === 'draft'"
              :disabled="busy"
              @click="act(`revisions/${item.revision_id}/review`)"
            >
              审核通过
            </button>
            <button
              v-if="item.status === 'approved'"
              :disabled="busy"
              @click="act(`revisions/${item.revision_id}/publish`)"
            >
              生成索引并发布
            </button>
          </td>
        </tr>
        <tr v-if="!content.revisions.length">
          <td colspan="4">暂无版本</td>
        </tr>
      </tbody>
    </table>
    <h2>常见问答（FAQ）</h2>
    <table>
      <thead>
        <tr>
          <th>标准问题</th>
          <th>版本</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="faq in content.faqs" :key="faq.faq_id + ':' + faq.revision">
          <td>{{ faq.question }} <span v-if="faq.synthetic" class="muted">合成样本</span></td>
          <td>{{ faq.revision }}</td>
          <td>{{ faq.status }}</td>
          <td>
            <button @click="inspect('faq', faq.faq_id)">查看内容</button>
            <button
              v-if="faq.status === 'draft'"
              :disabled="busy"
              @click="act(`faqs/${faq.faq_id}/review`)"
            >
              审核通过
            </button>
            <button
              v-if="faq.status === 'approved'"
              :disabled="busy"
              @click="act(`faqs/${faq.faq_id}/publish`)"
            >
              发布
            </button>
            <button
              v-if="faq.status === 'published'"
              :disabled="busy"
              @click="act(`faqs/${faq.faq_id}/retire`)"
            >
              停用
            </button>
          </td>
        </tr>
        <tr v-if="!content.faqs.length">
          <td colspan="4">暂无常见问答</td>
        </tr>
      </tbody>
    </table>
    <ElDialog
      v-model="creating"
      :title="kind === 'faq' ? '新增常见问答' : versionUnit ? '新增知识版本' : '新增知识'"
      width="640"
      destroy-on-close
    >
      <form class="knowledge-form" @submit.prevent="save">
        <label v-if="kind === 'faq'"
          >标准问题<input v-model="form.question" required maxlength="600"
        /></label>
        <label v-if="kind === 'faq'"
          >同义问法（每行一条）<textarea v-model="form.variants" rows="2" />
        </label>
        <label v-if="kind === 'knowledge' && !versionUnit"
          >标题<input v-model="form.title" required maxlength="160"
        /></label>
        <label v-if="!versionUnit"
          >主题<input v-model="form.topic" required maxlength="80"
        /></label>
        <label
          >内容<textarea
            v-model="form.body"
            required
            rows="6"
            :maxlength="kind === 'faq' ? 4000 : 12000"
          />
        </label>
        <div class="form-pair">
          <label>来源引用<input v-model="form.source_ref" required maxlength="160" /></label
          ><label>来源版本<input v-model="form.source_revision" required maxlength="80" /></label>
        </div>
        <label v-if="kind === 'faq'"
          >知识来源版本 ID（使用已确认规则时留空）<input
            v-model="form.knowledge_revision_id"
            maxlength="80"
        /></label>
        <div class="form-pair">
          <label
            >生效时间（本地）<input
              v-model="form.valid_from"
              type="datetime-local"
              required /></label
          ><label
            >失效时间（本地，可选）<input v-model="form.valid_until" type="datetime-local"
          /></label>
        </div>
        <template v-if="!versionUnit"
          ><label
            >可见范围<select v-model="form.visibility">
              <option value="public">公开</option>
              <option value="customer">登录顾客</option>
            </select></label
          >
          <label
            >适用商品 ID（通用政策留空）<input v-model="form.product_id" maxlength="80" /></label
        ></template>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <button class="primary" :disabled="busy">{{ busy ? '保存中' : '保存草稿' }}</button>
      </form>
    </ElDialog>
    <ElDialog :model-value="!!preview" title="审核内容" width="600" @close="preview = null">
      <template v-if="preview"
        ><h3>{{ preview.title || preview.question }}</h3>
        <p style="white-space: pre-wrap">{{ preview.body || preview.answer }}</p>
        <p class="muted">{{ preview.source_ref }} · {{ preview.source_revision }}</p></template
      >
    </ElDialog>
  </section>
</template>

<style scoped>
.knowledge-actions {
  display: flex;
  gap: 12px;
}
.knowledge-page h2 {
  margin-top: 40px;
}
.knowledge-form {
  display: grid;
  gap: 16px;
}
.knowledge-form label {
  display: grid;
  gap: 8px;
}
.knowledge-form input,
.knowledge-form textarea,
.knowledge-form select {
  padding: 10px;
  border: 1px solid #bfb4a8;
  border-radius: 8px;
  font: inherit;
  width: 100%;
}
.form-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
</style>
