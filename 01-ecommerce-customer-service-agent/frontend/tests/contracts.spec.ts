import { describe, expect, it } from 'vitest'
import { formatMoney, parseMoney } from '../../contracts/api'

describe('分单位金额契约', () => {
  it('金额显示不丢失超过JS安全整数的分', () => {
    expect(formatMoney(parseMoney({ currency: 'CNY', amount_minor: '9007199254740993' }))).toBe(
      '90071992547409.93',
    )
  })
  it.each([12.3, '01', '1e2', '9223372036854775808', '-1'])(
    '拒绝非整数字符串或溢出：%s',
    (amount) => {
      expect(() => parseMoney({ currency: 'CNY', amount_minor: amount })).toThrow()
    },
  )
})
