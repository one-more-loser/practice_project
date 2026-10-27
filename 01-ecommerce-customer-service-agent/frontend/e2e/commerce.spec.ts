import { test, expect, Page } from '@playwright/test'
import { readFileSync, mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
const root = resolve(process.cwd(), '..')
const accounts = JSON.parse(readFileSync(resolve(root, '.runtime/demo-accounts.json'), 'utf8'))
const evidence = resolve(
  root,
  'docs/06-acceptance',
  process.env.CS_EVIDENCE_SCOPE === 'V1-edge'
    ? 'V1-edge'
    : process.env.CS_EVIDENCE_SCOPE === 'V1-business'
      ? 'V1-business'
      : process.env.CS_EVIDENCE_SCOPE === 'B2'
        ? 'B2'
        : process.env.CS_EVIDENCE_SCOPE === 'debian-db'
          ? 'debian-db'
          : 'B1',
)
mkdirSync(evidence, { recursive: true })
async function login(page: Page, name: string) {
  await page.goto('/login')
  await page.getByLabel('账户', { exact: true }).fill(name)
  await page.getByLabel('密码', { exact: true }).fill(accounts[name])
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('button', { name: '退出', exact: true })).toBeVisible()
}
async function shipping(page: Page, dialog = false) {
  const form = dialog ? page.getByRole('dialog') : page.locator('form').first()
  await form.getByLabel('收件人', { exact: true }).fill('模拟顾客')
  await form.getByLabel('手机号', { exact: true }).fill('13900007777')
  await form.getByLabel('省 / 直辖市', { exact: true }).fill('浙江省')
  await form.getByLabel('市', { exact: true }).fill('杭州市')
  await form.getByLabel('区 / 县', { exact: true }).fill('西湖区')
  await form.getByLabel('详细地址', { exact: true }).fill(dialog ? '演示路88号' : '演示路66号')
}
test('公开商城布局、桌面宽度与键盘入口', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '日常好物', exact: true })).toBeVisible()
  await expect(page.locator('.product-card')).toHaveCount(4)
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: '跳到主要内容' })).toBeFocused()
  await page.mouse.click(1400, 150)
  await page.screenshot({ path: resolve(evidence, '商城-1440.png'), fullPage: true })
  await page.setViewportSize({ width: 1280, height: 800 })
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBeTruthy()
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.screenshot({ path: resolve(evidence, '商城-1280.png'), fullPage: true })
  expect(errors).toEqual([])
})
test('真实商城接口：充值、购物袋、下单、支付、一次改址、发货与本人收货', async ({ page }) => {
  await login(page, 'customer_a')
  await page.evaluate(async () => {
    const session = (await (await fetch('/api/v1/session')).json()).data
    const cart = (await (await fetch('/api/v1/me/cart')).json()).data
    for (const item of cart.items)
      await fetch(`/api/v1/me/cart/items/${item.sku_id}?expected_version=${item.version}`, {
        method: 'DELETE',
        headers: {
          'X-CSRF-Token': session.csrf_token,
          'Idempotency-Key': 'browser_' + crypto.randomUUID().replaceAll('-', ''),
        },
      })
  })
  await page.goto('/wallet')
  await expect(page.getByTestId('wallet-balance')).not.toContainText('正在读取')
  const before = await page.evaluate(async () =>
    BigInt((await (await fetch('/api/v1/me/wallet')).json()).data.balance_minor).toString(),
  )
  await page.getByLabel('充值金额', { exact: true }).fill('100')
  // 丢掉已到达服务端的第一份成功响应，重试必须复用原编号。
  let lost = false
  await page.route('**/api/v1/me/topups', async (route) => {
    if (!lost) {
      lost = true
      await route.fetch()
      await route.abort('failed')
    } else await route.continue()
  })
  await page.getByRole('button', { name: '确认模拟充值', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('结果尚未核对')
  await page.getByRole('button', { name: '确认模拟充值', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('模拟充值已入账')
  await page.unroute('**/api/v1/me/topups')
  const after = await page.evaluate(async () =>
    BigInt((await (await fetch('/api/v1/me/wallet')).json()).data.balance_minor).toString(),
  )
  expect(BigInt(after) - BigInt(before)).toBe(10000n)
  await page.goto('/')
  const cup = page
    .locator('.product-card')
    .filter({ has: page.getByRole('heading', { name: '日常陶瓷杯', exact: true }) })
  await cup.getByRole('combobox').selectOption('sku_cup_ivory')
  await cup.getByRole('button', { name: '加入购物袋', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('已加入购物袋')
  await page.goto('/cart')
  await page.getByRole('link', { name: '去结算', exact: true }).click()
  await shipping(page)
  await page.getByRole('button', { name: '核对价格与库存', exact: true }).click()
  await page.getByRole('button', { name: '提交订单', exact: true }).click()
  await page.getByRole('button', { name: '确认提交订单', exact: true }).click()
  await expect(page).toHaveURL(/\/orders\/order_/)
  const orderPath = new URL(page.url()).pathname
  await page.getByRole('button', { name: '余额支付 ¥40.00', exact: true }).click()
  await page.getByRole('button', { name: '确认余额支付', exact: true }).click()
  await expect(page.locator('.badge').filter({ hasText: '已付款' })).toBeVisible()
  await page.getByRole('button', { name: '修改收货地址', exact: true }).click()
  await shipping(page, true)
  await page.getByRole('button', { name: '核对新地址', exact: true }).click()
  await page.getByRole('button', { name: '确认修改地址', exact: true }).click()
  await expect(page.getByRole('button', { name: '地址已修改', exact: true })).toBeDisabled()
  await page.reload()
  await expect(page.getByRole('button', { name: '地址已修改', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await login(page, 'customer_b')
  await page.goto(orderPath)
  await expect(page.getByRole('alert')).toContainText('资源不存在或不可访问')
  expect(await page.locator('body').innerText()).not.toContain('13900007777')
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await login(page, 'shop_admin')
  const orderId = orderPath.split('/').pop()!
  const display = await page.evaluate(
    async (id) => (await (await fetch('/api/v1/shop/orders/' + id)).json()).data.display_no,
    orderId,
  )
  const row = page.getByRole('row').filter({ hasText: display })
  await row.getByRole('button', { name: '查看订单', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('*******7777')
  expect(await page.locator('body').innerText()).not.toContain('演示路88号')
  await page.getByRole('button', { name: '确认整单发货', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('整单发货成功')
  await page.getByRole('button', { name: '记录承运签收', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('已记录承运签收')
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await page.screenshot({ path: resolve(evidence, '店铺工作台.png'), fullPage: true })
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await login(page, 'customer_a')
  await page.goto(orderPath)
  await expect(page.locator('.badge').filter({ hasText: '运输中' })).toBeVisible()
  await page.getByRole('button', { name: '确认收货', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认收货', exact: true }).click()
  await expect(page.locator('.badge').filter({ hasText: '已完成' })).toBeVisible()
})
