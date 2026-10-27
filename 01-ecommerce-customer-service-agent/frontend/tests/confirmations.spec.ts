import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import ActionConfirm from '../src/components/ActionConfirm.vue'
import { ApiError } from '../src/lib/api'
const { submit } = vi.hoisted(() => ({ submit: vi.fn() }))
vi.mock('../src/lib/api', async (original) => ({
  ...(await original<typeof import('../src/lib/api')>()),
  mutate: submit,
}))
beforeEach(() => { submit.mockReset() })
function dialog() {
  return mount(ActionConfirm, {
    props: {
      title: '余额支付',
      proposal: {
        confirmation_id: 'test_confirmation',
        summary: { action: 'order.pay', resource_ref: 'order_test', payable_minor: '4000' },
        expires_at: '2026-10-22T04:00:00Z',
      },
    },
    global: {
      stubs: { ElDialog: { template: '<section><slot/><slot name="footer"/></section>' } },
    },
  })
}
it('未完成的确认只提交一次', async () => {
  let finish: (result: unknown) => void = () => {}
  submit.mockImplementation(() => new Promise((resolve) => (finish = resolve)))
  const wrapper = dialog()
  await wrapper.get('button.primary').trigger('click')
  await wrapper.get('button.primary').trigger('click')
  expect(submit).toHaveBeenCalledTimes(1)
  finish({ operation_id: 'op_test', business_ref: 'order_test', version: 2 })
  await flushPromises()
  expect(wrapper.emitted('done')).toHaveLength(1)
  wrapper.unmount()
})
it('业务明确拒绝后可以关闭并重新核对', async () => {
  submit.mockRejectedValue(new ApiError('内容已更新，请刷新并重新确认', 409, 'version_conflict'))
  const wrapper = dialog()
  await wrapper.get('button.primary').trigger('click')
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('内容已更新')
  expect(wrapper.get('button.primary').attributes('disabled')).toBeDefined()
  await wrapper.findAll('button')[0].trigger('click')
  expect(wrapper.emitted('close')).toHaveLength(1)
  wrapper.unmount()
})
