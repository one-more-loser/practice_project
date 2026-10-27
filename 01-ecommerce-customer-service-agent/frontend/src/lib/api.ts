let csrf = ''
export function csrfToken() {
  return csrf
}
let principal = ''
const storageKey = 'cs.pending.operations.v1'
let pending: Record<string, string> = {}
try {
  pending = JSON.parse(sessionStorage.getItem(storageKey) || '{}')
} catch {
  /* 无可恢复记录 */
}
function save() {
  try {
    sessionStorage.setItem(storageKey, JSON.stringify(pending))
  } catch {
    /* 当前页面保留原编号 */
  }
}
export function resetPending() {
  pending = {}
  save()
}
export function setSession(token: string, scope: string) {
  csrf = token
  principal = scope
}
export function setGuestSession(token: string) {
  // 登录状态不允许被迟到的游客请求覆盖。
  if (principal === 'guest' || principal === '') csrf = token
}
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code: string,
  ) {
    super(message)
  }
}
export async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  key?: string,
): Promise<T> {
  let response: Response
  const abort = new AbortController()
  const timer = setTimeout(() => abort.abort(), 15000)
  try {
    response = await fetch('/api/v1' + path, {
      method,
      credentials: 'same-origin',
      signal: abort.signal,
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(method !== 'GET' ? { 'X-CSRF-Token': csrf } : {}),
        ...(key ? { 'Idempotency-Key': key } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    })
  } catch {
    throw new ApiError(
      '连接中断，操作结果尚未核对。请保留本页，使用原内容重试。',
      0,
      'unknown_result',
    )
  } finally {
    clearTimeout(timer)
  }
  let envelope
  try {
    envelope = await response.json()
  } catch {
    throw new ApiError('响应无法读取，操作结果尚未核对。请使用原内容重试。', 0, 'unknown_result')
  }
  if (!response.ok)
    throw new ApiError(
      envelope.error?.message || '请求失败，请重试',
      response.status,
      envelope.error?.code || 'request_failed',
    )
  return envelope.data as T
}
export async function mutate<T>(path: string, method: string, body?: unknown): Promise<T> {
  const encoded = new TextEncoder().encode(JSON.stringify(body ?? null))
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', encoded)))
    .map((n) => n.toString(16).padStart(2, '0'))
    .join('')
  const prefix = principal + '|' + method + '|' + path + '|'
  const signature = prefix + hash
  if (
    path === '/me/topups' &&
    Object.keys(pending).some((key) => key.startsWith(prefix) && key !== signature)
  )
    throw new ApiError('上一笔充值结果待核对，请恢复原金额后重试。', 0, 'unknown_result')
  const key = pending[signature] ?? 'web_' + crypto.randomUUID().replaceAll('-', '')
  pending[signature] = key
  save()
  try {
    const result = await request<T>(path, method, body, key)
    delete pending[signature]
    save()
    return result
  } catch (error) {
    if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
      delete pending[signature]
      save()
    }
    throw error
  }
}
export function yuanToMinor(value: string): string {
  if (!/^(0|[1-9]\d*)(\.\d{1,2})?$/.test(value)) throw new Error('请输入大于0的金额，最多两位小数')
  const [whole, decimal = ''] = value.split('.')
  const minor = BigInt(whole) * 100n + BigInt(decimal.padEnd(2, '0'))
  if (minor <= 0n || minor > 9223372036854775807n) throw new Error('金额超出可用范围')
  return minor.toString()
}
export function money(minor: string) {
  const n = BigInt(minor)
  return (n / 100n).toString() + '.' + (n % 100n).toString().padStart(2, '0')
}
export function errorText(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请重试'
}
