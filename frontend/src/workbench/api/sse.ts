// SSE 事件类型与 POST/GET 流式通道（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { FsApiError, withProject } from './_base'
import type { BrowserEvidence, ChatAsk, ChatArtifact } from '../composables/chat-types.ts'

// ---------------------------------------------------------------- P2：选区 AI（SSE 流式）
export interface SseEvent {
  type:
    | 'token' | 'thought' | 'action' | 'observation' | 'reflection'
    | 'reasoning' | 'notice' | 'plan' | 'route' | 'final' | 'done'
    | 'context' | 'turn' | string
  text?: string
  /** notice 专用：true = **运行期进度**（「正在继续执行」这类，回合结束就成假陈述），
   *  只进流式中的运行指示；缺省 / false = 本轮**事实**（输出被截断、上下文已压缩），
   *  留在通知里。语义由发事件的后端定，前端不再比文案。 */
  transient?: boolean
  status?: 'error' | string
  /** P1：重试层错误分类
   *  - "permanent" — 不可恢复（fallback 也失败 / 非网络错误）→ 红条
   *  - "retried_failed" — 重试用尽 + 无 fallback → 黄条
   *  - 其他 — 旧的「服务异常」灰条
   *  旧的「服务异常」/异常类名字符串继续兼容，未识别按灰条处理。 */
  error_kind?: 'permanent' | 'retried_failed' | string
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
  /** P2：type=turn 时携带本轮的 turn_id，便于刷新后 GET partial 拉起 */
  turn_id?: string
  /** 精确断点续跑：失败/降级回合的 checkpoint id；续跑时回传 resume_checkpoint。 */
  checkpoint_id?: string
  session_id?: string
  /** P0：final 事件自带的「observation 摘要命中率」自检结果。
   *  "warn" 时前端在 .cd-answer 顶部加一行不显眼但可见的提示。 */
  consistency?: 'ok' | 'warn' | string
  /** 自检命中率（0..1），与 consistency 一起下发，便于前端调试。 */
  consistency_ratio?: number
  /** 与命中率正交的「原因」标签：如 'verbatim_dump'（回答以逐行裸铺源码为主、未综合）。
   *  缺省=无特殊原因。前端据此显示语义正确的提示，而非复用覆盖率徽章的文案。 */
  consistency_reason?: string
  /** final：本轮是「澄清反问」（后端未起任何工具/provider，直接请用户补充）。
   *  前端据此抑制「猜你想问」等把澄清当正常答案的下游渲染（BUG-3）。 */
  clarification_required?: boolean
  /** final：本轮为降级收尾（信息不足 / 未核验等），非一次干净的完整交付。 */
  degraded?: boolean
  /** 结构化「向用户提问 / 请求授权」载荷：终态澄清 final 与 needs_confirmation
   *  事件都可携带；前端渲染成可点选项框，点某项即把其 value 作为下一轮消息发回。 */
  ask?: ChatAsk
  /**
   * 本轮产物（模型写的文件、截的图）。
   *
   * 挂在 **observation** 事件上：产物是工具落盘/截图那一刻自报的，
   * 跟着那一次观察走最准（action 文本里也可能带 path，但被安全拦截的调用
   * 同样会带 path —— 照文本反推会把没做的记成做了）。
   */
  artifacts?: ChatArtifact[]
  /**
   * 工具这一步的**真实成败**（observation 事件专用）。
   *
   * 后端 execute_tool 已经算出这个布尔值，随观察一起下发。前端据此给时间线
   * 那一行染色，**不再从返回正文里 grep「错误/失败/超时」**——read_file 读到的
   * 源码/日志天然含这些字，历史上把一次成功的读取整排标红。缺省（旧后端 /
   * 非工具观察）时前端回落到只认回包开头的失败签名。
   */
  ok?: boolean
  /**
   * 浏览器动作留痕（observation 事件专用）：哪一页、第几步、动作前后
   * 各截了什么图。图片只给相对路径，避免把 base64 塞进事件流。
   */
  browser_evidence?: BrowserEvidence
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
    throw new FsApiError(0, '无法连接本地服务（127.0.0.1:8000），请确认 Agent 工作台 已启动。')
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
  onProtocolError?: (info: { kind: 'malformed_data'; payload: string; count: number }) => void,
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
    throw new FsApiError(0, '无法连接本地服务（127.0.0.1:8000），请确认 Agent 工作台 已启动。')
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
  // 协议畸形计数：之前每条 malformed data 行被静默吞掉，代理截断 /
  // 后端协议改动时客户端没有错误信号，只能等 EOF/重连。累计到阈值
  // 后通知调用方，让 UI 决定重连或展示错误。
  let malformedCount = 0
  const MALFORMED_REPORT_THRESHOLD = 5
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
        malformedCount++
        // 心跳行（`:` 开头的注释）由 line 不以 `data:` 过滤掉；这条
        // 已经走到 JSON.parse 失败，说明是后端发了我们认不出的 data
        // 行。报错出来比静默吞掉更安全——长跑 SSE 看不到业务事件时，
        // 用户至少知道协议变了。
        if (onProtocolError && malformedCount === MALFORMED_REPORT_THRESHOLD) {
          onProtocolError({ kind: 'malformed_data', payload, count: malformedCount })
        }
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
