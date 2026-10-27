import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiClient } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('同源 API 请求边界', () => {
  it('写请求携带CSRF、同源Cookie和幂等键，不传客户端身份', async () => {
    let outgoing: RequestInit | undefined
    let url = ''
    vi.stubGlobal('fetch', async (path: string, init: RequestInit) => {
      url = path; outgoing = init
      return new Response(JSON.stringify({ data: { plan_id: 'p1', version: 1 } }))
    })
    const client = new ApiClient(); client.csrfToken = 'csrf-contract'
    const plan = await client.request<{ plan_id: string; version: number }>('/plans', { method: 'POST', body: { title: '测试' } })
    expect(plan).toEqual({ plan_id: 'p1', version: 1 })
    expect(url).toBe('/api/ops/v1/plans')
    expect(outgoing?.credentials).toBe('same-origin')
    const headers = new Headers(outgoing?.headers)
    expect(headers.get('X-CSRF-Token')).toBe('csrf-contract')
    expect(headers.get('Idempotency-Key')).toBeTruthy()
    expect(headers.has('X-User')).toBe(false)
  })
  it('把后端403与请求编号交给界面，不能变成空数据成功', async () => {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ error: { code: 'forbidden', message: '没有成本查看权限' }, request_id: 'req-permission' }), { status: 403 }))
    await expect(new ApiClient().request('/plans/p1')).rejects.toMatchObject({ status: 403, message: '没有成本查看权限', requestId: 'req-permission' })
  })
  it('网络中断后再次提交相同操作复用幂等键', async () => {
    const keys: (string | null)[] = []
    let attempt = 0
    vi.stubGlobal('fetch', async (_path: string, init: RequestInit) => {
      keys.push(new Headers(init.headers).get('Idempotency-Key'))
      if (++attempt === 1) throw new TypeError('network')
      return new Response(JSON.stringify({ data: { status: 'sandbox_confirmed' } }))
    })
    const client = new ApiClient()
    await expect(client.request('/plans/p1/confirm', { method: 'POST', body: { expected_version: 2 } })).rejects.toThrow()
    await client.request('/plans/p1/confirm', { method: 'POST', body: { expected_version: 2 } })
    expect(keys[0]).toBeTruthy()
    expect(keys[1]).toBe(keys[0])
  })
})
