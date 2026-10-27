import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import SnapshotPanel from './SnapshotPanel.vue'
import type { Context } from '../types'

const context: Context = { actor_ref: 'operator', store_id: 'shop', display_name: '运营', capabilities: ['metrics.read', 'data.import', 'cost.read'], csrf_token: 'csrf', mode: 'demo', clock: '2026-10-23T00:00:00+08:00', dependencies: { identity: 'demo', activity: 'not_connected', model: 'disabled' } }
afterEach(() => vi.unstubAllGlobals())
describe('快照导入端点选择', () => {
  it('CSV文本包按原字符串提交到专用端点，Snapshot格式仍用原端点', async () => {
    const requests: {path: string; body: unknown}[] = []
    vi.stubGlobal('fetch', async (path: string, init: RequestInit) => { requests.push({ path, body: JSON.parse(init.body as string) }); return new Response(JSON.stringify({ data: { snapshot_id: 'csv_1' } })) })
    const bundle = { manifest: { snapshot_id: 'csv_1', revision: 1, store_id: 'shop', source_kind: 'business_import', as_of: context.clock, currency: 'CNY' }, skus_csv: 'sku_id,title\nsku_a,"大容量,保温杯"', orders_csv: 'order_id,paid_at', refunds_csv: 'refund_id,order_id' }
    const view = mount(SnapshotPanel, { props: { context, snapshots: [], selectedId: '' } })
    await view.get('[name="import-mode"]').setValue('csv')
    await view.get('#snapshot-json').setValue(JSON.stringify(bundle))
    await view.get('form').trigger('submit'); await flushPromises()
    expect(requests[0]).toEqual({ path: '/api/ops/v1/snapshots/import-csv', body: bundle })
    expect(view.emitted('imported')).toHaveLength(1)
    await view.get('[name="import-mode"]').setValue('snapshot')
    await view.get('#snapshot-json').setValue('{"snapshot_id":"json_1"}')
    await view.get('form').trigger('submit'); await flushPromises()
    expect(requests[1]?.path).toBe('/api/ops/v1/snapshots')
    view.unmount()
  })
})
