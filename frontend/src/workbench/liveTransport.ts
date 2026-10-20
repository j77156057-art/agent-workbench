/**
 * 实时流的 WebSocket 传输层。
 *
 * 这是 R0 协议冻结范围之外**唯一**的新代码：协议包字段全在 `realtimeProtocol.ts`，
 * 这里只负责「一条连接的生命周期」——握手（hello）、心跳、事件上行分发、
 * 断线重连、会话代数。
 *
 * 为什么单独抽一层：`liveStreamControl.ts` 里的自适应发送、在飞账本、相位判定
 * 全是**纯逻辑**（可脱离浏览器做行为契约测试），它们不知道 socket 的存在；
 * 而接线层（`useLiveSession`）既要管采集，又要管协议，不该再自己处理重连。
 *
 * 会话代数（generation）是这里最重要的一个概念：
 * 编码一帧 JPEG 是异步的，重连/关闭会让"旧会话的产物"晚到一步。
 * 接线层每捕获一帧就记下当时的代数，发包前再问一次 `isCurrent(gen)`，
 * 晚到的旧帧就地丢弃 —— 否则会把上一条连接的画��推进新会话。
 */
import {
  parseRealtimeServerEvent,
  realtimeCancel,
  realtimeHeartbeat,
  realtimeHello,
  realtimeSessionClose,
  type RealtimeClientControl,
  type RealtimeServerEvent,
} from './realtimeProtocol'

export type LiveSocketState = 'closed' | 'connecting' | 'open'

/** 默认心跳间隔：15s。后端对心跳只回 echo，不做业务。 */
const DEFAULT_HEARTBEAT_MS = 15_000
/** 断线重连退避：1s → 2s → 4s → 8s → 封顶 10s。 */
const DEFAULT_RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 10000]

export interface LiveTransportOptions {
  projectId: string
  sessionId?: string
  /** 服务端事件（已按 R0 解析并校验过 v/type/sent_at）。 */
  onEvent: (event: RealtimeServerEvent) => void
  onState?: (state: LiveSocketState) => void
  /** 面向用户的一句话提示（重连、降级等），不是协议错误。 */
  onNotice?: (message: string) => void
  heartbeatMs?: number
  reconnectDelaysMs?: number[]
  /** 覆盖连接地址（测试用）。默认按当前页面 origin 推导。 */
  buildUrl?: (projectId: string) => string
}

export interface LiveTransport {
  connect(): void
  /** 主动关闭（不再重连）。 */
  close(reason?: string): void
  sendControl(packet: RealtimeClientControl): boolean
  sendBinary(packet: ArrayBuffer): boolean
  /** 发送缓冲中未发出的字节数：背压自适应的输入。 */
  bufferedAmount(): number
  socketState(): LiveSocketState
  /** 当前会话代数；每次（重）连接或关闭后自增。 */
  generation(): number
  /** 这个代数是否仍是当前会话 —— 接线层用来丢弃过期产物。 */
  isCurrent(generation: number): boolean
}

function defaultUrl(projectId: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/vision/live-stream?project_id=${encodeURIComponent(projectId)}`
}

export function createLiveTransport(options: LiveTransportOptions): LiveTransport {
  const heartbeatMs = options.heartbeatMs ?? DEFAULT_HEARTBEAT_MS
  const reconnectDelays = options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS
  const buildUrl = options.buildUrl ?? defaultUrl

  let socket: WebSocket | null = null
  let state: LiveSocketState = 'closed'
  let intentionalClose = false
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectAttempt = 0
  let gen = 0
  const sessionId = options.sessionId ?? ''

  const setState = (next: LiveSocketState) => {
    if (state === next) return
    state = next
    options.onState?.(next)
  }

  const clearTimers = () => {
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  }

  const scheduleReconnect = () => {
    if (intentionalClose) return
    const delay = reconnectDelays[Math.min(reconnectAttempt, reconnectDelays.length - 1)]
    reconnectAttempt += 1
    setState('connecting')
    options.onNotice?.(`实时连接已断开，${Math.round(delay / 1000)} 秒后重连…`)
    reconnectTimer = setTimeout(() => { reconnectTimer = null; open() }, delay)
  }

  const open = () => {
    if (!options.projectId) {
      options.onNotice?.('没有选择项目，实时对话不可用。')
      setState('closed')
      return
    }
    let ws: WebSocket
    try {
      ws = new WebSocket(buildUrl(options.projectId))
    } catch {
      scheduleReconnect()
      return
    }
    socket = ws
    gen += 1
    setState('connecting')

    ws.onopen = () => {
      if (socket !== ws) return
      setState('open')
      reconnectAttempt = 0
      // 握手必须先发 hello：后端在收到 hello 之前收到任何东西都会关连接（1008）。
      ws.send(JSON.stringify(realtimeHello(options.projectId, sessionId)))
      if (heartbeatTimer) clearInterval(heartbeatTimer)
      heartbeatTimer = setInterval(() => {
        if (socket === ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify(realtimeHeartbeat()))
        }
      }, heartbeatMs)
    }

    ws.onmessage = (message: MessageEvent) => {
      if (socket !== ws) return
      // 后端只发 JSON 文本事件（音频 delta 也走 base64 文本，见 realtime_bridge.wire_events）。
      if (typeof message.data !== 'string') return
      const event = parseRealtimeServerEvent(message.data)
      if (!event) return
      options.onEvent(event)
    }

    ws.onerror = () => {
      // onerror 之后一定会有 onclose，重连逻辑统一在 onclose 里做。
    }

    ws.onclose = () => {
      if (socket !== ws) return
      socket = null
      if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null }
      setState('closed')
      scheduleReconnect()
    }
  }

  return {
    connect() {
      intentionalClose = false
      reconnectAttempt = 0
      if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return
      open()
    },
    close(reason = '') {
      intentionalClose = true
      clearTimers()
      gen += 1
      const ws = socket
      socket = null
      setState('closed')
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify(realtimeSessionClose(reason))) } catch { /* 关了就算了 */ }
      }
      try { ws?.close() } catch { /* ignore */ }
    },
    sendControl(packet: RealtimeClientControl) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return false
      try { socket.send(JSON.stringify(packet)); return true } catch { return false }
    },
    sendBinary(packet: ArrayBuffer) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return false
      try { socket.send(packet); return true } catch { return false }
    },
    socketState() { return state },
    bufferedAmount() {
      // socket 已关闭时按"清空"计：积压无从谈起，不该继续给自适应器加压。
      return socket && socket.readyState === WebSocket.OPEN ? socket.bufferedAmount : 0
    },
    generation() { return gen },
    isCurrent(generation: number) { return generation === gen },
  }
}

/** 抢话/取消用的小工具（接线层调用，避免直接依赖 realtimeProtocol 的细节）。 */
export function sendLiveCancel(transport: LiveTransport, reason = ''): boolean {
  return transport.sendControl(realtimeCancel(reason))
}
