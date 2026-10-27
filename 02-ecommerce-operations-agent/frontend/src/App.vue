<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, ApiError, errorMessage } from './api'
import type { Context, Plan, Snapshot } from './types'
import DashboardPanel from './components/DashboardPanel.vue'
import SnapshotPanel from './components/SnapshotPanel.vue'
import PlanPanel from './components/PlanPanel.vue'
import ActivityPanel from './components/ActivityPanel.vue'
const context = ref<Context | null>(null), snapshots = ref<Snapshot[]>([]), selectedId = ref(''), currentPlan = ref<Plan | null>(null)
const page = ref('dashboard'), busy = ref(false), error = ref(''), dirty = ref(false)
const bootstrap = ref<{ mode: 'demo' | 'integration'; demo_available: boolean; login_url: string } | null>(null)
const tabs = [{ id: 'dashboard', title: '经营概览', icon: '◫' }, { id: 'plans', title: '运营方案', icon: '▤' }, { id: 'activities', title: '活动与复盘', icon: '◷' }, { id: 'snapshots', title: '数据快照', icon: '▦' }]
const selected = computed(() => snapshots.value.find(s => s.snapshot_id === selectedId.value))
async function loadSnapshots() {
  snapshots.value = (await api.request<{items: Snapshot[]}>('/snapshots')).items
  if (!snapshots.value.some(s => s.snapshot_id === selectedId.value)) selectedId.value = snapshots.value[0]?.snapshot_id ?? ''
}
async function acceptContext(value: Context) { context.value = value; api.csrfToken = value.csrf_token; await loadSnapshots() }
async function initialize() {
  busy.value = true; error.value = ''
  try {
    bootstrap.value = await api.request('/bootstrap')
    try { await acceptContext(await api.request<Context>('/context')) }
    catch (e) { if (!(e instanceof ApiError && e.status === 401)) throw e }
  } catch (e) { error.value = errorMessage(e) } finally { busy.value = false }
}
async function login(role: string) {
  if (!bootstrap.value?.demo_available || busy.value) return
  busy.value = true; error.value = ''; api.resetSession(); snapshots.value = []; currentPlan.value = null; selectedId.value = ''
  try { await acceptContext(await api.request<Context>('/demo/session', { method: 'POST', body: { role } })); page.value = 'dashboard' }
  catch (e) { context.value = null; error.value = errorMessage(e) } finally { busy.value = false }
}
function navigate(id: string) { if (dirty.value) { error.value = '当前方案有未保存修改，请先保存或恢复内容。'; return } page.value = id; error.value = '' }
onMounted(initialize)
</script>
<template>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <div v-if="!context" class="welcome"><div class="wordmark"><span class="brand-mark">营</span> 店铺经营</div><h1>从数据走向经营决策</h1><p>查看经营状态，编制方案，核对执行条件。</p><p v-if="busy" role="status">正在检查会话…</p><p v-if="error" role="alert" class="feedback error">{{ error }}</p><div v-if="bootstrap?.demo_available" class="demo-login"><span class="badge warning">本机演示 · 模拟数据</span><p>选择演示角色进入工作台。</p><div class="button-row"><button class="primary" :disabled="busy" @click="login('owner')">以店长查看</button><button :disabled="busy" @click="login('operator')">以运营查看</button><button :disabled="busy" @click="login('viewer')">以查看者查看</button></div></div><a v-else-if="bootstrap" class="button primary" :href="bootstrap.login_url">前往统一登录</a><button v-if="error" @click="initialize">重新连接</button></div>
  <div v-else class="app-shell">
    <aside class="sidebar"><a href="#" class="wordmark" @click.prevent="navigate('dashboard')"><span class="brand-mark">营</span> 店铺经营</a><nav aria-label="经营导航"><button v-for="tab in tabs" :key="tab.id" :class="{ active: page === tab.id }" :aria-current="page === tab.id ? 'page' : undefined" @click="navigate(tab.id)"><span aria-hidden="true">{{ tab.icon }}</span>{{ tab.title }}</button></nav><div class="sidebar-bottom"><span class="identity-avatar">{{ context.display_name.slice(0, 1) }}</span><div><strong>{{ context.display_name }}</strong><small>{{ context.store_id }}</small></div></div></aside>
    <div class="content-shell"><header class="topbar"><span>{{ tabs.find(t => t.id === page)?.title }}</span><div class="topbar-actions"><span class="badge" :class="context.mode === 'demo' ? 'warning' : ''">{{ context.mode === 'demo' ? '演示模式' : '真实接入模式' }}</span><select v-if="bootstrap?.demo_available" aria-label="切换演示角色" :disabled="busy || dirty" :value="context.capabilities.includes('plan.confirm') ? 'owner' : context.capabilities.includes('plan.edit') ? 'operator' : 'viewer'" @change="login(($event.target as HTMLSelectElement).value)"><option value="owner">店长</option><option value="operator">运营</option><option value="viewer">查看者</option></select></div></header>
      <main id="main" tabindex="-1"><div class="context-bar"><label>分析快照<select v-model="selectedId" :disabled="dirty"><option v-if="!snapshots.length" value="">暂无快照</option><option v-for="s in snapshots" :key="s.snapshot_id" :value="s.snapshot_id">{{ s.snapshot_id }} · v{{ s.revision }}</option></select></label><span v-if="selected" class="muted">{{ selected.source_kind === 'synthetic' ? '模拟数据' : '导入数据' }}</span><span class="connection-state">01 活动{{ context.dependencies.activity === 'not_connected' ? '未接入' : context.dependencies.activity === 'contract_stub' ? '契约替身' : '已配置 · 待预检' }}</span></div><p v-if="error" role="alert" class="feedback error">{{ error }}</p>
        <DashboardPanel v-if="page === 'dashboard'" :context="context" :snapshot-id="selectedId" />
        <PlanPanel v-else-if="page === 'plans'" :context="context" :snapshots="snapshots" :snapshot-id="selectedId" @updated="currentPlan = $event" @dirty="dirty = $event" />
        <ActivityPanel v-else-if="page === 'activities'" :context="context" :initial-plan-id="currentPlan?.plan_id" />
        <SnapshotPanel v-else :context="context" :snapshots="snapshots" :selected-id="selectedId" @select="selectedId = $event" @imported="loadSnapshots" />
      </main><footer class="app-footer">经营判断由快照与约束支撑 · 活动事实以 01 店铺系统为准</footer>
    </div>
  </div>
</template>
