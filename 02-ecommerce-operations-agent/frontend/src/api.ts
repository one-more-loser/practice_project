export class ApiError extends Error {
  constructor(message: string, public status = 0, public code = 'network_error', public requestId = '') { super(message) }
}
export class ApiClient {
  csrfToken = ''
  private pendingKeys = new Map<string, string>()
  resetSession() { this.csrfToken = ''; this.pendingKeys.clear() }
  async request<T>(path: string, options: { method?: string; body?: unknown; signal?: AbortSignal; text?: boolean; shop?: boolean } = {}): Promise<T> {
    const method = options.method ?? 'GET', writing = method !== 'GET'
    const body = options.body === undefined ? undefined : JSON.stringify(options.body)
    const fingerprint = `${method}:${path}:${body ?? ''}`
    const headers = new Headers({ Accept: options.text ? 'text/markdown' : 'application/json' })
    if (body !== undefined) headers.set('Content-Type', 'application/json')
    if (writing) {
      if (this.csrfToken) headers.set('X-CSRF-Token', this.csrfToken)
      if (!this.pendingKeys.has(fingerprint)) this.pendingKeys.set(fingerprint, crypto.randomUUID())
      headers.set('Idempotency-Key', this.pendingKeys.get(fingerprint)!)
    }
    const timeout = new AbortController(), timer = setTimeout(() => timeout.abort(), 30_000)
    const signal = options.signal ? AbortSignal.any([options.signal, timeout.signal]) : timeout.signal
    try {
      const response = await fetch(`${options.shop ? "/api/v1/shop" : "/api/ops/v1"}${path}`, { method, headers, body, signal, credentials: 'same-origin' })
      if (response.ok && options.text) { this.pendingKeys.delete(fingerprint); return await response.text() as T }
      const result = await response.json()
      if (!response.ok) {
        if (response.status < 500) this.pendingKeys.delete(fingerprint)
        throw new ApiError(result.error?.message ?? '请求未完成，请刷新后重试', response.status, result.error?.code, result.request_id)
      }
      if (!['unknown', 'result_unknown'].includes(result.data?.status)) this.pendingKeys.delete(fingerprint)
      if (!('data' in result)) throw new ApiError('接口响应缺少 data，请检查服务版本', 502, 'invalid_response')
      return result.data as T
    } catch (error) {
      if (error instanceof ApiError) throw error
      if (options.signal?.aborted) throw new ApiError('已取消读取', 0, 'cancelled')
      throw new ApiError(writing ? '连接中断，操作结果待核对。刷新记录后可重试原操作。' : '无法读取数据，请检查后端服务后重试', 0, 'network_error')
    } finally { clearTimeout(timer) }
  }
}
export const api = new ApiClient()
export function errorMessage(error: unknown) {
  return error instanceof ApiError ? `${error.message}${error.requestId ? `（请求 ${error.requestId}）` : ''}` : error instanceof Error ? error.message : '操作未完成，请重试'
}
