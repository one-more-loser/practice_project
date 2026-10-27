import { describe, expect, it } from 'vitest'
import { loginTarget } from '../src/lib/login-target'

describe('统一登录跳转白名单', () => {
  it('允许两应用入口与本人订单', () => {
    for (const value of ['/shop', '/operations/', '/orders/order_abc']) expect(loginTarget(value)).toBe(value)
  })
  it('拒绝站外、协议相对、编码路径与未知目标', () => {
    for (const value of ['https://example.com', '//example.com', '/\\example.com', '/%2fexample.com', '/unknown', '/\nexample.com']) expect(loginTarget(value)).toBeNull()
  })
})
