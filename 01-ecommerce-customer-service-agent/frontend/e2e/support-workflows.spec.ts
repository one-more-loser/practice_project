import { test, expect, type Page } from '@playwright/test'
import { readFileSync, mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
const root = resolve(process.cwd(), '..'),
  accounts = JSON.parse(readFileSync(resolve(root, '.runtime/demo-accounts.json'), 'utf8')),
  evidence = resolve(
    root,
    'docs/06-acceptance',
    process.env.CS_EVIDENCE_SCOPE === 'V1-edge' ? 'V1-edge' : 'V1-business',
  )
mkdirSync(evidence, { recursive: true })
async function login(page: Page, name: string) {
  await page.goto('/login')
  await page.getByLabel('账户', { exact: true }).fill(name)
  await page.getByLabel('密码', { exact: true }).fill(accounts[name])
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('button', { name: '退出', exact: true })).toBeVisible()
}
test('真实人工协同：分派、双向消息、结束自动恢复及历史补偿', async ({ browser }) => {
  test.skip(process.env.CS_VERIFICATION_TARGET !== 'test', '只允许固定隔离测试库')
  const contexts = await Promise.all(
    Array.from({ length: 3 }, () =>
      browser.newContext({
        baseURL: 'http://127.0.0.1:5173',
        viewport: { width: 1440, height: 1000 },
      }),
    ),
  )
  const [customer, supervisor, staff] = await Promise.all(contexts.map((c) => c.newPage()))
  const errors: string[] = []
  for (const page of [customer, supervisor, staff])
    page.on('pageerror', (e) => errors.push(e.message))
  try {
    await login(customer, 'customer_a')
    await customer.getByRole('button', { name: '店铺客服', exact: true }).click()
    const chat = customer.getByRole('dialog', { name: '店铺客服', exact: true }),
      subject = '浏览器人工协同核验 ' + Math.random().toString(16).slice(2, 10)
    await chat.getByLabel('咨询内容').fill(subject)
    await chat.getByRole('button', { name: '联系人工', exact: true }).click()
    await expect(chat.getByRole('status').filter({ hasText: '等待客服接入' })).toBeVisible()
    await login(supervisor, 'supervisor')
    await supervisor.goto('/workbench')
    await supervisor
      .locator('.list-row')
      .filter({ hasText: subject })
      .getByRole('button', { name: '处理', exact: true })
      .click()
    await supervisor.getByLabel('处理内容', { exact: true }).fill('分派浏览器服务核验')
    await supervisor.getByLabel('分派客服', { exact: true }).selectOption('support_a')
    await supervisor.getByRole('button', { name: '确认分派', exact: true }).click()
    await login(staff, 'support_a')
    await staff.goto('/workbench')
    await staff
      .locator('.list-row')
      .filter({ hasText: subject })
      .getByRole('button', { name: '处理', exact: true })
      .click()
    await staff.getByRole('button', { name: '接入服务', exact: true }).click()
    await expect(chat.getByRole('status').filter({ hasText: '人工服务中' })).toBeVisible()
    await staff.getByLabel('处理内容', { exact: true }).fill('已经为您核对订单政策')
    await staff.getByRole('button', { name: '发送会话消息', exact: true }).click()
    await expect(chat.getByText('已经为您核对订单政策', { exact: true })).toHaveCount(1)
    await chat.getByLabel('咨询内容').fill('收到，谢谢您的协助')
    await chat.getByRole('button', { name: '发送', exact: true }).click()
    await expect(staff.getByText('收到,谢谢您的协助', { exact: false })).toBeVisible()
    await staff.screenshot({ path: resolve(evidence, '人工工作台.png'), fullPage: true })
    await staff.getByRole('button', { name: '结束服务', exact: true }).click()
    await expect(chat.getByText('人工服务中', { exact: true })).toHaveCount(0)
    await expect(chat.getByRole('button', { name: '联系人工', exact: true })).toBeVisible()
    await chat.getByRole('button', { name: 'Close this dialog' }).click()
    await customer.getByRole('button', { name: '店铺客服', exact: true }).click()
    await expect(chat.getByText('已经为您核对订单政策', { exact: true })).toHaveCount(1)
    await customer.screenshot({
      path: resolve(evidence, '顾客-人工结束与恢复.png'),
      fullPage: true,
    })
    expect(errors).toEqual([])
  } finally {
    await Promise.all(contexts.map((c) => c.close()))
  }
})

test('主管处置知识治理页面与周摘要', async ({ page }) => {
  test.skip(process.env.CS_VERIFICATION_TARGET !== 'test', '只允许固定隔离测试库')
  await login(page, 'supervisor')
  await page.goto('/experience')
  await expect(page.getByRole('heading', { name: '处置知识', exact: true })).toBeVisible()
  await expect(page.getByLabel('适用条件', { exact: true })).toBeVisible()
  await page.getByLabel('起始周一（北京时间）', { exact: true }).fill('2026-10-12')
  await page.getByRole('button', { name: '生成已结束周摘要', exact: true }).click()
  await expect(page.locator('.aggregate-grid')).toBeVisible()
  await page.screenshot({ path: resolve(evidence, '主管-处置知识与摘要.png'), fullPage: true })
})

async function api<T>(page: Page, path: string, method = 'GET', body?: unknown): Promise<T> {
  return page.evaluate(
    async ({ path, method, body }) => {
      const session = (await (await fetch('/api/v1/session')).json()).data
      const response = await fetch('/api/v1' + path, {
        method,
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': session.csrf_token,
          'Idempotency-Key': 'browser_' + crypto.randomUUID(),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      if (!response.ok) throw new Error('fixed browser setup request status ' + response.status)
      return (await response.json()).data
    },
    { path, method, body },
  )
}

test('真实部分退货页面：顾客申请、分派审核、退货登记、店铺核验到账', async ({ browser }) => {
  test.skip(process.env.CS_VERIFICATION_TARGET !== 'test', '只允许固定隔离测试库')
  const contexts = await Promise.all(
    Array.from({ length: 4 }, () =>
      browser.newContext({
        baseURL: 'http://127.0.0.1:5173',
        viewport: { width: 1440, height: 1000 },
      }),
    ),
  )
  const [customer, manager, staff, shop] = await Promise.all(contexts.map((c) => c.newPage()))
  const errors: string[] = []
  for (const p of [customer, manager, staff, shop]) p.on('pageerror', (e) => errors.push(e.message))
  try {
    await login(customer, 'customer_a')
    await api(customer, '/me/topups', 'POST', { currency: 'CNY', amount_minor: '20000' })
    const quote = await api<{ quote_id: string }>(customer, '/me/quotes', 'POST', {
      items: [{ sku_id: 'sku_cup_ivory', qty: 3 }],
      shipping_input_ref: 'shipping_sim_a',
    })
    async function confirmed(action: string, resource: string, version: number, parameters = {}) {
      const proposal = await api<{ confirmation_id: string }>(
        customer,
        '/action-proposals',
        'POST',
        { action, resource_ref: resource, expected_version: version, parameters },
      )
      return api<{ business_ref: string }>(
        customer,
        `/confirmations/${proposal.confirmation_id}/submit`,
        'POST',
        { decision: 'confirm' },
      )
    }
    const order = (await confirmed('order.create', quote.quote_id, 1)).business_ref
    await confirmed('order.pay', order, 1)
    await login(shop, 'shop_admin')
    const data = await api<{
      version: number
      items: { order_item_id: string; ordered_qty: number }[]
    }>(shop, '/shop/orders/' + order)
    await api(shop, `/shop/orders/${order}/shipments`, 'POST', {
      quantities: Object.fromEntries(data.items.map((i) => [i.order_item_id, i.ordered_qty])),
      expected_version: data.version,
    })
    await confirmed('order.receipt.confirm', order, 3)
    const before = BigInt(
      (await api<{ balance_minor: string }>(customer, '/me/wallet')).balance_minor,
    )
    await customer.goto('/orders/' + order)
    await customer.getByRole('button', { name: '申请退货退款', exact: true }).click()
    const form = customer.getByRole('dialog', { name: '退货退款', exact: true })
    await form.getByLabel('退货数量', { exact: true }).fill('1')
    await form.getByLabel('情况说明', { exact: true }).fill('模拟商品部分退货')
    await form.getByRole('button', { name: '核对申请', exact: true }).click()
    const submitted = customer.waitForResponse(
      (r) => r.request().method() === 'POST' && /\/confirmations\/[^/]+\/submit$/.test(r.url()),
    )
    await customer.getByRole('button', { name: '确认退货退款申请', exact: true }).click()
    const claim = (await (await submitted).json()).data
    await login(manager, 'supervisor')
    await api(manager, `/staff/tickets/${claim.ticket_id}/assignments`, 'POST', {
      staff_id: 'support_a',
      reason: '审核浏览器部分退货申请',
      expected_version: 1,
    })
    await login(staff, 'support_a')
    await staff.goto('/service')
    await staff.getByRole('button', { name: '查看申请', exact: true }).first().click()
    await staff.getByLabel('处理依据', { exact: true }).fill('核对本人收货、数量及适用政策')
    await staff.getByLabel('证据引用', { exact: true }).fill('policy:REQ-CS-v0.2')
    await staff.getByRole('button', { name: '审核通过', exact: true }).click()
    await expect(staff.getByText('待退回', { exact: true }).first()).toBeVisible()
    expect(
      BigInt((await api<{ balance_minor: string }>(customer, '/me/wallet')).balance_minor),
    ).toBe(before)
    await customer.goto('/service')
    await customer.getByRole('button', { name: '查看申请', exact: true }).first().click()
    await customer.getByLabel('退货承运商', { exact: true }).fill('模拟快递')
    await customer.getByLabel('退货单号', { exact: true }).fill('BROWSER-RETURN-001')
    await customer.getByRole('button', { name: '登记退货物流', exact: true }).click()
    await expect(customer.getByText('退货运输中', { exact: true }).first()).toBeVisible()
    expect(
      BigInt((await api<{ balance_minor: string }>(customer, '/me/wallet')).balance_minor),
    ).toBe(before)
    await shop.goto('/service')
    await shop.getByRole('button', { name: '查看申请', exact: true }).first().click()
    await shop.getByLabel('证据引用', { exact: true }).fill('inspection:browser-return-001')
    await shop.getByLabel('实收数量', { exact: true }).fill('1')
    await shop.getByLabel('可售数量', { exact: true }).fill('1')
    await shop.getByRole('button', { name: '确认实收并核对退款', exact: true }).click()
    await expect(shop.getByText('已退余额', { exact: true }).first()).toBeVisible()
    expect(
      BigInt((await api<{ balance_minor: string }>(customer, '/me/wallet')).balance_minor),
    ).toBe(before + 4000n)
    await customer.reload()
    await customer.getByRole('button', { name: '查看申请', exact: true }).first().click()
    await customer.screenshot({ path: resolve(evidence, '顾客-部分退货退款.png'), fullPage: true })
    expect(errors).toEqual([])
  } finally {
    await Promise.all(contexts.map((c) => c.close()))
  }
})
