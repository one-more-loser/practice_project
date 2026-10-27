import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import FoundationPage from '../src/app/FoundationPage.vue'

afterEach(() => vi.unstubAllGlobals())
describe('工程状态入口', () => {
  it('数据库错误时不能显示可用', async () => {
    vi.stubGlobal('fetch', async () => ({
      ok: false,
      json: async () => ({ data: { database: 'unready' } }),
    }))
    const wrapper = mount(FoundationPage, { global: { plugins: [ElementPlus] } })
    await wrapper.get('button').trigger('click')
    await vi.waitFor(() => expect(wrapper.get('[role=status]').text()).toContain('数据库尚未就绪'))
    wrapper.unmount()
  })
  it('断网时显示失败并允许重试', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new Error('offline')
    })
    const wrapper = mount(FoundationPage, { global: { plugins: [ElementPlus] } })
    await wrapper.get('button').trigger('click')
    await vi.waitFor(() => expect(wrapper.get('[role=status]').text()).toContain('服务连接失败'))
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })
})
