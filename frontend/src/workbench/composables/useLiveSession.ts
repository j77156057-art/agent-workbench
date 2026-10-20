/**
 * 实时对话会话的**接线层**：采集 → 编码 → 上行 → 事件归约 → 播放。
 *
 * 这一层是 R13 文档里反复提到、但一直没落地的那一层。已有的纯逻辑模块
 * （`realtimeProtocol` / `liveStreamControl` / `liveAudioControl` /
 * `liveVisionSampling`）全是"能测但没人用"的孤儿；这里把它们接起来，
 * 外面只暴露状态与动作，UI 组件不碰任何协议细节。
 *
 * 分工：
 * - `liveTransport.ts` —— 连接生命周期（握手/心跳/重连/会话代数）
 * - 本文件 —— 媒体采集、节流上行、事件归约、音频播放
 * - `LiveStreamPanel.vue` —— 只读状态 + 调动作
 *
 * 关键取舍：
 * 1. **降级不隐形**：`hello.ok` 里的 mode/degraded_to/reason 一律进
 *    `describeRealtimeMode`，面板顶部永远显示当前模式（原生/降级/不可用）。
 * 2. **相位来自既有判定**：`classifyLivePhase` 把 socket 状态 + 最近观察时间
 *    归成"未开始/连接中/正在看/正在回答/已暂停/已断线"，UI 不自己另算一套。
 * 3. **过期产物就地丢弃**：每次捕获都记下 transport 的会话代数，
 *    发包前再问一次 `isCurrent`（重连后旧帧不能推进新会话）。
 */
import { computed, onUnmounted, ref, type Ref } from 'vue'
import {
  createAdaptiveSender,
  createInFlightLedger,
  appendCaptionTurn,
  appendUserTranscriptDraft,
  classifyLivePhase,
  closeUserTranscript,
  describeLiveCapabilities,
  sendLiveVideoPacket,
  type LiveCaptionTurn,
  type LivePhase,
} from '../liveStreamControl'
import {
  createAudioPlaybackClock,
  createChunkPump,
  createVoiceGate,
  encodeAudioPacket,
  frameRms,
  resample,
  resolveLiveModelAudioFormat,
  TARGET_SAMPLE_RATE,
} from '../liveAudioControl'
import { encodeVideoFrame, type RealtimeServerEvent } from '../realtimeProtocol'
import { createLiveTransport, type LiveSocketState, type LiveTransport } from '../liveTransport'
import { describeRealtimeMode, type RealtimeModeView } from '../realtimeModeDisplay'
import { getProjectId, visionApi, type RealtimeStatusResp } from '../api'

export interface LiveObservation {
  at: number
  text: string
  latencyMs: number | null
  throttled: boolean
}

export interface LiveSessionStats {
  framesSent: number
  observations: number
  chunksSent: number
  lastE2eMs: number | null
  lastObservationAt: number | null
}

const MAX_OBSERVATIONS = 40
const MAX_CAPTIONS = 12

export interface UseLiveSession {
  phase: Ref<LivePhase>
  active: Ref<boolean>
  paused: Ref<boolean>
  modeView: Ref<RealtimeModeView | null>
  modePayload: Ref<Record<string, unknown> | null>
  captions: Ref<LiveCaptionTurn[]>
  observations: Ref<LiveObservation[]>
  error: Ref<string>
  notice: Ref<string>
  stats: Ref<LiveSessionStats>
  capabilities: Ref<string[]>
  videoEnabled: Ref<boolean>
  audioEnabled: Ref<boolean>
  cameraState: Ref<'idle' | 'on' | 'denied' | 'unavailable'>
  /** 圈选聚焦的归一矩形（0..1）；null=不聚焦，上行整帧。 */
  focusRegion: Ref<{ x: number; y: number; w: number; h: number } | null>
  /** 通道指标与时间线（后端只读端点，2 秒一轮询）。 */
  channelStatus: Ref<RealtimeStatusResp | null>
  /** 手动刷新一次通道指标。 */
  refreshChannelStatus: () => Promise<void>
  /** 把面板里的 <video> 交给接线层：**所见即所发**，用户看到的画面就是送出去的那一帧。 */
  bindVideo: (element: HTMLVideoElement | null) => void
  /** 单独开关麦克风（音频走 WS 上行到实时模型，不经过对话台）。 */
  enableAudio: () => Promise<void>
  disableAudio: () => void
  start: (options?: { video?: boolean; audio?: boolean }) => Promise<void>
  pause: () => void
  resume: () => void
  stop: () => void
  cancel: () => void
  refreshMode: () => Promise<void>
}

export function useLiveSession(): UseLiveSession {
  const active = ref(false)
  const paused = ref(false)
  const socketState = ref<LiveSocketState>('closed')
  const reconnectScheduled = ref(false)
  const error = ref('')
  const notice = ref('')
  const captions = ref<LiveCaptionTurn[]>([])
  const observations = ref<LiveObservation[]>([])
  const modeView = ref<RealtimeModeView | null>(null)
  /** 原始 payload：徽章组件（RealtimeModeBadge）吃的是原始形状，不是 view。 */
  const modePayload = ref<Record<string, unknown> | null>(null)
  const capabilities = ref<string[]>([])
  const videoEnabled = ref(true)
  const audioEnabled = ref(false)
  const cameraState = ref<'idle' | 'on' | 'denied' | 'unavailable'>('idle')
  const stats = ref<LiveSessionStats>({
    framesSent: 0, observations: 0, chunksSent: 0, lastE2eMs: null, lastObservationAt: null,
  })

  const sender = createAdaptiveSender()
  const ledger = createInFlightLedger()
  const playbackClock = createAudioPlaybackClock()

  let transport: LiveTransport | null = null
  let mediaStream: MediaStream | null = null
  let videoElement: HTMLVideoElement | null = null
  let canvas: HTMLCanvasElement | null = null
  let captureTimer: ReturnType<typeof setTimeout> | null = null
  let sequence = 0
  let audioContext: AudioContext | null = null
  let audioProcessor: ScriptProcessorNode | null = null
  let audioSource: MediaStreamAudioSourceNode | null = null
  const voiceGate = createVoiceGate()
  const chunkPump = createChunkPump()
  let audioSequence = 0
  let lastAudioSentAt = 0
  /** 最近一次**真实语音**（非静音）上行的时间：决定"用户是否正在说话"。 */
  let lastSpeechAt = 0

  /** 送一块静音（100ms 全零）：既是"停止说话≠停止推流"的尾，也是"音频在前"的垫。 */
  function sendSilenceChunk() {
    const t = transport
    if (!t) return
    const gen = t.generation()
    if (!t.isCurrent(gen)) return
    const silence = chunkPump.silenceChunk()
    if (t.sendBinary(encodeAudioPacket({
      version: 1, sequence: audioSequence++, capturedAt: Date.now(), payload: silence,
    }))) {
      lastAudioSentAt = Date.now()
    }
  }

  // ---- 相位：交给既有判定，UI 不另算一套 -------------------------------
  const phase = computed<LivePhase>(() => classifyLivePhase({
    active: active.value,
    socketState: socketState.value,
    paused: paused.value,
    reconnectScheduled: reconnectScheduled.value,
    lastObservationAgeS: stats.value.lastObservationAt === null
      ? null
      : (Date.now() - stats.value.lastObservationAt) / 1000,
  }))

  // ---- 事件归约 ---------------------------------------------------------
  function pushObservation(text: string, latencyMs: number | null, throttled: boolean) {
    if (!text && !throttled) return
    observations.value = [...observations.value, { at: Date.now(), text, latencyMs, throttled }].slice(-MAX_OBSERVATIONS)
  }

  function onEvent(event: RealtimeServerEvent) {
    switch (event.type) {
      case 'hello.ok': {
        const raw = event as unknown as Record<string, unknown>
        const view = describeRealtimeMode(raw)
        modeView.value = view
        modePayload.value = raw
        capabilities.value = describeLiveCapabilities(event.provider_capabilities)
          .split(' · ').filter(Boolean)
        if (view.isDegraded && view.tone !== 'unavailable') {
          notice.value = view.hint
        }
        break
      }
      case 'video.observation': {
        const sequenceNo = typeof event.sequence === 'number' ? event.sequence : null
        const latency = sequenceNo === null ? null : ledger.onObserved(sequenceNo, Date.now())
        const text = Array.isArray(event.observations)
          ? (event.observations as unknown[]).filter((x): x is string => typeof x === 'string').join('\n')
          : ''
        const throttled = event.throttled === true
        if (!throttled) {
          stats.value = {
            ...stats.value,
            observations: stats.value.observations + 1,
            lastE2eMs: latency ? latency.e2eMs : stats.value.lastE2eMs,
            lastObservationAt: Date.now(),
          }
        }
        pushObservation(text, latency ? latency.e2eMs : null, throttled)
        // 服务端限流 → 抬高发送间隔下限（自适应发送器的入口）。
        sender.feed({
          bufferedAmount: 0,
          latencyMs: latency ? latency.e2eMs : undefined,
          throttled,
          retryAfterS: typeof event.retry_after === 'number' ? event.retry_after : undefined,
          transportHealthy: !throttled,
        })
        break
      }
      case 'audio.transcript': {
        const text = typeof event.text === 'string' ? event.text : ''
        const final = event.final === true
        if (final) {
          // 整句收口：替换草稿（而不是再新起一条）。
          const last = captions.value[captions.value.length - 1]
          const hadDraft = Boolean(last && last.role === 'user' && !last.done)
          const next = closeUserTranscript(captions.value, text, hadDraft, MAX_CAPTIONS)
          if (next) captions.value = next
        } else {
          // 增量是**累计文本**，整条替换草稿（追加会碎成一屏碎片）。
          captions.value = appendUserTranscriptDraft(captions.value, text, MAX_CAPTIONS)
        }
        break
      }
      case 'model.delta': {
        captions.value = appendCaptionTurn(captions.value, {
          type: 'model.delta',
          text: typeof event.text === 'string' ? event.text : '',
          final: event.final === true,
        }, MAX_CAPTIONS)
        break
      }
      case 'model.audio': {
        void playModelAudio(event)
        break
      }
      case 'error': {
        const message = typeof event.message === 'string' ? event.message : '实时通道返回错误'
        const code = typeof event.code === 'string' ? event.code : ''
        // vendor_error 是**供应商的协议抱怨**，不是会话失败：
        // 典型是 `Error append image before append audio.`（一轮里图像抢在音频前）。
        // 它不影响会话继续，所以按提醒显示，不占红色错误条。
        if (code === 'vendor_error') {
          notice.value = message
        } else {
          error.value = message
          if (event.retryable !== true) notice.value = message
        }
        break
      }
      case 'cancel.ok': {
        // 打断后本轮助手回合必须**收口**：否则它一直停在"未完成"，
        // 下一句用户开口会被误判成"抢话"，于是每说一句就打断一次（cancel 风暴）。
        const last = captions.value[captions.value.length - 1]
        if (last && last.role === 'assistant' && !last.done) {
          captions.value = [...captions.value.slice(0, -1), { ...last, done: true }]
        }
        notice.value = '已取消本轮生成。'
        break
      }
      case 'session.closed': {
        notice.value = '实时会话已结束。'
        break
      }
      default:
        break
    }
  }

  // ---- 视频上行 ---------------------------------------------------------
  // ---- 圈选聚焦（focused region）----
  // 归一化矩形（0..1，相对预览画面）。设了它，上行的帧**就是**这块区域，
  // 且包头带 focused=true —— 后端据此把观察范围收窄到这块（api.py 的
  // focused_region="1" if header.get("focused")）。
  const focusRegion = ref<{ x: number; y: number; w: number; h: number } | null>(null)

  // ---- 通道指标（R10）：只读端点给的首字延迟/队列深度/连接数等 ----
  const channelStatus = ref<RealtimeStatusResp | null>(null)
  let metricsTimer: ReturnType<typeof setInterval> | null = null

  async function refreshChannelStatus() {
    try {
      channelStatus.value = await visionApi.realtimeChannelStatus()
    } catch {
      /* 指标拿不到不影响会话本身（它只是观测） */
    }
  }

  function startMetricsPolling() {
    if (metricsTimer) return
    void refreshChannelStatus()
    metricsTimer = setInterval(() => { void refreshChannelStatus() }, 2000)
  }

  function stopMetricsPolling() {
    if (metricsTimer) { clearInterval(metricsTimer); metricsTimer = null }
  }

  async function captureAndSendFrame() {
    if (!active.value || paused.value) return
    const t = transport
    const video = videoElement
    const surface = canvas
    if (!t || !video || !surface || video.readyState < 2 || !video.videoWidth) return

    // 「一轮里图像不能抢在音频前」：开麦时若最近没有音频上行，先补一块静音，
    // 让每一轮都满足"先音频、后图像"。否则供应商会回
    // `Error append image before append audio.`（实测：静音期推帧必踩）。
    //
    // 并且**原生模式下只在用户说话的窗口内推帧**：图像必须落在"有音频的那一轮"里，
    // 否则模型收到的是一个"只有图像的空轮次"，表现为答非所问的短回复
    // （实测：反复出现 "I hear you."）。静音期只维持静音块，让服务端 VAD 正常收句。
    //
    // ⚠️ 这个限制**只对原生模式成立**：降级（抽帧）模式下，画面是模型"看见屏幕"的
    // 唯一通路（后端旁路观察），此时必须持续推帧，否则降级等于什么都看不见。
    if (audioEnabled.value && modeView.value?.tone === 'native') {
      const now = Date.now()
      if (now - lastAudioSentAt > 1200) sendSilenceChunk()
      if (now - lastSpeechAt > 1500) return
    } else if (audioEnabled.value && Date.now() - lastAudioSentAt > 1200) {
      sendSilenceChunk()
    }

    const plan = sender.plan()
    const width = video.videoWidth
    const height = video.videoHeight
    const focus = focusRegion.value
    // 圈选时只取那块区域：上行帧 = 选区内容（所见即所发），并标 focused。
    const crop = focus
      ? { sx: Math.max(0, Math.round(focus.x * width)), sy: Math.max(0, Math.round(focus.y * height)),
          sw: Math.max(1, Math.round(focus.w * width)), sh: Math.max(1, Math.round(focus.h * height)) }
      : null
    const scale = Math.min(1, plan.maxEdge / (crop ? crop.sw : width), plan.maxEdge / (crop ? crop.sh : height))
    const targetWidth = Math.max(1, Math.round((crop ? crop.sw : width) * scale))
    const targetHeight = Math.max(1, Math.round((crop ? crop.sh : height) * scale))
    if (surface.width !== targetWidth) surface.width = targetWidth
    if (surface.height !== targetHeight) surface.height = targetHeight
    const context = surface.getContext('2d')
    if (!context) return
    if (crop) {
      context.drawImage(video, crop.sx, crop.sy, crop.sw, crop.sh, 0, 0, targetWidth, targetHeight)
    } else {
      context.drawImage(video, 0, 0, targetWidth, targetHeight)
    }

    const capturedAt = Date.now()
    const blob = await new Promise<Blob | null>((resolve) => {
      surface.toBlob((value) => resolve(value), 'image/jpeg', plan.quality)
    })
    if (!blob) return
    const gen = t.generation()
    const current = sequence++
    const result = await sendLiveVideoPacket({
      encode: () => encodeVideoFrame(current, capturedAt, blob, Boolean(crop)),
      isCurrent: () => t.isCurrent(gen) && active.value && !paused.value,
      bufferedAmount: () => t.bufferedAmount(),
      send: (packet) => { t.sendBinary(packet) },
    })
    if (result === 'sent') {
      ledger.onSent(current, capturedAt, Date.now())
      stats.value = { ...stats.value, framesSent: stats.value.framesSent + 1 }
      sender.feed({ bufferedAmount: t.bufferedAmount(), transportHealthy: true })
    } else if (result === 'backpressure') {
      // 发送缓冲积压：立刻降一档，别等自适应器慢慢反应。
      sender.feed({ bufferedAmount: t.bufferedAmount(), transportHealthy: false })
    }
  }

  function scheduleNextFrame() {
    if (captureTimer) clearTimeout(captureTimer)
    const plan = sender.plan()
    captureTimer = setTimeout(async () => {
      try { await captureAndSendFrame() } catch { /* 单帧失败不拖垮整条会话 */ }
      if (active.value) scheduleNextFrame()
    }, plan.intervalMs)
  }

  // ---- 音频上行 + 模型语音播放 -----------------------------------------
  function pumpAudio(samples: Float32Array) {
    const pcm = resample(samples, audioContext ? audioContext.sampleRate : 48000, TARGET_SAMPLE_RATE)
    const rms = frameRms(pcm)
    const aiSpeaking = captions.value.some(c => c.role === 'assistant' && !c.done)
    const signal = voiceGate.feed(rms, Date.now(), aiSpeaking)
    if (signal.bargeIn) {
      transport?.sendControl({ v: 1, type: 'cancel', reason: 'barge-in' })
    }
    if (!signal.sending) return
    const chunks = chunkPump.push(pcm)
    for (const chunk of chunks) {
      const gen = transport?.generation() ?? -1
      if (!transport || !transport.isCurrent(gen)) return
      const packet = encodeAudioPacket({
        version: 1,
        sequence: audioSequence++,
        capturedAt: Date.now(),
        payload: chunk,
      })
      if (transport.sendBinary(packet)) {
        lastAudioSentAt = Date.now()
        lastSpeechAt = Date.now()
        stats.value = { ...stats.value, chunksSent: stats.value.chunksSent + 1 }
      }
    }
    // 说完之后必须继续推一段静音，服务端 VAD 靠尾部静音收句。
    if (signal.tailDone) {
      sendSilenceChunk()
    }
  }

  async function playModelAudio(event: RealtimeServerEvent) {
    const raw = (event as unknown as { audio?: unknown }).audio
    if (typeof raw !== 'string' || !raw) return
    const format = resolveLiveModelAudioFormat(event.encoding, event.sample_rate)
    if (!format) return
    let binary: Uint8Array
    try {
      const binaryString = atob(raw)
      binary = new Uint8Array(binaryString.length)
      for (let i = 0; i < binaryString.length; i += 1) binary[i] = binaryString.charCodeAt(i)
    } catch { return }
    if (binary.length < 2) return
    const samples = new Int16Array(binary.buffer, binary.byteOffset, Math.floor(binary.length / 2))
    const pcm = new Float32Array(samples.length)
    for (let i = 0; i < samples.length; i += 1) pcm[i] = samples[i] / 32768
    if (!audioContext) return
    const buffer = audioContext.createBuffer(1, pcm.length, format.sampleRate)
    buffer.copyToChannel(pcm, 0)
    const source = audioContext.createBufferSource()
    source.buffer = buffer
    const startAt = playbackClock.schedule(audioContext.currentTime, buffer.duration)
    source.connect(audioContext.destination)
    source.start(startAt)
  }

  async function startAudio() {
    if (!navigator.mediaDevices?.getUserMedia) {
      error.value = '当前浏览器不支持麦克风采集（getUserMedia 不可用）。'
      audioEnabled.value = false
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      audioContext = new AudioContext()
      await audioContext.resume()
      audioSource = audioContext.createMediaStreamSource(stream)
      audioProcessor = audioContext.createScriptProcessor(4096, 1, 1)
      audioProcessor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0)
        pumpAudio(new Float32Array(input))
      }
      audioSource.connect(audioProcessor)
      // ScriptProcessor 必须连到目的地才会被调度；接一个静音增益节点避免回授。
      const sink = audioContext.createGain()
      sink.gain.value = 0
      audioProcessor.connect(sink)
      sink.connect(audioContext.destination)
      audioEnabled.value = true
    } catch (e) {
      error.value = `麦克风不可用：${(e as Error).message || '未知原因'}`
      audioEnabled.value = false
    }
  }

  async function startVideo() {
    if (!navigator.mediaDevices?.getUserMedia) {
      error.value = '当前浏览器不支持摄像头采集（getUserMedia 不可用）。'
      videoEnabled.value = false
      return
    }
    const element = videoElement
    if (!element) {
      error.value = '没有可用的视频预览元素，摄像头未启动。'
      videoEnabled.value = false
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      })
      mediaStream = stream
      element.srcObject = stream
      element.muted = true
      element.playsInline = true
      await element.play()
      canvas = document.createElement('canvas')
      cameraState.value = 'on'
      videoEnabled.value = true
    } catch (e) {
      error.value = `摄像头不可用：${(e as Error).message || '未知原因'}`
      cameraState.value = 'denied'
      videoEnabled.value = false
    }
  }

  // ---- 生命周期 ---------------------------------------------------------
  async function refreshMode() {
    try {
      const status = await visionApi.realtimeStatus()
      const raw = status as unknown as Record<string, unknown>
      modeView.value = describeRealtimeMode(raw)
      modePayload.value = raw
    } catch { /* 拿不到就保持"未确认"，不假装 */ }
  }

  async function start(options: { video?: boolean; audio?: boolean } = {}) {
    if (active.value) return
    error.value = ''
    notice.value = ''
    const projectId = getProjectId()
    if (!projectId) {
      error.value = '没有选择项目，实时对话不可用（后端按项目隔离实时会话）。'
      return
    }
    await refreshMode()
    transport = createLiveTransport({
      projectId,
      onEvent,
      onState: (state) => {
        socketState.value = state
        reconnectScheduled.value = state === 'connecting' && !transport?.socketState().match(/open/)
      },
      onNotice: (message) => { notice.value = message },
    })
    active.value = true
    paused.value = false
    sequence = 0
    audioSequence = 0
    sender.reset()
    ledger.clear()
    transport.connect()
    startMetricsPolling()
    if (options.video !== false) await startVideo()
    if (options.audio) await startAudio()
    if (videoEnabled.value) scheduleNextFrame()
  }

  function pause() {
    if (!active.value) return
    paused.value = true
  }

  function resume() {
    if (!active.value) return
    paused.value = false
  }

  function stop() {
    active.value = false
    paused.value = false
    stopMetricsPolling()
    if (captureTimer) { clearTimeout(captureTimer); captureTimer = null }
    transport?.close('client')
    transport = null
    socketState.value = 'closed'
    if (mediaStream) { mediaStream.getTracks().forEach(track => track.stop()); mediaStream = null }
    if (videoElement) { videoElement.srcObject = null }
    disableAudio()
    videoElement = null
    canvas = null
    cameraState.value = 'idle'
    audioEnabled.value = false
    sender.reset()
    ledger.clear()
  }
  function cancel() {
    transport?.sendControl({ v: 1, type: 'cancel', reason: 'user' })
  }

  function disableAudio() {
    if (audioProcessor) { audioProcessor.onaudioprocess = null; audioProcessor.disconnect(); audioProcessor = null }
    if (audioSource) { audioSource.disconnect(); audioSource = null }
    if (audioContext) { void audioContext.close(); audioContext = null }
    voiceGate.reset()
    chunkPump.reset()
    audioEnabled.value = false
  }

  function bindVideo(element: HTMLVideoElement | null) {
    videoElement = element
  }

  onUnmounted(stop)

  return {
    phase, active, paused, modeView, modePayload, captions, observations, error, notice, stats,
    capabilities, videoEnabled, audioEnabled, cameraState, bindVideo, enableAudio: startAudio, disableAudio,
    focusRegion, channelStatus, refreshChannelStatus,
    start, pause, resume, stop, cancel, refreshMode,
  }
}
