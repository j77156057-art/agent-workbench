/**
 * R1/R6 前端媒体模块：实时视频流的自适应发送与会话状态归类。
 *
 * 不包含任何协议包字段（那在 realtimeProtocol.ts，属 R0 冻结范围）。这里全部是
 * 纯逻辑，由 AutonomousCockpit.vue 驱动，可脱离浏览器直接做行为契约测试。
 * 「正在听」属 R2 音频链路，本模块暂不产生该状态。
 */

export interface LiveSendSample {
  /** WebSocket 发送缓冲中未发出的字节数 */
  bufferedAmount: number
  /** 帧采集 → 其观察结果返回的端到端延迟 ms（仅已观测帧提供） */
  latencyMs?: number | null
  /** 服务端本帧明确限流（video.observation.throttled） */
  throttled?: boolean
  /** 服务端限流退避秒数（retry_after） */
  retryAfterS?: number | null
  /** 在飞帧滞留超过两个发送周期：编码/网络跟不上采集 */
  encodeOverrun?: boolean
  /** 原生通道没有逐帧回执：编码及时且网络队列低时可恢复档位。 */
  transportHealthy?: boolean
}

export interface LiveSendPlan {
  /** 下一帧的发送间隔，ms */
  intervalMs: number
  /** 采集缩放的长边上限，px */
  maxEdge: number
  /** JPEG 质量，0-1 */
  quality: number
}

const INTERVALS = [300, 500, 900, 1500, 2600, 4000]
const EDGES = [1600, 1280, 960, 640, 480]
const QUALITIES = [0.85, 0.8, 0.72, 0.62, 0.5]

const DEFAULT_LEVEL = 1 // 500ms / 1280px / 0.8：与本模块接入前的固定行为一致
const PRESSURE_LATENCY_MS = 6000
const PRESSURE_BUFFER_BYTES = 250_000
const HEALTHY_LATENCY_MS = 2500
const HEALTHY_BUFFER_BYTES = 60_000
const HEALTHY_STREAK = 4 // 恢复要连续健康 4 次才升一档，避免抖动
const THROTTLE_FLOOR_MAX_MS = 8000

export const LIVE_VIDEO_BUFFER_LIMIT = 1_000_000

/** 预算包括即将加入队列的整包，避免一张大图把音频也堵在后面。 */
export function canQueueLiveVideo(bufferedAmount: number, packetBytes = 0, limit = LIVE_VIDEO_BUFFER_LIMIT): boolean {
  return bufferedAmount + packetBytes < limit
}

/** 编码会异步让出执行权；暂停、重连或圈选后，旧包不得进入新会话。 */
export async function sendLiveVideoPacket(input: {
  encode(): Promise<ArrayBuffer>
  isCurrent(): boolean
  bufferedAmount(): number
  send(packet: ArrayBuffer): void
  bufferLimit?: number
}): Promise<'sent' | 'stale' | 'backpressure'> {
  if (!input.isCurrent()) return 'stale'
  if (!canQueueLiveVideo(input.bufferedAmount(), 0, input.bufferLimit)) return 'backpressure'
  const packet = await input.encode()
  if (!input.isCurrent()) return 'stale'
  if (!canQueueLiveVideo(input.bufferedAmount(), packet.byteLength, input.bufferLimit)) return 'backpressure'
  input.send(packet)
  return 'sent'
}

/** 原生通道没有逐帧回执，只有兼容抽帧通道能用在飞年龄衡量模型压力。 */
export function liveVideoOverrun(mode: string, encodeMs: number, pendingAgeMs: number, intervalMs: number): boolean {
  return encodeMs > intervalMs * 2
    || (mode === 'sampled-frames' && pendingAgeMs > Math.max(6000, intervalMs * 2))
}

export interface AdaptiveSender {
  plan(): LiveSendPlan
  feed(sample: LiveSendSample): LiveSendPlan
  setMinimumInterval(intervalMs: number): void
  reset(): void
  level(): number
}

/**
 * 背压自适应：网络或模型变慢 → 逐级降频并压缩分辨率/码率；持续恢复 → 逐级升回。
 * 降级看单次压力信号即可生效；恢复需要连续健康样本。服务端 throttled/retry_after
 * 在此期间直接抬高发送间隔下限。
 */
export function createAdaptiveSender(): AdaptiveSender {
  let level = DEFAULT_LEVEL
  let healthyStreak = 0
  let throttleFloorUntil = 0
  let minimumIntervalMs = 0

  const step = (delta: number) => { level = Math.max(0, Math.min(INTERVALS.length - 1, level + delta)) }

  return {
    level() { return level },
    setMinimumInterval(intervalMs) {
      minimumIntervalMs = Number.isFinite(intervalMs) ? Math.max(0, intervalMs) : 0
    },
    plan() {
      const floor = Math.max(0, throttleFloorUntil - Date.now())
      return {
        intervalMs: Math.max(minimumIntervalMs, Math.min(8000, Math.max(INTERVALS[level], floor))),
        maxEdge: EDGES[Math.min(level, EDGES.length - 1)],
        quality: QUALITIES[Math.min(level, QUALITIES.length - 1)],
      }
    },
    feed(sample) {
      const pressured = sample.throttled === true
        || (typeof sample.latencyMs === 'number' && sample.latencyMs >= PRESSURE_LATENCY_MS)
        || sample.bufferedAmount >= PRESSURE_BUFFER_BYTES
        || sample.encodeOverrun === true
      if (sample.throttled) {
        const backoffS = typeof sample.retryAfterS === 'number' && sample.retryAfterS > 0
          ? Math.min(sample.retryAfterS, THROTTLE_FLOOR_MAX_MS / 1000) : 1
        throttleFloorUntil = Math.max(throttleFloorUntil, Date.now() + backoffS * 1000)
      }
      if (pressured) {
        healthyStreak = 0
        step(1)
      } else if ((typeof sample.latencyMs === 'number' && sample.latencyMs <= HEALTHY_LATENCY_MS
        || sample.transportHealthy === true) && sample.bufferedAmount <= HEALTHY_BUFFER_BYTES) {
        healthyStreak += 1
        if (healthyStreak >= HEALTHY_STREAK) { step(-1); healthyStreak = 0 }
      } else healthyStreak = 0
      return this.plan()
    },
    reset() { level = DEFAULT_LEVEL; healthyStreak = 0; throttleFloorUntil = 0; minimumIntervalMs = 0 },
  }
}

export type LivePhase = 'idle' | 'connecting' | 'viewing' | 'answering' | 'paused' | 'offline'

export const LIVE_PHASE_LABELS: Record<LivePhase, string> = {
  idle: '未开始', connecting: '连接中', viewing: '正在看', answering: '正在回答',
  paused: '已暂停', offline: '已断线',
}

export interface LivePhaseInput {
  active: boolean
  socketState: 'closed' | 'connecting' | 'open'
  paused: boolean
  reconnectScheduled: boolean
  /** 距最近一条观察结果的秒数；从未收到为 null */
  lastObservationAgeS: number | null
}

/** R6 验收门槛：用户要能一眼看懂“正在看、正在听、正在回答、已断线”。 */
export function classifyLivePhase(input: LivePhaseInput): LivePhase {
  if (!input.active) return 'idle'
  if (input.paused) return 'paused'
  if (input.socketState !== 'open') return input.reconnectScheduled ? 'connecting' : 'offline'
  if (input.lastObservationAgeS !== null && input.lastObservationAgeS <= 5) return 'answering'
  return 'viewing'
}

export interface ObservationLatency {
  /** 画面采集 → 观察返回（含排队+模型耗时） */
  e2eMs: number
  /** 发出 → 观察返回（服务端+模型耗时） */
  analyzeMs: number
  /** ledger 中仍未等到观察的在飞帧数 */
  queueDepth: number
}

export interface InFlightLedger {
  onSent(sequence: number, capturedAt: number, at: number): void
  onObserved(sequence: number, at: number): ObservationLatency | null
  pendingCount(): number
  /** 最老在飞帧的滞留时间 ms（无在飞帧为 0） */
  oldestPendingAge(now: number): number
  /** 回收服务端没有回帧的旧记录，返回回收条数。 */
  prune(now: number, maxAgeMs?: number): number
  clear(): void
}

/** 在飞帧账本：服务端背压只会为最新帧产出观察，较新帧回帧即视为更早帧已被取代。 */
export function createInFlightLedger(): InFlightLedger {
  const frames = new Map<number, { capturedAt: number; sentAt: number }>()
  const maxEntries = 64
  const defaultMaxAgeMs = 30_000
  const prune = (now: number, maxAgeMs = defaultMaxAgeMs): number => {
    let removed = 0
    for (const [key, frame] of frames) {
      if (now - frame.sentAt > maxAgeMs) {
        frames.delete(key)
        removed += 1
      }
    }
    while (frames.size > maxEntries) {
      const oldest = frames.keys().next().value
      if (oldest === undefined) break
      frames.delete(oldest)
      removed += 1
    }
    return removed
  }
  return {
    onSent(sequence, capturedAt, at) {
      frames.set(sequence, { capturedAt, sentAt: at })
      prune(at)
    },
    onObserved(sequence, at) {
      const frame = frames.get(sequence)
      if (!frame) return null
      frames.delete(sequence)
      for (const key of frames.keys()) { if (key < sequence) frames.delete(key) }
      return {
        e2eMs: Math.max(0, at - frame.capturedAt),
        analyzeMs: Math.max(0, at - frame.sentAt),
        queueDepth: frames.size,
      }
    },
    pendingCount() { return frames.size },
    oldestPendingAge(now) {
      let oldest = 0
      for (const frame of frames.values()) oldest = Math.max(oldest, now - frame.sentAt)
      return oldest
    },
    prune,
    clear() { frames.clear() },
  }
}

export interface LiveCaptionTurn { role: 'user' | 'assistant'; text: string; done: boolean }

const CAPABILITY_LABELS: Record<string, string> = {
  'audio.in': '麦克风音频输入', 'video.in': '视频帧输入', 'text.out': '文字回复',
  'audio.out': '语音回复', interrupt: '可打断',
}

/** provider_capabilities 的中文一行摘要；没有能力信息时返回空串（抽帧模式）。 */
export function describeLiveCapabilities(caps: unknown): string {
  if (!Array.isArray(caps) || caps.length === 0) return ''
  return caps.map(item => CAPABILITY_LABELS[String(item)] || String(item)).join(' · ')
}

/**
 * 把 model.delta / audio.transcript 线上事件归约成有界字幕时间线。
 * 关键语义：适配器先发增量 .delta，再发一条 final 的全量文本——final 时必须
 * **整条替换**当前助手回合，否则回答会被拼接成两遍。
 */
/**
 * 用户语音转写的收尾：把一条 `audio.transcript` 的 final 归并进字幕时间线。
 *
 * 返回值语义是这条契约的核心：**返回 `null` 表示这条 final 是重放，忽略它**——
 * 调用方据此同时决定「不记账」和「不转交」。两边绑在一条路径上，就不可能出现
 * 「字幕去了重、语音指令却重复转交」或反过来漏转的情况。
 *
 * 三种形态：
 * - 有草稿（增量已显示）：整条替换草稿，final 的全量文本更准；
 * - 无草稿、但与上一条用户字幕同文：适配器只发 final 且被重放 → null；
 * - 其余：新起一条用户回合。
 */
export function closeUserTranscript(
  captions: LiveCaptionTurn[],
  text: string,
  hadDraft: boolean,
  limit = 8,
): LiveCaptionTurn[] | null {
  const finalText = text.trim()
  if (!finalText) return null
  const last = captions[captions.length - 1]
  if (hadDraft && last && last.role === 'user') {
    return [...captions.slice(0, -1), { ...last, text: finalText, done: true }].slice(-limit)
  }
  if (last && last.role === 'user' && last.text === finalText) return null
  const spoken: LiveCaptionTurn = { role: 'user', text: finalText, done: true }
  return [...captions, spoken].slice(-limit)
}

/** 只留「有内容」的字：去掉空白/标点/符号，再去掉纯语气词。 */
function voiceContent(text: string): string {
  return text
    .replace(/[\s\p{P}\p{S}]/gu, '')
    .replace(/[嗯啊哦呃唉诶呀哈哎唔喔噢嘛么]+/gu, '')
}

/**
 * 一条语音转写值不值得**转交主 Agent**（A 档是「全部转交」，但噪音必须挡在门外）。
 *
 * 为什么需要这道门：实测误识别与旁音照样会起一整轮 Agent（`不如自己喝。`、`什么金？`
 * 各起了一轮，7~27s，Agent 还得先猜用户想说什么）。下面三条都是**硬条件**，不猜意图——
 * 猜意图要么靠关键词（脆），要么靠模型配合（不可靠）。被挡下的回合由调用方在状态栏明说，
 * 不静默吞掉。
 */
export function shouldDispatchVoiceTurn(
  text: string,
  previous: { text?: string; at?: number } = {},
  now = Date.now(),
  minGapMs = 1200,
): boolean {
  const trimmed = String(text || '').trim()
  // 1) 去掉标点与语气词后什么都不剩 → 这不是一句话（「嗯」「啊？」「。。。」）。
  if (!voiceContent(trimmed)) return false
  // 2) 与上一条**同一句话**（忽略标点差异）→ 误识别常把同一句吐两遍，别转交两次。
  const prev = String(previous.text || '')
  if (prev && voiceContent(prev) === voiceContent(trimmed)) return false
  // 3) 距上次转交太近 → 同一句被切成两段会连着来；这一条挡的是连发，不是短句。
  if (typeof previous.at === 'number' && previous.at > 0 && now - previous.at < minGapMs) return false
  return true
}

/**
 * 两条界面提醒是不是在说**同一处**（用于跨来源合并：抽帧告警 vs 实时模型自述）。
 *
 * 为什么会各弹一条：抽帧视觉模型和实时模型是两条独立的观测链，两边都发现同一个问题时，
 * 用户会看到两条几乎一样的横幅。这里只做"是否同一处"的判断，合并展示、保留两边的原文。
 *
 * 判据刻意保守：按汉字二元组算重叠率（对"同一处、不同说法"宽容），但**字数太少就不判**
 * （「报错」两个字随便都能撞上，误合并会把两处不同的问题说成一处）。
 */
export function sameVisualFocus(left: string, right: string, threshold = 0.5): boolean {
  const grams = (text: string): Set<string> => {
    const chars = String(text || '').replace(/[\s\p{P}\p{S}]/gu, '').toLowerCase()
    const out = new Set<string>()
    for (let i = 0; i + 1 < chars.length; i += 1) out.add(chars.slice(i, i + 2))
    return out
  }
  const a = grams(left)
  const b = grams(right)
  const smaller = Math.min(a.size, b.size)
  if (smaller < 3) return false
  let shared = 0
  for (const gram of a) if (b.has(gram)) shared += 1
  return shared / smaller >= threshold
}

/** 忽略记录的裁剪：只留时间窗内的、且不超过条数上限（避免无界增长）。 */
export function pruneDismissedVisualAlerts(
  dismissed: { text: string; at: number }[],
  now = Date.now(),
  windowMs = 600_000,
  limit = 20,
): { text: string; at: number }[] {
  return dismissed.filter(item => now - item.at <= windowMs).slice(-limit)
}

/**
 * 这条提醒是不是用户**刚忽略过**的那一处（忽略期内不再弹）。
 *
 * 为什么需要：后端的相似度去重只有 45 秒窗口，过了窗口同一处会再弹一次 —— 用户刚点完
 * 「忽略」，几秒后又看到同一条，会以为按钮没生效。判据复用 `sameVisualFocus()`（同一处、
 * 换说法也算同一处）；并带时间窗：忽略只在一段时间内有效，之后同一个位置真的又出问题
 * 仍应能提醒。
 */
export function isDismissedVisualAlert(
  text: string,
  dismissed: { text: string; at: number }[],
  now = Date.now(),
  windowMs = 600_000,
): boolean {
  return pruneDismissedVisualAlerts(dismissed, now, windowMs).some(item => sameVisualFocus(text, item.text))
}

export function appendCaptionTurn(
  captions: LiveCaptionTurn[],
  wire: { type: string; text?: unknown; final?: unknown },
  limit = 8,
): LiveCaptionTurn[] {
  const text = typeof wire.text === 'string' ? wire.text.trim() : ''
  if (wire.type === 'audio.transcript') {
    if (!text) return captions
    const spoken: LiveCaptionTurn = { role: 'user', text, done: true }
    return [...captions, spoken].slice(-limit)
  }
  if (wire.type !== 'model.delta') return captions
  const last = captions[captions.length - 1]
  const streaming = last && last.role === 'assistant' && !last.done ? last : null
  if (wire.final === true) {
    const finalText = text || (streaming ? streaming.text : '')
    if (!finalText) return captions
    const closed: LiveCaptionTurn = { role: 'assistant', text: finalText, done: true }
    return (streaming ? [...captions.slice(0, -1), closed] : [...captions, closed]).slice(-limit)
  }
  if (!text) return captions
  const open: LiveCaptionTurn = streaming
    ? { role: 'assistant', text: streaming.text + text, done: false }
    : { role: 'assistant', text, done: false }
  return [...(streaming ? captions.slice(0, -1) : captions), open].slice(-limit)
}
