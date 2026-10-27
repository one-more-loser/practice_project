import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ActivityPanel from './ActivityPanel.vue'
import { api } from '../api'
import { createDraft } from '../format'
import type { Context, Plan, Snapshot } from '../types'

afterEach(() => { vi.unstubAllGlobals(); api.resetSession() })

it('无实际时保留未知水位及执行门禁，不编造经营结果', async () => {
  const context: Context = { actor_ref: 'owner', store_id: 'shop', display_name: '店长', capabilities: ['metrics.read', 'cost.read', 'plan.edit', 'activity.publish'], csrf_token: 'csrf', mode: 'demo', clock: '2026-10-23T00:00:00+08:00', dependencies: { identity: 'demo', activity: 'not_connected', model: 'disabled' } }
  const snapshot: Snapshot = { snapshot_id: 's1', revision: 1, store_id: 'shop', source_kind: 'synthetic', as_of: context.clock, currency: 'CNY', skus: [], orders: [], refunds: [] }
  const plan: Plan = { plan_id: 'p1', version: 1, status: 'sandbox_confirmed', source_kind: 'synthetic', updated_at: context.clock, draft: createDraft(snapshot, context.clock), evaluation: null, approval: { approval_ref: 'sandbox' } }
  vi.stubGlobal('fetch', async (path: string) => new Response(JSON.stringify({ data: path.endsWith('/plans/p1') ? plan : path.includes('/monitoring?') ? { plan, actual: null, freshness: null, deviations: [], status: 'awaiting_actual' } : { items: path.endsWith('/plans') ? [plan] : [] } })))
  const view = mount(ActivityPanel, { props: { context, initialPlanId: 'p1' } })
  try {
    await flushPromises()
    expect(view.text()).toContain('数据时效：未提供')
    expect(view.text()).toContain('尚无活动实际')
    for (const label of ['发布活动']) {
      expect(view.findAll('button').find(button => button.text() === label)!.attributes('disabled')).toBeDefined()
    }
    expect(view.findAll('button').some(button => ['暂停','恢复','结束'].includes(button.text()))).toBe(false)
  } finally { view.unmount() }
})

