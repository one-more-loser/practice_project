import { afterEach, expect, it, vi } from 'vitest'
import { mutate, setSession, resetPending, yuanToMinor } from '../src/lib/api'

afterEach(() => {
  vi.unstubAllGlobals()
  resetPending()
  setSession('', '')
})
it('响应丢失后重试保持同一操作编号', async () => {
  const keys: string[] = []
  setSession('test-csrf', 'customer_a')
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (_url, options) => {
      keys.push(options.headers['Idempotency-Key'])
      if (keys.length === 1) throw new TypeError('network')
      return { ok: true, status: 200, json: async () => ({ data: { balance_minor: '1234' } }) }
    }),
  )
  await expect(
    mutate('/me/topups', 'POST', { currency: 'CNY', amount_minor: '1234' }),
  ).rejects.toThrow('结果尚未核对')
  await mutate('/me/topups', 'POST', { currency: 'CNY', amount_minor: '1234' })
  expect(keys[0]).toBe(keys[1])
})
it('金额转换使用整数，拒绝指数和多于两位小数', () => {
  expect(yuanToMinor('12.34')).toBe('1234')
  expect(yuanToMinor('0.66')).toBe('66')
  for (const value of ['1e3', '12.345', '-1', '0']) expect(() => yuanToMinor(value)).toThrow()
})
