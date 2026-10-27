import { afterEach, expect, it, vi } from 'vitest'
import { subscribe } from '../src/features/support/realtime'

class Socket {
  static current: Socket
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => Promise<void>) | null = null
  onclose: (() => void) | null = null
  sent: { type: string; seq?: number }[] = []
  constructor() { Socket.current = this }
  send(body: string) { this.sent.push(JSON.parse(body)) }
  close() { this.onclose?.() }
}
afterEach(() => vi.unstubAllGlobals())

it('late update completion cannot regress the acknowledged event cursor', async () => {
  vi.stubGlobal('WebSocket', Socket)
  const done: (() => void)[] = []
  const update = vi.fn(() => new Promise<void>((resolve) => done.push(resolve)))
  const stop = subscribe('conv_test', 1, update, vi.fn())
  const s = Socket.current
  const frame = (seq: number) => ({ data: JSON.stringify({ type: 'event', event: { seq } }) })
  const a = s.onmessage!(frame(2)), b = s.onmessage!(frame(3))
  done[1]!(); await b
  done[0]!(); await a
  expect(s.sent.filter((v) => v.type === 'delivery.ack').map((v) => v.seq)).toEqual([3])
  await s.onmessage!(frame(3))
  expect(update).toHaveBeenCalledTimes(2)
  stop()
})

it('disposing a subscription suppresses an acknowledgement from an in-flight update', async () => {
  vi.stubGlobal('WebSocket', Socket)
  let done!: () => void
  const stop = subscribe('conv_test', 1, () => new Promise<void>((resolve) => { done = resolve }), vi.fn())
  const s = Socket.current
  const pending = s.onmessage!({ data: JSON.stringify({ type: 'event', event: { seq: 1 } }) })
  stop(); done(); await pending
  expect(s.sent).toEqual([])
})
