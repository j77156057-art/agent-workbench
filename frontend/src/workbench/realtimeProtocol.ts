export const REALTIME_PROTOCOL_VERSION = 1 as const

export type RealtimeClientControl =
  | { v: 1; type: 'hello'; project_id: string; session_id?: string; capabilities: string[] }
  | { v: 1; type: 'heartbeat'; sent_at: number }
  | { v: 1; type: 'cancel'; reason?: string }
  | { v: 1; type: 'session.close'; reason?: string }
  | { v: 1; type: 'agent.result'; request_id: string; text: string }

export type RealtimeServerEvent = {
  v: 1
  type: 'hello.ok' | 'heartbeat' | 'video.observation' | 'model.observation' | 'audio.transcript' | 'model.delta' | 'model.audio' | 'error' | 'cancel.ok' | 'session.closed' | 'agent.result.ok'
  sent_at: number
  sequence?: number
  captured_at?: number
  session_id?: string
  [key: string]: unknown
}

/**
 * `hello.ok` 在 M2·③ 之后**保证携带**的模式字段（`realtime_bridge.SessionBridge.hello_fields`）。
 * 前端接 WS 时把 `event` 转成这个窄类型直接喂 `describeRealtimeMode`，降级就不再隐形。
 * `degraded_to` 后端为 null 或字符串；未知态用 `null` 表达，不用 `undefined`。
 */
export interface HelloOkEvent extends RealtimeServerEvent {
  type: 'hello.ok'
  mode: 'native-realtime' | 'sampled-frames' | 'unavailable' | string
  degraded_to: string | null
  reason: string
  provider: string
  provider_capabilities: string[]
}

export function isHelloOk(event: RealtimeServerEvent): event is HelloOkEvent {
  return event.type === 'hello.ok'
}

export function realtimeHello(projectId: string, sessionId = ''): RealtimeClientControl {
  return { v: REALTIME_PROTOCOL_VERSION, type: 'hello', project_id: projectId,
    ...(sessionId ? { session_id: sessionId } : {}), capabilities: ['video.frame', 'audio.chunk', 'interrupt'] }
}

export function realtimeHeartbeat(): Extract<RealtimeClientControl, { type: 'heartbeat' }> {
  return { v: REALTIME_PROTOCOL_VERSION, type: 'heartbeat', sent_at: Date.now() }
}

export function realtimeCancel(reason = ''): Extract<RealtimeClientControl, { type: 'cancel' }> {
  return { v: REALTIME_PROTOCOL_VERSION, type: 'cancel', ...(reason ? { reason } : {}) }
}

export function realtimeSessionClose(reason = ''): Extract<RealtimeClientControl, { type: 'session.close' }> {
  return { v: REALTIME_PROTOCOL_VERSION, type: 'session.close', ...(reason ? { reason } : {}) }
}

export function isRealtimeEventType<T extends RealtimeServerEvent['type']>(
  event: RealtimeServerEvent, type: T,
): event is RealtimeServerEvent & { type: T } {
  return event.type === type
}

export function encodeVideoFrame(sequence: number, capturedAt: number, payload: Blob, focused = false): Promise<ArrayBuffer> {
  return payload.arrayBuffer().then(bytes => {
    const header = new TextEncoder().encode(JSON.stringify({ v: REALTIME_PROTOCOL_VERSION,
      type: 'video.frame', sequence, captured_at: capturedAt, focused }) + '\n')
    const packet = new Uint8Array(header.length + bytes.byteLength)
    packet.set(header); packet.set(new Uint8Array(bytes), header.length)
    return packet.buffer
  })
}

export function parseRealtimeServerEvent(raw: unknown): RealtimeServerEvent | null {
  if (typeof raw !== 'string') return null
  try {
    const event = JSON.parse(raw) as RealtimeServerEvent
    return event && event.v === REALTIME_PROTOCOL_VERSION && typeof event.type === 'string' && typeof event.sent_at === 'number'
      ? event : null
  } catch { return null }
}
