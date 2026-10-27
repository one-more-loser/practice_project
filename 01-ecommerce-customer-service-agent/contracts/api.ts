// API-CS-v0.1基础切片。未知状态必须报契约错误，不能推定成功。
export interface MoneyMinor { currency: 'CNY'; amount_minor: string }
export type OperationStatus = 'accepted' | 'processing' | 'awaiting_agent_review' | 'succeeded' | 'rejected' | 'failed' | 'result_unknown'
export interface Envelope<T> { contract_version: 'API-CS-v0.1'; request_id: string; data: T }
export interface ApiError { contract_version: 'API-CS-v0.1'; request_id: string; error: { code: string; message: string; retryable: boolean } }
export interface StreamEvent { contract_version: 'EVT-CS-v0.1'; event_id: string; stream_id: string; conversation_id: string; seq: number; mode_epoch: number; type: string; data: unknown; occurred_at: string }

export function parseMoney(value: unknown): MoneyMinor {
  if (!value || typeof value !== 'object') throw new Error('金额契约错误')
  const input = value as Record<string, unknown>
  if (input.currency !== 'CNY' || typeof input.amount_minor !== 'string'
    || !/^(0|[1-9][0-9]{0,18})$/.test(input.amount_minor)
    || BigInt(input.amount_minor) > 9223372036854775807n
    || Object.keys(input).some(key => !['currency', 'amount_minor'].includes(key))) throw new Error('金额契约错误')
  return { currency: 'CNY', amount_minor: input.amount_minor }
}

export function formatMoney(value: MoneyMinor): string {
  const minor = BigInt(parseMoney(value).amount_minor)
  return `${minor / 100n}.${(minor % 100n).toString().padStart(2, '0')}`
}
