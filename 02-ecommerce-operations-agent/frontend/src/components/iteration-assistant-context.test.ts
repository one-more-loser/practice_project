import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AssistantPanel from './AssistantPanel.vue'
import { api } from '../api'

afterEach(() => { vi.unstubAllGlobals(); api.resetSession() })
const answer = (value: string) => new Response(JSON.stringify({ data: {
  answer: value, mode: 'deterministic', evidence: [], missing: [],
} }))

it('切换方案立即清除旧答案，迟到的旧方案结果不得附着到新方案', async () => {
  let resolve!: (value: Response) => void
  // fetch故意忽略AbortSignal，验证组件本身的上下文防线。
  vi.stubGlobal('fetch', () => new Promise<Response>(done => { resolve = done }))
  const view = mount(AssistantPanel, { props: { snapshotId: 's1', planId: 'A' } })
  try {
    await view.get('textarea').setValue('当前方案如何')
    await view.get('form').trigger('submit')
    await view.setProps({ planId: 'B' })
    resolve(answer('旧方案A的利润解释'))
    await flushPromises()
    expect(view.text()).not.toContain('旧方案A的利润解释')
    expect(view.find('.assistant-answer').exists()).toBe(false)
  } finally { view.unmount() }
})

it('切换方案也清除已经完成的旧答案，新方案仍可重新问数', async () => {
  vi.stubGlobal('fetch', async (_path: string, options: RequestInit) => {
    const body = JSON.parse(String(options.body))
    return answer(`方案${body.plan_id}的解释`)
  })
  const view = mount(AssistantPanel, { props: { snapshotId: 's1', planId: 'A' } })
  try {
    await view.get('textarea').setValue('当前方案如何')
    await view.get('form').trigger('submit'); await flushPromises()
    expect(view.text()).toContain('方案A的解释')
    await view.setProps({ planId: 'B' })
    expect(view.text()).not.toContain('方案A的解释')
    await view.get('form').trigger('submit'); await flushPromises()
    expect(view.text()).toContain('方案B的解释')
  } finally { view.unmount() }
})
