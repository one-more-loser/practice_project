import { csrfToken, errorText } from '../../lib/api'

// 数据库事件游标用于补偿，WebSocket只承担投递。调用者在重连时重读权威历史。
export function subscribe(
  ref: string,
  epoch: number,
  onUpdate: () => Promise<void>,
  onError: (s: string) => void,
) {
  let socket: WebSocket | null = null,
    stopped = false,
    after = 0,
    timer: ReturnType<typeof setTimeout> | undefined
  const connect = () => {
    if (stopped) return
    socket = new WebSocket(
      `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/v1/realtime`,
    )
    socket.onopen = () =>
      socket?.send(
        JSON.stringify({
          type: 'hello',
          csrf_token: csrfToken(),
          subscriptions: [{ conversation_id: ref, after_seq: after, expected_epoch: epoch }],
        }),
      )
    socket.onmessage = async ({ data }) => {
      try {
        const frame = JSON.parse(data)
        if (frame.type === 'ping') socket?.send(JSON.stringify({ type: 'pong' }))
        if (frame.type === 'access.revoked' || frame.type === 'error') {
          onError(frame.message)
          stopped = true
          socket?.close()
        }
        if (frame.type === 'mode.sync') {
          epoch = frame.mode_epoch
          await onUpdate()
        }
        if (frame.type === 'ready') await onUpdate()
        if (frame.type === 'event' && frame.event.seq > after) {
          await onUpdate()
          // 同时到达的事件可能倒序完成；卸载或更高游标完成后不再确认旧事件。
          if (stopped || frame.event.seq <= after) return
          after = frame.event.seq
          socket?.send(JSON.stringify({ type: 'delivery.ack', conversation_id: ref, seq: after }))
        }
      } catch (e) {
        stopped = true
        onError(errorText(e))
        socket?.close()
      }
    }
    socket.onclose = () => {
      if (!stopped) timer = setTimeout(connect, 1500)
    }
  }
  connect()
  return () => {
    stopped = true
    clearTimeout(timer)
    socket?.close()
  }
}
