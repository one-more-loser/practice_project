import { test, expect } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

test('DeepSeek/Qwen：真实政策检索、引用和游客登录引导', async ({ page }) => {
  const failures: string[] = []
  const directModelCalls: string[] = []
  page.on('pageerror', (error) => failures.push(error.message))
  page.on('request', (request) => {
    const target = new URL(request.url())
    if (target.hostname === 'api.deepseek.com' || target.port === '8010') {
      directModelCalls.push(target.host)
    }
  })
  await page.goto('/')
  await page.getByRole('button', { name: '店铺客服', exact: true }).click()
  const chat = page.getByRole('dialog', { name: '店铺客服', exact: true })
  await chat.getByLabel('咨询内容').fill('没发货前，每个订单允许修改几次收货地址？')
  await chat.getByRole('button', { name: '发送', exact: true }).click()
  await expect(chat.locator('.chat-message.assistant')).toHaveCount(1, { timeout: 60000 })
  await expect(chat.locator('.chat-message.assistant')).toContainText('一次')
  const citations = chat.locator('.chat-citations button')
  await expect(citations.first()).toBeVisible()
  await citations.first().click()
  const source = page.getByRole('dialog', { name: '资料依据', exact: true })
  await expect(source).toContainText('REQ-CS-v0.2')
  await source.getByRole('button', { name: 'Close this dialog' }).click()
  await expect(source).not.toBeVisible()
  const evidence = resolve('..', 'docs/06-acceptance/models')
  mkdirSync(evidence, { recursive: true })
  await chat.locator('.chat-history').evaluate((element) => { element.scrollTop = 0 })
  await page.screenshot({ path: resolve(evidence, '真实模型客服与引用.png') })
  await chat.getByLabel('咨询内容').fill('请查询我的订单和账户余额')
  await chat.getByRole('button', { name: '发送', exact: true }).click()
  await expect(chat.locator('.chat-message.assistant')).toHaveCount(2, { timeout: 60000 })
  await expect(chat.locator('.chat-message.assistant').last()).toContainText('登录')
  expect(failures).toEqual([])
  expect(directModelCalls).toEqual([])
})

for (const question of [
  '我想了解店铺规则：订单还没寄出，可以改几回收货地址？',
  '按店铺政策，一个订单已经改过一次地址，还可以改第二次吗？',
  '请查询公开政策：发货以后还能修改收货地址吗？',
  '店铺政策以什么操作作为已经发货的界限？',
]) {
  test(`公开政策路由：${question}`, async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: '店铺客服', exact: true }).click()
    const chat = page.getByRole('dialog', { name: '店铺客服', exact: true })
    await chat.getByLabel('咨询内容').fill(question)
    await chat.getByRole('button', { name: '发送', exact: true }).click()
    await expect(chat.locator('.chat-message.assistant')).toHaveCount(1, { timeout: 60000 })
    await expect(chat.locator('.chat-citations button').first()).toBeVisible()
    await expect(chat.locator('.chat-message.assistant')).toContainText('发货')
  })
}
