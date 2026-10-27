import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import DashboardPanel from './DashboardPanel.vue'
import type { Context, Dashboard } from '../types'

const context: Context = { actor_ref: 'viewer', store_id: 'store_A', display_name: '查看者', capabilities: ['metrics.read'], csrf_token: 'csrf', mode: 'integration', clock: '2026-11-10T12:00:00+08:00', dependencies: { identity: 'ready', activity: 'ready', model: 'disabled' } }
function dashboard(refund: string | null): Dashboard {
  return {
    snapshot: { snapshot_id: 'snap', revision: 2, source_kind: 'synthetic', as_of: context.clock, currency: 'CNY' },
    metrics: { payment_minor: '10000', refund_minor: refund, net_paid_minor: refund == null ? null : '10000', paid_order_count: 1, paid_units: 1, average_order_minor: '10000', cohort_refund_order_rate: null, cohort_refund_amount_rate: null, channels: [] },
    filter_missing: refund == null ? ['多SKU订单退款缺原生行分摊'] : [], skus: [],
    history: [
      { date: '2026-11-07', payment_minor: '10000', refund_minor: '0' },
      { date: '2026-11-08', payment_minor: '0', refund_minor: refund, ...(refund == null ? { refund_missing: [{ refund_id: 'R1', order_id: 'O1', occurred_at: '2026-11-08T12:00:00+08:00', reason: 'missing_sku_allocation' as const }] } : {}) },
    ],
  }
}
afterEach(() => vi.unstubAllGlobals())

describe('R1 Dashboard退款展示', () => {
  it.each([null, '0'])('退款独占日金额%j保留日期，并区分未知和完整零', async (refund) => {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ data: dashboard(refund) })))
    const view = mount(DashboardPanel, { props: { context, snapshotId: 'snap' } })
    await flushPromises()
    const rows = view.findAll('details tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.findAll('td')[2]!.text()).toBe('¥0.00')
    expect(rows[1]!.findAll('td')[0]!.text()).toBe('2026-11-08')
    expect(rows[1]!.findAll('td')[1]!.text()).toBe('¥0.00')
    const amount = rows[1]!.findAll('td')[2]!
    if (refund == null) {
      expect(amount.get('.money').text()).toBe('未知')
      expect(amount.text()).toContain('R1 · O1')
      expect(amount.text()).toContain('退款分摊依据缺失')
      expect(view.findAll('.metric-strip .money').map(item => item.text())).toEqual(['¥100.00', '未知', '未知'])
    } else {
      expect(amount.text()).toBe('¥0.00')
      expect(amount.find('details').exists()).toBe(false)
    }
    view.unmount()
  })
})
