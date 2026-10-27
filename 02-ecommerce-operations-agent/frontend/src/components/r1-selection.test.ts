import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ActivityPanel from './ActivityPanel.vue'
import CapabilityAction from './CapabilityAction.vue'
import { api } from '../api'
import type { Context } from '../types'
import historicalMonitoring from '../../../docs/06-acceptance/XOPS-20261023-01/02-original-acceptance/live-monitoring.json'

afterEach(() => { vi.unstubAllGlobals(); api.resetSession() })
const context: Context = { actor_ref: 'manager', store_id: 'shop', display_name: '店长', capabilities: ['metrics.read','cost.read','plan.edit','plan.confirm','activity.publish','activity.manage'], csrf_token: 'csrf', mode: 'integration', clock: '2026-10-23T00:00:00Z', dependencies: { identity: 'connected', activity: 'configured', model: 'disabled' } }
const a = { plan_id: 'pA', store_id: 'shop', version: 1, status: 'confirmed', approval: { plan_ref: 'pA', plan_version: 1, mode: 'integration', evidence_kind: 'integration', approval_ref: 'approval-A', manifest_digest: 'digest-A', preview: { contract_version: 'XOPS-ACTIVITY-v0.1', store_id: 'shop', plan_id: 'pA', plan_version: 1, manifest_digest: 'digest-A', status: 'passed', evidence_kind: 'integration', preview_ref: 'preview-A' } }, draft: { title: '方案A', monitoring: { interval_minutes: 5 } } }
const b = { ...a, plan_id: 'pB', approval: { ...a.approval, plan_ref: 'pB', approval_ref: 'approval-B', preview: { ...a.approval.preview, plan_id: 'pB', preview_ref: 'preview-B' } }, draft: { ...a.draft, title: '方案B' } }
const json = (data: unknown, status = 200) => new Response(JSON.stringify({ data }), {status})

for (const state of ['loading', 'failed'] as const) {
  it(`切换到B且读取${state}时不得发布旧方案A`, async () => {
    const writes: unknown[] = []
    let release: (() => void) | undefined
    vi.stubGlobal('fetch', async (path: string, init: RequestInit) => {
      if (init.method === 'POST') { writes.push(JSON.parse(String(init.body))); return json({status: 'unknown'}, 202) }
      if (path.endsWith('/plans/pB')) {
        if (state === 'loading') await new Promise<void>(resolve => { release = resolve })
        return new Response(JSON.stringify({error: {code: 'storage_unavailable', message: '读取B失败'}}), {status: 503})
      }
      if (path.endsWith('/plans/pA')) return json(a)
      if (path.includes('/monitoring?')) return json({plan: a, actual: null, freshness: null, deviations: [], status: 'awaiting_actual'})
      return json({items: path.endsWith('/plans') ? [a, b] : []})
    })
    const view = mount(ActivityPanel, { props: {context, initialPlanId: 'pA'} })
    try {
      await flushPromises()
      await view.get('select').setValue('pB')
      await flushPromises()
      const publish = view.findAll('button').find(item => item.text() === '发布活动')!
      await publish.trigger('click')
      await flushPromises()
      // 模拟网络仅在内存中；如收到A的写请求，即违反当前选中B的发布意图。
      expect(writes, `当前选择pB；实际写入=${JSON.stringify(writes)}`).toEqual([])
    } finally { view.unmount(); release?.(); await flushPromises() }
  })
}

it('正常读取A后可以明确发布A', async () => {
  const writes: unknown[] = []
  vi.stubGlobal('fetch', async (path: string, init: RequestInit) => {
    if (init.method === 'POST') {writes.push(JSON.parse(String(init.body))); return json({status: 'unknown'}, 202)}
    return json(path.endsWith('/plans/pA') ? a : path.includes('/monitoring?') ? {plan:a,actual:null,freshness:null,deviations:[],status:'awaiting_actual'} : {items: path.endsWith('/plans') ? [a] : []})
  })
  const view = mount(ActivityPanel, {props: {context,initialPlanId:'pA'}})
  try {
    await flushPromises()
    await view.findAll('button').find(item => item.text() === '发布活动')!.trigger('click')
    await flushPromises()
    expect(writes).toEqual([{plan_id:'pA',plan_version:1,approval_ref:'approval-A',manifest_digest:'digest-A',preview_ref:'preview-A'}])
  } finally {view.unmount()}
})

type TestPlan = typeof a
type Write = { path: string; body: unknown; key: string | null }
function setup(options: {
  detail?: (id: string) => Promise<Response> | Response
  monitored?: (id: string) => TestPlan
  monitoringResponse?: (id: string) => Promise<Response> | Response
  write?: (path: string) => Response
} = {}) {
  const writes: Write[] = []
  vi.stubGlobal('fetch', async (path: string, init: RequestInit) => {
    if (init.method === 'POST') {
      writes.push({ path, body: JSON.parse(String(init.body)), key: new Headers(init.headers).get('Idempotency-Key') })
      return options.write?.(path) ?? json({ status: 'unknown' }, 202)
    }
    const id = path.split('/plans/')[1]
    if (id) return options.detail?.(id) ?? json(id === 'pA' ? a : b)
    if (path.includes('/monitoring?')) {
      const selected = path.endsWith('pA') ? 'pA' : 'pB'
      if (options.monitoringResponse) return options.monitoringResponse(selected)
      return json({ plan: options.monitored?.(selected) ?? (selected === 'pA' ? a : b), actual: null, freshness: null, deviations: [], status: 'awaiting_actual' })
    }
    return json({ items: path.endsWith('/plans') ? [a, b] : [] })
  })
  const view = mount(ActivityPanel, { props: { context, initialPlanId: 'pA' } })
  const activate = async () => {
    // 绕过HTML disabled触发组件事件，验证发布函数自身的防线。
    view.findAllComponents(CapabilityAction).find(action => action.props('label') === '发布活动')!.vm.$emit('activate')
    await flushPromises()
  }
  return { view, writes, activate }
}

it('清空选择同步失效，直接触发发布函数也不发送旧审批', async () => {
  const { view, writes, activate } = setup()
  try {
    await flushPromises(); await view.get('select').setValue('')
    await activate(); expect(writes).toEqual([])
    expect(view.findAll('button').find(button => button.text() === '发布活动')!.attributes('disabled')).toBeDefined()
  } finally { view.unmount() }
})

it('迟到B详情不能覆盖重新选择的A，也不能清除A的加载状态', async () => {
  let releaseB!: (response: Response) => void
  const delayedB = new Promise<Response>(resolve => { releaseB = resolve })
  const { view, writes, activate } = setup({ detail: id => id === 'pB' ? delayedB : json(a) })
  try {
    await flushPromises(); await view.get('select').setValue('pB'); await flushPromises()
    expect(view.get('select').attributes('disabled')).toBeUndefined()
    await view.get('select').setValue('pA'); await flushPromises()
    releaseB(json(b)); await flushPromises()
    await activate()
    expect(writes).toHaveLength(1); expect(writes[0].body).toMatchObject({ plan_id: 'pA', approval_ref: 'approval-A' })
  } finally { view.unmount(); releaseB(json(b)); await flushPromises() }
})

it('迟到失败不污染新选择，B失败后重选A可以发布', async () => {
  let releaseB!: (response: Response) => void
  const delayedB = new Promise<Response>(resolve => { releaseB = resolve })
  const { view, writes, activate } = setup({ detail: id => id === 'pB' ? delayedB : json(a) })
  try {
    await flushPromises(); await view.get('select').setValue('pB'); await flushPromises()
    await view.get('select').setValue('pA'); await flushPromises()
    releaseB(json({ error: 'late failure' }, 503)); await flushPromises()
    expect(view.find('[role="alert"]').exists()).toBe(false)
    await activate(); expect(writes).toHaveLength(1)
  } finally { view.unmount(); releaseB(json({}, 503)); await flushPromises() }
})

const invalidCases: [string, (plan: TestPlan) => void][] = [
  ['详情方案身份不符', plan => { plan.plan_id = 'pB' }],
  ['详情店铺不符', plan => { plan.store_id = 'other' }],
  ['状态未确认', plan => { plan.status = 'evaluated' }],
  ['审批绑定其他方案', plan => { plan.approval.plan_ref = 'pB' }],
  ['审批版本不符', plan => { plan.approval.plan_version = 2 }],
  ['审批编号缺失', plan => { plan.approval.approval_ref = '' }],
  ['审批摘要缺失', plan => { plan.approval.manifest_digest = '' }],
  ['预检编号缺失', plan => { plan.approval.preview.preview_ref = '' }],
  ['预检摘要不符', plan => { plan.approval.preview.manifest_digest = 'other' }],
  ['预检版本不符', plan => { plan.approval.preview.plan_version = 2 }],
  ['预检方案不符', plan => { plan.approval.preview.plan_id = 'pB' }],
  ['预检店铺不符', plan => { plan.approval.preview.store_id = 'other' }],
  ['预检未通过', plan => { plan.approval.preview.status = 'failed' }],
  ['演示审批', plan => { plan.approval.mode = 'demo' }],
  ['替身预检', plan => { plan.approval.preview.evidence_kind = 'contract_stub' }],
]
for (const [name, mutate] of invalidCases) {
  it(`${name}时按钮与发布函数均关闭`, async () => {
    const plan = structuredClone(a); mutate(plan)
    const { view, writes, activate } = setup({ detail: () => json(plan), monitored: () => plan })
    try {
      await flushPromises()
      expect(view.findAll('button').find(button => button.text() === '发布活动')!.attributes('disabled')).toBeDefined()
      await activate(); expect(writes).toEqual([])
    } finally { view.unmount() }
  })
}

for (const mismatch of ['version', 'approval'] as const) {
  it(`监控与详情${mismatch}不一致时直接发布也关闭`, async () => {
    const observed = structuredClone(a)
    if (mismatch === 'version') observed.version = 2
    else observed.approval.approval_ref = 'approval-replaced'
    const { view, writes, activate } = setup({ monitored: () => observed })
    try {
      await flushPromises(); await activate(); expect(writes).toEqual([])
      if (mismatch === 'version') expect(view.text()).toContain('尚无活动实际')
    } finally { view.unmount() }
  })
}

it('未知结果保留原幂等键供同一审批重试', async () => {
  const { view, writes, activate } = setup()
  try {
    await flushPromises(); await activate()
    expect(view.text()).toContain('发布结果待核对')
    await activate(); expect(writes).toHaveLength(2)
    expect(writes[0].key).toBeTruthy(); expect(writes[1].key).toBe(writes[0].key)
    expect(writes[1].body).toEqual(writes[0].body)
  } finally { view.unmount() }
})

it('当前B完整读取后仅发送B的绑定', async () => {
  const { view, writes, activate } = setup()
  try {
    await flushPromises(); await view.get('select').setValue('pB'); await flushPromises(); await activate()
    expect(writes).toHaveLength(1)
    expect(writes[0].body).toMatchObject({ plan_id: 'pB', approval_ref: 'approval-B', preview_ref: 'preview-B' })
  } finally { view.unmount() }
})

it('B加载中及失败后绕过按钮触发仍不发送审批，重新读取后恢复', async () => {
  let releaseB!: (response: Response) => void
  let failed = false
  const pending = new Promise<Response>(resolve => { releaseB = resolve })
  const { view, writes, activate } = setup({ detail: id => id === 'pB' && !failed ? pending : json(id === 'pA' ? a : b) })
  try {
    await flushPromises(); await view.get('select').setValue('pB'); await flushPromises()
    await activate(); expect(writes).toEqual([])
    releaseB(json({}, 503)); await flushPromises(); await activate(); expect(writes).toEqual([])
    failed = true
    await view.findAll('button').find(button => button.text() === '刷新记录')!.trigger('click'); await flushPromises()
    await activate(); expect(writes).toHaveLength(1); expect(writes[0].body).toMatchObject({ plan_id: 'pB' })
  } finally { view.unmount(); releaseB(json({}, 503)); await flushPromises() }
})

it('B监控迟到不能覆盖重新选择的A', async () => {
  let releaseB!: (response: Response) => void
  const pending = new Promise<Response>(resolve => { releaseB = resolve })
  const monitored = (plan: TestPlan) => json({ plan, actual: null, freshness: null, deviations: [], status: 'awaiting_actual' })
  const { view, writes, activate } = setup({ monitoringResponse: id => id === 'pB' ? pending : monitored(a) })
  try {
    await flushPromises(); await view.get('select').setValue('pB'); await flushPromises()
    await view.get('select').setValue('pA'); await flushPromises()
    releaseB(monitored(b)); await flushPromises(); await activate()
    expect(writes).toHaveLength(1); expect(writes[0].body).toMatchObject({ plan_id: 'pA' })
  } finally { view.unmount(); releaseB(monitored(b)); await flushPromises() }
})

it('清空选择后迟到详情不能恢复发布', async () => {
  let releaseB!: (response: Response) => void
  const pending = new Promise<Response>(resolve => { releaseB = resolve })
  const { view, writes, activate } = setup({ detail: id => id === 'pB' ? pending : json(a) })
  try {
    await flushPromises(); await view.get('select').setValue('pB'); await flushPromises()
    await view.get('select').setValue(''); releaseB(json(b)); await flushPromises(); await activate()
    expect(writes).toEqual([]); expect(view.get('select').element.value).toBe('')
  } finally { view.unmount(); releaseB(json(b)); await flushPromises() }
})

it('真实历史响应的完整审批字段可用于确认态正向对照', async () => {
  // 仅复用历史生产响应的审批形状；显式构造confirmed状态，不重放真实发布。
  const plan = { ...structuredClone(historicalMonitoring.plan), status: 'confirmed' }
  const writes: unknown[] = []
  vi.stubGlobal('fetch', async (path: string, init: RequestInit) => {
    if (init.method === 'POST') { writes.push(JSON.parse(String(init.body))); return json({ status: 'unknown' }, 202) }
    return json(path.includes('/monitoring?') ? { ...historicalMonitoring, plan } : path.includes('/plans/') ? plan : { items: path.endsWith('/plans') ? [plan] : [] })
  })
  const view = mount(ActivityPanel, { props: { context: { ...context, store_id: plan.store_id }, initialPlanId: plan.plan_id } })
  try {
    await flushPromises()
    const button = view.findAll('button').find(item => item.text() === '发布活动')!
    expect(button.attributes('disabled')).toBeUndefined(); await button.trigger('click'); await flushPromises()
    expect(writes).toEqual([{ plan_id: plan.plan_id, plan_version: plan.version, approval_ref: plan.approval.approval_ref, manifest_digest: plan.approval.manifest_digest, preview_ref: plan.approval.preview.preview_ref }])
  } finally { view.unmount() }
})

it('暂停与恢复继续发送当前01活动修订和理由', async () => {
  let phase = 'active', revision = 3
  const writes: { path: string; body: unknown }[] = []
  vi.stubGlobal('fetch', async (path: string, init: RequestInit) => {
    if (init.method === 'POST') {
      writes.push({ path, body: JSON.parse(String(init.body)) })
      phase = path.endsWith('/pause') ? 'paused' : 'active'; revision += 1
      return json({ status: 'committed', phase, activity_revision: revision })
    }
    return json({ items: path.endsWith('/plans') ? [] : [{ activity_id: 'act1', phase, activity_revision: revision }] })
  })
  const view = mount(ActivityPanel, { props: { context } })
  try {
    await flushPromises(); await view.get('input').setValue('核对库存')
    for (const label of ['暂停', '恢复']) {
      await view.findAll('button').find(button => button.text() === label)!.trigger('click'); await flushPromises()
    }
    expect(writes).toEqual([
      { path: '/api/v1/shop/activities/act1/pause', body: { expected_revision: 3, reason: '核对库存' } },
      { path: '/api/v1/shop/activities/act1/resume', body: { expected_revision: 4, reason: '核对库存' } },
    ])
  } finally { view.unmount() }
})


