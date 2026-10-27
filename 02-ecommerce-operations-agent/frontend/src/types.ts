export type Minor = string
export type SourceKind = 'synthetic' | 'business_import' | 'derived'
export interface Context {
  actor_ref: string; store_id: string; display_name: string; capabilities: string[]
  csrf_token: string; mode: 'demo' | 'integration'; clock: string
  dependencies: { identity: string; activity: string; model: string }
}
export interface Sku {
  sku_id: string; title: string; category: string; base_price_minor: Minor
  cost_minor?: Minor | null; variable_fee_minor?: Minor | null
  available_qty: number | null; price_version: number | string
}
export interface Snapshot {
  snapshot_id: string; revision: number; store_id: string; source_kind: SourceKind
  as_of: string; currency: 'CNY'; skus: Sku[]; orders: unknown[]; refunds: unknown[]
  execution_supported_rules?: string[]
}
export interface PlanLine {
  sku_id: string; quantities: { low: number; base: number; high: number }
  direct_price_minor: Minor; basket_qty: number; coupon_minor: Minor; coupon_threshold_minor: Minor
}
export interface Resource {
  role: string; volume_per_unit: string; productivity_per_hour: string; people: number
  shift_hours: string; attendance: string; efficiency: string; source: string; confirmed: boolean
}
export interface Task {
  title: string; owner: string; due_at: string; stage: 'before_publish' | 'during' | 'after'; done: boolean
}
export interface Draft {
  title: string; snapshot_id: string; expected_snapshot_revision: number
  start_at: string; end_at: string; selected_candidate: string; fixed_fee_minor: Minor
  goal: { min_revenue_minor: Minor; min_profit_minor: Minor; budget_minor: Minor; scenario_policy: string }
  lines: PlanLine[]; resources: Resource[]; tasks: Task[]
  monitoring: { interval_minutes: number; data_source: string; stop_conditions: string[] }; notes: string
}
export interface Check { code: string; label: string; status: 'pass' | 'fail' | 'unknown'; reason: string }
export interface Calculation {
  revenue_minor: Minor; gross_profit_minor: Minor | null; contribution_profit_minor: Minor | null
  budget_used_minor: Minor; units: number; checks: Check[]
  resources?: ResourceResult[]; resource_checks?: Check[]
}
export interface ResourceResult { role: string; required_hours: string; available_hours: string; gap_hours: string; additional_people: number; source: string; confirmed: boolean }
export interface Evaluation {
  candidates: { id: string; label: string; scenarios: Record<string, Calculation>; calculation_passed: boolean; rule_supported: boolean }[]
  selected_candidate: string; feasibility: 'needs_data' | 'infeasible' | 'checks_passed'; issues: string[]
  resources: ResourceResult[]
  task_checks: Check[]; lineage: Record<string, unknown>
}
export interface PlanSummary {
  plan_id: string; title?: string; version: number; store_id?: string; status: string
  source_kind: SourceKind; created_at?: string; updated_at: string
}
export interface Plan extends PlanSummary {
  draft: Draft; evaluation: Evaluation | null; approval: Record<string, unknown> | null
  validity?: { snapshot_current: boolean; latest_snapshot_revision: number; execution_frozen: boolean; note: string }
}
export interface Dashboard {
  snapshot: Pick<Snapshot, 'snapshot_id' | 'revision' | 'source_kind' | 'as_of' | 'currency'>
  metrics: { payment_minor: Minor; refund_minor: Minor | null; net_paid_minor: Minor | null; paid_order_count: number; paid_units: number; average_order_minor: Minor | null; cohort_refund_order_rate: string | null; cohort_refund_amount_rate: string | null; channels: Record<string, unknown>[] }
  filter_missing?: string[]
  skus: Sku[]; cost_coverage?: { known: number; total: number }
  history: { date: string; payment_minor: Minor; refund_minor: Minor | null; refund_missing?: { refund_id: string; order_id: string; occurred_at: string; reason: 'missing_sku_allocation' }[] }[]
  contribution_profit_minor?: Minor | null; profit_missing_reason?: string
}
export interface AssistantResult { answer: string; mode: string; evidence: { title: string; value: unknown }[]; missing: string[] }
export interface Monitoring { plan: PlanSummary | Plan; actual?: Record<string, unknown> | null; deviations: unknown[]; freshness: unknown; status: string }
