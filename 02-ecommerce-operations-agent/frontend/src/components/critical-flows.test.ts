import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import MoneyValue from './MoneyValue.vue'
import CapabilityAction from './CapabilityAction.vue'
import PlanEditor from './PlanEditor.vue'
import type { Draft, Snapshot } from '../types'

const draft: Draft = {
  title: '现货直降', snapshot_id: 'snapshot_test', expected_snapshot_revision: 1,
  start_at: '2026-11-11T00:00:00+08:00', end_at: '2026-11-12T00:00:00+08:00',
  selected_candidate: 'direct', fixed_fee_minor: '10000',
  goal: { min_revenue_minor: '900000', min_profit_minor: '250000', budget_minor: '100000', scenario_policy: 'base' },
  lines: [{ sku_id: 'sku_A', quantities: { low: 80, base: 100, high: 120 }, direct_price_minor: '9200', basket_qty: 1, coupon_minor: '800', coupon_threshold_minor: '10000' }],
  resources: [{ role: '客服', volume_per_unit: '0.3', productivity_per_hour: '20', people: 1, shift_hours: '8', attendance: '1', efficiency: '0.75', source: '排班确认', confirmed: true }],
  tasks: [{ title: '发布前复核', owner: '店长', due_at: '2026-11-10T18:00:00+08:00', stage: 'before_publish', done: true }],
  monitoring: { interval_minutes: 5, data_source: '01.operations_snapshot', stop_conditions: ['预算不足停止'] }, notes: '保留依据',
}
const snapshot: Snapshot = { snapshot_id: 'snapshot_test', revision: 1, store_id: 'store', source_kind: 'synthetic', as_of: '2026-10-23T00:00:00+08:00', currency: 'CNY', skus: [{ sku_id: 'sku_A', title: '测试商品', category: '日用', base_price_minor: '10000', cost_minor: '5000', variable_fee_minor: '200', available_qty: 200, price_version: 1 }], orders: [], refunds: [] }

describe('金额展示', () => {
  it('保留超过 Number 安全范围的分，不损失末两位', () => {
    const view = mount(MoneyValue, { props: { value: '900719925474099301' } })
    expect(view.text()).toBe('¥9,007,199,254,740,993.01')
  })
  it('未知成本对应金额保持未知，不能显示为零', () => {
    expect(mount(MoneyValue, { props: { value: null } }).text()).toBe('未知')
  })
})

describe('权限及依赖门禁', () => {
  it('无确认权限时不可触发确认事件，并给出可见原因', async () => {
    const view = mount(CapabilityAction, { props: { capabilities: ['metrics.read'], capability: 'plan.confirm', label: '确认方案' } })
    expect(view.find('button').exists()).toBe(true)
    expect(view.get('button').attributes('disabled')).toBeDefined()
    await view.get('button').trigger('click')
    expect(view.emitted('activate')).toBeUndefined()
    expect(view.text()).toContain('没有此操作权限')
  })
  it('01未接入时有权限也不能伪造发布成功', async () => {
    const view = mount(CapabilityAction, { props: { capabilities: ['activity.publish'], capability: 'activity.publish', label: '发布活动', blockedReason: '01 活动接口未接入' } })
    expect(view.find('button').exists()).toBe(true)
    expect(view.get('button').attributes('disabled')).toBeDefined()
    expect(view.text()).toContain('01 活动接口未接入')
    await view.get('button').trigger('click')
    expect(view.emitted('activate')).toBeUndefined()
  })
})

describe('方案表单提交', () => {
  it('编辑价格后发出完整草稿，金额仍是整数分字符串', async () => {
    const view = mount(PlanEditor, { props: { draft, snapshot, canEdit: true, saving: false } })
    expect(view.find('[name="line-0-price"]').exists()).toBe(true)
    await view.get('[name="line-0-price"]').setValue('9155')
    await view.get('form').trigger('submit')
    const emitted = view.emitted('save')?.[0]?.[0] as typeof draft
    expect(emitted.lines[0]?.direct_price_minor).toBe('9155')
    expect(emitted.goal.budget_minor).toBe('100000')
    expect(emitted.tasks[0]?.owner).toBe('店长')
    expect(emitted.notes).toBe('保留依据')
    expect(draft.lines[0]?.direct_price_minor).toBe('9200')
  })
  it('只读身份不能从表单提交写入', async () => {
    const view = mount(PlanEditor, { props: { draft, snapshot, canEdit: false, saving: false } })
    expect(view.find('form').exists()).toBe(true)
    await view.get('form').trigger('submit')
    expect(view.emitted('save')).toBeUndefined()
    expect(view.get('fieldset').attributes('disabled')).toBeDefined()
  })
})
