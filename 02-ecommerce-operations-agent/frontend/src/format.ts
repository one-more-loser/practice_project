import type { Draft, Snapshot } from './types'
// 金额只做单位展示；BigInt 保留后端整数分，不参与财务测算。
export function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined) return '未知'
  if (!/^-?\d+$/.test(value)) return '金额格式异常'
  const amount = BigInt(value), absolute = amount < 0n ? -amount : amount
  return `${amount < 0n ? '-' : ''}¥${new Intl.NumberFormat('zh-CN').format(absolute / 100n)}.${String(absolute % 100n).padStart(2, '0')}`
}
export function dateTime(value?: string) {
  if (!value || Number.isNaN(Date.parse(value))) return '未提供'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))
}
export function localInput(value: string) {
  if (Number.isNaN(Date.parse(value))) return ''
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)).replace(' ', 'T')
}
export const fromLocal = (value: string) => value ? `${value}:00+08:00` : ''
export const cloneDraft = (draft: Draft): Draft => JSON.parse(JSON.stringify(draft))
export const sourceName = (value: string) => ({ synthetic: '模拟数据', business_import: '业务导入', derived: '派生数据' }[value] ?? value)
export const statusName = (value: string) => ({ cancelled: '审批已撤销', cancel_pending: '撤销待终态核对', confirmed: '方案已确认', publishing: '发布中 · 待回执', closed_no_effect: '意图已关闭 · 无执行效果', draft: '草稿', evaluated: '已评估', needs_data: '待补数据', infeasible: '约束未通过', checks_passed: '02 检查通过', ready_for_confirmation: '待确认', sandbox_confirmed: '演示已确认 · 待01预检', confirmed_feasible: '方案已确认', invalidated: '待重新评估', rejected: '已驳回', not_connected: '01 未接入', awaiting_actuals: '等待实际数据', awaiting_actual: '等待实际数据', actual_received: '已收到实际数据', no_actuals: '无实际数据', scheduled: '待开始', active: '进行中', paused: '已暂停', ended: '已结束', committed: '已持久接收 · 待状态核对', result_unknown: '结果待核对' }[value] ?? value)
export function createDraft(snapshot: Snapshot, clock: string): Draft {
  const now = Date.parse(clock) || Date.now(), start = new Date(now + 2 * 86_400_000), end = new Date(now + 3 * 86_400_000)
  return {
    title: '现货直降方案', snapshot_id: snapshot.snapshot_id, expected_snapshot_revision: snapshot.revision,
    start_at: start.toISOString(), end_at: end.toISOString(), selected_candidate: 'direct', fixed_fee_minor: '10000',
    goal: { min_revenue_minor: '0', min_profit_minor: '0', budget_minor: '100000', scenario_policy: 'base' },
    lines: snapshot.skus.slice(0, 1).map(sku => ({ sku_id: sku.sku_id, quantities: { low: sku.available_qty === null ? 0 : Math.min(10, sku.available_qty), base: sku.available_qty === null ? 0 : Math.min(20, sku.available_qty), high: sku.available_qty === null ? 0 : Math.min(30, sku.available_qty) }, direct_price_minor: sku.base_price_minor, basket_qty: 1, coupon_minor: '0', coupon_threshold_minor: sku.base_price_minor })),
    resources: ['客服', '仓储'].map(role => ({ role, volume_per_unit: role === '客服' ? '0.3' : '1', productivity_per_hour: '20', people: 1, shift_hours: '8', attendance: '1', efficiency: '0.75', source: '待核对排班', confirmed: false })),
    tasks: [
      { title: '复核价格、库存与执行条件', owner: '店长', due_at: new Date(start.getTime() - 3_600_000).toISOString(), stage: 'before_publish', done: false },
      { title: '客服值守与仓储履约', owner: '运营负责人', due_at: start.toISOString(), stage: 'during', done: false },
      { title: '核对活动实际并完成复盘', owner: '运营负责人', due_at: new Date(end.getTime() + 86_400_000).toISOString(), stage: 'after', done: false },
    ],
    monitoring: { interval_minutes: 5, data_source: '01.operations_snapshot', stop_conditions: ['预算或活动数量不足时停止新优惠订单', '库存或履约容量异常时人工核对'] },
    notes: `初始参数待核对；依据选定快照和负责人确认后评估。${snapshot.skus.some(sku => sku.available_qty === null) ? '当前快照存在未知库存；该商品初始情景数量仅占位为0，不代表库存为0或可执行，需补齐库存依据。' : ''}`,
  }
}
