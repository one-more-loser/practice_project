import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import PlanPanel from './PlanPanel.vue'
import { api } from '../api'
import { createDraft } from '../format'
import type { Context, Plan, Snapshot } from '../types'

afterEach(() => { vi.unstubAllGlobals(); api.resetSession() })
describe('审批撤销请求', () => {
  it('提交当前版本、理由及安全头；待终态时禁重复撤销且不宣称活动停止', async () => {
    const context: Context = { actor_ref: 'owner', store_id: 'shop', display_name: '店长', capabilities: ['metrics.read', 'cost.read', 'plan.edit', 'plan.confirm'], csrf_token: 'csrf-cancel', mode: 'integration', clock: '2026-10-23T00:00:00+08:00', dependencies: { identity: 'connected', activity: 'configured', model: 'disabled' } }
    const snapshot: Snapshot = { snapshot_id: 's1', revision: 1, store_id: 'shop', source_kind: 'synthetic', as_of: context.clock, currency: 'CNY', skus: [], orders: [], refunds: [] }
    const plan: Plan = { plan_id: 'p1', version: 3, status: 'confirmed', source_kind: 'synthetic', updated_at: context.clock, draft: createDraft(snapshot, context.clock), evaluation: null, approval: { approval_ref: 'a1' }, validity: { snapshot_current: true, latest_snapshot_revision: 1, execution_frozen: true, note: '执行假设已冻结' } }
    const writes: RequestInit[] = []
    const reconciliations: RequestInit[] = []
    vi.stubGlobal('fetch', async (path: string, init: RequestInit) => {
      let data: unknown = { items: [plan] }
      if (path.endsWith('/plans/p1')) data = plan
      if (path.endsWith('/cancel-approval')) { writes.push(init); data = { ...plan, status: 'cancel_pending' } }
      if (path.endsWith('/reconcile-execution')) { reconciliations.push(init); data = { ...plan, status: 'active' } }
      return new Response(JSON.stringify({ data }))
    })
    api.csrfToken = context.csrf_token
    const view = mount(PlanPanel, { props: { context, snapshots: [snapshot], snapshotId: 's1' }, global: { stubs: { PlanEditor: true, EvaluationPanel: true } } })
    await flushPromises(); await view.get('.plan-list-item').trigger('click'); await flushPromises()
    const cancelButton = () => view.findAll('button').find(button => button.text() === '申请撤销审批')!
    expect(cancelButton().attributes('disabled')).toBeDefined()
    await view.get('.confirmation-section textarea').setValue('库存变动，申请关闭审批')
    await cancelButton().trigger('click'); await flushPromises()
    expect(writes).toHaveLength(1)
    expect(JSON.parse(writes[0]!.body as string)).toEqual({ expected_version: 3, reason: '库存变动，申请关闭审批' })
    expect(new Headers(writes[0]!.headers).get('X-CSRF-Token')).toBe('csrf-cancel')
    expect(new Headers(writes[0]!.headers).get('Idempotency-Key')).toBeTruthy()
    expect(view.text()).toContain('撤销请求已记录；以执行终态为准，已提交活动需在01暂停/结束')
    expect(view.text()).toContain('尚不能确认无执行效果')
    expect(cancelButton().attributes('disabled')).toBeDefined()
    await cancelButton().trigger('click'); await flushPromises()
    expect(writes).toHaveLength(1)
    await view.findAll('button').find(button => button.text() === '核对未决执行')!.trigger('click'); await flushPromises()
    expect(reconciliations).toHaveLength(1)
    expect(JSON.parse(reconciliations[0]!.body as string)).toEqual({ expected_version: 3 })
    expect(view.find('.detail-heading .badge').text()).toBe('进行中')
    expect(view.findComponent({ name: 'PlanEditor' }).props('canEdit')).toBe(false)
    expect(view.text()).not.toContain('审批已撤销')
    view.unmount()
  })
})
