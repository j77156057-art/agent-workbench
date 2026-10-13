/**
 * R2 语音闭环前端纯逻辑：PCM 采集数学、能量 VAD、静音尾契约、分片泵与上行封包。
 *
 * 不含任何浏览器 API（采集与播放接口留在 ChatDock scope="cockpit" 接线层，
 * v2 重构前曾由 AutonomousCockpit.vue 承担，E 阶段舱组件删除后改由 ChatDock 接管），
 * 因此可脱离页面用 node 做行为契约测试。
 *
 * 关键契约（来自 AI-D 真机实测，HANDOFF 2026-09-29 / commit 0db20ff）：
 * - 输入 pcm16 mono 16k；DashScope Omni 的 response.audio.delta 线上值为
 *   ``encoding: "pcm24"``，实测/适配器约定是 **16-bit little-endian PCM、24 kHz、mono**。
 *   ``pcm24`` 是服务端的 24 kHz 格式名，不是 24-bit 样本；播放端必须按 2 字节样本解码。
 * - **停止说话 ≠ 停止推流**：说完后必须继续推 ~1 秒静音，服务端 VAD 靠尾部静音收句，
 *   在响亮采样上掐流会导致 turn 永不闭合、服务端回收会话（现象是"转写完整却零回复"）。
 * 协议包字段沿用 R0 的媒体包头（v/type/sequence/captured_at + '\n' + 载荷），
 * 本模块只做 audio.chunk 的组包，不改协议定义。
 */

export const TARGET_SAMPLE_RATE = 16_000
/** 当前 Omni provider 的 model.audio：16-bit LE PCM @ 24kHz。 */
export const OUTPUT_SAMPLE_RATE = 24_000
export const OUTPUT_ENCODING = 'pcm24'
export const OUTPUT_BITS_PER_SAMPLE = 16 as const
export const CHUNK_MS = 100
const MAX_CHUNK_BYTES = 1_000_000

/** Small jitter reserve without changing the PCM sample rate or voice pitch. */
export function createAudioPlaybackClock() {
  let nextAt = 0
  let reserve = 0.06
  return {
    reset() { nextAt = 0; reserve = 0.06 },
    schedule(now: number, duration: number): number {
      if (!Number.isFinite(now) || now < 0 || !Number.isFinite(duration) || duration <= 0) {
        throw new Error('Invalid playback time or duration')
      }
      let startAt: number
      if (nextAt === 0 || now - nextAt > 0.5) {
        // A new answer only needs a 60 ms reserve, even after a stalled answer.
        reserve = 0.06
        startAt = now + reserve
      } else if (nextAt < now + 0.005) {
        // Refill on underrun; cap additional latency at 140 ms.
        reserve = Math.min(0.14, reserve + 0.02)
        startAt = now + reserve
      } else {
        // Adjacent PCM fragments share an exact boundary: no per-chunk pause.
        startAt = nextAt
      }
      nextAt = startAt + duration
      return startAt
    },
  }
}

export interface LiveModelAudioFormat {
  sampleRate: number
  bitsPerSample: 16
  /** 字段来源，方便诊断服务端是否开始携带显式采样率。 */
  source: 'wire_sample_rate' | 'wire_encoding'
}

/**
 * Resolve the provider's model.audio format before decoding bytes.
 *
 * The normalised provider event carries `encoding: "pcm24"`; the name is
 * DashScope's 24 kHz PCM16 shorthand.  A future provider may carry an
 * explicit `sample_rate`, which wins over the shorthand.  Unknown/missing
 * metadata is rejected so a different wire format cannot be played at the
 * wrong speed while looking successful.
 */
export function resolveLiveModelAudioFormat(
  encoding: unknown,
  sampleRate: unknown = undefined,
): LiveModelAudioFormat | null {
  const explicit = typeof sampleRate === 'number'
    ? sampleRate
    : typeof sampleRate === 'string' && sampleRate.trim() ? Number(sampleRate) : NaN
  if (Number.isFinite(explicit) && explicit >= 8_000 && explicit <= 96_000) {
    return { sampleRate: Math.round(explicit), bitsPerSample: OUTPUT_BITS_PER_SAMPLE, source: 'wire_sample_rate' }
  }
  const token = typeof encoding === 'string' ? encoding.trim().toLowerCase() : ''
  if (token === 'pcm24') return { sampleRate: OUTPUT_SAMPLE_RATE, bitsPerSample: OUTPUT_BITS_PER_SAMPLE, source: 'wire_encoding' }
  return null
}

export function clampInt16(value: number): number {
  const scaled = Math.round(value * 32767)
  return Math.max(-32768, Math.min(32767, scaled))
}

export function frameRms(frame: Float32Array): number {
  if (frame.length === 0) return 0
  let sum = 0
  for (const sample of frame) sum += sample * sample
  return Math.sqrt(sum / frame.length)
}

/** 线性插值重采样（语音足够；确定性、可测）。 */
export function resample(input: Float32Array, sourceRate: number, targetRate: number): Float32Array {
  if (sourceRate <= 0 || targetRate <= 0 || input.length === 0) return new Float32Array(0)
  if (sourceRate === targetRate) return input.slice()
  const ratio = sourceRate / targetRate
  const outLength = Math.max(1, Math.floor(input.length / ratio))
  const output = new Float32Array(outLength)
  for (let i = 0; i < outLength; i++) {
    const position = i * ratio
    const index = Math.floor(position)
    const next = Math.min(index + 1, input.length - 1)
    const fraction = position - index
    output[i] = input[index] * (1 - fraction) + input[next] * fraction
  }
  return output
}

/** Float32 [-1,1] → 16bit little-endian PCM 字节。 */
export function floatToInt16Le(pcm: Float32Array): Uint8Array {
  const out = new Uint8Array(pcm.length * 2)
  for (let i = 0; i < pcm.length; i++) {
    const value = clampInt16(Math.max(-1, Math.min(1, pcm[i])))
    const unsigned = value < 0 ? value + 0x10000 : value
    out[i * 2] = unsigned & 0xff
    out[i * 2 + 1] = (unsigned >> 8) & 0xff
  }
  return out
}

export type VoiceGateState = 'idle' | 'speech' | 'tail'

export interface VoiceGateSignal {
  state: VoiceGateState
  started: boolean
  ended: boolean
  tailDone: boolean
  /** 空闲判定为"用户在 AI 说话时开口"——接线层应立即停播并 cancel。 */
  bargeIn: boolean
  /** 「停止说话 ≠ 停止推流」：speech 与 tail 期间都必须继续发送分片。 */
  sending: boolean
}

export interface VoiceGateOptions {
  startRms?: number
  endRms?: number
  /** 持续多少毫秒安静算说完（进入静音尾）。 */
  speechHoldMs?: number
  /** 静音尾长度：必须 ≥ 服务端 VAD 收句所需的尾部静音。 */
  tailMs?: number
  /** 起始去抖：连续 N 帧超过起说阈值才算开口。 */
  startFrames?: number
}

const GATE_DEFAULTS = {
  startRms: 0.02, endRms: 0.012, speechHoldMs: 300, tailMs: 1000, startFrames: 2,
}

/**
 * 能量 VAD 状态机：idle → speech → tail → idle。
 * 时间由调用方喂入（atMs 单调），便于脱离时钟做确定性测试。
 */
export function createVoiceGate(options: VoiceGateOptions = {}) {
  const opts = { ...GATE_DEFAULTS, ...options }
  let state: VoiceGateState = 'idle'
  let silenceMs = 0
  let tailMs = 0
  let loudStreak = 0
  let lastAt: number | null = null

  const signal = (extra: Partial<VoiceGateSignal>): VoiceGateSignal => ({
    state, started: false, ended: false, tailDone: false, bargeIn: false,
    sending: state !== 'idle', ...extra,
  })

  return {
    state(): VoiceGateState { return state },
    sending(): boolean { return state !== 'idle' },
    reset() { state = 'idle'; silenceMs = 0; tailMs = 0; loudStreak = 0; lastAt = null },
    feed(rms: number, atMs: number, aiSpeaking = false): VoiceGateSignal {
      const dt = lastAt === null ? 0 : Math.max(0, atMs - lastAt)
      lastAt = atMs
      const loud = rms >= opts.startRms
      const quiet = rms < opts.endRms
      if (state === 'idle') {
        loudStreak = loud ? loudStreak + 1 : 0
        if (loudStreak >= opts.startFrames) {
          state = 'speech'; loudStreak = 0; silenceMs = 0
          return signal({ started: true, bargeIn: aiSpeaking })
        }
        return signal({})
      }
      if (state === 'speech') {
        if (loud) loudStreak += 1
        silenceMs = quiet ? silenceMs + dt : 0
        if (silenceMs >= opts.speechHoldMs) {
          state = 'tail'; tailMs = 0; silenceMs = 0
          return signal({ ended: true })
        }
        return signal({})
      }
      // tail：静音尾期间重新开口 = 下一句（或抢话），立即回到 speech。
      tailMs += dt
      if (loud) {
        state = 'speech'; loudStreak = 0
        return signal({ started: true, bargeIn: aiSpeaking })
      }
      if (tailMs >= opts.tailMs) {
        state = 'idle'
        return signal({ tailDone: true, sending: false })
      }
      return signal({})
    },
  }
}

/**
 * 定长分片泵：喂入重采样到目标率的样本，积攒满一片返回一片 int16 字节。
 * 只产整片（不足不返回），避免服务端拿到碎片时钟。
 */
export function createChunkPump(chunkMs: number = CHUNK_MS, sampleRate: number = TARGET_SAMPLE_RATE) {
  const chunkSamples = Math.max(1, Math.round(sampleRate * chunkMs / 1000))
  let buffer = new Float32Array(chunkSamples)
  let filled = 0
  return {
    chunkSamples(): number { return chunkSamples },
    pending(): number { return filled },
    push(samples: Float32Array): Uint8Array[] {
      const chunks: Uint8Array[] = []
      let index = 0
      while (index < samples.length) {
        const take = Math.min(chunkSamples - filled, samples.length - index)
        buffer.set(samples.subarray(index, index + take), filled)
        filled += take; index += take
        if (filled === chunkSamples) {
          chunks.push(floatToInt16Le(buffer))
          buffer = new Float32Array(chunkSamples)
          filled = 0
        }
      }
      return chunks
    },
    /** 静音尾：产 100ms 全零片。 */
    silenceChunk(): Uint8Array {
      return new Uint8Array(chunkSamples * 2)
    },
    reset() { buffer = new Float32Array(chunkSamples); filled = 0 },
  }
}

export interface AudioPacketInput {
  version: number
  sequence: number
  capturedAt: number
  payload: Uint8Array
}

/** 组 audio.chunk 上行包：UTF-8 JSON 头 + '\n' + PCM 载荷（与 R0 媒体包同封）。 */
export function encodeAudioPacket(input: AudioPacketInput): ArrayBuffer {
  if (!Number.isInteger(input.sequence) || input.sequence < 0) {
    throw new Error('sequence 必须是非负整数')
  }
  if (!Number.isInteger(input.capturedAt) || input.capturedAt <= 0) {
    throw new Error('captured_at 必须是正整数毫秒')
  }
  if (input.payload.length === 0 || input.payload.length > MAX_CHUNK_BYTES) {
    throw new Error(`音频分片大小非法：${input.payload.length}`)
  }
  const header = new TextEncoder().encode(JSON.stringify({
    v: input.version, type: 'audio.chunk',
    sequence: input.sequence, captured_at: input.capturedAt,
  }) + '\n')
  const packet = new Uint8Array(header.length + input.payload.length)
  packet.set(header, 0)
  packet.set(input.payload, header.length)
  return packet.buffer
}
