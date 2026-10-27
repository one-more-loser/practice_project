import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import App from './App.vue'
import PlanPanel from './components/PlanPanel.vue'
import DashboardPanel from './components/DashboardPanel.vue'
import EvaluationPanel from './components/EvaluationPanel.vue'
import { api } from './api'
import type { Context } from './types'

const viewer: Context = { actor_ref: 'viewer', store_id: 'shop', display_name: '查看者', capabilities: ['metrics.read'], csrf_token: 'csrf', mode: 'demo', clock: '2026-10-23T00:00:00+08:00', dependencies: { identity: 'demo', activity: 'not_connected', model: 'disabled' } }
afterEach(() => { vi.unstubAllGlobals(); api.resetSession() })
const response = (data: unknown, status = 200) => new Response(JSON.stringify(status === 200 ? { data } : { error: { code: 'unauthenticated', message: '请登录' } }), { status })
const shallowPanels = { DashboardPanel: true, SnapshotPanel: true, PlanPanel: true, ActivityPanel: true }

describe('启动会话门禁', () => {
  it('真实接入未登录时只显示统一登录，不调用演示登录', async () => {
    const paths: string[] = []
    vi.stubGlobal('fetch', async (path: string) => { paths.push(path); return path.endsWith('/bootstrap') ? response({ mode: 'integration', demo_available: false, login_url: '/login' }) : response(null, 401) })
    const view = mount(App, { global: { stubs: shallowPanels } }); await flushPromises()
    expect(view.get('a.button').attributes('href')).toBe('/login')
    expect(view.text()).not.toContain('以店长查看')
    expect(paths).toEqual(['/api/ops/v1/bootstrap', '/api/ops/v1/context'])
    view.unmount()
  })
  it('演示可用时先等待显式角色选择，再读取获准快照', async () => {
    const paths: string[] = []
    vi.stubGlobal('fetch', async (path: string) => {
      paths.push(path)
      if (path.endsWith('/bootstrap')) return response({ mode: 'demo', demo_available: true, login_url: '/login' })
      if (path.endsWith('/context')) return response(null, 401)
      if (path.endsWith('/demo/session')) return response(viewer)
      return response({ items: [] })
    })
    const view = mount(App, { global: { stubs: shallowPanels } }); await flushPromises()
    expect(paths.some(path => path.endsWith('/demo/session'))).toBe(false)
    await view.findAll('button').find(b => b.text() === '以查看者查看')!.trigger('click'); await flushPromises()
    expect(paths.slice(-2)).toEqual(['/api/ops/v1/demo/session', '/api/ops/v1/snapshots'])
    expect(view.text()).toContain('查看者')
    expect(api.csrfToken).toBe('csrf')
    view.unmount()
  })
  it('查看者点击元信息不会请求敏感方案详情', async () => {
    const paths: string[] = []
    vi.stubGlobal('fetch', async (path: string) => { paths.push(path); return response({ items: [{ plan_id: 'p1', version: 1, source_kind: 'synthetic', status: 'draft', updated_at: '2026-10-23T00:00:00+08:00' }] }) })
    const view = mount(PlanPanel, { props: { context: viewer, snapshots: [], snapshotId: '' } }); await flushPromises()
    await view.get('.plan-list-item').trigger('click'); await flushPromises()
    expect(paths).toEqual(['/api/ops/v1/plans'])
    expect(view.text()).toContain('详情需要成本权限')
    view.unmount()
  })
  it('查看者看板接受不含任何成本字段的服务端投影', async () => {
    vi.stubGlobal('fetch', async () => response({ snapshot: { snapshot_id: 's1', revision: 1, source_kind: 'synthetic', as_of: viewer.clock, currency: 'CNY' }, metrics: { payment_minor: '30000', refund_minor: '10000', net_paid_minor: '20000', paid_order_count: 1, paid_units: 2, average_order_minor: '30000', cohort_refund_order_rate: '1', cohort_refund_amount_rate: '.33', channels: [] }, skus: [], history: [] }))
    const view = mount(DashboardPanel, { props: { context: viewer, snapshotId: 's1' }, global: { stubs: { AssistantPanel: true } } }); await flushPromises()
    expect(view.text()).toContain('¥200.00')
    expect(view.text()).not.toContain('成本覆盖')
    expect(view.text()).not.toContain('贡献利润')
    view.unmount()
  })
  it('base政策通过时仍展示high情景岗位缺口', () => {
    const check = { code: 'capacity', label: '仓储容量', status: 'fail' as const, reason: '高情景缺少2工时' }
    const calculation = { revenue_minor: '100', gross_profit_minor: '50', contribution_profit_minor: '40', budget_used_minor: '10', units: 120, checks: [], resources: [{ role: '仓储', required_hours: '8', available_hours: '6', gap_hours: '2', additional_people: 1, source: '排班', confirmed: true }], resource_checks: [check] }
    const view = mount(EvaluationPanel, { props: { evaluation: { candidates: [{ id: 'direct', label: '独立直降', scenarios: { high: calculation }, calculation_passed: true, rule_supported: true }], selected_candidate: 'direct', feasibility: 'checks_passed', issues: [], resources: [], task_checks: [], lineage: {} } } })
    expect(view.text()).toContain('独立直降 · 高情景岗位风险')
    expect(view.text()).toContain('高情景缺少2工时')
    view.unmount()
  })
})
