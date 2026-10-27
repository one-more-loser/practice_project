import { test, expect } from '@playwright/test'
import { readFileSync, mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

// 此旅程必须搭配显式固定测试库，不能发布到应用库或覆盖历史B1证据。
test('B2：知识审核发布、真实SSE、关闭重开、引用和未配置模型', async ({ page }) => {
  test.skip(process.env.CS_VERIFICATION_TARGET !== 'test', '只允许固定测试库')
  const root = resolve(process.cwd(), '..')
  const accounts = JSON.parse(readFileSync(resolve(root, '.runtime/demo-accounts.json'), 'utf8'))
  const evidence = resolve(
    root,
    'docs/06-acceptance',
    process.env.CS_EVIDENCE_SCOPE === 'V1-edge'
      ? 'V1-edge'
      : process.env.CS_EVIDENCE_SCOPE === 'V1-business'
        ? 'V1-business'
        : 'B2',
  )
  mkdirSync(evidence, { recursive: true })
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto('/login')
  await page.getByLabel('账户', { exact: true }).fill('knowledge_admin')
  await page.getByLabel('密码', { exact: true }).fill(accounts.knowledge_admin)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('link', { name: '知识管理', exact: true }).click()
  const row = page.getByRole('row').filter({ hasText: '在哪里查看订单？' })
  await expect(row).toHaveCount(1)
  await row.getByRole('button', { name: '查看内容' }).click()
  const preview = page.getByRole('dialog', { name: '审核内容' })
  await expect(preview).toContainText('登录后进入')
  await preview.getByRole('button', { name: 'Close this dialog' }).click()
  await row.getByRole('button', { name: '审核通过' }).click()
  await row.getByRole('button', { name: '发布', exact: true }).click()
  await expect(row).toContainText('published')
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await page.getByRole('button', { name: '店铺客服', exact: true }).click()
  const chat = page.getByRole('dialog', { name: '店铺客服', exact: true })
  await expect(chat.getByLabel('咨询内容')).toBeEnabled()
  await chat.getByLabel('咨询内容').fill('在哪里查看订单？')
  const accepted = page.waitForResponse(
    (r) => r.request().method() === 'POST' && /\/conversations\/[^/]+\/messages$/.test(r.url()),
  )
  await chat.getByRole('button', { name: '发送', exact: true }).click()
  expect((await accepted).status()).toBe(202)
  await chat.getByRole('button', { name: 'Close this dialog' }).click()
  await page.getByRole('button', { name: '店铺客服', exact: true }).click()
  await expect(chat.locator('.chat-message.assistant')).toHaveCount(1)
  await expect(chat.locator('.chat-message.assistant')).toContainText('登录后进入')
  await expect(chat.locator('.chat-message.customer')).toHaveCount(1)
  await chat.getByRole('button', { name: '[1] 在哪里查看订单？', exact: true }).click()
  const source = page.getByRole('dialog', { name: '资料依据' })
  await expect(source).toContainText('REQ-CS-v0.2')
  await source.getByRole('button', { name: 'Close this dialog' }).click()
  await expect(source).not.toBeVisible()
  await page.screenshot({ path: resolve(evidence, '客服-真实SSE与引用.png'), fullPage: true })
  await chat.getByLabel('咨询内容').fill('请介绍没有发布资料的未知商品保修年限')
  await chat.getByRole('button', { name: '发送', exact: true }).click()
  await expect(chat.getByRole('alert')).toContainText('咨询服务尚未配置')
  await expect(chat.locator('.chat-message.assistant')).toHaveCount(1)
  expect(errors).toEqual([])
})
