// SSE 事件类型与 POST/GET 流式通道（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { FsApiError, withProject } from './_base'

// ---------------------------------------------------------------- P2：选区 AI（SSE 流式）
export interface SseEvent {
  type:
    | 'token' | 'thought' | 'action' | 'observation' | 'reflection'
    | 'reasoning' | 'notice' | 'plan' | 'route' | 'final' | 'done'
    | 'context' | string
  text?: string
  status?: 'error' | string
  error_kind?: string
  steps?: string[]
  /** type=context 时的上下文窗口用量 */
  used_tokens?: number
  context_window?: number
  prompt_budget?: number
  percent?: number
  level?: 'ok' | 'high' | 'warn' | string
  /** 会话历史 token（与压缩触发口径同源） */
  history_tokens?: number
  compact_trigger_tokens?: number
  compact_percent?: number
}

/** 上下文窗口占用（GET /api/context 与 SSE context 事件同构） */
export interface ContextUsage {
  used_tokens: number
  context_window: number
  prompt_budget: number
  percent: number
  level: 'ok' | 'high' | 'warn'
  /** 会话历史 token 占用（压缩口径，可选：旧后端可能不下发） */
  history_tokens?: number
  /** 压缩触发线（= prompt_budget × COMPACT_TRIGGER_RATIO） */
  compact_trigger_tokens?: number
  /** 历史 token / 压缩触发线（0-100）：level 即由此分级 */
  compact_percent?: number
}

export interface SseStreamHandlers {
  onEvent: (ev: SseEvent) => void
  signal?: AbortSignal
}

export interface SelectionAiRequest {
  path: string
  lang: string
  start_line: number
  end_line: number
  selection: string
  /** 改写指令（rewrite 快通道）；解释/Review/提问走 agent 不用此结构 */
  instruction?: string
  file_context?: string
  task_id?: string
  task_region?: string
  allowed_paths?: string[]
  verification?: string[]
  engine?: string
}

/**
 * 以 POST 发起 SSE：预检错误（400/409/413 等非 event-stream 响应）读成 JSON 抛 FsApiError；
 * 流开始后的错误由 final 事件承载。AbortError 原样上抛，由调用方区分"用户停止"。
 */
export async function postSse(url: string, init: RequestInit, h: SseStreamHandlers): Promise<void> {
  let res: Response
  try {
    res = await fetch(url, withProject({ ...init, signal: h.signal }))
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    throw new FsApiError(0, '无法连接本地服务（127.0.0.1:8000），请确认 DocMind 已启动。')
  }
  const ctype = res.headers.get('content-type') || ''
  if (!res.ok || !ctype.includes('text/event-stream')) {
    let body: { error?: string } | null = null
    try {
      body = await res.json()
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new FsApiError(res.status, body?.error || `请求失败（HTTP ${res.status}）`, (body as Record<string, unknown>) || {})
  }
  if (!res.body) throw new FsApiError(0, '服务未返回数据流。')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let streamDone = false
  const handleChunk = (chunk: string) => {
    for (const raw of chunk.split('\n')) {
      const line = raw.trimStart()
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload || payload === '[DONE]') continue
      try {
        const event = JSON.parse(payload) as SseEvent
        if (event.type === 'done') streamDone = true
        h.onEvent(event)
      } catch {
        /* 忽略半条/非 JSON 心跳 */
      }
    }
  }
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let sep: number
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      handleChunk(chunk)
    }
  }
  buf += decoder.decode()
  if (buf.trim()) handleChunk(buf)
  if (!streamDone && !h.signal?.aborted) {
    throw new FsApiError(0, '流式连接在服务完成前中断；已收到的内容已保留，请重试并检查服务日志。')
  }
}

/**
 * 以 GET 订阅 SSE（工作流实时事件用）：预检错误读成 JSON 抛 FsApiError；
 * AbortError 原样上抛，由调用方区分"主动退订/终态自关"与真实故障。
 */
export async function openSse(
  url: string,
  signal: AbortSignal,
  onEvent: (ev: SseEvent) => void,
): Promise<void> {
  let res: Response
  try {
    res = await fetch(url, withProject({
      method: 'GET',
      headers: { Accept: 'text/event-stream' },
      signal,
    }))
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    throw new FsApiError(0, '无法连接本地服务（127.0.0.1:8000），请确认 DocMind 已启动。')
  }
  const ctype = res.headers.get('content-type') || ''
  if (!res.ok || !ctype.includes('text/event-stream')) {
    let body: { error?: string } | null = null
    try {
      body = await res.json()
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new FsApiError(res.status, body?.error || `请求失败（HTTP ${res.status}）`, (body as Record<string, unknown>) || {})
  }
  if (!res.body) throw new FsApiError(0, '服务未返回数据流。')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let streamDone = false
  const handleChunk = (chunk: string) => {
    for (const raw of chunk.split('\n')) {
      const line = raw.trimStart()
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload || payload === '[DONE]') continue
      try {
        const event = JSON.parse(payload) as SseEvent
        if ((event as unknown as { kind?: string }).kind === '__stream_done__') streamDone = true
        onEvent(event)
      } catch {
        /* 忽略半条/非 JSON 心跳 */
      }
    }
  }
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let sep: number
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      handleChunk(chunk)
    }
  }
  buf += decoder.decode()
  if (buf.trim()) handleChunk(buf)
  if (!streamDone && !signal.aborted) {
    throw new FsApiError(0, '工作流事件流在终态前中断；请重新打开工作流查看最新状态。')
  }
}
