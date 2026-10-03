<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { agentApi, getProjectId, getSessionId, visionApi, workflowEvents } from '../api'
import type { ProjectProfile, VisualFeedbackRecord, WorkflowAcceptanceReport, WorkflowEvent, WorkflowEvaluation, WorkflowState, WorkflowSummary } from '../api'
import { blobDataUrl } from '../previewFeedback'
import { capturePreviewFrame, refreshPreviewFrame } from '../workflowPreviewCapture'
import type { ChatUiContext, PreviewFeedbackRequest } from '../previewFeedback'
import { appEvents } from '../eventBus'
import { sampleVisualFrame, shouldAnalyzeVisualFrame } from '../liveVisionSampling'
import type { VisualSample } from '../liveVisionSampling'
import { cropFocusFrame, normalizeFocusRegion } from '../liveVisionFocus'
import { encodeLiveFrame } from '../liveFrameEncoding'
import type { EncodedLiveFrame, FrameEncodingOptions } from '../liveFrameEncoding'
import { LiveFrameEncoder } from '../liveFrameEncoder'
import { createLiveVisualWindow } from '../liveVisualWindow'
import type { FocusRegion } from '../liveVisionFocus'
import { advanceVisionAlert } from '../liveVisionAlerts'
import type { PendingVisionAlert, VisionAnomaly } from '../liveVisionAlerts'
import type { TimedObservation } from '../voiceVisionSync'
import { advanceVisualActionLoop, beginVisualActionLoop, restoreVisualActionLoop } from '../visualActionLoop'
import type { VisualActionLoop, VisualActionStep } from '../visualActionLoop'
import { encodeVideoFrame, parseRealtimeServerEvent, realtimeHello, realtimeCancel } from '../realtimeProtocol'
import {
  classifyLivePhase, createAdaptiveSender, createInFlightLedger, LIVE_PHASE_LABELS,
  appendCaptionTurn, closeUserTranscript, describeLiveCapabilities, shouldDispatchVoiceTurn,
  sameVisualFocus, isDismissedVisualAlert, pruneDismissedVisualAlerts,
  canQueueLiveVideo, sendLiveVideoPacket, liveVideoOverrun,
} from '../liveStreamControl'
import type { LivePhase, LiveCaptionTurn } from '../liveStreamControl'
import {
  createAudioPlaybackClock, createChunkPump, createVoiceGate, encodeAudioPacket, frameRms, resample,
  resolveLiveModelAudioFormat, TARGET_SAMPLE_RATE,
} from '../liveAudioControl'
import { REALTIME_PROTOCOL_VERSION } from '../realtimeProtocol'
import { createVoiceVerificationGate } from '../voiceVerification'
import CockpitApprovalQueue from './CockpitApprovalQueue.vue'
import CockpitModelBar from './CockpitModelBar.vue'
import WorkflowCard from './WorkflowCard.vue'

const emit = defineEmits<{ ready: []; runtimePreview: [visible: boolean]; toggleFiles: [] }>()
const detailsOpen = ref(false)
const historyDialog = ref<HTMLDialogElement | null>(null)
const historyBusy = ref(false)
const startDialog = ref<HTMLDialogElement | null>(null)
const replaceTasks = ref<WorkflowSummary[]>([])
const stageMode = ref<'outputs' | 'runtime'>('outputs')
const terminalStatuses = new Set(['completed', 'failed', 'interrupted'])
const activeHistory = computed(() => history.value.find(item => !terminalStatuses.has(item.status)))
const deletableHistory = computed(() => history.value.filter(item => terminalStatuses.has(item.status)))

function setStageMode(mode: 'outputs' | 'runtime') {
  stageMode.value = mode
  emit('runtimePreview', mode === 'runtime')
}
defineExpose({ showOutputs: () => setStageMode('outputs') })
function newGoal() {
  setStageMode('outputs')
  stopLiveVision()
  pendingDesktopDispatches.length = 0
  generation++
  unsubscribe?.(); unsubscribe = null
  if (refreshTimer) clearTimeout(refreshTimer)
  selectedId.value = ''; workflow.value = null; evaluation.value = null; acceptanceReport.value = null
  loadActionLoop()
  feedbackHistory.value = []; feedbackOpenId.value = ''; feedbackText.value = ''
  feedbackRegion.value = null; annotationId.value = ''; regionOwner.value = ''
  visualInspecting.value = false; visualInspectStatus.value = ''
  goal.value = ''; error.value = ''; detailsOpen.value = false
}
function openHistory() {
  historyDialog.value?.showModal()
  void loadList()
}
function restoreHistory(id: string) {
  historyDialog.value?.close()
  select(id)
}
async function deleteHistory(items: WorkflowSummary[]) {
  const targets = items.filter(item => terminalStatuses.has(item.status))
  if (!targets.length || historyBusy.value) return
  if (!window.confirm(`删除 ${targets.length} 条已结束的任务记录？记录删除后无法恢复；项目文件、聊天记录和长期记忆会保留。`)) return
  historyBusy.value = true
  error.value = ''
  const ticket = generation, projectId = getProjectId()
  try {
    for (const item of targets) {
      if (ticket !== generation || projectId !== getProjectId()) break
      const result = await agentApi.workflowDelete(item.workflow_id)
      if (!result.ok) throw new Error(result.error || '任务记录删除失败')
      if (ticket !== generation || projectId !== getProjectId()) return
      try { window.localStorage.removeItem(feedbackStorageKey(item.workflow_id)) } catch { /* Optional local cache. */ }
      history.value = history.value.filter(row => row.workflow_id !== item.workflow_id)
      if (selectedId.value === item.workflow_id) {
        unsubscribe?.(); unsubscribe = null
        selectedId.value = ''; workflow.value = null; evaluation.value = null; acceptanceReport.value = null
        feedbackHistory.value = []; feedbackOpenId.value = ''; detailsOpen.value = false
      }
    }
  } catch (e) { error.value = (e as Error).message }
  finally { historyBusy.value = false }
}
async function pauseHistory(item: WorkflowSummary) {
  if (historyBusy.value) return
  historyBusy.value = true
  error.value = ''
  const ticket = generation
  try {
    const result = await agentApi.workflowInterrupt(item.workflow_id, '用户在任务历史中暂停任务')
    if (!result.workflow) throw new Error(result.error || '暂停失败')
    if (ticket !== generation) return
    history.value = history.value.map(row => row.workflow_id === item.workflow_id ? { ...row, status: result.workflow!.status } : row)
    if (selectedId.value === item.workflow_id) workflow.value = result.workflow
  } catch (e) { error.value = (e as Error).message }
  finally { historyBusy.value = false }
}

const goal = ref('')
const workflow = ref<WorkflowState | null>(null)
const evaluation = ref<WorkflowEvaluation | null>(null)
const acceptanceReport = ref<WorkflowAcceptanceReport | null>(null)
const reportBusy = ref(false)
const chatHint = ref('')
const projectProfile = ref<ProjectProfile | null>(null)
const selectedId = ref('')
const history = ref<WorkflowSummary[]>([])
const error = ref('')
const busy = ref(false)
const feedbackOpenId = ref('')
const feedbackText = ref('')
const liveWidth = ref(100)
const liveHeight = ref(360)
const liveReload = ref<Record<string, number>>({})
const annotationId = ref('')
const feedbackRegion = ref<{ x: number; y: number; width: number; height: number } | null>(null)
interface FeedbackRecord extends VisualFeedbackRecord { sentAt: string }
const feedbackHistory = ref<FeedbackRecord[]>([])
const feedbackSending = ref(false)
const snapshotBusyId = ref('')
const visualInspecting = ref(false)
const visualInspectStatus = ref('')
const liveVisionActive = ref(false)
const liveVisionBusy = ref(false)
const liveVisionStatus = ref('')
const liveVisionMode = ref<'sampled-frames' | 'native-realtime' | 'native' | 'unavailable'>('sampled-frames')
const liveVisionModeLabel = ref('兼容抽帧')
const liveVisionModeReason = ref('当前按最新视频帧进行视觉理解。')
const liveVisionLimitations = ref<string[]>([])
const liveStreamCaptions = ref<LiveCaptionTurn[]>([])
// 转写增量和最终事件属于同一个用户回合；单独保存草稿，避免每个 delta
// 都变成一条“你说”，也避免重连/多轮时把上一回合重复拼进来。
const liveTranscriptDraft = ref('')
const liveStreamCapabilities = ref('')
const liveVisionObservation = ref('')
const liveVisionTimeline = ref<TimedObservation[]>([])
const liveVisionPaused = ref(false)
const liveVisionFocus = ref<FocusRegion | null>(null)
const liveVisionDraftFocus = ref<FocusRegion | null>(null)
const liveVisionSelecting = ref(false)
const liveVisionSnapshotUrl = ref('')
const liveVisionAlert = ref<(VisionAnomaly & { detectedAt: string; atMs: number }) | null>(null)
// `frame` 是**报出这条提醒时**的那一帧快照，不是"点排查时"的当前帧：模型说的是几秒前
// 看到的问题，用户过一会儿才点「交给 AI 排查」，那时画面可能已经变了——拿当前帧去核实
// 等于核实错了对象。
const liveRealtimeAlert = ref<{ id: string; text: string; detectedAt: string; frame: Blob | null; atMs: number } | null>(null)
const voiceVerification = createVoiceVerificationGate()
// 用户忽略过的实时提醒（10 分钟窗口、最多 20 条）：后端的相似度去重只有 45s，过了窗口
// 同一处会再弹，用户会以为「忽略」没生效。
let dismissedRealtimeAlerts: { text: string; at: number }[] = []
// 跨来源合并（在**渲染时**算，不动状态，避免两条链抢着改同一个 ref）：
// 抽帧视觉模型与实时模型是两条独立观测链，同一个问题会被两边各报一次。近 20 秒内且判为
// 同一处时，只渲染一条合并横幅，并保留两边的原文——合并是为了少一条重复横幅，
// 不是为了隐藏信息。
const mergedVisualAlert = computed(() => {
  const vision = liveVisionAlert.value
  const realtime = liveRealtimeAlert.value
  if (!vision || !realtime) return null
  if (Math.abs(realtime.atMs - vision.atMs) > 20_000) return null
  return sameVisualFocus(realtime.text, `${vision.target}${vision.evidence}`)
    ? { vision, realtime } : null
})
const liveVisionAlertImageUrl = ref('')
const liveVisionAlertStatus = ref('')
// R1/R6：自适应发送与实时状态。计划档位只影响采集/发送节奏，不触碰协议包字段。
let liveAdaptive = createAdaptiveSender()
let liveLedger = createInFlightLedger()
let liveStreamLastObservationAt = 0
const liveStreamConnected = ref(false)
const liveStreamReconnectScheduled = ref(false)
const liveStreamRecovering = ref(false)
const liveVisionPhase = ref<LivePhase>('idle')
const liveVisionMetrics = ref<{ latencyMs: number | null; intervalMs: number; dropped: number }>(
  { latencyMs: null, intervalMs: 500, dropped: 0 })
const liveVisionPhaseLabel = computed(() => LIVE_PHASE_LABELS[liveVisionPhase.value])
function refreshLiveVisionPhase() {
  liveVisionPhase.value = classifyLivePhase({
    active: liveVisionActive.value,
    socketState: liveStreamSocket && liveStreamConnected.value ? 'open'
      : liveStreamReconnectScheduled.value ? 'connecting' : 'closed',
    paused: liveVisionPaused.value,
    reconnectScheduled: liveStreamReconnectScheduled.value,
    lastObservationAgeS: liveStreamLastObservationAt
      ? (Date.now() - liveStreamLastObservationAt) / 1000 : null,
  })
}
function refreshLiveVisionMetrics(latencyMs: number | null = null) {
  liveVisionMetrics.value = {
    latencyMs: latencyMs !== null ? latencyMs : liveVisionMetrics.value.latencyMs,
    intervalMs: liveVisualWindow.captureInterval(Date.now(), liveAdaptive.plan().intervalMs, liveVideoImageBudget.value),
    dropped: liveVisionMetrics.value.dropped,
  }
}
function closeOpenLiveCaption() {
  const current = liveStreamCaptions.value
  const last = current[current.length - 1]
  if (!last || last.role !== 'assistant' || last.done) return
  liveStreamCaptions.value = [...current.slice(0, -1), { ...last, done: true }]
}
function resolveLiveStreamRecovery() {
  if (!liveStreamRecovering.value) return
  liveStreamRecovering.value = false
  liveStreamReconnectScheduled.value = false
  liveVisionStatus.value = '实时模型连接已恢复，继续实时对话。'
  refreshLiveVisionPhase()
}
function mergeLiveTranscript(previous: string, next: string): string {
  if (!previous) return next
  if (!next) return previous
  // 厂商有时重复发送“当前完整前缀”，有时只发送新增尾巴；两种形态都归并。
  if (next.startsWith(previous)) return next
  if (previous.startsWith(next)) return previous
  return `${previous}${next}`
}
function appendLiveCaption(event: ReturnType<typeof parseRealtimeServerEvent>) {
  if (!event) return
  if (event.type !== 'audio.transcript') {
    // 如果服务端因抢话/重连跳过了 transcript.final，模型已经开始回答时结束
    // 旧的用户草稿，避免下一轮 final 把上一句再次拼进去。
    if (event.type === 'model.delta' && liveTranscriptDraft.value) liveTranscriptDraft.value = ''
    const next = appendCaptionTurn(liveStreamCaptions.value, event)
    if (next !== liveStreamCaptions.value) liveStreamCaptions.value = next
    return
  }
  const text = typeof event.text === 'string' ? event.text.trim() : ''
  const final = event.final === true
  if (!final) {
    const hadDraft = !!liveTranscriptDraft.value
    const merged = mergeLiveTranscript(liveTranscriptDraft.value, text)
    if (!merged) return
    liveTranscriptDraft.value = merged
    const current = liveStreamCaptions.value
    const last = current[current.length - 1]
    const updated: LiveCaptionTurn[] = hadDraft && last?.role === 'user'
      ? [...current.slice(0, -1), { ...last, text: merged, done: true }]
      : [...current, { role: 'user', text: merged, done: true }]
    liveStreamCaptions.value = updated.slice(-8)
    return
  }
  const hadDraft = !!liveTranscriptDraft.value
  const finalText = text || liveTranscriptDraft.value
  liveTranscriptDraft.value = ''
  // 一条线性路径：`null` = 这条 final 是重放（同文或空），既不上字幕、也不转交。
  // 转交与记账绑在一起，就不可能一边去重一边重复转交（或反过来漏转）。
  const next = closeUserTranscript(liveStreamCaptions.value, finalText, hadDraft)
  if (!next) return
  liveStreamCaptions.value = next
  const consent = voiceVerification.decide(finalText, Date.now())
  if (consent !== 'none') {
    if (consent === 'approve') reviewRealtimeAlert(true)
    else if (consent === 'decline') {
      dismissRealtimeAlert()
      liveVisionAlertStatus.value = '已取消核查。'
    } else if (consent === 'expired') {
      liveVisionAlertStatus.value = '这条核查询问已过期，请点击核查按钮重新确认。'
    } else if (consent === 'clarify') {
      liveVisionAlertStatus.value = '还未确认核查。可以说“可以核查一下”，或点击下方按钮；说“不用”则取消。'
    }
    // Consent replies are not independent development commands.
    return
  }
  // 噪音门控：误识别与旁音不起 Agent 轮。挡下的回合在状态栏明说，不静默吞掉。
  const now = Date.now()
  if (!shouldDispatchVoiceTurn(finalText, lastVoiceDispatch, now)) {
    liveVisionStatus.value = `「${finalText}」没有转交（像语气词或重复语音）；要执行就直接说一遍完整的意思。`
    return
  }
  lastVoiceDispatch = { text: finalText, at: now }
  sendVoiceTurnToAgent(finalText)
}
/**
 * 语音指令转交主 Agent（A 档：默认全部转交，但噪音挡在门外）。
 *
 * 为什么必须在我们这侧做：实时模型**没有工具通道**（网关只转发音频/视频/文本事件），
 * 它自己永远改不了文件。所以「接收到指令就让主 Agent 开始改」只能由我们把这条转写
 * 当成一条对话消息派进开发舱对话台——主 Agent 有工具、有工作流和审批门，由它判断该不该动手。
 *
 * 两条刻意的约定：
 * - **prompt 只放用户原话**：框架说明一律走 uiContext（后端转成 system_context，不作为
 *   用户消息展示）。先前把「【语音指令】…（来自开发舱实时语音…）」拼进 prompt，结果这段
 *   内部说明以**用户自己的话**的形式出现在对话流里。
 * - **uiContext = cockpit_voice_turn**：后端据此附加语音轮次的回答约束（最多两三句、
 *   不罗列能力、闲聊时不提议改动），并且仍然附带实时观察时间线。
 */
function sendVoiceTurnToAgent(text: string) {
  const projectId = getProjectId()
  if (!projectId) return
  appEvents.emit('docmind:send-chat', {
    target: 'cockpit',
    projectId,
    prompt: text,
    uiContext: 'cockpit_voice_turn',
    onStatus: (status, detail) => {
      // 对话台正忙/有待审的门时，这条语音指令不会自动重试——必须让用户知道它没送到。
      if (status === 'pending') {
        liveVisionStatus.value = detail || '对话台正忙，这条语音指令未能送入，请稍后重说。'
      }
    },
  })
}

/** 最近一次真正转交过的语音（噪音门控用：同句不重发、连发只发一次）。 */
let lastVoiceDispatch: { text: string; at: number } = { text: '', at: 0 }
async function refreshLiveVisionMode() {
  try {
    const result = await visionApi.realtimeStatus()
    if (!result.ok) return
    liveVisionMode.value = result.mode || 'sampled-frames'
    liveVisionModeLabel.value = result.mode_label || '兼容抽帧'
    liveVisionModeReason.value = result.mode_reason || ''
    liveVisionLimitations.value = Array.isArray(result.limitations) ? result.limitations : []
  } catch {
    liveVisionMode.value = 'sampled-frames'
    liveVisionModeLabel.value = '兼容抽帧'
    liveVisionModeReason.value = '实时模式状态暂时不可读取，当前仍按兼容抽帧运行。'
    liveVisionLimitations.value = []
  }
}
function stopAiPlayback() {
  liveAudioPlaybackRevision++
  for (const source of liveAudioSources) {
    source.onended = null
    try { source.stop() } catch { /* 已自然结束 */ }
    source.disconnect()
  }
  liveAudioSources.clear()
  liveAudioPlaybackClock.reset()
  liveAiSpeaking.value = false
}
function interruptLiveStream(reason = '用户在开发舱打断') {
  const socket = liveStreamSocket
  if (!socket || socket.readyState !== WebSocket.OPEN) return
  try { socket.send(JSON.stringify(realtimeCancel(reason))) } catch { return }
  liveStreamSendRevision++
  resetLiveStreamDedup()
  stopAiPlayback()
  closeOpenLiveCaption()
  liveLedger.clear()
  liveStreamFrames.clear()
  liveVisionPhase.value = 'viewing'
  liveVisionStatus.value = '已打断：旧帧的理解结果不再返回，AI 只看接下来的新画面。'
}

// ---- R2 语音闭环：麦克风采集 → audio.chunk 上行 → model.audio 播放 → 抢话自动打断 ----
// 分片/VAD/静音尾/包封的数学全在 liveAudioControl.ts（node 契约测试覆盖）；
// 这里只做浏览器 API 接线。静音尾时序：tail 期把等长零样本喂进分片泵，按实时节奏
// 自然产 100ms 全零片——停止说话后绝不掐流（AI-D 真机契约，0db20ff）。
const liveMicActive = ref(false)
const liveAiSpeaking = ref(false)
const liveMicStatus = ref('')
let liveAudioStream: MediaStream | null = null
let liveAudioContext: AudioContext | null = null
let liveAudioNode: AudioWorkletNode | null = null
const liveAudioSources = new Set<AudioBufferSourceNode>()
const liveAudioPlaybackClock = createAudioPlaybackClock()
let liveAudioPlaybackRevision = 0
let liveAudioSequence = 0
let liveNativeAudioReady = false
let liveStreamReadyForVideo = false
let liveVoiceGate = createVoiceGate()
let liveChunkPump = createChunkPump()

function getLiveAudioContext(): AudioContext | null {
  if (liveAudioContext && liveAudioContext.state !== 'closed') return liveAudioContext
  try { liveAudioContext = new AudioContext() } catch { return null }
  return liveAudioContext
}
function sendLiveAudioChunk(payload: Uint8Array): boolean {
  const socket = liveStreamSocket
  if (!socket || socket.readyState !== WebSocket.OPEN || socket.bufferedAmount > 500_000) return false
  liveAudioSequence += 1
  try {
    socket.send(encodeAudioPacket({ version: REALTIME_PROTOCOL_VERSION,
      sequence: liveAudioSequence, capturedAt: Date.now(), payload }))
    return true
  } catch { /* 断线由重连路径接管 */ return false }
}
function sendNativeAudioReady(): boolean {
  if (liveNativeAudioReady) return true
  // DashScope Omni rejects input_image_buffer.append until it has seen an
  // audio append. Send one 100ms PCM16 silence block before releasing video.
  const sent = sendLiveAudioChunk(new Uint8Array(3_200))
  if (sent) liveNativeAudioReady = true
  return sent
}
function handleLiveAudioFrame(frame: Float32Array) {
  const context = liveAudioContext
  if (!context) return
  const signal = liveVoiceGate.feed(frameRms(frame), Date.now(), liveAiSpeaking.value)
  if (signal.started) {
    resetLiveStreamDedup(false)
    liveVisualWindow.activate(Date.now(), '用户开始说话')
    scheduleLiveStreamFrame(liveVisionGeneration, 0)
  }
  if (signal.bargeIn) {
    liveStreamSendRevision++
    resetLiveStreamDedup()
    stopAiPlayback()
    closeOpenLiveCaption()
    const socket = liveStreamSocket
    if (socket && socket.readyState === WebSocket.OPEN) {
      try { socket.send(JSON.stringify(realtimeCancel('用户抢话'))) } catch { /* 忽略 */ }
    }
    liveMicStatus.value = '已抢话：AI 停止说话，继续听你讲。'
  }
  if (!signal.sending) {
    if (signal.tailDone && liveChunkPump.pending() > 0) liveChunkPump.reset()
    return
  }
  const tailLength = Math.round(frame.length * TARGET_SAMPLE_RATE / context.sampleRate)
  const samples = signal.state === 'tail'
    ? new Float32Array(tailLength)
    : resample(frame, context.sampleRate, TARGET_SAMPLE_RATE)
  for (const chunk of liveChunkPump.push(samples)) sendLiveAudioChunk(chunk)
}
async function startLiveMic() {
  if (liveMicActive.value) return
  if (!navigator.mediaDevices?.getUserMedia) { liveMicStatus.value = '当前浏览器不支持麦克风采集。'; return }
  if (!liveStreamSocket || liveStreamSocket.readyState !== WebSocket.OPEN) {
    liveMicStatus.value = '请先连接实时视频流再开麦。'
    return
  }
  try {
    liveAudioStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false })
    const context = getLiveAudioContext()
    if (!context) throw new Error('当前浏览器不支持音频上下文')
    await context.resume()
    const workletSource = `class LiveMicTap extends AudioWorkletProcessor {
  process(inputs) {
    const frame = new Float32Array(inputs[0][0])
    this.port.postMessage(frame.buffer, [frame.buffer])
    return true
  }
}
registerProcessor('live-mic-tap', LiveMicTap)`
    const url = URL.createObjectURL(new Blob([workletSource], { type: 'application/javascript' }))
    await context.audioWorklet.addModule(url)
    URL.revokeObjectURL(url)
    liveAudioNode = new AudioWorkletNode(context, 'live-mic-tap')
    liveAudioNode.port.onmessage = (message: MessageEvent) => {
      handleLiveAudioFrame(new Float32Array(message.data))
    }
    // 零增益汇接：维持回调节拍，但绝不把麦克风原声送回扬声器（监听回授）。
    const sink = context.createGain()
    sink.gain.value = 0
    liveAudioNode.connect(sink)
    sink.connect(context.destination)
    context.createMediaStreamSource(liveAudioStream).connect(liveAudioNode)
    liveVoiceGate = createVoiceGate()
    liveChunkPump = createChunkPump()
    liveMicActive.value = true
    liveMicStatus.value = '语音已连接：说完会自动补静音尾收句，AI 说话时可直接抢话。'
  } catch (cause) {
    await stopLiveMic()
    liveMicStatus.value = (cause as Error).name === 'NotAllowedError'
      ? '未获得麦克风授权。' : `麦克风启动失败：${(cause as Error).message || '未知原因'}`
  }
}
async function stopLiveMic(message = '麦克风已关闭。') {
  liveMicActive.value = false
  if (liveAudioNode) { try { liveAudioNode.port.onmessage = null; liveAudioNode.disconnect() } catch { /* 忽略 */ } liveAudioNode = null }
  for (const track of liveAudioStream?.getAudioTracks() ?? []) track.stop()
  liveAudioStream = null
  liveVoiceGate.reset()
  liveChunkPump.reset()
  stopAiPlayback()
  if (message) liveMicStatus.value = message
}
async function playLiveModelAudio(raw: { audio?: unknown; encoding?: unknown; sample_rate?: unknown; sampleRate?: unknown }) {
  if (typeof raw.audio !== 'string' || !raw.audio) return
  const context = getLiveAudioContext()
  if (!context) return
  const revision = liveAudioPlaybackRevision
  try {
    // DashScope's `pcm24` means 16-bit PCM at 24 kHz.  Prefer an explicit
    // sample_rate if a future provider supplies one; reject unknown metadata
    // instead of playing bytes at the wrong speed and masking a protocol drift.
    const format = resolveLiveModelAudioFormat(raw.encoding, raw.sample_rate ?? raw.sampleRate)
    if (!format) {
      liveMicStatus.value = `模型音频格式无法识别（encoding=${String(raw.encoding ?? '缺失')}），已跳过播放。`
      return
    }
    const binary = atob(raw.audio)
    const bytes = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
    if (bytes.byteLength % (format.bitsPerSample / 8) !== 0) {
      liveMicStatus.value = '模型音频长度不是完整 PCM16 样本，已跳过播放。'
      return
    }
    const samples = new Float32Array(bytes.length / 2)
    const view = new DataView(bytes.buffer)
    for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32767
    if (!samples.length) return
    if (context.state === 'suspended') await context.resume()
    // A resume may finish after interruption or reconnect; never revive old audio.
    if (revision !== liveAudioPlaybackRevision) return
    if (context.state !== 'running') {
      liveMicStatus.value = '声音播放尚未启用，请点击开麦后重试。'
      return
    }
    const buffer = context.createBuffer(1, samples.length, format.sampleRate)
    buffer.copyToChannel(samples, 0)
    const source = context.createBufferSource()
    source.buffer = buffer
    source.connect(context.destination)
    const startAt = liveAudioPlaybackClock.schedule(context.currentTime, buffer.duration)
    source.start(startAt)
    liveAudioSources.add(source)
    source.onended = () => {
      source.disconnect()
      liveAudioSources.delete(source)
      if (liveAudioSources.size === 0) liveAiSpeaking.value = false
    }
    liveAiSpeaking.value = true
  } catch {
    if (revision === liveAudioPlaybackRevision) liveMicStatus.value = '声音播放失败，请重新开麦后重试。'
  }
}
const clickDescription = ref('')
const clickGoal = ref('')
const clickBusy = ref(false)
const clickExecuting = ref(false)
const clickError = ref('')
const clickStatus = ref('')
const clickProposal = ref<{
  proposal_id: string; image: string; bbox: number[]; point: { x: number; y: number }
  label: string; evidence: string; expires_in: number; goal: string; description: string
} | null>(null)
const clickBeforeImage = ref('')
const clickAfterImage = ref('')
const clickVerification = ref<{ status: 'met' | 'unmet' | 'uncertain' | 'unavailable'; evidence: string; confidence?: number; next_target?: string } | null>(null)
const clickFeedbackStatus = ref('')
const lastClickFeedbackId = ref('')
const clickReviewSaving = ref(false)
const actionLoop = ref<VisualActionLoop | null>(null)
let clickRequestGeneration = 0

function actionLoopKey(projectId = getProjectId(), workflowId = selectedId.value) {
  return `docmind.visualActionLoop:${projectId}:${workflowId || 'manual'}`
}
function saveActionLoop() {
  const loop = actionLoop.value
  if (!loop) return
  try { sessionStorage.setItem(actionLoopKey(loop.projectId, loop.workflowId), JSON.stringify(loop)) } catch { /* private mode */ }
}
function loadActionLoop() {
  try { actionLoop.value = restoreVisualActionLoop(sessionStorage.getItem(actionLoopKey()), getProjectId(), selectedId.value) }
  catch { actionLoop.value = null }
}
function setActionLoop(next: VisualActionLoop) { actionLoop.value = next; saveActionLoop() }
function startActionLoop() {
  if (clickBusy.value || !getProjectId()) return
  const goal = clickGoal.value.trim(), target = clickDescription.value.trim()
  if (!goal || !target) { clickError.value = '连续视觉任务需要填写目标效果和第一步控件。'; return }
  setActionLoop(beginVisualActionLoop(getProjectId(), selectedId.value, goal, target))
  void requestClickTarget(target, goal)
}
function stopActionLoop() {
  if (!actionLoop.value || clickExecuting.value) return
  clickRequestGeneration++
  clickBusy.value = false
  setActionLoop({ ...actionLoop.value, phase: 'stopped', note: '本轮已停止；已执行的点击不会自动撤销。' })
  clickProposal.value = null
  clickStatus.value = ''
}
async function acceptActionLoop() {
  if (actionLoop.value?.phase !== 'awaiting_user' || clickReviewSaving.value) return
  const loopId = actionLoop.value.id, projectId = getProjectId()
  const wid = workflow.value?.workflow_id, feedbackId = lastClickFeedbackId.value
  if (wid && feedbackId && feedbackHistory.value.some(item => item.id === feedbackId)) {
    try {
      const saved = await agentApi.workflowFeedbackStatus(wid, feedbackId, 'accepted')
      if (!saved.ok || !saved.feedback) throw new Error(saved.error || '验收结果未能保存')
      if (projectId !== getProjectId() || actionLoop.value?.id !== loopId) return
      upsertFeedback(mapFeedback(saved.feedback))
    } catch (cause) {
      if (projectId === getProjectId() && actionLoop.value?.id === loopId) clickError.value = (cause as Error).message || '验收结果未能保存'
      return
    }
  }
  if (projectId !== getProjectId() || actionLoop.value?.id !== loopId || actionLoop.value.phase !== 'awaiting_user') return
  setActionLoop({ ...actionLoop.value, phase: 'completed', note: '你已确认画面效果。项目功能和文件仍以正式验收报告为准。' })
}
function continueActionLoop() {
  const loop = actionLoop.value
  if (!loop || !['needs_review', 'awaiting_user'].includes(loop.phase) || clickBusy.value) return
  const target = clickDescription.value.trim()
  if (!target) { clickError.value = '请先填写下一步要定位的控件。'; return }
  setActionLoop({ ...loop, phase: 'locating', lastTarget: target, note: `正在重新定位：${target}` })
  void requestClickTarget(target, loop.goal, true)
}
const liveVisionSource = ref<'网页预览' | '桌面窗口' | '屏幕共享' | '摄像头'>('网页预览')
const sharedVideo = ref<HTMLVideoElement | null>(null)
const screenSharing = ref(false)
const cameraSharing = ref(false)
const regionOwner = ref('')
const feedbackStatusLabel: Record<string, string> = { pending: '待发送', processing: 'AI 处理中', awaiting_review: '等待检查效果', failed: '处理未完成', accepted: '效果已确认', needs_changes: '需要继续修改', sent: '历史记录，发送状态未确认' }
let resizeState: { startX: number; startY: number; width: number; height: number; containerWidth: number } | null = null
let regionState: { id: string; rect: DOMRect; startX: number; startY: number } | null = null
const liveFrameRefs = new Map<string, HTMLIFrameElement>()
const captureErrors = new Map<string, string>()
const feedbackUpdates = new Map<string, Promise<void>>()
const pendingDesktopDispatches: Array<{
  record: FeedbackRecord; prompt: string; images: Blob[]; wid: string; projectId: string; ticket: number
}> = []
let unsubscribe: (() => void) | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let refreshTimer: ReturnType<typeof setTimeout> | null = null
let liveVisionTimer: ReturnType<typeof setTimeout> | null = null
let liveVisionGeneration = 0
let liveVisionArtifactId = ''
let liveVisionLastSample: VisualSample | null = null
let liveVisionLastAnalyzedAt = 0
let liveVisionQuietSamples = 0
let liveVisionRequest: AbortController | null = null
let liveVisionLatestFrame: Blob | null = null
let liveVisionLatestFocusFrame: Blob | null = null
let liveVisionLatestCapturedAt = 0
let liveVisionDrag: { bounds: DOMRect; start: { x: number; y: number } } | null = null
let liveVisionFocusRevision = 0
let pendingVisionAlert: PendingVisionAlert | null = null
let liveVisionAlertFrame: Blob | null = null
const acknowledgedVisionAlerts = new Set<string>()
let screenStream: MediaStream | null = null
let liveStreamSocket: WebSocket | null = null
let liveStreamTimer: ReturnType<typeof setTimeout> | null = null
let liveStreamReconnectTimer: ReturnType<typeof setTimeout> | null = null
let liveStreamHeartbeatTimer: ReturnType<typeof setInterval> | null = null
let liveStreamSequence = 0
const liveStreamFrames = new Map<number, { image: Blob; focusImage: Blob | null; capturedAt: number }>()
const LIVE_STREAM_FRAME_LIMIT = 32
const LIVE_STREAM_FRAME_MAX_AGE_MS = 30_000
let liveStreamSendRevision = 0
let liveStreamCapture: object | null = null
let liveCaptureCanvas: HTMLCanvasElement | null = null
let liveFocusCanvas: HTMLCanvasElement | null = null
let liveSampleCanvas: HTMLCanvasElement | null = null
let liveFrameEncoder: LiveFrameEncoder | null = null
let liveStreamLastSentSample: VisualSample | null = null
let liveStreamLastSentAt = 0
const liveStreamStaticSkipped = ref(0)
const liveVisualWindow = createLiveVisualWindow()
let liveForceStreamFrame = true
const liveVideoImagesUsed = ref(0)
const liveVideoImageBudget = ref(90)
try {
  const saved = Number(localStorage.getItem('docmind.liveVideoImageBudget'))
  if (saved >= 6 && saved <= 240) liveVideoImageBudget.value = Math.floor(saved)
} catch { /* local storage may be unavailable */ }
watch(liveVideoImageBudget, value => {
  const normalized = Number.isFinite(Number(value)) ? Math.max(6, Math.min(240, Math.floor(Number(value)))) : 90
  liveVideoImageBudget.value = normalized
  try { localStorage.setItem('docmind.liveVideoImageBudget', String(normalized)) } catch { /* ignore */ }
})

function resetLiveStreamDedup(resetWindow = true) {
  liveStreamLastSentSample = null
  liveStreamLastSentAt = 0
  liveForceStreamFrame = true
  if (resetWindow) liveVisualWindow.reset()
}

function pruneLiveStreamFrames(now = Date.now()): number {
  let removed = 0
  for (const [sequence, frame] of liveStreamFrames) {
    if (now - frame.capturedAt > LIVE_STREAM_FRAME_MAX_AGE_MS) {
      liveStreamFrames.delete(sequence)
      removed += 1
    }
  }
  while (liveStreamFrames.size > LIVE_STREAM_FRAME_LIMIT) {
    const oldest = liveStreamFrames.keys().next().value
    if (oldest === undefined) break
    liveStreamFrames.delete(oldest)
    removed += 1
  }
  return removed
}
let generation = 0
const statusLabel: Record<string, string> = {
  awaiting_choice: '等待选择方案', generating_options: '正在设计方案', planning: '正在规划',
  planned: '等待启动', awaiting_approval: '等待计划审核', executing: '持续执行中',
  completed: '执行完成，等待验收', failed: '执行失败', interrupted: '已暂停',
  awaiting_research: '等待资料',
}
/**
 * 「现在正在做什么」——**这一条是治「用户以为卡了」的**。
 *
 * 实测 `wf-f3da9d3ffb32`：全程 360 秒里 **98% 是静默的**，而每一段静默都是一次
 * 模型调用（87s 在等重规划方案、58s 在等结论、41s 在等规划器…）。harness 全都
 * 知道自己在等模型，只是没说；界面上状态一直是静态的「持续执行中」，
 * 于是用户看到的就是「卡了」。
 *
 * 所以这里由**最新事件 + 心跳**推出当前动作，并把已等待时长一起说出来——
 * 数字每秒在跳本身就是「它还活着」的凭据。
 */
const ACTIVITY_TEXT: Record<string, string> = {
  clarify: 'AI 正在理解你的目标',
  options_pending: 'AI 正在设计方案',
  options_generated: 'AI 正在把方案拆成任务',
  choice: '已选定方案，正在准备执行',
  plan: '计划已生成，等待你批准',
  project_stage_created: '正在建立临时试做区',
  capability_lease_created: '正在授予工具权限',
  auto_execute_start: '正在启动执行',
  approval_required: '等待你批准执行计划',
  approval_granted: '已批准，正在启动',
  execute_start: '开始执行',
  context_prepared: '正在准备上游上下文',
  wave: '正在进入任务轮次',
  wave_start: '正在启动本轮任务',
  before_tool: '正在调用工具',
  after_tool: '工具已返回，AI 正在判断下一步',
  subagent_start: '子代理正在工作',
  subagent_complete: '子代理已完成',
  task_complete: '任务已完成',
  task_blocked: '任务被阻塞',
  wave_complete: '本轮已结束，准备下一轮',
  graph_replan_start: 'AI 正在重新规划（这一步最慢，通常几十秒）',
  graph_replan_proposal: '重规划已就绪',
  graph_loop: '正在汇总与复核',
  review: '正在验收复核',
  experience: '正在记录本次经验',
}
/** 超过这个秒数就明确说「已经等了多久」——静默越久，越需要这句话。 */
const ACTIVITY_AFTER_SECONDS = 8
const activityTick = ref(Date.now())
let activityTimer: ReturnType<typeof setInterval> | null = null

const nowActivity = computed((): { text: string; waited: number; stale: boolean } | null => {
  const status = workflow.value?.status || ''
  if (!workflow.value) return null
  if (['completed', 'failed', 'interrupted', 'awaiting_choice', 'awaiting_approval', 'planned'].includes(status)) {
    // 终态与等人操作的节点不需要「正在做什么」——那种时候界面本来就该是静的。
    return null
  }
  const events = workflow.value?.timeline || workflow.value?.events || []
  // timeline 是倒序的（最新在前）；找不到就用数组末尾。
  const latest = (events[0]?.ts && events.length ? events[0] : events[events.length - 1]) || null
  const kind = String(latest?.kind || '')
  const text = ACTIVITY_TEXT[kind] || (kind ? statusLabel[status] || '正在处理' : '')
  const stamp = Date.parse(String(latest?.ts || ''))
  const waited = Number.isFinite(stamp) ? Math.max(0, Math.floor((activityTick.value - stamp) / 1000)) : 0
  return { text, waited, stale: waited >= ACTIVITY_AFTER_SECONDS }
})
function startActivityTicker() {
  if (activityTimer) return
  activityTimer = setInterval(() => { activityTick.value = Date.now() }, 1000)
}
function stopActivityTicker() {
  if (activityTimer) { clearInterval(activityTimer); activityTimer = null }
}

const cockpitSteps = [
  { key: 'goal', label: '目标', hint: '描述想实现的结果' },
  { key: 'execute', label: '执行', hint: '计划、工具与修改' },
  { key: 'preview', label: '预览', hint: '查看真实项目效果' },
  { key: 'accept', label: '验收', hint: '检查证据并决定' },
] as const
const cockpitStepIndex = computed(() => {
  const status = workflow.value?.status || ''
  if (!workflow.value) return 0
  if (stageMode.value === 'runtime') return 2
  if (['awaiting_choice', 'generating_options', 'planning', 'planned', 'awaiting_approval', 'awaiting_research'].includes(status)) return 0
  if (['executing'].includes(status)) return 1
  if (['completed', 'failed', 'interrupted'].includes(status)) return 3
  return 1
})
const pending = computed(() => workflow.value &&
  ['awaiting_choice', 'planned', 'awaiting_approval', 'awaiting_research'].includes(workflow.value.status))
const taskResults = computed<Record<string, Record<string, unknown>>>(() => {
  const report = workflow.value?.results || {}
  return (report.results as Record<string, Record<string, unknown>> | undefined)
    || report as Record<string, Record<string, unknown>>
})
const doneCount = computed(() => Object.values(taskResults.value).filter(r => r.status === 'ok').length)
const timelineEvents = computed(() => (workflow.value?.timeline || workflow.value?.events || []).slice().reverse())
const actualToolCalls = computed(() => timelineEvents.value.filter(event => ['before_tool', 'after_tool', 'before_mcp', 'after_mcp'].includes(String(event.kind))))
const hasExecution = computed(() => !!(workflow.value?.steps || typeof workflow.value?.review?.ok === 'boolean' || timelineEvents.value.some(event => event.kind === 'execute_start')))
const previewArtifacts = computed(() => workflow.value?.preview?.artifacts || [])
const previewKindLabel: Record<string, string> = {
  code: '代码', text: '文本', diff: '差异', image: '图片', audio: '音频',
  video: '视频', interactive: '实时画面', model: '模型', structured: '结构化数据', binary: '文件', unknown: '证据',
}
const capabilityLabel: Record<string, string> = {
  read_local: '读取项目', read_external: '读取外部资料', write_local: '写入项目',
  write_external: '写入外部服务', exec: '执行命令', network: '联网 / MCP', admin: '管理操作',
}
const timelineLabel: Record<string, string> = {
  execute_start: '开始执行', subagent_start: '子代理开始', subagent_complete: '子代理完成',
  task_complete: '任务完成', task_blocked: '任务阻塞', before_tool: '工具调用前', after_tool: '工具调用后',
  before_mcp: 'MCP 调用前', after_mcp: 'MCP 调用后', approval_required: '等待审批',
  approval_granted: '已批准', approval_denied: '已拒绝', project_checkpoint_created: '创建项目快照',
  project_checkpoint_restored: '恢复项目快照', visual_snapshot_saved: '保存视觉证据', review: '验收检查',
  acceptance_decided: '用户验收决定', interrupt: '已暂停', resume: '已恢复', evaluation: '质量评估',
  // 下面这批此前**没有**标签，界面上直接显示英文 kind 名。
  // 实测 `wf-f3da9d3ffb32` 一次运行发出 27 种 kind，其中 16 种是英文——
  // 包括 `graph_replan_start` / `graph_replan_proposal` 这两个用户最需要看懂的
  // （重规划开始/结束）。「时间线上一半是英文」这件事本身就在劝退人读它。
  clarify: '正在理解你的目标',
  options_pending: '等待方案生成',
  options_generated: '方案已生成',
  choice: '已选定方案',
  plan: '任务计划已生成',
  project_stage_created: '已建立临时试做区',
  capability_lease_created: '已授予临时工具权限',
  auto_execute_start: '自动开始执行',
  context_prepared: '已准备上游上下文',
  wave: '进入任务轮次',
  wave_start: '本轮任务开始',
  wave_complete: '本轮任务结束',
  graph_replan_start: 'AI 正在重新规划',
  graph_replan_proposal: '重规划方案已就绪',
  graph_loop: '正在汇总与复核',
  experience: '正在记录本次经验',
}
function timelineDetail(event: WorkflowEvent): string {
  const task = event.task_id ? `任务 ${event.task_id} · ` : ''
  const detail = event.error || event.conclusion || event.reason || event.summary || event.detail || event.status || ''
  return `${task}${String(detail || '已记录').replace(/\s+/g, ' ').slice(0, 320)}`
}
function timelineCanReplay(event: WorkflowEvent): boolean {
  const taskId = String(event.task_id || '')
  const status = String(taskId ? taskResults.value[taskId]?.status || event.status || '' : '')
  return !!taskId && ['failed', 'blocked'].includes(status)
}
function leaseIsExpired(lease: WorkflowState['capability_lease']): boolean {
  return !!lease?.expires_at_epoch && Date.now() / 1000 >= lease.expires_at_epoch
}
function leaseStatusLabel(lease: WorkflowState['capability_lease']): string {
  if (!lease) return '未启用'
  if (lease.status === 'released') return '已释放'
  if (leaseIsExpired(lease)) return '已过期'
  return '当前有效'
}
function profileMcpLabel(item: { name?: string; key?: string; enabled?: boolean; capabilities?: string[] }): string {
  const name = item.name || item.key || 'MCP'
  const caps = item.capabilities?.length ? ` · ${item.capabilities.slice(0, 4).join('、')}` : ''
  return `${name}${item.enabled === false ? '（已停用）' : ''}${caps}`
}
function previewSource(artifact: { uri?: string; path?: string; id?: string }): string {
  const uri = String(artifact.uri || '')
  if (/^https?:\/\//i.test(uri) || /^\/(?!\/)/.test(uri)) return uri
  const id = String(artifact.id || '')
  return id && workflow.value?.workflow_id
    ? `/api/agent/workflow/${encodeURIComponent(workflow.value.workflow_id)}/preview/artifact/${encodeURIComponent(id)}`
    : ''
}
function feedbackStorageKey(id = workflow.value?.workflow_id) {
  return id ? `docmind.cockpit.feedback.${id}` : ''
}
function loadFeedbackHistory(id: string) {
  feedbackHistory.value = []
  try {
    const raw = window.localStorage.getItem(feedbackStorageKey(id))
    const parsed = raw ? JSON.parse(raw) : []
    if (Array.isArray(parsed)) feedbackHistory.value = parsed.filter(row => row && typeof row.note === 'string').slice(0, 20).map(mapFeedback)
  } catch { /* 浏览器存储不可用时只保留当前会话记录 */ }
}
function mapFeedback(item: VisualFeedbackRecord | Record<string, unknown>): FeedbackRecord {
  return {
    ...item as VisualFeedbackRecord,
    id: String(item.id || `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`),
    label: String(item.label || '实时画面'),
    note: String(item.note || ''),
    region: item.region as FeedbackRecord['region'],
    screenshot: !!item.screenshot,
    sent_at: String(item.sent_at || ('sentAt' in item && item.sentAt) || new Date().toISOString()),
    sentAt: String(item.sent_at || ('sentAt' in item && item.sentAt) || new Date().toISOString()),
  }
}
function upsertFeedback(record: FeedbackRecord) {
  const rows = [record, ...feedbackHistory.value.filter(row => row.id !== record.id)]
  feedbackHistory.value = rows.sort((a, b) => b.sentAt.localeCompare(a.sentAt)).slice(0, 20)
  saveFeedbackHistory()
}
function saveFeedbackHistory() {
  const key = feedbackStorageKey()
  if (!key) return
  try { window.localStorage.setItem(key, JSON.stringify(feedbackHistory.value.slice(0, 20))) } catch { /* ignore quota/private mode */ }
}
function liveFrameStyle() {
  return { width: `${liveWidth.value}%`, height: `${liveHeight.value}px` }
}
function startResize(ev: PointerEvent) {
  const handle = ev.currentTarget as HTMLElement
  handle.setPointerCapture(ev.pointerId)
  resizeState = { startX: ev.clientX, startY: ev.clientY, width: liveWidth.value, height: liveHeight.value,
    containerWidth: handle.parentElement?.parentElement?.clientWidth || 300 }
  window.addEventListener('pointermove', resizeLiveFrame)
  window.addEventListener('pointerup', stopResize, { once: true })
}
function resizeLiveFrame(ev: PointerEvent) {
  if (!resizeState) return
  liveWidth.value = Math.max(45, Math.min(100, resizeState.width + (ev.clientX - resizeState.startX) * 100 / resizeState.containerWidth))
  liveHeight.value = Math.max(220, Math.min(720, resizeState.height + ev.clientY - resizeState.startY))
}
function stopResize() {
  resizeState = null
  window.removeEventListener('pointermove', resizeLiveFrame)
  window.removeEventListener('pointerup', stopResize)
}
function setLiveFrameRef(id: string | undefined, value: unknown) {
  if (!id) return
  if (value instanceof HTMLIFrameElement) liveFrameRefs.set(id, value)
  else liveFrameRefs.delete(id)
}
function reloadLive(id: string | undefined) {
  if (!id) return
  liveReload.value = { ...liveReload.value, [id]: (liveReload.value[id] || 0) + 1 }
}
function fullscreenLive(id: string | undefined) {
  if (!id) return
  const frame = liveFrameRefs.get(id)
  void frame?.parentElement?.requestFullscreen?.().catch(() => { error.value = '当前环境未允许全屏查看' })
}
function toggleAnnotation(id: string) {
  regionOwner.value = id
  feedbackRegion.value = null
  annotationId.value = annotationId.value === id ? '' : id
  if (annotationId.value !== id) {
    feedbackRegion.value = null
    regionState = null
  }
}
function beginAnnotation(ev: PointerEvent, id: string) {
  const target = ev.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  regionState = { id, rect, startX: ev.clientX, startY: ev.clientY }
  regionOwner.value = id
  feedbackRegion.value = { x: 0, y: 0, width: 0, height: 0 }
  target.setPointerCapture?.(ev.pointerId)
}
function moveAnnotation(ev: PointerEvent, id: string) {
  if (!regionState || regionState.id !== id) return
  const { rect, startX, startY } = regionState
  const endX = Math.max(rect.left, Math.min(rect.right, ev.clientX))
  const endY = Math.max(rect.top, Math.min(rect.bottom, ev.clientY))
  const left = Math.min(startX, endX) - rect.left
  const top = Math.min(startY, endY) - rect.top
  feedbackRegion.value = {
    x: Math.round((left / rect.width) * 1000) / 10,
    y: Math.round((top / rect.height) * 1000) / 10,
    width: Math.round((Math.abs(endX - startX) / rect.width) * 1000) / 10,
    height: Math.round((Math.abs(endY - startY) / rect.height) * 1000) / 10,
  }
}
function endAnnotation() {
  regionState = null
  annotationId.value = ''
  if (feedbackRegion.value?.width && feedbackRegion.value.height) feedbackOpenId.value = regionOwner.value
}
function regionStyle() {
  const r = feedbackRegion.value
  if (!r) return {}
  return { left: `${r.x}%`, top: `${r.y}%`, width: `${r.width}%`, height: `${r.height}%` }
}
async function captureLiveFrame(id: string): Promise<Blob | null> {
  try {
    const frame = liveFrameRefs.get(id)
    if (!frame) throw new Error('预览尚未显示，请展开画面后重试')
    const image = await capturePreviewFrame(frame)
    captureErrors.delete(id)
    return image
  } catch (e) {
    captureErrors.set(id, (e as Error).message || '当前画面无法截图')
    return null
  }
}
function clearLiveVisionTimer() {
  if (liveVisionTimer) { clearTimeout(liveVisionTimer); liveVisionTimer = null }
}
function setLiveVisionSnapshot(image: Blob) {
  if (liveVisionSelecting.value) return
  const previous = liveVisionSnapshotUrl.value
  liveVisionSnapshotUrl.value = URL.createObjectURL(image)
  if (previous) URL.revokeObjectURL(previous)
}
function resetFocusObservation() {
  liveVisionFocusRevision++
  resetLiveStreamDedup()
  liveVisionRequest?.abort()
  liveVisionRequest = null
  liveVisionLastSample = null
  liveVisionLastAnalyzedAt = 0
  liveVisionQuietSamples = 0
  liveVisionObservation.value = ''
  liveVisionTimeline.value = []
  pendingVisionAlert = null
  dismissVisionAlert()
  liveRealtimeAlert.value = null
  acknowledgedVisionAlerts.clear()
  appEvents.emit('docmind:live-vision-frame', {
    projectId: getProjectId(), image: liveVisionLatestFrame, capturedAt: liveVisionLatestCapturedAt, focusImage: null, timeline: [],
  })
  if (liveVisionActive.value && !liveVisionPaused.value) {
    if (screenSharing.value || cameraSharing.value) scheduleLiveStreamFrame(liveVisionGeneration, 100)
    else scheduleLiveVision(liveVisionGeneration, 100)
  }
}
function dismissVisionAlert() {
  liveVisionAlert.value = null
  liveVisionAlertFrame = null
  liveVisionAlertStatus.value = ''
  if (liveVisionAlertImageUrl.value) URL.revokeObjectURL(liveVisionAlertImageUrl.value)
  liveVisionAlertImageUrl.value = ''
}
function reviewVisionAlert() {
  const alert = liveVisionAlert.value
  if (!alert) return
  const projectId = getProjectId()
  const prompt = `持续视觉在两次画面中发现疑似异常：${alert.target}。画面线索：${alert.evidence}。附件是发现时的真实画面。请结合当前项目文件、运行日志和画面核实是否确有问题；先提出修改方案与验收条件，修改前等待用户确认。`
  appEvents.emit('docmind:send-chat', {
    target: 'cockpit', projectId, prompt,
    images: liveVisionAlertFrame ? [liveVisionAlertFrame] : [],
    uiContext: 'app_interface_inspect',
    onStatus: (status, detail) => {
      if (projectId !== getProjectId() || !liveVisionAlert.value) return
      liveVisionAlertStatus.value = status === 'processing' ? 'AI 正在核实画面…'
        : status === 'awaiting_review' ? 'AI 已回复，请在右侧对话中查看。'
          : detail || '暂未送达，请稍后重试。'
    },
  })
}
/**
 * 忽略一条实时提醒：清掉横幅，并**记住这一处**（忽略期内不再弹同一处）。
 *
 * 后端相似度去重只有 45 秒窗口，过了窗口同一处会再弹一次 —— 用户刚点完「忽略」又看到
 * 同一条，会以为按钮没生效。窗口 10 分钟（见 isDismissedVisualAlert 的说明）。
 */
function dismissRealtimeAlert() {
  voiceVerification.reset()
  const alert = liveRealtimeAlert.value
  if (alert) dismissedRealtimeAlerts = pruneDismissedVisualAlerts([...dismissedRealtimeAlerts,
    { text: alert.text, at: Date.now() }], Date.now())
  liveRealtimeAlert.value = null
}
function reviewRealtimeAlert(alreadyApproved = false) {
  const alert = liveRealtimeAlert.value
  if (!alert) return
  if (!alreadyApproved && !voiceVerification.approve(alert.id, Date.now())) {
    // Clicking is explicit consent, including when the spoken offer has expired.
    voiceVerification.offer(alert.id, alert.text, Date.now())
    if (!voiceVerification.approve(alert.id, Date.now())) return
  }
  if (!voiceVerification.matches(alert.id)) return
  const projectId = getProjectId()
  const stream = liveStreamSocket
  const generation = liveVisionGeneration
  // 附**报警当刻**的快照；只有拿不到快照时才退回当前帧（并如实说明是"当前画面"）。
  const frame = alert.frame || liveVisionLatestFrame
  const frameNote = alert.frame ? '附件是报出这条提醒时的画面' : '附件是当前画面（报警时的画面已不可用）'
  appEvents.emit('docmind:send-chat', {
    target: 'cockpit', projectId,
    prompt: `我同意核查这条线索。实时模型提醒：「${alert.text}」。${frameNote}。请只读核查当前项目文件、现有运行日志和画面，说明结论、证据和不确定之处；不要修改文件、执行修复或启动工作流。`,
    images: frame ? [frame] : [],
    // 核查专用上下文在服务端绑定只读工具，不能扩大为修改或工作流执行。
    uiContext: 'verify_realtime_alert',
    onResult: text => {
      if (projectId !== getProjectId() || generation !== liveVisionGeneration || !voiceVerification.complete(alert.id)) return
      if (stream && stream === liveStreamSocket && stream.readyState === WebSocket.OPEN) {
        try {
          stream.send(JSON.stringify({ v: REALTIME_PROTOCOL_VERSION, type: 'agent.result', request_id: alert.id,
            text: text.length > 3900 ? text.slice(0, 3900) + '\n（报告已截短；完整证据和限制请查看右侧对话，不要根据节选推断未展示内容。）' : text }))
          liveVisionAlertStatus.value = '核查报告已提交语音端，等待播报。'
        } catch { liveVisionAlertStatus.value = '核查已结束，语音连接不可用，请查看右侧完整报告。' }
      } else liveVisionAlertStatus.value = '核查已结束，请查看右侧报告；当前语音连接已变更。'
    },
    onStatus: (status, detail) => {
      if (projectId !== getProjectId() || generation !== liveVisionGeneration || liveRealtimeAlert.value?.id !== alert.id) return
      if (status === 'processing') liveVisionAlertStatus.value = '已获得同意，后台正在只读核查这条线索…'
      else if (status === 'pending' || status === 'failed') {
        voiceVerification.retry(alert.id, Date.now())
        liveVisionAlertStatus.value = detail || '核查未完成，请稍后重新确认。'
      } else if (!liveVisionAlertStatus.value.includes('报告')) liveVisionAlertStatus.value = '核查已结束，请查看右侧报告。'
    },
  })
}
function beginFocusSelection() {
  if (!liveVisionSnapshotUrl.value) {
    liveVisionStatus.value = '等待取得当前画面后再圈选。'
    return
  }
  liveVisionSelecting.value = true
  liveVisionDraftFocus.value = null
  liveVisionStatus.value = '在下方快照上拖拽圈选重点区域。'
}
function clearFocusSelection() {
  liveVisionSelecting.value = false
  liveVisionDraftFocus.value = null
  liveVisionFocus.value = null
  liveVisionDrag = null
  resetFocusObservation()
  liveVisionStatus.value = '已恢复整屏观察。'
}
function focusPointerDown(event: PointerEvent) {
  if (!liveVisionSelecting.value) return
  const element = event.currentTarget as HTMLElement
  liveVisionDrag = { bounds: element.getBoundingClientRect(), start: { x: event.clientX, y: event.clientY } }
  liveVisionDraftFocus.value = null
  element.setPointerCapture(event.pointerId)
}
function focusPointerMove(event: PointerEvent) {
  if (!liveVisionDrag) return
  liveVisionDraftFocus.value = normalizeFocusRegion(liveVisionDrag.bounds, liveVisionDrag.start,
    { x: event.clientX, y: event.clientY })
}
function focusPointerUp(event: PointerEvent) {
  if (!liveVisionDrag) return
  const selected = normalizeFocusRegion(liveVisionDrag.bounds, liveVisionDrag.start,
    { x: event.clientX, y: event.clientY })
  liveVisionDrag = null
  liveVisionDraftFocus.value = null
  if (!selected) {
    liveVisionStatus.value = '圈选范围太小，请拖拽一个更大的区域。'
    return
  }
  liveVisionFocus.value = selected
  liveVisionSelecting.value = false
  resetFocusObservation()
  liveVisionStatus.value = '已聚焦重点区域；AI 将分析该区域的新画面。'
}
function focusPointerCancel() {
  liveVisionDrag = null
  liveVisionDraftFocus.value = null
}
function focusRegionStyle(region: FocusRegion | null) {
  return region ? {
    left: `${region.x * 100}%`, top: `${region.y * 100}%`,
    width: `${region.width * 100}%`, height: `${region.height * 100}%`,
  } : {}
}
function clickTargetStyle() {
  const box = clickProposal.value?.bbox
  return box?.length === 4 ? {
    left: `${box[0] * 100}%`, top: `${box[1] * 100}%`,
    width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%`,
  } : {}
}
async function requestClickTarget(description: string, goal: string, keepResult = false) {
  if (!description || clickBusy.value) return
  clickBusy.value = true
  clickError.value = ''
  clickStatus.value = ''
  clickProposal.value = null
  if (!keepResult) {
    clickBeforeImage.value = ''
    clickAfterImage.value = ''
    clickVerification.value = null
  }
  const projectId = getProjectId()
  const ticket = ++clickRequestGeneration
  try {
    const result = await visionApi.locateClick(description, goal)
    if (ticket !== clickRequestGeneration || projectId !== getProjectId()) return
    if (!result.ok || !result.proposal_id || !result.image || !result.bbox || !result.point) {
      throw new Error(result.error || '没有找到可靠的点击位置')
    }
    clickProposal.value = {
      proposal_id: result.proposal_id, image: result.image, bbox: result.bbox,
      point: result.point, label: result.label || description,
      evidence: result.evidence || '', expires_in: result.expires_in || 45,
      goal, description,
    }
    clickDescription.value = description
    clickGoal.value = goal
    clickStatus.value = '请核对截图中的绿色目标框；确认后只执行一次点击。'
    if (actionLoop.value?.phase === 'locating' && actionLoop.value.projectId === projectId && actionLoop.value.workflowId === selectedId.value) {
      setActionLoop({ ...actionLoop.value, phase: 'awaiting_confirmation', lastTarget: description,
        note: `已定位「${result.label || description}」，等待你确认这一步。` })
    }
  } catch (cause) {
    if (ticket === clickRequestGeneration && projectId === getProjectId()) {
      clickError.value = (cause as Error).message || '视觉定位失败'
      if (actionLoop.value?.phase === 'locating') setActionLoop({ ...actionLoop.value, phase: 'needs_review', note: '定位失败，请修改控件描述后重试。' })
    }
  } finally { if (ticket === clickRequestGeneration) clickBusy.value = false }
}
function locateClickTarget() {
  if (actionLoop.value && ['needs_review', 'awaiting_user'].includes(actionLoop.value.phase)) {
    continueActionLoop()
    return
  }
  void requestClickTarget(clickDescription.value.trim(), clickGoal.value.trim())
}
async function executeLocatedClick() {
  const proposal = clickProposal.value
  if (!proposal || clickBusy.value) return
  clickBusy.value = true
  clickExecuting.value = true
  clickError.value = ''
  clickStatus.value = '正在核对窗口并执行点击…'
  const projectId = getProjectId()
  const ticket = ++clickRequestGeneration
  let nextTarget = ''
  try {
    const result = await visionApi.executeClick(proposal.proposal_id)
    if (ticket !== clickRequestGeneration || projectId !== getProjectId()) return
    clickProposal.value = null
    if (!result.ok || !result.executed) throw new Error(result.error || '点击未执行')
    clickBeforeImage.value = result.before || proposal.image
    clickAfterImage.value = result.after || ''
    clickVerification.value = result.verification || { status: 'unavailable', evidence: '未获得视觉复验结果。' }
    const review = clickVerification.value
    const loop = actionLoop.value
    if (loop?.phase === 'awaiting_confirmation' && loop.projectId === projectId && loop.workflowId === selectedId.value) {
      const step: VisualActionStep = {
        at: new Date().toISOString(), target: proposal.label, status: review.status,
        evidence: review.evidence, nextTarget: review.next_target || '',
      }
      const advanced = advanceVisualActionLoop(loop, step)
      setActionLoop(advanced)
      if (advanced.phase === 'locating') nextTarget = advanced.lastTarget
      else if (advanced.phase === 'needs_review' || advanced.phase === 'awaiting_user') clickDescription.value = ''
    }
    clickStatus.value = loop?.phase === 'awaiting_confirmation' && actionLoop.value?.id === loop.id
      ? actionLoop.value.note
      : review.status === 'met' ? 'AI 认为画面已达到目标，请最终验收。'
        : review.status === 'unmet' ? 'AI 认为目标未达成，正在准备下一步建议。'
          : '点击已执行，但画面不足以确认目标；请人工检查。'
    if ((!loop || loop.phase === 'completed' || loop.phase === 'stopped') && review.status === 'unmet' && review.next_target?.trim()) nextTarget = review.next_target.trim()
    if (review.status === 'met' || review.status === 'unmet' || review.status === 'uncertain') {
      void submitClickReview(proposal, result.before || proposal.image, result.after || '', review,
        !!result.before)
    }
  } catch (cause) {
    if (ticket === clickRequestGeneration && projectId === getProjectId()) {
      clickError.value = (cause as Error).message || '点击未能完成'
      clickProposal.value = null
      if (actionLoop.value?.phase === 'awaiting_confirmation') setActionLoop({ ...actionLoop.value,
        phase: 'needs_review', note: '本步未能确认执行，请重新定位后再试。' })
    }
  } finally { if (ticket === clickRequestGeneration) { clickBusy.value = false; clickExecuting.value = false } }
  if (nextTarget && ticket === clickRequestGeneration && projectId === getProjectId()) {
    await requestClickTarget(nextTarget, proposal.goal, true)
  }
}
async function submitClickReview(
  proposal: NonNullable<typeof clickProposal.value>, beforeImage: string, afterImage: string,
  review: NonNullable<typeof clickVerification.value>,
  beforeIsFresh: boolean,
) {
  const wid = workflow.value?.workflow_id
  const projectId = getProjectId()
  const ticket = generation
  const clickTicket = clickRequestGeneration
  if (!wid) {
    if (clickTicket === clickRequestGeneration) clickFeedbackStatus.value = '当前没有自主开发任务；复验结果保留在此处，可新建任务后告诉 AI 继续处理。'
    return
  }
  if (review.status === 'met') clickReviewSaving.value = true
  clickFeedbackStatus.value = '正在把视觉复验结果加入当前开发任务…'
  const sentAt = new Date().toISOString()
  const loop = actionLoop.value
  const loopNote = loop?.projectId === projectId && loop.workflowId === wid
    ? `连续视觉任务 ${loop.id} · 第 ${loop.steps.length} 步。` : ''
  const note = `${loopNote}已确认点击「${proposal.label}」。期望：${proposal.goal || '出现可见响应'}。` +
    `视觉复验：${review.status === 'met' ? '可能达成，等待用户确认' : review.status === 'unmet' ? '尚未达成' : '无法确认'}；可见证据：${review.evidence}。` +
    (review.next_target ? `模型建议下一控件：${review.next_target}（尚未获点击许可）。` : '')
  const record: FeedbackRecord = {
    id: `desktop-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    artifact_id: 'desktop_visual_review', label: '桌面视觉复验', note,
    screenshot: !!afterImage, sent_at: sentAt, sentAt, status: 'pending', snapshots: {},
  }
  lastClickFeedbackId.value = record.id
  try {
    const saved = await agentApi.workflowVisualFeedback(wid, {
      id: record.id, label: record.label, note: record.note, artifact_id: record.artifact_id,
      screenshot: record.screenshot, sent_at: sentAt,
    }, projectId)
    if (!saved.ok || !saved.feedback) throw new Error(saved.error || '视觉反馈保存失败')
    Object.assign(record, mapFeedback(saved.feedback))
    if (afterImage) {
      const snapshot = await agentApi.workflowFeedbackSnapshot(wid, record.id, 'before', afterImage, projectId)
      if (!snapshot.ok || !snapshot.feedback) throw new Error(snapshot.error || '点击后画面保存失败')
      Object.assign(record, mapFeedback(snapshot.feedback))
    }
    if (ticket !== generation || projectId !== getProjectId() || workflow.value?.workflow_id !== wid) return
    upsertFeedback(record)
    if (review.status === 'met') {
      const updated = await agentApi.workflowFeedbackStatus(wid, record.id, 'awaiting_review', '视觉模型认为画面可能达成，等待用户验收', projectId)
      if (!updated.ok || !updated.feedback) throw new Error(updated.error || '视觉验收状态保存失败')
      if (ticket !== generation || projectId !== getProjectId() || workflow.value?.workflow_id !== wid) return
      upsertFeedback(mapFeedback(updated.feedback))
      if (clickTicket === clickRequestGeneration) clickFeedbackStatus.value = '该步视觉证据已保存到当前任务，等待你验收。'
      return
    }
    const imageOrder: string[] = []
    const images: Blob[] = []
    if (beforeImage) { images.push(await (await fetch(beforeImage)).blob()); imageOrder.push(beforeIsFresh ? '点击前最后一帧' : '定位时截图（可能早于点击）') }
    if (afterImage) { images.push(await (await fetch(afterImage)).blob()); imageOrder.push('点击后') }
    if (ticket !== generation || projectId !== getProjectId() || workflow.value?.workflow_id !== wid) return
    const prompt = [
      `当前项目工作流：${wid}。这是工作台在用户确认桌面点击后自动记录的复验资料。`,
      `用户先前填写的期望：${proposal.goal || '点击后出现可见响应'}。已点击控件：${proposal.label}。`,
      `视觉模型的未核实判断：${review.status}；其画面证据：${review.evidence}。`,
      `附件图片顺序：${imageOrder.join('、') || '没有可用截图'}。`,
      '先核对当前项目文件、运行状态和画面，再决定下一步；若需修改文件或继续操作软件，提出具体方案与验收条件并等待现有审批。点击不会自动撤销，也不要重复执行同一操作。',
    ].join('\n')
    if (workflow.value?.status === 'executing') {
      if (pendingDesktopDispatches.length < 5) {
        pendingDesktopDispatches.push({ record, prompt, images, wid, projectId, ticket })
        if (clickTicket === clickRequestGeneration) clickFeedbackStatus.value = '已加入当前开发任务；正在执行的工作流结束后，AI 将核对这次复验。'
      } else if (clickTicket === clickRequestGeneration) {
        clickFeedbackStatus.value = '运行中的任务已积累较多视觉反馈；记录已保存，可在任务结束后手动重试。'
      }
    } else {
      dispatchFeedback(record, prompt, images, wid, projectId, 'desktop_visual_review', true)
      if (clickTicket === clickRequestGeneration) clickFeedbackStatus.value = '已加入当前开发任务，AI 将核对证据并提出下一步。'
    }
  } catch (cause) {
    record.detail = (cause as Error).message || '复验反馈未能发送'
    if (ticket === generation && projectId === getProjectId() && workflow.value?.workflow_id === wid) {
      upsertFeedback(record)
      if (clickTicket === clickRequestGeneration) clickFeedbackStatus.value = `复验反馈已保留，发送未完成：${record.detail}。可在视觉反馈记录中重试。`
    }
  } finally {
    if (review.status === 'met') clickReviewSaving.value = false
  }
}
function stopLiveVision(message = '') {
  voiceVerification.reset()
  if (!message) {
    clickRequestGeneration++
    clickBusy.value = false
    clickExecuting.value = false
    clickProposal.value = null
    clickBeforeImage.value = ''
    clickAfterImage.value = ''
    clickVerification.value = null
    clickFeedbackStatus.value = ''
    lastClickFeedbackId.value = ''
    clickError.value = ''
    clickStatus.value = ''
  }
  liveVisionGeneration++
  liveStreamSendRevision++
  liveFrameEncoder?.dispose()
  liveFrameEncoder = null
  liveSampleCanvas = null
  resetLiveStreamDedup()
  liveStreamStaticSkipped.value = 0
  liveCaptureCanvas = null
  liveFocusCanvas = null
  if (liveStreamTimer) { clearTimeout(liveStreamTimer); liveStreamTimer = null }
  if (liveStreamReconnectTimer) { clearTimeout(liveStreamReconnectTimer); liveStreamReconnectTimer = null }
  if (liveStreamHeartbeatTimer) { clearInterval(liveStreamHeartbeatTimer); liveStreamHeartbeatTimer = null }
  if (liveStreamSocket) {
    if (liveStreamSocket.readyState === WebSocket.OPEN) {
      try { liveStreamSocket.send(JSON.stringify({ v: 1, type: 'session.close', reason: message || 'stopped' })) } catch { /* closing */ }
    }
    liveStreamSocket.close(); liveStreamSocket = null
  }
  liveStreamConnected.value = false
  liveStreamReconnectScheduled.value = false
  liveStreamRecovering.value = false
  liveNativeAudioReady = false
  liveStreamReadyForVideo = false
  liveLedger.clear()
  liveAdaptive.reset()
  liveStreamLastObservationAt = 0
  liveStreamCaptions.value = []
  liveTranscriptDraft.value = ''
  liveStreamCapabilities.value = ''
  liveVisionMetrics.value = { latencyMs: null, intervalMs: 500, dropped: 0 }
  liveVisionPhase.value = 'idle'
  void stopLiveMic('')
  liveStreamFrames.clear()
  liveStreamSequence = 0
  liveVisionRequest?.abort()
  liveVisionRequest = null
  liveVisionActive.value = false
  liveVisionBusy.value = false
  liveVisionPaused.value = false
  liveVisionLastSample = null
  liveVisionLastAnalyzedAt = 0
  liveVisionQuietSamples = 0
  liveVisionObservation.value = ''
  liveVisionTimeline.value = []
  pendingVisionAlert = null
  if (!message) {
    dismissVisionAlert()
    liveRealtimeAlert.value = null
  }
  acknowledgedVisionAlerts.clear()
  liveVisionFocus.value = null
  liveVisionDraftFocus.value = null
  liveVisionSelecting.value = false
  liveVisionDrag = null
  liveVisionLatestFrame = null
  liveVisionLatestFocusFrame = null
  liveVisionLatestCapturedAt = 0
  if (liveVisionSnapshotUrl.value) URL.revokeObjectURL(liveVisionSnapshotUrl.value)
  liveVisionSnapshotUrl.value = ''
  clearLiveVisionTimer()
  if (screenStream) {
    screenStream.getTracks().forEach(track => track.stop())
    screenStream = null
  }
  screenSharing.value = false
  cameraSharing.value = false
  appEvents.emit('docmind:live-vision-frame', { projectId: getProjectId(), image: null })
  if (message) liveVisionStatus.value = message
}
function firstInteractiveArtifact() {
  return previewArtifacts.value.find(item => item.kind === 'interactive' || item.renderer === 'interactive')
}
async function desktopVisionBlob(): Promise<Blob> {
  const result = await visionApi.captureDesktopFrame('embedded')
  if (!result.ok || !result.image) throw new Error(result.error || '当前没有可捕获的桌面窗口')
  const response = await fetch(result.image)
  if (!response.ok) throw new Error('桌面视觉帧读取失败')
  return response.blob()
}
async function sharedScreenFrame(focus: FocusRegion | null = null, deduplicate = false): Promise<EncodedLiveFrame> {
  const video = sharedVideo.value
  if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
    throw new Error('共享画面尚未就绪，请稍后重试')
  }
  const plan = liveAdaptive.plan()
  const latest = deduplicate ? liveVisualWindow.latest() : null
  const options: FrameEncodingOptions = { ...plan, focus,
    previous: latest?.sample || liveStreamLastSentSample,
    lastSentAt: Math.max(latest?.capturedAt || 0, liveStreamLastSentAt), capturedAt: Date.now(),
    refreshMs: liveVisionMode.value === 'native-realtime' ? 5000 : 10_000,
    force: !deduplicate || liveForceStreamFrame }
  if (deduplicate) liveForceStreamFrame = false
  if (typeof Worker === 'function' && typeof OffscreenCanvas === 'function' && typeof createImageBitmap === 'function') {
    const encoder = liveFrameEncoder ??= new LiveFrameEncoder()
    if (encoder.available()) {
      try {
        return await encoder.encode(await createImageBitmap(video), options)
      } catch {
        encoder.dispose()
        if (liveFrameEncoder !== encoder || !liveVisionActive.value) throw new Error('视频会话已停止')
        // Worker/CSP/驱动不支持时回退浏览器画布，不让后台编码故障中断通话。
        options.capturedAt = Date.now()
      }
    }
  }
  return encodeLiveFrame(video, video.videoWidth, video.videoHeight, options, {
    frame: liveCaptureCanvas ??= document.createElement('canvas'),
    focus: liveFocusCanvas ??= document.createElement('canvas'),
    sample: liveSampleCanvas ??= document.createElement('canvas'),
  })
}
async function sharedScreenBlob(): Promise<Blob> {
  const result = await sharedScreenFrame()
  if (!result.image) throw new Error('共享画面截图失败')
  return result.image
}
function scheduleLiveStreamFrame(ticket: number, delay = liveVisualWindow.captureInterval(
  Date.now(), liveAdaptive.plan().intervalMs, liveVideoImageBudget.value)) {
  if (liveStreamTimer) clearTimeout(liveStreamTimer)
  if (!liveVisionActive.value || liveVisionPaused.value || ticket !== liveVisionGeneration) return
  liveStreamTimer = setTimeout(() => { liveStreamTimer = null; void sendLiveStreamFrame(ticket) }, delay)
}
async function sendLiveStreamFrame(ticket: number) {
  if (!liveVisionActive.value || liveVisionPaused.value || ticket !== liveVisionGeneration) return
  if (liveStreamCapture) return // 同一时刻最多一次异步采集/编码。
  const socket = liveStreamSocket
  if (!socket || socket.readyState !== WebSocket.OPEN) return
  if (!liveStreamReadyForVideo) {
    if (liveVisionMode.value === 'native-realtime') liveStreamReadyForVideo = sendNativeAudioReady()
    if (!liveStreamReadyForVideo) { scheduleLiveStreamFrame(ticket); return }
  }
  if (document.hidden) { scheduleLiveStreamFrame(ticket, 1000); return }
  const projectId = getProjectId(), focusRevision = liveVisionFocusRevision
  const sendRevision = liveStreamSendRevision
  const capture = {}
  liveStreamCapture = capture
  const startedAt = performance.now()
  const isCurrent = () => ticket === liveVisionGeneration && sendRevision === liveStreamSendRevision
    && focusRevision === liveVisionFocusRevision && projectId === getProjectId()
    && !liveVisionPaused.value && liveVisionActive.value
    && socket === liveStreamSocket && socket.readyState === WebSocket.OPEN
  try {
    // 在编码前就检查背压。网络已经跟不上时，截图/压缩这一轮没有价值，
    // 继续做只会占用主线程并让下一轮更晚到达。
    const videoBufferLimit = liveMicActive.value ? 128_000 : undefined
    if (!canQueueLiveVideo(socket.bufferedAmount, 0, videoBufferLimit)) {
      liveVisionMetrics.value = { ...liveVisionMetrics.value, dropped: liveVisionMetrics.value.dropped + 1 }
      liveAdaptive.feed({ bufferedAmount: socket.bufferedAmount })
      return
    }
    liveLedger.prune(Date.now())
    pruneLiveStreamFrames()
    const captured = await sharedScreenFrame(liveVisionFocus.value, true)
    if (!isCurrent()) return
    if (captured.image) liveVisualWindow.push({ ...captured, image: captured.image })
    else liveStreamStaticSkipped.value++
    const now = Date.now()
    liveVideoImagesUsed.value = liveVisualWindow.usage(now)
    if (!liveVisualWindow.due(now, liveAdaptive.plan().intervalMs,
      liveVisionMode.value === 'native-realtime', liveVideoImageBudget.value)) return
    const selection = liveVisualWindow.select(now)
    if (!selection) return
    const { image, focusImage, capturedAt, sample } = selection.current
    let uploadImage = image
    const hasContext = typeof createImageBitmap === 'function' && !!(selection.before || selection.key || focusImage)
    if (hasContext) {
      const options: FrameEncodingOptions = { ...liveAdaptive.plan(), focus: null, previous: null,
        lastSentAt: 0, capturedAt, refreshMs: 5000, force: true,
        context: {
          ...(selection.before ? { before: { image: selection.before.focusImage || selection.before.image,
            capturedAt: selection.before.capturedAt } } : {}),
          ...(selection.key ? { key: { image: selection.key.focusImage || selection.key.image,
            capturedAt: selection.key.capturedAt } } : {}),
          detail: focusImage, reason: selection.reason,
        } }
      const bitmap = await createImageBitmap(image)
      const encoder = liveFrameEncoder
      if (encoder?.available()) {
        const composite = await encoder.encode(bitmap, options)
        uploadImage = composite.image || image
      } else {
        try {
          const composite = await encodeLiveFrame(bitmap, bitmap.width, bitmap.height, options, {
            frame: liveCaptureCanvas ??= document.createElement('canvas'),
            focus: liveFocusCanvas ??= document.createElement('canvas'),
            sample: liveSampleCanvas ??= document.createElement('canvas'),
          })
          uploadImage = composite.image || image
        } finally { bitmap.close() }
      }
    }
    if (!isCurrent() || Date.now() - capturedAt > 2500) return
    if (!liveVisualWindow.canUpload(Date.now(), uploadImage.size, liveVideoImageBudget.value)) return
    const focused = !!liveVisionFocus.value && !hasContext
    liveVisionLatestFrame = image
    liveVisionLatestFocusFrame = focusImage
    liveVisionLatestCapturedAt = capturedAt
    setLiveVisionSnapshot(image)
    appEvents.emit('docmind:live-vision-frame', {
      projectId, image, capturedAt, focusImage, observation: liveVisionObservation.value,
      timeline: liveVisionTimeline.value,
    })
    const sequence = ++liveStreamSequence
    let queuedBeforeSend = socket.bufferedAmount
    const outcome = await sendLiveVideoPacket({
      bufferLimit: videoBufferLimit,
      encode: () => encodeVideoFrame(sequence, capturedAt, uploadImage, focused),
      isCurrent, bufferedAmount: () => socket.bufferedAmount,
      send: packet => { queuedBeforeSend = socket.bufferedAmount; socket.send(packet) },
    })
    if (outcome === 'stale') return
    if (outcome === 'sent') {
      liveVisualWindow.uploaded(selection.current, Date.now(), uploadImage.size)
      liveVideoImagesUsed.value = liveVisualWindow.usage(Date.now())
      liveStreamLastSentSample = sample
      liveStreamLastSentAt = capturedAt
      liveStreamFrames.set(sequence, { image, focusImage, capturedAt })
      pruneLiveStreamFrames()
      liveLedger.onSent(sequence, capturedAt, Date.now())
      const encodeMs = performance.now() - startedAt
      // 只看发送前滞留的队列，新包刚加入缓冲不能被当成网络积压。
      liveAdaptive.feed({ bufferedAmount: queuedBeforeSend,
        encodeOverrun: liveVideoOverrun(liveVisionMode.value, encodeMs,
          liveLedger.oldestPendingAge(Date.now()), liveAdaptive.plan().intervalMs),
        transportHealthy: liveVisionMode.value === 'native-realtime' && encodeMs <= liveAdaptive.plan().intervalMs })
    } else {
      // 发不出去的新帧直接丢弃而不补发旧帧：模型永远只该看到最新画面。
      liveVisionMetrics.value = { ...liveVisionMetrics.value, dropped: liveVisionMetrics.value.dropped + 1 }
      liveAdaptive.feed({ bufferedAmount: socket.bufferedAmount, encodeOverrun: true })
    }
  } catch (cause) {
    if (isCurrent()) liveVisionStatus.value = (cause as Error).message || '实时画面采集失败'
  } finally {
    if (liveStreamCapture === capture) liveStreamCapture = null
    if (liveVisionActive.value && !liveVisionPaused.value && liveStreamReadyForVideo) {
      refreshLiveVisionMetrics()
      scheduleLiveStreamFrame(liveVisionGeneration)
    }
  }
}
function receiveLiveStreamObservation(raw: string, ticket: number, projectId: string) {
  if (ticket !== liveVisionGeneration || projectId !== getProjectId()) return
  const event = parseRealtimeServerEvent(raw)
  if (!event) return
  // 暂停只丢媒体结果，握手和连接错误仍须处理；否则暂停期间完成握手，恢复后永远无法发帧。
  if (liveVisionPaused.value && !['hello.ok', 'heartbeat', 'error', 'session.closed'].includes(event.type)) return
  if (event.type === 'hello.ok') {
    const recovered = liveStreamRecovering.value
    liveStreamRecovering.value = false
    liveStreamReconnectScheduled.value = false
    const mode = event.mode === 'native-realtime' || event.mode === 'sampled-frames'
      ? event.mode : 'sampled-frames'
    liveVisionMode.value = mode
    liveAdaptive.setMinimumInterval(mode === 'sampled-frames'
      && typeof event.video_min_interval_ms === 'number' ? event.video_min_interval_ms : 0)
    liveVisionModeLabel.value = mode === 'native-realtime' ? '原生实时' : '兼容抽帧'
    liveVisionModeReason.value = typeof event.reason === 'string' && event.reason
      ? event.reason : mode === 'native-realtime'
        ? '当前会话使用原生实时模型。' : '当前会话按最新视频帧理解。'
    liveStreamCapabilities.value = describeLiveCapabilities(event.provider_capabilities)
    liveVisionLimitations.value = mode === 'native-realtime'
      ? ['模型连接失败时会自动降级为兼容抽帧模式。', '当前 Omni 语音按 wire 的 pcm24（16-bit / 24kHz）播放；未知音频格式会跳过并提示。']
      : ['当前只处理最新视频帧，不提供原生音频流回复。', '理解结果可能晚于正在播放的画面。']
    if (mode === 'sampled-frames' && typeof event.degraded_to === 'string' && event.degraded_to
      && event.degraded_to !== 'sampled-frames') {
      liveVisionLimitations.value = [...liveVisionLimitations.value, `原生通道未起：已回退到 ${event.degraded_to}。`]
    }
    if (mode === 'native-realtime') {
      liveStreamReadyForVideo = sendNativeAudioReady()
      if (!liveStreamReadyForVideo) {
        liveVisionStatus.value = '实时模型需要先建立音频通道，正在等待音频就绪。'
      }
    } else {
      liveStreamReadyForVideo = true
    }
    if (recovered) liveVisionStatus.value = '实时视频连接已恢复，继续实时对话。'
    scheduleLiveStreamFrame(ticket, 0)
    refreshLiveVisionPhase()
    return
  }
  if (event.type === 'model.audio') {
    resolveLiveStreamRecovery()
    liveStreamLastObservationAt = Date.now()
    refreshLiveVisionPhase()
    void playLiveModelAudio(event as typeof event & { audio?: unknown; encoding?: unknown })
    return
  }
  if (event.type === 'model.delta' || event.type === 'audio.transcript') {
    resolveLiveStreamRecovery()
    appendLiveCaption(event)
    liveStreamLastObservationAt = Date.now()
    refreshLiveVisionPhase()
    return
  }
  if (event.type === 'model.observation') {
    if (event.source === 'realtime-model' && event.verified === false && typeof event.text === 'string') {
      const text = event.text.slice(0, 300)
      // 用户刚忽略过这一处就别再弹了（后端 45s 去重窗过后同一处还会再来）。
      if (isDismissedVisualAlert(text, dismissedRealtimeAlerts)) return
      const id = `voice-check-${Date.now()}-${event.sequence || 0}`
      if (!voiceVerification.offer(id, text, Date.now())) return
      liveRealtimeAlert.value = {
        id,
        text,
        atMs: Date.now(),
        detectedAt: new Date(typeof event.captured_at === 'number' ? event.captured_at : event.sent_at)
          .toLocaleTimeString('zh-CN', { hour12: false }),
        // 报警当刻的画面：此刻模型正看着它，快照下来供后续核实（见类型声明处的说明）。
        frame: liveVisionLatestFrame,
      }
      liveVisionAlertStatus.value = '这是待核实线索。可以核查一下吗？说“可以核查一下”或点击核查按钮后才会开始。'
    }
    return
  }
  if (event.type === 'agent.result.ok') {
    if (event.request_id === liveRealtimeAlert.value?.id) liveVisionAlertStatus.value = '核查报告已交给语音前端，正在生成播报。'
    return
  }
  if (event.type === 'heartbeat' || event.type === 'cancel.ok') return
  if (event.type === 'error') {
    const message = typeof event.message === 'string' ? event.message : '实时视觉连接发生错误。'
    if (event.code === 'agent_result_failed') {
      liveVisionAlertStatus.value = '核查报告未能播报，请查看右侧完整报告。'
      return
    }
    if (event.code === 'audio_not_ready') return
    if (event.code === 'audio_backpressure') {
      stopAiPlayback()
      closeOpenLiveCaption()
      liveMicStatus.value = message
      liveVisionStatus.value = message
      return
    }
    if (event.code === 'stream_broken' && event.retryable === true) {
      liveStreamRecovering.value = true
      liveStreamReconnectScheduled.value = true
      liveVisionStatus.value = '实时模型连接中断，正在恢复并重放未完成语音…'
      refreshLiveVisionPhase()
      return
    }
    liveVisionStatus.value = message
    return
  }
  if (event.type === 'session.closed') return
  if (event.type !== 'video.observation') return
  const result = event as typeof event & {
    ok?: boolean; error?: string; observations?: string[]; anomalies?: VisionAnomaly[]; audit?: { mode?: string }
  }
  if (result.throttled === true) {
    resetLiveStreamDedup()
    liveLedger.onObserved(result.sequence || 0, Date.now())
    for (const key of liveStreamFrames.keys()) {
      if (key <= (result.sequence || 0)) liveStreamFrames.delete(key)
    }
    liveVisionMetrics.value = { ...liveVisionMetrics.value, dropped: liveVisionMetrics.value.dropped + 1 }
    liveAdaptive.feed({ bufferedAmount: liveStreamSocket?.bufferedAmount || 0, throttled: true,
      retryAfterS: typeof result.retry_after === 'number' ? result.retry_after : null })
    refreshLiveVisionMetrics()
    scheduleLiveStreamFrame(ticket)
    return // 限流没有新观察，不能刷新字幕、告警确认或“AI 最近理解”时间。
  }
  if (!result.ok || result.audit?.mode === 'unavailable' || result.audit?.mode === 'error') {
    liveVisionStatus.value = result.error || '视觉模型暂不可用；实时画面仍在播放，暂不提供画面理解。'
    liveStreamSocket?.close()
    liveStreamFrames.clear()
    return
  }
  const frame = liveStreamFrames.get(result.sequence || 0)
  const capturedAt = frame?.capturedAt || result.captured_at || 0
  for (const key of liveStreamFrames.keys()) {
    if (key <= (result.sequence || 0)) liveStreamFrames.delete(key)
  }
  const observation = (result.observations || []).join('\n').trim()
  if (observation) {
    liveVisionObservation.value = observation.slice(-5000)
    liveVisionTimeline.value = [...liveVisionTimeline.value, {
      at: new Date(capturedAt || Date.now()).toLocaleTimeString('zh-CN', { hour12: false }),
      observation: observation.slice(-1200), capturedAt, observedAt: Date.now(),
    }].slice(-5)
  }
  if (frame) {
    const alertProgress = advanceVisionAlert(pendingVisionAlert, result.anomalies || [])
    pendingVisionAlert = alertProgress.confirmed ? null : alertProgress.pending
    if (alertProgress.confirmed && !acknowledgedVisionAlerts.has(alertProgress.key)) {
      acknowledgedVisionAlerts.add(alertProgress.key)
      dismissVisionAlert()
      liveVisionAlert.value = { ...alertProgress.confirmed, atMs: Date.now(),
        detectedAt: new Date(capturedAt).toLocaleTimeString('zh-CN', { hour12: false }) }
      liveVisionAlertFrame = frame.image
      liveVisionAlertImageUrl.value = URL.createObjectURL(frame.image)
    }
  }
  if (liveVisionLatestFrame) appEvents.emit('docmind:live-vision-frame', {
    projectId, image: liveVisionLatestFrame, capturedAt: liveVisionLatestCapturedAt,
    focusImage: liveVisionLatestFocusFrame, observation: liveVisionObservation.value,
    timeline: liveVisionTimeline.value,
  })
  liveVisionLastAnalyzedAt = Date.now()
  liveStreamLastObservationAt = Date.now()
  const latency = liveLedger.onObserved(result.sequence || 0, Date.now())
  const plan = liveAdaptive.feed({
    bufferedAmount: liveStreamSocket?.readyState === WebSocket.OPEN ? liveStreamSocket.bufferedAmount : 0,
    // 原生模式的低频旁路模型延迟不代表主实时通道的吞吐。
    latencyMs: liveVisionMode.value === 'sampled-frames' && latency ? latency.e2eMs : null,
    throttled: result.throttled === true,
    retryAfterS: typeof result.retry_after === 'number' ? result.retry_after : null,
  })
  liveVisionMetrics.value = {
    latencyMs: latency ? latency.e2eMs : liveVisionMetrics.value.latencyMs,
    intervalMs: plan.intervalMs,
    dropped: liveVisionMetrics.value.dropped,
  }
  refreshLiveVisionPhase()
  liveVisionStatus.value = `实时画面传送中 · ${liveVisionSource.value} · AI 最近理解 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`
}
function startLiveStream(ticket: number, retry = 0) {
  const projectId = getProjectId()
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(`${scheme}//${location.host}/api/vision/live-stream?project_id=${encodeURIComponent(projectId)}`)
  let connectedAt = 0
  const reconnecting = retry > 0 || liveStreamRecovering.value
  liveStreamSocket = socket
  liveStreamSendRevision++
  resetLiveStreamDedup()
  liveStreamSequence = 0
  liveAudioSequence = 0
  liveNativeAudioReady = false
  liveStreamReadyForVideo = false
  liveStreamFrames.clear()
  socket.onopen = () => {
    if (socket !== liveStreamSocket || ticket !== liveVisionGeneration || projectId !== getProjectId()) { socket.close(); return }
    connectedAt = Date.now()
    liveStreamConnected.value = true
    liveStreamReconnectScheduled.value = false
    liveStreamRecovering.value = false
    liveAdaptive.reset()
    liveLedger.clear()
    refreshLiveVisionPhase()
    socket.send(JSON.stringify(realtimeHello(projectId, getSessionId())))
    if (liveStreamHeartbeatTimer) clearInterval(liveStreamHeartbeatTimer)
    liveStreamHeartbeatTimer = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN && ticket === liveVisionGeneration) {
        try { socket.send(JSON.stringify({ v: 1, type: 'heartbeat', sent_at: Date.now() })) } catch { /* reconnect path */ }
      }
    }, 12_000)
    liveVisionStatus.value = reconnecting
      ? `实时视频连接已恢复 · ${liveVisionSource.value} · 继续传送画面和语音`
      : `实时视频已连接 · ${liveVisionSource.value} · 画面持续传送，AI 异步理解最新帧`
  }
  socket.onmessage = event => {
    if (socket === liveStreamSocket) receiveLiveStreamObservation(String(event.data), ticket, projectId)
  }
  socket.onclose = event => {
    if (liveStreamSocket !== socket || ticket !== liveVisionGeneration) return
    const recoveryFailed = liveStreamRecovering.value
    liveStreamSocket = null
    voiceVerification.reset()
    liveStreamSendRevision++
    liveStreamReadyForVideo = false
    liveNativeAudioReady = false
    liveStreamFrames.clear()
    liveLedger.clear()
    if (liveStreamTimer) { clearTimeout(liveStreamTimer); liveStreamTimer = null }
    liveStreamConnected.value = false
    liveStreamRecovering.value = false
    stopAiPlayback()
    closeOpenLiveCaption()
    liveTranscriptDraft.value = ''
    if (liveStreamHeartbeatTimer) { clearInterval(liveStreamHeartbeatTimer); liveStreamHeartbeatTimer = null }
    if (!liveVisionActive.value || projectId !== getProjectId()) { refreshLiveVisionPhase(); return }
    if (event.code === 1008) {
      liveStreamReconnectScheduled.value = false
      liveVisionStatus.value = '实时视觉连接被拒绝；请确认当前项目和访问来源。画面仍在本地播放。'
      refreshLiveVisionPhase()
      return
    }
    const nextRetry = connectedAt && Date.now() - connectedAt > 10_000 ? 0 : retry + 1
    const delay = Math.min(10_000, 500 * 2 ** Math.min(nextRetry, 4))
    liveStreamReconnectScheduled.value = true
    liveVisionStatus.value = recoveryFailed
      ? `实时模型恢复失败；${Math.ceil(delay / 1000)} 秒后重新连接…`
      : `视频仍在播放；视觉连接已断开，${Math.ceil(delay / 1000)} 秒后重连…`
    refreshLiveVisionPhase()
    liveStreamReconnectTimer = setTimeout(() => {
      liveStreamReconnectScheduled.value = false
      liveStreamReconnectTimer = null
      if (ticket === liveVisionGeneration && liveVisionActive.value && projectId === getProjectId()) startLiveStream(ticket, nextRetry)
    }, delay)
  }
  socket.onerror = () => {
    if (liveStreamSocket === socket && ticket === liveVisionGeneration) liveVisionStatus.value = '视觉流连接失败；画面仍在本地播放。'
  }
}
async function startMediaVision(source: 'screen' | 'camera') {
  if (!getProjectId()) {
    liveVisionStatus.value = '请先选择当前项目。'
    return
  }
  if (!navigator.mediaDevices || (source === 'screen' && !navigator.mediaDevices.getDisplayMedia)
      || (source === 'camera' && !navigator.mediaDevices.getUserMedia)) {
    liveVisionStatus.value = source === 'camera' ? '当前浏览器不支持摄像头。' : '当前浏览器不支持屏幕共享。'
    return
  }
  try {
    const stream = source === 'screen'
      ? await navigator.mediaDevices.getDisplayMedia({
        video: { width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 15, max: 30 } }, audio: false })
      : await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 24, max: 30 } }, audio: false })
    stopLiveVision()
    screenStream = stream
    screenSharing.value = source === 'screen'
    cameraSharing.value = source === 'camera'
    liveVisionSource.value = source === 'screen' ? '屏幕共享' : '摄像头'
    liveVisionActive.value = true
    liveVisionObservation.value = ''
    liveVisionTimeline.value = []
    liveVisionMetrics.value = { latencyMs: null, intervalMs: 500, dropped: 0 }
    liveStreamLastObservationAt = 0
    liveStreamCaptions.value = []
    liveTranscriptDraft.value = ''
    liveStreamCapabilities.value = ''
    liveVisionPhase.value = 'connecting'
    liveVisionStatus.value = `${liveVisionSource.value}已启动，正在连接实时视觉流…`
    stream.getVideoTracks()[0]?.addEventListener('ended', () => stopLiveVision(`${liveVisionSource.value}已结束。`), { once: true })
    await nextTick()
    if (sharedVideo.value) {
      sharedVideo.value.srcObject = stream
      await sharedVideo.value.play()
    }
    await refreshLiveVisionMode()
    const ticket = ++liveVisionGeneration
    startLiveStream(ticket)
  } catch (cause) {
    stopLiveVision((cause as Error).name === 'NotAllowedError'
      ? `未获得${source === 'camera' ? '摄像头' : '屏幕共享'}授权。`
      : (cause as Error).message || '实时画面启动失败。')
  }
}
function startScreenShare() { void startMediaVision('screen') }
function startCameraShare() { void startMediaVision('camera') }
function scheduleLiveVision(ticket: number, delay = 1400) {
  clearLiveVisionTimer()
  if (!liveVisionActive.value) return
  liveVisionTimer = setTimeout(() => { liveVisionTimer = null; void sampleLiveVision(ticket) }, Math.max(400, delay))
}
function toggleLiveVisionPause() {
  liveVisionPaused.value = !liveVisionPaused.value
  liveStreamSendRevision++
  resetLiveStreamDedup()
  if (liveVisionPaused.value) {
    liveVisionRequest?.abort()
    liveVisionRequest = null
    liveStreamFrames.clear()
    liveLedger.clear()
    liveVisionStatus.value = '观察已暂停，画面仍在播放。'
    refreshLiveVisionPhase()
    if (liveStreamTimer) { clearTimeout(liveStreamTimer); liveStreamTimer = null }
  } else {
    liveVisionLastSample = null
    liveVisionQuietSamples = 0
    liveVisionStatus.value = '正在恢复画面观察…'
    refreshLiveVisionPhase()
    if (screenSharing.value || cameraSharing.value) scheduleLiveStreamFrame(liveVisionGeneration, 100)
    else scheduleLiveVision(liveVisionGeneration, 100)
  }
}
async function sampleLiveVision(ticket: number) {
  if (!liveVisionActive.value || ticket !== liveVisionGeneration || liveVisionBusy.value) return
  if (liveVisionPaused.value) return
  const focusRevision = liveVisionFocusRevision
  if (document.hidden) {
    liveVisionStatus.value = '窗口暂时不可见，已暂停采样；返回后继续。'
    scheduleLiveVision(ticket, 1400)
    return
  }
  liveVisionBusy.value = true
  try {
    const artifact = previewArtifacts.value.find(item => item.id === liveVisionArtifactId) || firstInteractiveArtifact()
    let image: Blob
    if (screenSharing.value || cameraSharing.value) {
      image = await sharedScreenBlob()
      liveVisionSource.value = screenSharing.value ? '屏幕共享' : '摄像头'
    } else if (artifact?.id) {
      const captured = await captureLiveFrame(artifact.id)
      if (!captured) throw new Error(captureErrors.get(artifact.id) || '网页预览当前帧无法读取')
      image = captured
      liveVisionSource.value = '网页预览'
    } else {
      image = await desktopVisionBlob()
      liveVisionSource.value = '桌面窗口'
    }
    if (!liveVisionActive.value || liveVisionPaused.value || ticket !== liveVisionGeneration || focusRevision !== liveVisionFocusRevision) return
    liveVisionLatestFrame = image
    const capturedAt = Date.now()
    liveVisionLatestCapturedAt = capturedAt
    setLiveVisionSnapshot(image)
    const focused = !!liveVisionFocus.value
    const analysisImage = liveVisionFocus.value ? await cropFocusFrame(image, liveVisionFocus.value) : image
    if (!liveVisionActive.value || liveVisionPaused.value || ticket !== liveVisionGeneration || focusRevision !== liveVisionFocusRevision) return
    appEvents.emit('docmind:live-vision-frame', {
      projectId: getProjectId(), image, capturedAt, focusImage: focused ? analysisImage : null,
      observation: liveVisionObservation.value, timeline: liveVisionTimeline.value,
    })
    const sample = await sampleVisualFrame(analysisImage)
    if (!liveVisionActive.value || liveVisionPaused.value || ticket !== liveVisionGeneration || focusRevision !== liveVisionFocusRevision) return
    if (!pendingVisionAlert && !shouldAnalyzeVisualFrame(liveVisionLastSample, sample, liveVisionLastAnalyzedAt, Date.now())) {
      liveVisionQuietSamples++
      liveVisionStatus.value = `持续观察中 · ${liveVisionSource.value} · 画面变化较小，已跳过重复分析`
      scheduleLiveVision(ticket, liveVisionQuietSamples >= 3 ? 2400 : 1400)
      return
    }
    liveVisionQuietSamples = 0
    const request = new AbortController()
    liveVisionRequest = request
    const result = await visionApi.analyzeLiveFrame(analysisImage, liveVisionObservation.value, request.signal, focused)
    if (liveVisionRequest === request) liveVisionRequest = null
    if (!liveVisionActive.value || ticket !== liveVisionGeneration || focusRevision !== liveVisionFocusRevision) return
    if (liveVisionPaused.value) return
    if (result.throttled) {
      scheduleLiveVision(ticket, Math.max(500, (result.retry_after || 1) * 1000))
      return
    }
    if (!result.ok) throw new Error(result.error || '视觉模型未返回结果')
    const mode = result.audit?.mode || ''
    if (mode === 'unavailable' || mode === 'error') {
      stopLiveVision(mode === 'unavailable'
        ? '持续视觉需要配置 Harness 视觉模型；当前画面仍可手动发送给支持视觉的模型。'
        : '视觉模型本轮分析失败，已停止持续观察，请稍后重试。')
      return
    }
    liveVisionLastSample = sample
    liveVisionLastAnalyzedAt = Date.now()
    const rawObservation = (result.observations || []).join('\n').trim()
    const observation = rawObservation && focused ? `【重点区域】${rawObservation}` : rawObservation
    if (observation) {
      liveVisionObservation.value = observation.slice(-5000)
      liveVisionTimeline.value = [...liveVisionTimeline.value, {
        at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
        observation: observation.slice(-1200),
        capturedAt,
        observedAt: Date.now(),
      }].slice(-5)
    }
    const alertProgress = advanceVisionAlert(pendingVisionAlert, result.anomalies || [])
    pendingVisionAlert = alertProgress.confirmed ? null : alertProgress.pending
    if (alertProgress.confirmed && !acknowledgedVisionAlerts.has(alertProgress.key)) {
      acknowledgedVisionAlerts.add(alertProgress.key)
      dismissVisionAlert()
      liveVisionAlert.value = {
        ...alertProgress.confirmed,
        atMs: Date.now(),
        detectedAt: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
      }
      liveVisionAlertFrame = image
      liveVisionAlertImageUrl.value = URL.createObjectURL(image)
    }
    appEvents.emit('docmind:live-vision-frame', {
      projectId: getProjectId(), image, capturedAt, focusImage: focused ? analysisImage : null,
      observation: liveVisionObservation.value,
      timeline: liveVisionTimeline.value,
    })
    liveVisionStatus.value = `持续观察中 · ${liveVisionSource.value} · ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`
    scheduleLiveVision(ticket, 1400)
  } catch (cause) {
    if ((cause as Error).name === 'AbortError' || ticket !== liveVisionGeneration) return
    stopLiveVision((cause as Error).message || '持续视觉读取失败，已停止观察。')
  } finally {
    liveVisionRequest = null
    liveVisionBusy.value = false
    if (liveVisionActive.value && !liveVisionPaused.value && ticket === liveVisionGeneration && !liveVisionTimer) {
      scheduleLiveVision(ticket, 100)
    }
  }
}
function toggleLiveVision(artifact?: { id?: string }) {
  if (liveVisionActive.value && !screenSharing.value && !cameraSharing.value) {
    stopLiveVision('已停止持续视觉观察。')
    return
  }
  if (liveVisionActive.value) stopLiveVision()
  if (!getProjectId()) {
    liveVisionStatus.value = '请先选择当前项目。'
    return
  }
  dismissVisionAlert()
  liveVisionArtifactId = artifact?.id || firstInteractiveArtifact()?.id || ''
  liveVisionLastSample = null
  liveVisionLastAnalyzedAt = 0
  liveVisionQuietSamples = 0
  liveVisionObservation.value = ''
  liveVisionTimeline.value = []
  liveVisionStatus.value = '正在启动持续视觉观察…'
  liveVisionActive.value = true
  const ticket = ++liveVisionGeneration
  void sampleLiveVision(ticket)
}
/**
 * 让 Agent 主动查看当前项目界面。
 *
 * 网页预览直接截取当前 iframe 并作为多模态输入发送；原生桌面窗口没有
 * 可读取的 iframe 时仍发送观察意图，Agent 会按 computer-use 技能调用
 * dev_desktop_capture，避免把“看过画面”误报成事实。
 */
async function inspectCurrentInterface(artifact?: { id?: string; label?: string }) {
  if (visualInspecting.value) return
  if (!getProjectId()) {
    visualInspectStatus.value = '请先选择当前项目，再让 AI 观察界面。'
    return
  }
  visualInspecting.value = true
  visualInspectStatus.value = '正在读取当前界面…'
  const ticket = generation
  const projectId = getProjectId()
  try {
    const image = screenSharing.value
      ? await sharedScreenBlob()
      : artifact?.id ? await captureLiveFrame(artifact.id) : null
    if (ticket !== generation || projectId !== getProjectId()) return
    const label = artifact?.label || '当前项目界面'
    const prompt = [
      `请先浏览并分析当前项目界面（${label}），再回答或执行下一步。`,
      image
        ? '我附上了刚刚截取的真实画面。请只描述画面中可观察到的事实，并结合项目文件、运行日志和工具结果核对，不要把截图当作源码或功能验收。'
        : '当前没有可直接读取的网页预览，请调用 dev_desktop_capture 获取当前项目嵌入窗口或前台窗口的真实画面；如果仍无法捕获，要明确说明原因，不要猜测画面。',
      '如果发现明显问题，请先给出问题位置、证据和修改方案；得到修改指令后再执行。',
    ].join('\n')
    const request: PreviewFeedbackRequest = {
      target: 'cockpit', projectId, images: image ? [image] : [], prompt,
      uiContext: 'app_interface_inspect',
      onStatus: (status, detail) => {
        if (ticket !== generation) return
        if (status === 'processing') {
          visualInspecting.value = true
          visualInspectStatus.value = detail || 'AI 正在查看当前界面…'
        } else if (status === 'awaiting_review') {
          visualInspecting.value = false
          visualInspectStatus.value = 'AI 已完成界面观察，回复已显示在协作对话中。'
        } else if (status === 'failed') {
          visualInspecting.value = false
          visualInspectStatus.value = detail || '界面观察未完成，请重试。'
        } else if (status === 'pending') {
          visualInspecting.value = false
          visualInspectStatus.value = detail || '当前对话正忙，界面观察请求已暂存，请稍后重试。'
        }
      },
    }
    appEvents.emit('docmind:send-chat', request)
    appEvents.emit('docmind:focus-chat', { target: 'cockpit' })
  } catch (cause) {
    visualInspecting.value = false
    visualInspectStatus.value = (cause as Error).message || '当前界面读取失败，请重试。'
  }
}
function toggleFeedback(id: string) {
  feedbackOpenId.value = feedbackOpenId.value === id ? '' : id
  if (feedbackOpenId.value !== id) feedbackText.value = ''
}
async function sendFeedback(artifact: { id?: string; label?: string }) {
  const note = feedbackText.value.trim()
  if (!note || !workflow.value || feedbackSending.value) return
  feedbackSending.value = true
  const wid = workflow.value.workflow_id
  const projectId = getProjectId()
  const ticket = generation
  const label = artifact.label || '实时画面'
  const region = regionOwner.value === artifact.id ? feedbackRegion.value : null
  let prompt = [
    `请继续修改当前项目。用户针对自主开发舱中的「${label}」提出反馈：`,
    note,
    region && region.width > 0 && region.height > 0
      ? `用户标注区域（相对画面百分比）：左 ${region.x}%、上 ${region.y}%、宽 ${region.width}%、高 ${region.height}%。请优先检查该区域。`
      : '',
    `当前预览尺寸约为 ${Math.round(liveWidth.value)}% × ${Math.round(liveHeight.value)}px。请先使用可用的截图、运行或项目检查工具核对实际效果，再直接完成修改并验证。工作流：${workflow.value.workflow_id}`,
  ].filter(Boolean).join('\n')
  const screenshot = artifact.id ? await captureLiveFrame(artifact.id) : null
  if (!screenshot) prompt += `\n本次反馈未附截图：${captureErrors.get(artifact.id || '') || '当前画面无法截图'}。请使用项目运行或检查工具确认效果，不要声称已看过画面。`
  const sentAt = new Date().toISOString()
  const record: FeedbackRecord = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    artifact_id: artifact.id, label, note, region: region && region.width > 0 && region.height > 0 ? { ...region } : undefined,
    screenshot: !!screenshot, sentAt, sent_at: sentAt, status: 'pending', snapshots: {},
  }
  try {
    const saved = await agentApi.workflowVisualFeedback(wid, {
      id: record.id, label: record.label, note: record.note, region: record.region,
      artifact_id: record.artifact_id, screenshot: record.screenshot, sent_at: record.sentAt,
    }, projectId)
    if (!saved.ok || !saved.feedback) throw new Error(saved.error || '反馈保存失败')
    Object.assign(record, mapFeedback(saved.feedback))
    if (screenshot) {
      const snapshot = await agentApi.workflowFeedbackSnapshot(wid, record.id, 'before', await blobDataUrl(screenshot), projectId)
      if (!snapshot.ok || !snapshot.feedback) throw new Error(snapshot.error || '截图保存失败')
      Object.assign(record, mapFeedback(snapshot.feedback))
    }
    if (ticket !== generation || projectId !== getProjectId()) return
    upsertFeedback(record)
    dispatchFeedback(record, prompt, screenshot ? [screenshot] : [], wid, projectId)
    feedbackText.value = ''
    feedbackOpenId.value = ''
    feedbackRegion.value = null
  } catch (e) {
    if (ticket === generation) {
      record.detail = (e as Error).message; upsertFeedback(record)
      error.value = `反馈已保留，尚未发送：${record.detail}`
    }
  } finally { feedbackSending.value = false }
}

function dispatchFeedback(record: FeedbackRecord, prompt: string, images: Blob[], wid: string, projectId: string,
                          uiContext: ChatUiContext = 'web_preview_feedback', autoRetry = false) {
  const ticket = generation
  let reviewStarted = false
  let revision = 0
  const update = async (status: string, detail = '') => {
    const current = ++revision
    Object.assign(record, { status, detail })
    if (workflow.value?.workflow_id === wid && getProjectId() === projectId) upsertFeedback(record)
    const previous = feedbackUpdates.get(record.id) || Promise.resolve()
    const queued = previous.then(async () => {
      try {
        const result = await agentApi.workflowFeedbackStatus(wid, record.id, status, detail, projectId)
        if (!result.ok || !result.feedback) throw new Error(result.error || '反馈状态未同步')
        if (current === revision) Object.assign(record, mapFeedback(result.feedback))
      } catch {
        if (current === revision) record.detail = `${detail}（状态尚未同步，请稍后重试）`
      }
      if (current === revision && workflow.value?.workflow_id === wid && getProjectId() === projectId) upsertFeedback(record)
    })
    feedbackUpdates.set(record.id, queued)
    await queued
    if (feedbackUpdates.get(record.id) === queued) feedbackUpdates.delete(record.id)
  }
  const onStatus: PreviewFeedbackRequest['onStatus'] = (status, detail) => {
    if (record.artifact_id === 'desktop_visual_review' && lastClickFeedbackId.value === record.id &&
        workflow.value?.workflow_id === wid && getProjectId() === projectId) {
      clickFeedbackStatus.value = status === 'pending' ? (detail || '视觉复验反馈已排队')
        : status === 'processing' ? 'AI 正在核对桌面复验结果与项目状态…'
          : status === 'failed' ? (detail || '自动分析未完成；可在反馈记录中重试')
            : 'AI 已回复，正在记录当前画面供你验收。'
    }
    if (status !== 'awaiting_review') { void update(status, detail); return }
    if (reviewStarted) return
    reviewStarted = true
    void (async () => {
      await update('processing', 'AI 回复已结束，正在刷新预览并记录效果')
      const failure = ticket === generation && workflow.value?.workflow_id === wid && getProjectId() === projectId
        ? await captureAfter(record, true)
        : '页面或项目已切换，请回到原工作流记录当前效果'
      await update('awaiting_review', failure
        ? `AI 回复已结束，自动记录未完成：${failure}。请检查效果后手动记录。`
        : '已刷新预览并保存复验画面，请对照检查；截图不代表功能或源码已通过验收')
    })()
  }
  const request: PreviewFeedbackRequest = { prompt, images, projectId, uiContext, autoRetry, onStatus,
    workflowId: uiContext === 'desktop_visual_review' ? wid : undefined,
    feedbackId: uiContext === 'desktop_visual_review' ? record.id : undefined }
  appEvents.emit('docmind:send-chat', { ...request, target: 'cockpit' })
}
watch(() => workflow.value?.status, status => {
  if (status === 'executing' || !pendingDesktopDispatches.length) return
  const waiting = pendingDesktopDispatches.splice(0)
  for (const item of waiting) {
    if (item.ticket !== generation || item.projectId !== getProjectId() ||
        workflow.value?.workflow_id !== item.wid) continue
    dispatchFeedback(item.record, item.prompt, item.images, item.wid, item.projectId,
      'desktop_visual_review', true)
  }
})

async function retryFeedback(record: FeedbackRecord) {
  if (!workflow.value || feedbackSending.value) return
  feedbackSending.value = true
  const wid = workflow.value.workflow_id, projectId = getProjectId(), ticket = generation
  try {
    const saved = await agentApi.workflowVisualFeedback(wid, record as unknown as Record<string, unknown>, projectId)
    if (!saved.ok || !saved.feedback) throw new Error(saved.error || '反馈保存失败')
    Object.assign(record, mapFeedback(saved.feedback))
    const images: Blob[] = []
    if (record.snapshots?.before) {
      const response = await fetch(agentApi.workflowFeedbackSnapshotUrl(wid, record.id, 'before', projectId))
      if (!response.ok) throw new Error('修改前截图读取失败')
      images.push(await response.blob())
    }
    if (ticket !== generation || projectId !== getProjectId()) return
    const desktop = record.artifact_id === 'desktop_visual_review'
    const prompt = `请根据当前项目的视觉反馈继续核实并提出修改方案：\n${record.note}\n工作流：${wid}\n预览：${record.label}\n附件：${desktop ? '点击后、修改前的桌面画面' : '反馈时的预览画面'}\n先检查当前项目状态；之前执行的操作不会自动撤销，请避免重复修改。`
    if (desktop && workflow.value?.status === 'executing') {
      if (pendingDesktopDispatches.some(item => item.record.id === record.id)) return
      if (pendingDesktopDispatches.length >= 5) throw new Error('待处理视觉反馈已满，请稍后重试')
      pendingDesktopDispatches.push({ record, prompt, images, wid, projectId, ticket })
      clickFeedbackStatus.value = '当前任务仍在执行；反馈已排队，任务结束后自动处理。'
    } else {
      dispatchFeedback(record, prompt, images, wid, projectId,
        desktop ? 'desktop_visual_review' : 'web_preview_feedback', desktop)
    }
  } catch (e) { if (ticket === generation) error.value = (e as Error).message }
  finally { feedbackSending.value = false }
}

async function captureAfter(record: FeedbackRecord, refresh = false): Promise<string | null> {
  if (!workflow.value || snapshotBusyId.value) return '另一个截图正在保存，请稍后重试'
  const wid = workflow.value.workflow_id, ticket = generation, projectId = getProjectId()
  snapshotBusyId.value = record.id
  try {
    if (refresh && record.artifact_id !== 'desktop_visual_review') {
      const frame = liveFrameRefs.get(record.artifact_id || '')
      if (!frame) throw new Error('预览尚未显示，请展开画面后重试')
      await refreshPreviewFrame(frame)
    }
    if (ticket !== generation || projectId !== getProjectId()) throw new Error('项目或工作流已切换')
    const desktop = record.artifact_id === 'desktop_visual_review'
    const desktopFrame = desktop ? await visionApi.captureDesktopFrame('embedded', true) : null
    const image = desktop ? desktopFrame?.image || '' : record.artifact_id ? await captureLiveFrame(record.artifact_id) : null
    if (!image) throw new Error(desktop ? desktopFrame?.error || '当前嵌入窗口无法截图'
      : captureErrors.get(record.artifact_id || '') || '当前画面无法读取截图')
    if (ticket !== generation || projectId !== getProjectId()) throw new Error('项目或工作流已切换')
    const saved = await agentApi.workflowFeedbackSnapshot(wid, record.id, 'after',
      typeof image === 'string' ? image : await blobDataUrl(image), projectId)
    if (!saved.ok || !saved.feedback) throw new Error(saved.error || '截图保存失败')
    if (ticket === generation) {
      const current = feedbackHistory.value.find(item => item.id === record.id) || record
      upsertFeedback({ ...current, snapshots: saved.feedback.snapshots })
    }
    return null
  } catch (e) {
    const message = (e as Error).message
    if (!refresh && ticket === generation) error.value = message
    return message
  }
  finally { snapshotBusyId.value = '' }
}

async function decideFeedback(record: FeedbackRecord, accepted: boolean) {
  if (!workflow.value) return
  const wid = workflow.value.workflow_id, ticket = generation
  try {
    const saved = await agentApi.workflowFeedbackStatus(wid, record.id, accepted ? 'accepted' : 'needs_changes')
    if (!saved.ok || !saved.feedback) throw new Error(saved.error || '决定保存失败')
    if (ticket === generation) upsertFeedback(mapFeedback(saved.feedback))
    if (!accepted && record.artifact_id === 'desktop_visual_review') clickFeedbackStatus.value = '已标记需要继续修改；可在视觉反馈记录中重新发送给 AI。'
    else if (!accepted && record.artifact_id) { feedbackOpenId.value = record.artifact_id; feedbackText.value = `继续改进：${record.note}` }
  } catch (e) { if (ticket === generation) error.value = (e as Error).message }
}

async function hydrate(id: string, ticket = generation) {
  try {
    const r = await agentApi.workflow(id)
    if (ticket !== generation) return
    if (r.ok && r.workflow) {
      workflow.value = r.workflow
      const terminal = ['completed', 'failed', 'interrupted'].includes(r.workflow.status)
      if (terminal && (r.workflow.steps || typeof r.workflow.review?.ok === 'boolean' || r.workflow.timeline?.some(event => event.kind === 'execute_start'))) {
        try {
          const evaluated = await agentApi.workflowEvaluation(id)
          if (ticket === generation && evaluated.ok && evaluated.evaluation) evaluation.value = evaluated.evaluation
        } catch { /* 评估是增强信息，接口暂时不可用时不影响工作流状态展示 */ }
      } else if (ticket === generation) evaluation.value = null
      if (r.workflow.visual_feedback?.length) {
        const server = r.workflow.visual_feedback.map(item => feedbackUpdates.has(item.id)
          ? feedbackHistory.value.find(row => row.id === item.id) || mapFeedback(item)
          : mapFeedback(item))
        const ids = new Set(server.map(item => item.id))
        feedbackHistory.value = [...server, ...feedbackHistory.value.filter(item => !ids.has(item.id))]
          .sort((a, b) => b.sentAt.localeCompare(a.sentAt)).slice(0, 20)
        saveFeedbackHistory()
      } else if (!feedbackHistory.value.length) loadFeedbackHistory(id)
    }
    else error.value = r.error || '无法读取工作流'
  } catch (e) { if (ticket === generation) error.value = (e as Error).message }
}
async function loadProjectProfile(ticket = generation) {
  try {
    const r = await agentApi.projectProfile()
    if (ticket !== generation) return
    projectProfile.value = r.ok && r.profile ? r.profile : null
  } catch { if (ticket === generation) projectProfile.value = null }
}
function scheduleHydrate() {
  if (refreshTimer || !workflow.value) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    if (workflow.value) void hydrate(workflow.value.workflow_id)
  }, 350)
}
function select(id: string) {
  stopLiveVision()
  pendingDesktopDispatches.length = 0
  generation++
  feedbackOpenId.value = ''; feedbackText.value = ''; feedbackRegion.value = null; annotationId.value = ''; regionOwner.value = ''
  visualInspecting.value = false; visualInspectStatus.value = ''
  unsubscribe?.()
  selectedId.value = id
  loadActionLoop()
  workflow.value = null
  projectProfile.value = null
  evaluation.value = null
  acceptanceReport.value = null
  chatHint.value = ''
  loadFeedbackHistory(id)
  error.value = ''
  const ticket = generation
  void hydrate(id, ticket)
  unsubscribe = workflowEvents(id, 0, {
    onEvent: (event: WorkflowEvent) => {
      if (ticket !== generation) return
      if (event.status && workflow.value) workflow.value = { ...workflow.value, status: event.status }
      scheduleHydrate()
    },
    onDone: scheduleHydrate,
    onError: scheduleHydrate,
  })
}
async function loadList() {
  const ticket = generation
  try {
    const r = await agentApi.workflowList(100)
    if (ticket !== generation) return
    history.value = r.items || []
  } catch (e) { error.value = (e as Error).message }
}
async function start() {
  if (!goal.value.trim() || busy.value) return
  replaceTasks.value = history.value.filter(item => !terminalStatuses.has(item.status))
  error.value = ''
  if (replaceTasks.value.length) { startDialog.value?.showModal(); return }
  await beginStart(false)
}
async function beginStart(pauseOld: boolean) {
  const prompt = goal.value.trim()
  if (!prompt || busy.value) return
  const projectId = getProjectId(), ticket = generation
  const stillCurrent = () => projectId === getProjectId() && ticket === generation
  busy.value = true
  error.value = ''
  try {
    const latest = await agentApi.workflowList(100)
    if (!stillCurrent()) return
    history.value = latest.items || []
    const active = history.value.filter(item => !terminalStatuses.has(item.status))
    if (active.length && (!pauseOld || active.some(item => !replaceTasks.value.some(old => old.workflow_id === item.workflow_id)))) {
      replaceTasks.value = active
      if (!startDialog.value?.open) startDialog.value?.showModal()
      return
    }
    for (const item of active) {
      const paused = await agentApi.workflowInterrupt(item.workflow_id, '用户确认暂停旧任务并开始新目标')
      if (!stillCurrent()) return
      if (!paused.workflow || !terminalStatuses.has(paused.workflow.status)) throw new Error(paused.error || '旧任务尚未暂停，未启动新目标')
      history.value = history.value.map(row => row.workflow_id === item.workflow_id ? { ...row, status: paused.workflow!.status } : row)
    }
    // 直接走后端工作流启动（方案选择/审批仍在本舱与对话卡片内完成）
    const r = await agentApi.workflowStart(prompt)
    if (!r.workflow) throw new Error(r.error || '工作流启动失败')
    if (!stillCurrent()) return
    startDialog.value?.close()
    goal.value = ''
    select(r.workflow.workflow_id)
    void loadList()
  } catch (e) {
    error.value = (e as Error).message || '工作流启动失败'
  } finally {
    busy.value = false
  }
}
function openChat() {
  const current = workflow.value
  const status = current?.status
  const question = status === 'awaiting_choice'
    ? '请解释当前这些方案的差别，并建议最适合我目标的方案。'
    : status === 'planned' || status === 'awaiting_approval'
      ? '请解释这份执行计划、会修改哪些文件，以及如何按验收条件检查结果。'
        : status === 'completed'
        ? '请总结本轮开发结果、实际使用的工具、验收证据和仍需改进的地方。'
        : '请根据当前开发任务的状态，解释接下来应该做什么。'
  const options = current?.options?.map(option => `- ${option.title}：${option.summary}`).join('\n') || ''
  const tasks = current?.tasks?.map(task => `- ${String(task.task || task.id || '')}`).join('\n') || ''
  const context = current ? `当前开发任务：${current.request || ''}\n任务状态：${statusLabel[current.status] || current.status}${options ? `\n候选方案：\n${options}` : ''}${tasks ? `\n执行计划：\n${tasks}` : ''}\n` : ''
  const prompt = `${context}${question}`
  chatHint.value = '已聚焦右侧对话框；空白草稿会填入任务问题，可编辑后发送。'
  appEvents.emit('docmind:focus-chat', { target: 'cockpit', qIfEmpty: prompt })
  document.getElementById('wb-cockpit-chat-slot')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}
async function openAcceptanceReport() {
  if (!workflow.value || !hasExecution.value || reportBusy.value) return
  reportBusy.value = true
  error.value = ''
  const id = workflow.value.workflow_id
  try {
    const result = await agentApi.workflowAcceptanceReport(id)
    if (!result.ok || !result.report) throw new Error(result.error || '验收报告暂时无法读取')
    if (workflow.value?.workflow_id === id) acceptanceReport.value = result.report
  } catch (e) { error.value = (e as Error).message }
  finally { reportBusy.value = false }
}
function openMcpSettings() {
  // App.vue 仍以 window 事件监听设置打开请求
  window.dispatchEvent(new CustomEvent('docmind:open-settings', { detail: { tab: 'mcp' } }))
}
async function control(action: 'interrupt' | 'resume') {
  if (!workflow.value || busy.value) return
  busy.value = true
  try {
    const r = action === 'interrupt'
      ? await agentApi.workflowInterrupt(workflow.value.workflow_id)
      : await agentApi.workflowResume(workflow.value.workflow_id)
    if (r.workflow) { workflow.value = r.workflow; void loadList() }
    else error.value = r.error || '操作失败'
  } catch (e) { error.value = (e as Error).message || '操作失败'
  } finally { busy.value = false }
}

async function rollbackProject() {
  if (!workflow.value || busy.value || !workflow.value.project_checkpoint?.id) return
  if (!window.confirm('将恢复模型执行前的项目文件快照，是否继续？')) return
  busy.value = true
  error.value = ''
  try {
    const r = await agentApi.workflowProjectRollback(workflow.value.workflow_id, true)
    if (r.rollback?.ok) await hydrate(workflow.value.workflow_id)
    else error.value = r.error || '项目快照恢复失败'
  } catch (e) { error.value = (e as Error).message || '项目快照恢复失败' }
  finally { busy.value = false }
}
async function applyRecovery(option: NonNullable<NonNullable<WorkflowState['recovery']>['options']>[number]) {
  if (!workflow.value || busy.value || !option) return
  const action = option.action || ''
  if (action === 'rollback') { await rollbackProject(); return }
  if (action === 'inspect') { openChat(); return }
  if (action !== 'retry_failed') { openChat(); return }
  const taskIds = (option.task_ids || []).filter(Boolean)
  if (!taskIds.length) { openChat(); return }
  if (!window.confirm(`将按顺序重试 ${taskIds.length} 个失败任务，是否继续？`)) return
  busy.value = true
  error.value = ''
  try {
    for (const taskId of taskIds) {
      const r = await agentApi.workflowSubagentRetry(workflow.value.workflow_id, taskId)
      if (!r.workflow) throw new Error(r.error || `任务 ${taskId} 重试失败`)
      workflow.value = r.workflow
    }
    await hydrate(workflow.value.workflow_id)
  } catch (e) { error.value = (e as Error).message || '重试失败任务时发生错误' }
  finally { busy.value = false }
}
async function approvePlan() {
  if (!workflow.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const id = workflow.value.workflow_id
    const first = workflow.value.status === 'awaiting_approval'
      ? await agentApi.workflowApprove(id, true, true)
      : await agentApi.workflowExecute(id)
    if (!first.workflow) { error.value = first.error || '启动失败'; return }
    workflow.value = first.workflow
    if (first.workflow.status === 'awaiting_approval') {
      const next = await agentApi.workflowApprove(id, true, true)
      if (next.workflow) workflow.value = next.workflow
      else error.value = next.error || '批准后启动失败'
    }
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function decide(approved: boolean, note: string) {
  if (!workflow.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const r = await agentApi.workflowAcceptanceDecide(
      workflow.value.workflow_id, approved, note)
    if (r.workflow) workflow.value = r.workflow
    else error.value = r.error || '验收决定保存失败'
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
function onProjectChanged() {
  stopLiveVision()
  pendingDesktopDispatches.length = 0
  startDialog.value?.close()
  generation++
  unsubscribe?.()
  workflow.value = null
  evaluation.value = null
  selectedId.value = ''
  loadActionLoop()
  acceptanceReport.value = null
  chatHint.value = ''
  history.value = []
  feedbackHistory.value = []
  feedbackOpenId.value = ''; feedbackText.value = ''; feedbackRegion.value = null; annotationId.value = ''; regionOwner.value = ''
  visualInspecting.value = false; visualInspectStatus.value = ''
  error.value = ''
  void loadProjectProfile(generation)
  void loadList()
}
// 右侧「与 AI 协作」栏收起状态：本地记忆；收起时舞台区占满宽度，
// 对话台状态保留（只裁剪不卸载），靠边缘拉手重新展开
const COLLAB_STORAGE_KEY = 'docmind.cockpit.collab'
const collabCollapsed = ref(false)
try { collabCollapsed.value = localStorage.getItem(COLLAB_STORAGE_KEY) === '1' } catch { /* 无存储时默认展开 */ }
watch(collabCollapsed, v => {
  try { localStorage.setItem(COLLAB_STORAGE_KEY, v ? '1' : '0') } catch { /* 忽略写入失败 */ }
})
let offContextChanged: (() => void) | null = null
onMounted(() => {
  emit('ready')
  loadActionLoop()
  void loadProjectProfile()
  void loadList()
  // project-changed 仍由 App.vue 以 window 事件派发；context-changed 已收敛到事件总线
  window.addEventListener('docmind:project-changed', onProjectChanged)
  offContextChanged = appEvents.on('docmind:project-context-changed', onProjectChanged)
  pollTimer = setInterval(() => { if (workflow.value) void hydrate(workflow.value.workflow_id) }, 10000)
  // 心跳独立于 10 秒轮询：它只驱动「已等 N 秒」这个数字，
  // 不发请求——10 秒跳一次的秒表没法让人相信系统还活着。
  startActivityTicker()
})
onBeforeUnmount(() => {
  stopActivityTicker()
  stopLiveVision()
  pendingDesktopDispatches.length = 0
  generation++
  unsubscribe?.()
  offContextChanged?.()
  if (pollTimer) clearInterval(pollTimer)
  if (refreshTimer) clearTimeout(refreshTimer)
  stopResize()
  window.removeEventListener('docmind:project-changed', onProjectChanged)
})
</script>

<template>
  <div class="acp">
    <header class="acp-head">
      <div><h2>自主开发工作室</h2><p>与 AI 一起设计、修改和验证当前项目。</p></div>
      <div class="acp-head-actions">
        <span v-if="workflow" class="acp-state wb-status-chip">{{ statusLabel[workflow.status] || workflow.status }}</span>
        <button @click="emit('toggleFiles')">项目文件</button>
        <button :disabled="!workflow" :aria-expanded="detailsOpen" @click="detailsOpen = !detailsOpen">{{ detailsOpen ? '收起详情' : '任务详情' }}</button>
        <button @click="openHistory">任务历史 · {{ history.length }}</button>
        <button :disabled="busy || historyBusy" @click="newGoal">新目标</button>
      </div>
    </header>
    <p v-if="error" class="acp-error acp-error-banner" role="alert">{{ error }}</p>
    <section v-if="mergedVisualAlert" class="acp-vision-alert-banner" role="alert">
      <div><b>两个来源都报了这一处 · {{ mergedVisualAlert.vision.detectedAt }}</b><p>抽帧观察：{{ mergedVisualAlert.vision.target }}：{{ mergedVisualAlert.vision.evidence }}</p><p>实时模型：{{ mergedVisualAlert.realtime.text }}</p><small>两条独立观测链指向同一处；实时模型那句属自述、尚未核实。</small></div>
      <button @click="reviewRealtimeAlert()">交给 AI 排查</button><button @click="dismissVisionAlert(); dismissRealtimeAlert()">忽略</button>
    </section>
    <section v-else-if="liveVisionAlert" class="acp-vision-alert-banner" role="alert">
      <img v-if="liveVisionAlertImageUrl" :src="liveVisionAlertImageUrl" alt="疑似异常出现时的画面快照">
      <div><b>AI 发现疑似界面异常 · {{ liveVisionAlert.detectedAt }}</b><p>{{ liveVisionAlert.target }}：{{ liveVisionAlert.evidence }}</p><small>{{ liveVisionAlertStatus || '已由两次画面观察复核；仍需结合项目状态确认。' }}</small></div>
      <button @click="reviewVisionAlert()">交给 AI 排查</button><button @click="dismissVisionAlert()">忽略</button>
    </section>
    <section v-else-if="liveRealtimeAlert" class="acp-realtime-alert-banner" role="alert">
      <div><b>实时模型提醒 · {{ liveRealtimeAlert.detectedAt }}</b><p>{{ liveRealtimeAlert.text }}</p><small>{{ liveVisionAlertStatus || '实时模型自述，尚未核实。' }}</small></div>
      <button @click="reviewRealtimeAlert()">同意核查</button><button @click="dismissRealtimeAlert()">不用核查</button>
    </section>
    <nav class="acp-flow-rail" aria-label="自主开发流程">
      <div
        v-for="(step, index) in cockpitSteps"
        :key="step.key"
        class="acp-flow-step"
        :class="{ active: index === cockpitStepIndex, complete: index < cockpitStepIndex }"
      >
        <span class="acp-flow-index">{{ index < cockpitStepIndex ? '✓' : index + 1 }}</span>
        <span class="acp-flow-copy"><b>{{ step.label }}</b><small>{{ step.hint }}</small></span>
      </div>
    </nav>
    <div class="acp-studio" :class="{ 'is-collab-collapsed': collabCollapsed }">
      <section class="acp-stage" aria-label="项目画面与输出">
        <div class="acp-stage-bar">
          <div class="acp-stage-tabs">
            <button :class="{ on: stageMode === 'outputs' }" @click="setStageMode('outputs')">项目输出</button>
            <button :class="{ on: stageMode === 'runtime' }" @click="setStageMode('runtime')">引擎画面 / 场景</button>
          </div>
          <button @click="openMcpSettings">连接工具与预览</button>
        </div>
        <div class="acp-stage-content">
          <section class="acp-execute-region" aria-label="执行">
          <div class="acp-region-kicker"><span>02</span><b>执行</b><small>计划、工具调用、修改和运行记录</small></div>
          <div v-if="workflow" class="acp-workflow-surface">
            <div class="acp-surface-heading"><div><h3>当前任务与审核</h3><p v-if="workflow.status === 'awaiting_choice'">请先查看并选择方案。选择后 AI 会生成计划，修改项目之前仍需你审核。</p><p v-else-if="workflow.status === 'planning' || workflow.status === 'generating_options'">AI 正在准备方案与验收条件，完成后会在这里显示。</p><p v-else>方案、执行记录和验收条件会随任务更新。</p></div><span>{{ statusLabel[workflow.status] || workflow.status }}</span></div>
            <p v-if="workflow.project_stage?.status === 'draft'" class="acp-stage-boundary">项目文件正在临时试做区中修改。命令隔离：{{ workflow.project_stage.process_backend === 'container' ? '容器' : workflow.project_stage.process_backend === 'host_compat' ? '宿主兼容模式（文件与网络未隔离）' : '容器未配置，命令暂停' }}。通过复核后由你决定是否应用。</p>
            <WorkflowCard :key="workflow.workflow_id" :workflow-id="workflow.workflow_id" :seed="workflow" @activity="scheduleHydrate" />
            <section class="acp-evidence-card wb-card">
              <div class="acp-surface-heading"><div><h3>实际工具调用</h3><p>这里只记录本轮工作流真实发生的调用；项目能力画像显示的是可用配置。</p></div><span>{{ actualToolCalls.length }} 条记录</span></div>
              <p v-if="!actualToolCalls.length" class="acp-muted">尚未调用工具。选择方案并批准执行计划后，这里会显示工具与 MCP 调用。</p>
              <details v-for="event in actualToolCalls" :key="event.seq || `${event.kind}-${event.ts}`" class="acp-tool-call"><summary><b>{{ timelineLabel[event.kind || ''] || event.kind }}</b><span>{{ String(event.tool || event.name || event.action || event.server || event.target || '查看详情') }}</span></summary><p>{{ timelineDetail(event) }}</p></details>
            </section>
          </div>
          <p v-else class="acp-muted acp-region-placeholder">选择新目标后，AI 的方案、工具调用和修改记录会显示在这里。</p>
          </section>
          <section class="acp-preview-region" aria-label="预览">
          <div class="acp-region-kicker acp-region-preview"><span>03</span><b>实时画面</b><small>与 AI 边看边聊</small><button class="acp-inspect-button" :disabled="visualInspecting" @click="inspectCurrentInterface()">{{ visualInspecting ? 'AI 正在查看…' : 'AI 浏览当前界面' }}</button><button class="acp-live-vision-button" :class="{ on: liveVisionActive && !screenSharing && !cameraSharing }" @click="toggleLiveVision()">{{ liveVisionActive && !screenSharing && !cameraSharing ? '停止观察项目' : '观察项目画面' }}</button><button class="acp-live-vision-button" :class="{ on: screenSharing }" @click="screenSharing ? stopLiveVision('屏幕共享已停止。') : startScreenShare()">{{ screenSharing ? '停止共享屏幕' : '共享屏幕给 AI' }}</button><button class="acp-live-vision-button" :class="{ on: cameraSharing }" @click="cameraSharing ? stopLiveVision('摄像头已停止。') : startCameraShare()">{{ cameraSharing ? '关闭摄像头' : '打开摄像头' }}</button></div>
          <div id="wb-cockpit-runtime-slot" v-show="stageMode === 'runtime'" class="acp-runtime-slot" />
          <div v-show="stageMode === 'outputs'" class="acp-preview-output">
          <div v-if="!workflow && !previewArtifacts.length" class="acp-stage-empty">
            <div class="acp-stage-glyph" aria-hidden="true">⌘</div>
            <h3>{{ workflow ? '项目画面在这里展开' : '从你想实现的效果开始' }}</h3>
            <p>网页、游戏、电路设计、桌面工具或其他项目，都可以在这里查看模型提交的真实结果。</p>
            <div class="acp-stage-chips"><span>可操作的预览</span><span>框选画面反馈</span><span>持续修改与验证</span></div>
            <div class="acp-stage-empty-actions">
              <button @click="openChat">与 AI 沟通下一步</button>
              <button @click="setStageMode('runtime')">打开引擎与场景预览</button>
            </div>
            <small>尚未取得网页预览时，AI 会尝试通过桌面视觉工具读取当前项目窗口；有预览后可直接附上实时画面。</small>
          </div>
          <div v-if="workflow" class="acp-stage-evidence">
        <div v-if="workflow.preview?.changes?.total" class="acp-change-summary">
          <b>{{ workflow.preview.changes.total }} 个文件有变化</b>
          <span>新增 {{ workflow.preview.changes.counts?.added || 0 }}</span>
          <span>修改 {{ workflow.preview.changes.counts?.modified || 0 }}</span>
          <span>删除 {{ workflow.preview.changes.counts?.deleted || 0 }}</span>
        </div>
        <div v-if="previewArtifacts.length" class="acp-previews">
          <details v-for="artifact in previewArtifacts" :key="artifact.id" class="acp-preview" :open="artifact.id === previewArtifacts[0]?.id">
            <summary><b>{{ previewKindLabel[artifact.kind] || artifact.kind }}</b><span>{{ artifact.label }}</span></summary>
            <p v-if="artifact.summary">{{ artifact.summary }}</p>
            <div v-if="(artifact.kind === 'interactive' || artifact.renderer === 'interactive') && previewSource(artifact)" class="acp-live-frame">
              <div class="acp-live-bar"><i></i><span>实时预览</span><small>拖动右下角调整大小</small><button class="acp-feedback-trigger acp-vision-trigger" :disabled="visualInspecting" @click.stop="inspectCurrentInterface(artifact)">{{ visualInspecting ? 'AI 查看中…' : 'AI 观察当前界面' }}</button><button class="acp-feedback-trigger acp-stream-trigger" :class="{ on: liveVisionActive && liveVisionArtifactId === artifact.id }" @click.stop="toggleLiveVision(artifact)">{{ liveVisionActive && liveVisionArtifactId === artifact.id ? '停止实时视觉' : '开启实时视觉' }}</button><button class="acp-feedback-trigger" @click.stop="reloadLive(artifact.id)">刷新</button><button class="acp-feedback-trigger" @click.stop="fullscreenLive(artifact.id)">全屏</button><button class="acp-feedback-trigger" @click.stop="toggleFeedback(artifact.id)">反馈给 AI</button><button class="acp-feedback-trigger" @click.stop="toggleAnnotation(artifact.id)">{{ annotationId === artifact.id ? '取消标注' : '标注区域' }}</button></div>
              <div class="acp-live-viewport" :style="liveFrameStyle()">
                <iframe :key="`${artifact.id}-${liveReload[artifact.id] || 0}`" :ref="(el) => setLiveFrameRef(artifact.id, el)" :src="previewSource(artifact)" :title="artifact.label" loading="lazy" allow="fullscreen" />
                <div v-if="annotationId === artifact.id" class="acp-annotation-layer" @pointerdown="beginAnnotation($event, artifact.id)" @pointermove="moveAnnotation($event, artifact.id)" @pointerup="endAnnotation" @pointercancel="endAnnotation" />
                <div v-if="regionOwner === artifact.id && feedbackRegion && feedbackRegion.width > 0" class="acp-annotation-box" :style="regionStyle()" />
                <button class="acp-resize-handle" title="拖动调整预览大小" @pointerdown.stop.prevent="startResize" />
              </div>
              <div v-if="feedbackOpenId === artifact.id" class="acp-feedback-pop" @click.stop>
                <div class="acp-feedback-head"><b>告诉 AI 需要怎么改</b><button @click="toggleFeedback(artifact.id)">×</button></div>
                <textarea v-model="feedbackText" rows="3" placeholder="例如：按钮太靠右，移动到画面下方并增大字号…" @keydown.ctrl.enter="sendFeedback(artifact)" />
                <div class="acp-feedback-foot"><small>Ctrl+Enter 发送</small><button :disabled="!feedbackText.trim() || feedbackSending" @click="sendFeedback(artifact)">{{ feedbackSending ? '保存并发送中…' : '发送并修改' }}</button></div>
              </div>
            </div>
            <img v-if="artifact.kind === 'image' && previewSource(artifact)" :src="previewSource(artifact)" :alt="artifact.label" />
            <video v-else-if="artifact.kind === 'video' && previewSource(artifact)" controls :src="previewSource(artifact)" />
            <audio v-else-if="artifact.kind === 'audio' && previewSource(artifact)" controls :src="previewSource(artifact)" />
            <pre v-if="artifact.before || artifact.after">{{ artifact.before ? `变更前：\n${artifact.before}\n\n` : '' }}{{ artifact.after ? `变更后：\n${artifact.after}` : '' }}</pre>
            <small v-if="artifact.path">路径：{{ artifact.path }}</small>
            <small v-if="artifact.change_summary">变更：{{ artifact.change_summary.before_lines || 0 }} 行 → {{ artifact.change_summary.after_lines || 0 }} 行，新增 {{ artifact.change_summary.added_lines || 0 }}，删除 {{ artifact.change_summary.removed_lines || 0 }}</small>
            <small v-if="artifact.evidence?.length">证据：{{ artifact.evidence.join('、') }}</small>
          </details>
        </div>
        <p v-else class="acp-muted">模型可以在执行过程中提交画面、文件、资源和验证证据。</p>
        <div v-if="feedbackHistory.length" class="acp-feedback-history">
          <h4>视觉反馈与效果对比</h4>
          <details v-for="item in feedbackHistory" :key="item.id" class="acp-feedback-record" :open="item.id === feedbackHistory[0]?.id">
            <summary><b>{{ item.label }}</b><span>{{ feedbackStatusLabel[item.status || 'pending'] || item.status }}</span></summary>
            <p>{{ item.note }}</p>
            <small>{{ new Date(item.sentAt).toLocaleString('zh-CN', { hour12: false }) }} · {{ item.screenshot ? '附带截图' : '文字反馈，未附截图' }}<span v-if="item.region"> · 已标注区域</span></small>
            <p v-if="item.detail" class="acp-muted">{{ item.detail }}</p>
            <div v-if="item.snapshots?.before || item.snapshots?.after" class="acp-snapshot-pair">
              <figure><figcaption>修改前</figcaption><img v-if="item.snapshots?.before" :src="agentApi.workflowFeedbackSnapshotUrl(workflow.workflow_id, item.id, 'before')" alt="反馈发送时的画面" /><small v-else>未取得截图</small></figure>
              <figure><figcaption>复验画面</figcaption><img v-if="item.snapshots?.after" :src="`${agentApi.workflowFeedbackSnapshotUrl(workflow.workflow_id, item.id, 'after')}&v=${encodeURIComponent(item.snapshots.after.captured_at)}`" alt="记录的当前效果" /><small v-else>AI 回复后会自动刷新并记录；也可手动记录当前效果</small></figure>
            </div>
            <div class="acp-feedback-actions">
              <button v-if="['pending', 'failed', 'needs_changes'].includes(item.status || 'pending')" :disabled="feedbackSending" @click="retryFeedback(item)">重新发送</button>
              <button :disabled="!!snapshotBusyId || !item.artifact_id || item.status === 'processing'" @click="captureAfter(item)">{{ snapshotBusyId === item.id ? '保存截图中…' : '记录当前效果' }}</button>
              <button v-if="item.status === 'awaiting_review' || item.snapshots?.after" @click="decideFeedback(item, true)">效果满意</button>
              <button v-if="item.status === 'awaiting_review' || item.snapshots?.after" @click="decideFeedback(item, false)">继续修改</button>
            </div>
          </details>
        </div>

          </div>
          </div>
          <p v-if="visualInspectStatus" class="acp-visual-inspect-status" role="status">{{ visualInspectStatus }}</p>
          <section v-if="liveVisionActive || liveVisionStatus || liveVisionObservation" class="acp-live-vision-panel" aria-live="polite">
<div><b><i :class="{ pulse: liveVisionActive && !liveVisionPaused }" />{{ screenSharing || cameraSharing ? '实时视频 · ' + liveVisionPhaseLabel : liveVisionActive ? 'AI 持续视觉观察中' : '实时视觉' }}</b><small v-if="screenSharing || cameraSharing">{{ liveVisionSource }} · 每 {{ (liveVisionMetrics.intervalMs / 1000).toFixed(1) }} 秒检查画面变化 · 理解延迟 {{ liveVisionMetrics.latencyMs === null ? '—' : (liveVisionMetrics.latencyMs / 1000).toFixed(1) + ' 秒' }} · 跳过静态画面 {{ liveStreamStaticSkipped }} · 丢弃旧帧 {{ liveVisionMetrics.dropped }}</small><small v-else>{{ liveVisionSource }}</small><button v-if="liveVisionActive && (screenSharing || cameraSharing)" :disabled="!liveStreamConnected || liveVisionPaused" @click="interruptLiveStream()">打断</button><button v-if="liveVisionActive" @click="toggleLiveVisionPause()">{{ liveVisionPaused ? '继续观察' : '暂停观察' }}</button><button v-if="liveVisionActive" @click="stopLiveVision('已停止持续视觉观察。')">停止</button></div>
            <div v-if="liveVisionActive && (screenSharing || cameraSharing)" class="acp-live-audio">
              <label>每分钟最多发送画面 <input v-model.number="liveVideoImageBudget" type="number" min="6" max="240" step="6" aria-label="每分钟最多发送画面" style="width: 5em"></label>
              <small>最近一分钟已发送 {{ liveVideoImagesUsed }} 张；关键变化会保留前后画面，更多画面会增加模型消耗。</small>
            </div>
            <div v-if="liveStreamConnected" class="acp-live-audio">
              <button :disabled="!liveVisionActive" @click="liveMicActive ? stopLiveMic() : startLiveMic()">{{ liveMicActive ? '闭麦' : '开麦对话' }}</button>
              <span><i :class="{ pulse: liveAiSpeaking }" />{{ liveAiSpeaking ? 'AI 正在说话，可直接开口打断' : (liveMicActive ? '正在听你说话' : '麦克风未开') }}</span>
              <small>{{ liveMicStatus || '说完会自动补一段静音让服务端收句；无需手动结束。' }}</small>
            </div>
            <video v-if="screenSharing || cameraSharing" ref="sharedVideo" class="acp-shared-video" autoplay muted playsinline aria-label="正在共享给 AI 的实时画面" />
            <div v-if="liveVisionActive && liveVisionSnapshotUrl" class="acp-focus-controls"><span>{{ liveVisionFocus ? '正在观察圈选区域' : '正在观察整个画面' }}</span><button v-if="!liveVisionSelecting" @click="beginFocusSelection()">{{ liveVisionFocus ? '重新圈选' : '圈选重点区域' }}</button><button v-if="liveVisionSelecting" @click="liveVisionSelecting = false; focusPointerCancel()">取消圈选</button><button v-if="liveVisionFocus" @click="clearFocusSelection()">恢复整屏</button></div>
            <div v-if="liveVisionActive && liveVisionSnapshotUrl" class="acp-focus-viewport"><div class="acp-focus-snapshot" :class="{ selecting: liveVisionSelecting }" @pointerdown="focusPointerDown" @pointermove="focusPointerMove" @pointerup="focusPointerUp" @pointercancel="focusPointerCancel"><img :src="liveVisionSnapshotUrl" alt="用于圈选重点区域的当前画面快照" draggable="false"><span v-if="liveVisionDraftFocus || liveVisionFocus" class="acp-focus-region" :style="focusRegionStyle(liveVisionDraftFocus || liveVisionFocus)" /></div></div>
            <p class="acp-live-vision-mode"><b>当前模式：{{ liveVisionModeLabel }}</b> · {{ liveVisionModeReason }}<span v-if="liveStreamCapabilities"> · 模型能力：{{ liveStreamCapabilities }}</span><span v-if="liveVisionMode === 'sampled-frames'"> 原生实时模型可用前，系统会继续保留此兼容路径。</span></p>
            <p>{{ liveVisionStatus }}<br>{{ screenSharing || cameraSharing ? '视频持续播放并传送最新画面；当前视觉模型按帧理解，分析可能滞后于视频。' : 'AI 根据画面变化采样分析，静止画面约 20 秒复查一次。' }}</p>
            <ul v-if="liveVisionLimitations.length" class="acp-live-vision-limitations"><li v-for="item in liveVisionLimitations" :key="item">{{ item }}</li></ul>
            <ol v-if="liveVisionTimeline.length" class="acp-vision-timeline"><li v-for="(item, index) in liveVisionTimeline" :key="`${item.at}-${index}`"><time>{{ item.at }}</time><span>{{ item.observation }}</span></li></ol>
            <ul v-if="liveStreamCaptions.length" class="acp-live-captions" aria-live="polite"><li v-for="(item, index) in liveStreamCaptions" :key="`cap-${index}`" :class="item.role === 'user' ? 'acp-caption-user' : 'acp-caption-assistant'"><b>{{ item.role === 'user' ? '你说' : 'AI' }}</b><span>{{ item.text }}</span><em v-if="!item.done">（正在回答…）</em></li></ul>
          </section>
          <details class="acp-visual-click-panel">
            <summary><b>高级：桌面控件操作</b><small>可选 · 仅操作当前项目已嵌入的窗口</small></summary>
            <div class="acp-visual-click-input"><input v-model="clickDescription" maxlength="160" placeholder="例如：画面右上角的保存按钮" :disabled="clickBusy" @keydown.enter="locateClickTarget()"><button :disabled="clickBusy || !clickDescription.trim()" @click="locateClickTarget()">{{ clickBusy && !clickProposal ? 'AI 定位中…' : 'AI 定位控件' }}</button></div>
            <div class="acp-visual-click-input"><input v-model="clickGoal" maxlength="240" placeholder="期望的可见效果（连续任务必填）" :disabled="clickBusy || (!!actionLoop && !['completed', 'stopped'].includes(actionLoop.phase))" /></div>
            <div v-if="!actionLoop || ['completed', 'stopped'].includes(actionLoop.phase)" class="acp-visual-click-input"><button :disabled="clickBusy || !clickDescription.trim() || !clickGoal.trim()" @click="startActionLoop()">按目标连续观察与复验</button><small>每一步都要你确认，AI 只自动定位下一步</small></div>
            <div v-if="actionLoop" class="acp-visual-click-verification" :data-status="actionLoop.phase === 'completed' ? 'met' : actionLoop.phase === 'needs_review' ? 'uncertain' : 'unmet'">
              <b>连续视觉任务 · {{ actionLoop.phase === 'completed' ? '已验收' : actionLoop.phase === 'stopped' ? '已停止' : actionLoop.phase === 'awaiting_user' ? '等你验收' : actionLoop.phase === 'needs_review' ? '需人工判断' : actionLoop.phase === 'awaiting_confirmation' ? '等你确认点击' : '定位中' }}</b>
              <p>目标：{{ actionLoop.goal }}。{{ actionLoop.note }}</p>
              <ol v-if="actionLoop.steps.length"><li v-for="(step, index) in actionLoop.steps" :key="`${step.at}-${index}`">{{ index + 1 }}. {{ step.target }} → {{ step.status === 'met' ? '可能达成' : step.status === 'unmet' ? '未达成' : '无法确认' }}：{{ step.evidence }}</li></ol>
              <div class="acp-visual-click-input">
                <button v-if="actionLoop.phase === 'awaiting_user'" :disabled="clickReviewSaving" @click="acceptActionLoop()">{{ clickReviewSaving ? '正在保存证据…' : '验收画面效果' }}</button>
                <button v-if="['needs_review', 'awaiting_user'].includes(actionLoop.phase)" :disabled="clickBusy || !clickDescription.trim()" @click="continueActionLoop()">按新控件继续</button>
                <button v-if="!['completed', 'stopped'].includes(actionLoop.phase)" :disabled="clickExecuting" @click="stopActionLoop()">停止本轮</button>
              </div>
            </div>
            <p v-if="clickError" class="acp-error" role="alert">{{ clickError }}</p>
            <p v-if="clickStatus" class="acp-muted" role="status">{{ clickStatus }}</p>
            <p v-if="clickFeedbackStatus" class="acp-muted" role="status">{{ clickFeedbackStatus }}</p>
            <div v-if="clickProposal" class="acp-visual-click-review">
              <div class="acp-visual-click-shot"><img :src="clickProposal.image" alt="待确认的桌面窗口截图"><span :style="clickTargetStyle()" /></div>
              <div class="acp-visual-click-detail"><b>{{ clickProposal.label }}</b><small>{{ clickProposal.evidence }}</small><small>窗口客户区坐标：{{ clickProposal.point.x }}, {{ clickProposal.point.y }} · 建议 {{ clickProposal.expires_in }} 秒内确认</small><div><button :disabled="clickBusy" @click="clickProposal = null">取消</button><button :disabled="clickBusy" @click="executeLocatedClick()">确认并点击一次</button></div></div>
            </div>
            <div v-if="clickVerification" class="acp-visual-click-verification" :data-status="clickVerification.status"><b>{{ clickVerification.status === 'met' ? '视觉复验：可能达成' : clickVerification.status === 'unmet' ? '视觉复验：尚未达成' : '视觉复验：无法确认' }}</b><p>{{ clickVerification.evidence }}</p><small v-if="clickVerification.next_target && !clickProposal">下一步建议：{{ clickVerification.next_target }}。请重新定位后确认。</small></div>
            <div v-if="clickBeforeImage || clickAfterImage" class="acp-visual-click-after"><b>操作前后画面</b><div><figure v-if="clickBeforeImage"><img :src="clickBeforeImage" alt="点击前的桌面窗口画面"><figcaption>点击前</figcaption></figure><figure v-if="clickAfterImage"><img :src="clickAfterImage" alt="点击后重新捕获的桌面窗口画面"><figcaption>点击后</figcaption></figure></div></div>
          </details>
          </section>
        </div>
        <footer class="acp-stage-footer"><span>{{ workflow ? `${doneCount}/${workflow.tasks?.length || 0} 个任务完成` : '当前项目 · 尚未启动新任务' }}</span><span>修改前审核 · 结果由你验收</span></footer>
      </section>
      <!-- 协作栏拉手：与舞台、协作栏同级的固定宽 flex 项；收起时只把协作栏宽度收到 0，
           拉手始终在，不遮挡任何内容 -->
      <button
        type="button"
        class="acp-collab-rail"
        :class="{ 'is-collapsed': collabCollapsed }"
        :title="collabCollapsed ? '展开「与 AI 协作」栏' : '收起「与 AI 协作」栏'"
        :aria-expanded="!collabCollapsed"
        aria-label="切换与 AI 协作栏"
        @click="collabCollapsed = !collabCollapsed"
      >
        <svg v-if="!collabCollapsed" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
          <path d="M4.5 2.5 L8 6 L4.5 9.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <template v-else>
          <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
            <path d="M7.5 2.5 L4 6 L7.5 9.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span class="acp-collab-rail-label">AI 协作</span>
        </template>
      </button>
      <aside class="acp-collaborator" aria-label="与 AI 协作">
        <div class="acp-collaborator-head"><b>与 AI 协作</b><span>{{ workflow ? statusLabel[workflow.status] || workflow.status : '准备开始' }}</span></div>
        <p v-if="nowActivity" class="acp-activity" :class="{ 'is-stale': nowActivity.stale }" role="status" aria-live="polite">
          <i class="acp-activity-dot" aria-hidden="true" />
          <span>{{ nowActivity.text }}</span>
          <span v-if="nowActivity.stale" class="acp-activity-wait">已等待 {{ nowActivity.waited }} 秒，仍在运行</span>
        </p>
        <section class="acp-goal-region" aria-label="目标">
        <div class="acp-controls">
        <div class="acp-region-kicker acp-region-goal"><span>01</span><b>目标</b><small>告诉 AI 要实现的结果</small></div>
        <div v-if="!workflow" class="acp-compose">
          <label for="acp-development-goal">新建持续开发任务</label>
          <textarea id="acp-development-goal" v-model="goal" rows="3" aria-label="开发目标" placeholder="填写整项开发目标；针对当前画面提意见，请用左侧截图反馈或下方聊天。" @keydown.ctrl.enter="start" />
          <button :disabled="!goal.trim() || busy" @click="start">{{ busy ? '正在启动…' : activeHistory ? '开始新目标…' : '开始自主开发' }}</button>
          <small v-if="activeHistory">有旧任务待处理。点击开始即可选择暂停旧任务并启动新目标。<button class="acp-inline-link" @click="restoreHistory(activeHistory.workflow_id)">继续旧任务</button></small>
          <small v-else>AI 提出方案与验收条件，审核后持续推进。需要结合画面时，点击预览区的「AI 观察当前界面」。</small>
          <button v-if="goal.trim()" class="acp-inline-link" @click="appEvents.emit('docmind:focus-chat', { q: goal, target: 'cockpit' })">只想讨论？带到下方聊天</button>
        </div>
        <div v-else class="acp-current-goal">
          <p>{{ workflow.request }}</p>
          <div><button v-if="!terminalStatuses.has(workflow.status)" :disabled="busy" @click="control('interrupt')">暂停任务</button><button v-if="workflow.status === 'interrupted'" :disabled="busy" @click="control('resume')">恢复任务</button><button @click="detailsOpen = !detailsOpen">计划与验收</button></div>
        </div>
        <details class="acp-model-drawer"><summary>自主执行模型</summary><CockpitModelBar /></details>
        <p v-if="workflow && chatHint" class="acp-chat-hint" role="status">{{ chatHint }}</p>
        <button v-if="workflow" class="acp-talk-button" @click="openChat">与 AI 沟通下一步</button>
        </div>
        </section>
        <div id="wb-cockpit-chat-slot" class="acp-chat-slot" />
        <section class="acp-acceptance-region" aria-label="验收">
          <div class="acp-region-kicker"><span>04</span><b>验收</b><small>检查证据并作最终决定</small></div>
          <p v-if="!workflow" class="acp-muted">开始任务后，验收条件和审批会显示在这里。</p>
          <template v-else>
            <details class="acp-approval-drawer" :open="pending || workflow.status === 'completed'"><summary>外部操作与 MCP 审核</summary><CockpitApprovalQueue :workflow="workflow" external-only @approve-plan="approvePlan" @final-decision="decide" @open-settings="openMcpSettings" /></details>
            <div class="acp-acceptance-report">
              <div class="acp-surface-heading"><div><h3>验收报告</h3><p>{{ hasExecution ? '验收条件、质量检查和模型复盘' : '执行后生成' }}</p></div><button :disabled="!hasExecution || reportBusy" @click="openAcceptanceReport">{{ reportBusy ? '读取中…' : acceptanceReport ? '刷新报告' : '查看报告' }}</button></div>
              <template v-if="acceptanceReport"><p>任务状态：{{ statusLabel[acceptanceReport.status || ''] || acceptanceReport.status }} · {{ acceptanceReport.generated_at || '时间未记录' }}</p><div v-for="item in acceptanceReport.acceptance?.items || []" :key="item.id" class="acp-report-row"><b>{{ item.required ? '必需' : '可选' }}</b><span>{{ item.statement }}</span><small>{{ item.method }} · {{ item.evidence.join('、') || '待补证据' }}</small></div><p v-if="acceptanceReport.review?.project">独立项目复核：{{ acceptanceReport.review.project.status }} · {{ acceptanceReport.review.project.message }} · {{ acceptanceReport.review.project.change_count || 0 }} 个文件变化</p><div v-for="(item, index) in acceptanceReport.review?.project?.tests || []" :key="index" class="acp-report-row"><b>{{ item.ok ? '通过' : '失败' }}</b><span>{{ item.command }}</span><small>{{ item.output || item.error || `退出码 ${item.exit_code}` }}</small></div><p v-if="acceptanceReport.evaluation">质量检查：{{ acceptanceReport.evaluation.passed ? '通过' : '未通过' }} · {{ Math.round((acceptanceReport.evaluation.score || 0) * 100) }}%</p><p v-if="acceptanceReport.self_review?.summary">模型复盘：{{ acceptanceReport.self_review.summary }}</p></template>
            </div>
          </template>
        </section>
      </aside>
    </div>
    <dialog ref="startDialog" class="acp-history-dialog wb-modal-shell" aria-label="开始新开发目标" @cancel="busy && $event.preventDefault()">
      <header><div><h3>开始新开发目标</h3><p>下面的旧任务尚未结束。暂停后保留记录与现有文件，可以在历史中恢复。</p></div><button :disabled="busy" aria-label="取消启动" @click="startDialog?.close()">×</button></header>
      <p v-for="item in replaceTasks" :key="item.workflow_id" class="acp-goal">{{ item.request }} · {{ statusLabel[item.status] || item.status }}</p>
      <p>新目标：{{ goal }}</p>
      <p v-if="error" class="acp-error" role="alert">{{ error }}</p>
      <div class="acp-history-tools"><button :disabled="busy" @click="startDialog?.close()">取消</button><button :disabled="busy || !goal.trim()" @click="beginStart(true)">{{ busy ? '正在暂停并启动…' : '暂停旧任务并开始新目标' }}</button></div>
    </dialog>
    <dialog ref="historyDialog" class="acp-history-dialog wb-modal-shell">
      <header><div><h3>本项目任务历史</h3><p>点击任务主动恢复；删除记录不会撤销项目文件修改。</p></div><button aria-label="关闭任务历史" @click="historyDialog?.close()">×</button></header>
      <div class="acp-history-tools"><span>最近 {{ history.length }} 条记录</span><button :disabled="historyBusy || !deletableHistory.length" @click="deleteHistory(deletableHistory)">清空列表中已结束的记录</button></div>
      <p v-if="!history.length" class="acp-muted">当前项目没有任务历史。</p>
      <p v-if="error" class="acp-error" role="alert">{{ error }}</p>
      <div v-for="item in history" :key="item.workflow_id" class="acp-history-row">
        <button class="acp-history" :disabled="historyBusy" @click="restoreHistory(item.workflow_id)">{{ item.request || item.workflow_id }}<small>{{ statusLabel[item.status] || item.status }} · {{ item.updated_at || item.created_at ? new Date(item.updated_at || item.created_at || '').toLocaleString('zh-CN', { hour12: false }) : '时间未记录' }}</small></button>
        <button v-if="!terminalStatuses.has(item.status)" :disabled="historyBusy" @click="pauseHistory(item)">暂停</button>
        <button class="acp-delete-history" :disabled="historyBusy || !terminalStatuses.has(item.status)" :title="terminalStatuses.has(item.status) ? '删除任务记录' : '请先打开任务并暂停执行'" @click="deleteHistory([item])">删除</button>
      </div>
    </dialog>
    <div v-if="workflow && detailsOpen" class="acp-grid acp-inspector">
      <section class="acp-panel"><h3>目标与验收</h3><p class="acp-goal">{{ workflow.request }}</p>
        <div v-if="workflow.selected_option" class="acp-option"><b>当前方案</b><span>{{ workflow.selected_option.title }}</span><small>{{ workflow.selected_option.summary }}</small></div>
        <div v-if="workflow.acceptance_contract?.items?.length" class="acp-list"><b>验收条件 · 第 {{ workflow.acceptance_contract.revision }} 版</b>
          <div v-for="item in workflow.acceptance_contract.items" :key="item.id" class="acp-row"><em>{{ item.required ? '必需' : '可选' }}</em><span>{{ item.statement }}<small>{{ item.method }} · {{ item.evidence.join('、') || '待补证据' }}</small></span></div>
          <p v-if="workflow.acceptance_contract.final_decision === 'accepted'" class="acp-ok">用户已验收通过</p>
          <p v-else-if="workflow.acceptance_contract.final_decision === 'rejected'" class="acp-error">用户未通过：{{ workflow.acceptance_contract.final_note }}</p>
        </div>
      </section>
      <section class="acp-panel"><div class="acp-panel-head"><h3>执行现场</h3><span>{{ doneCount }}/{{ workflow.tasks?.length || 0 }} 个任务完成</span></div>
        <div v-if="workflow.tasks?.length" class="acp-list"><div v-for="task in workflow.tasks" :key="String(task.id)" class="acp-row"><em>{{ String(taskResults[String(task.id)]?.status || task.status || '待执行') }}</em><span>{{ task.task }}<small>{{ taskResults[String(task.id)]?.conclusion || '' }}</small></span></div></div>
        <p v-else class="acp-muted">方案确认后显示模型的任务分工。</p>
        <h3>任务时间线</h3>
        <div v-if="timelineEvents.length" class="acp-timeline">
          <details v-for="event in timelineEvents" :key="event.seq || `${event.kind}-${event.ts}`" class="acp-timeline-item">
            <summary><span><b>{{ timelineLabel[event.kind || ''] || event.kind || '事件' }}</b><small v-if="event.task_id">{{ event.task_id }}</small></span><time>{{ event.ts }}</time></summary>
            <p>{{ timelineDetail(event) }}</p>
            <button v-if="timelineCanReplay(event)" :disabled="busy" @click.stop="applyRecovery({ id: 'timeline-retry', action: 'retry_failed', title: '从此任务继续', detail: '', task_ids: [String(event.task_id)] })">从此任务继续</button>
          </details>
        </div>
        <p v-else class="acp-muted">模型开始执行后，这里会记录计划、工具、审批、修改和验收事件。</p>
      </section>
      <aside class="acp-panel"><h3>验证与恢复</h3>
        <p v-if="pending">请在左侧「当前任务与审核」里选择方案、查看计划与验收条件。</p>
        <p v-else-if="workflow.status === 'completed'">执行已结束。请对照验收条件、任务结果和实际项目效果做最终验收。</p>
        <p v-else>模型正在使用当前项目内已授权的工具和能力推进任务。</p>
        <button @click="openChat">与 AI 沟通下一步</button>
        <div v-if="workflow.recovery?.status === 'required'" class="acp-recovery">
          <div class="acp-recovery-head"><b>失败后的下一步</b><span>需要审核</span></div>
          <p>{{ workflow.recovery.summary || '执行没有通过复核，请选择下一步。' }}</p>
          <small v-if="workflow.recovery.evidence?.uncertain_task_ids?.length" class="acp-recovery-warning">
            有 {{ workflow.recovery.evidence.uncertain_task_ids.length }} 个任务的副作用尚未确认，重试前请先核对外部状态。
          </small>
          <div v-for="option in (workflow.recovery.options || [])" :key="option.id || option.title" class="acp-recovery-option">
            <div><b>{{ option.title }}</b><small>{{ option.detail }}</small></div>
            <button :disabled="busy" @click="applyRecovery(option)">{{ option.action === 'retry_failed' ? '重试这些任务' : option.action === 'rollback' ? '恢复项目快照' : '查看并重新规划' }}</button>
          </div>
        </div>
        <div v-if="evaluation" class="acp-evaluation">
          <div class="acp-evaluation-head"><b>工作流质量评估</b><span :class="evaluation.passed ? 'acp-ok' : 'acp-error'">{{ evaluation.passed ? '通过' : '未通过' }}</span></div>
          <div class="acp-evaluation-score"><strong>{{ Math.round((evaluation.score ?? 0) * 100) }}%</strong><small>{{ evaluation.task_count || 0 }} 个任务 · {{ evaluation.steps || 0 }} 步 · 重规划 {{ evaluation.replans || 0 }} 次</small></div>
          <div class="acp-evaluation-metrics">
            <span>失败 {{ evaluation.metrics?.failed_tasks || 0 }}</span><span>阻塞 {{ evaluation.metrics?.blocked_tasks || 0 }}</span><span>不确定副作用 {{ evaluation.metrics?.idempotency_in_doubt || 0 }}</span>
          </div>
          <details class="acp-evaluation-checks"><summary>查看检查项</summary><div v-for="check in (evaluation.checks || [])" :key="check.name" :class="check.ok ? 'acp-ok' : 'acp-error'"><span>{{ check.ok ? '✓' : '!' }}</span>{{ check.name }}<small>{{ check.detail }}</small></div></details>
        </div>
        <div v-if="workflow.self_review?.status === 'ready'" class="acp-self-review">
          <div class="acp-self-review-head"><b>模型自我复盘</b><span>{{ Math.round((workflow.self_review.confidence || 0) * 100) }}% 可信度</span></div>
          <p>{{ workflow.self_review.summary }}</p>
          <details v-if="workflow.self_review.changed?.length"><summary>做了什么</summary><small v-for="item in workflow.self_review.changed" :key="item">✓ {{ item }}</small></details>
          <details v-if="workflow.self_review.verified?.length"><summary>验证了什么</summary><small v-for="item in workflow.self_review.verified" :key="item">✓ {{ item }}</small></details>
          <details v-if="workflow.self_review.uncertainties?.length" open><summary>仍不确定</summary><small v-for="item in workflow.self_review.uncertainties" :key="item">! {{ item }}</small></details>
          <details v-if="workflow.self_review.next_steps?.length"><summary>建议下一步</summary><small v-for="item in workflow.self_review.next_steps" :key="item">→ {{ item }}</small></details>
        </div>
        <div v-if="projectProfile" class="acp-profile">
          <div class="acp-profile-head"><b>项目能力画像</b><span>{{ projectProfile.kind || 'generic' }}</span></div>
          <small class="acp-muted">自动记录当前项目可复用的工具、连接器和验收方式</small>
          <div class="acp-profile-grid"><span>工具 <b>{{ projectProfile.tools?.length || 0 }}</b></span><span>MCP <b>{{ projectProfile.mcp?.length || 0 }}</b></span><span>运行命令 <b>{{ projectProfile.run_commands?.length || 0 }}</b></span><span>预览适配器 <b>{{ projectProfile.preview_adapters?.length || 0 }}</b></span></div>
          <details v-if="projectProfile.mcp?.length"><summary>MCP 连接器</summary><small v-for="item in projectProfile.mcp" :key="item.key">{{ profileMcpLabel(item) }}</small></details>
          <details v-if="projectProfile.tools?.length"><summary>可用工具</summary><small>{{ projectProfile.tools.join('、') }}</small></details>
          <details v-if="projectProfile.run_commands?.length"><summary>运行命令</summary><small v-for="item in projectProfile.run_commands" :key="item">{{ item }}</small></details>
          <details v-if="projectProfile.acceptance_methods?.length || projectProfile.acceptance_scripts?.length"><summary>验收方式</summary><small v-for="item in [...(projectProfile.acceptance_methods || []), ...(projectProfile.acceptance_scripts || [])]" :key="item">{{ item }}</small></details>
          <details v-if="projectProfile.preview_adapters?.length"><summary>预览适配器</summary><small>{{ projectProfile.preview_adapters.join('、') }}</small></details>
        </div>
        <div v-if="workflow.project_checkpoint?.id" class="acp-checkpoint">
          <b>执行前快照</b>
          <small>{{ workflow.project_checkpoint.file_count || 0 }} 个文件 · {{ Math.round((workflow.project_checkpoint.bytes || 0) / 1024) }} KB</small>
          <button :disabled="busy" @click="rollbackProject">恢复执行前版本</button>
        </div>
        <div v-if="workflow.capability_lease" class="acp-lease">
          <div class="acp-lease-head"><b>工具权限租约</b><span :class="leaseStatusLabel(workflow.capability_lease) === '当前有效' ? 'acp-ok' : 'acp-muted'">{{ leaseStatusLabel(workflow.capability_lease) }}</span></div>
          <small>{{ (workflow.capability_lease.capabilities || []).map(item => capabilityLabel[item] || item).join(' · ') }}</small>
          <small v-if="workflow.capability_lease.expires_at">到期：{{ new Date(workflow.capability_lease.expires_at).toLocaleString() }}</small>
        </div>
        <button v-if="workflow.status === 'executing'" :disabled="busy" @click="control('interrupt')">暂停工作流</button>
        <button v-if="workflow.status === 'interrupted'" :disabled="busy" @click="control('resume')">恢复工作流</button>

      </aside>
    </div>

  </div>
</template>

<style scoped>
.acp { flex: 1; min-width: 0; overflow: auto; padding: 22px; background: var(--bg-raised); color: var(--text); }
.acp-head,.acp-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.acp-head h2 { margin: 0; font-size: 19px; }.acp-head p,.acp-panel p { color: var(--text-muted); font-size: 12px; line-height: 1.6; }
.acp-vision-alert-banner { display: flex; align-items: center; gap: 10px; margin: 12px 0; padding: 10px; border: 1px solid rgba(230, 151, 45, .55); border-radius: 9px; background: rgba(230, 151, 45, .1); }
.acp-vision-alert-banner img { width: 92px; max-height: 72px; object-fit: contain; border-radius: 5px; background: #101521; }
.acp-vision-alert-banner div { flex: 1; min-width: 0; }
.acp-vision-alert-banner b { font-size: 12px; }.acp-vision-alert-banner p { margin: 3px 0; font-size: 11px; overflow-wrap: anywhere; }.acp-vision-alert-banner small { color: var(--text-muted); font-size: 10px; }
.acp-realtime-alert-banner { display: flex; align-items: center; gap: 10px; margin: 12px 0; padding: 10px; border: 1px dashed rgba(83, 170, 193, .7); border-radius: 9px; background: rgba(83, 170, 193, .09); }
.acp-realtime-alert-banner div { flex: 1; min-width: 0; }
.acp-realtime-alert-banner b { font-size: 12px; }.acp-realtime-alert-banner p { margin: 3px 0; font-size: 11px; overflow-wrap: anywhere; }.acp-realtime-alert-banner small { color: var(--text-muted); font-size: 10px; }
@media (max-width: 640px) { .acp-vision-alert-banner { flex-wrap: wrap; }.acp-vision-alert-banner div { flex-basis: calc(100% - 110px); } }
@media (max-width: 640px) { .acp-realtime-alert-banner { flex-wrap: wrap; }.acp-realtime-alert-banner div { flex-basis: calc(100% - 110px); } }
.acp-state { border: 1px solid var(--border); border-radius: 99px; padding: 5px 9px; font-size: 11px; white-space: nowrap; }
.acp-compose { display: flex; gap: 8px; margin: 16px 0; }.acp-compose textarea { flex: 1; resize: vertical; min-width: 0; border: 1px solid var(--border); border-radius: 9px; padding: 10px; color: var(--text); background: var(--bg-raised); font: inherit; }
button { border: 1px solid var(--border); background: var(--bg-raised); color: var(--text); border-radius: 7px; padding: 7px 10px; cursor: pointer; font: inherit; font-size: 12px; }
button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }button:disabled { opacity: .5; cursor: default; }.acp-compose button { background: var(--accent); color: white; border-color: var(--accent); }
.acp-grid { display: grid; grid-template-columns: minmax(220px,1fr) minmax(270px,1.3fr) minmax(200px,.8fr); gap: 12px; }.acp-panel { min-width: 0; border: 1px solid var(--border); border-radius: 11px; padding: 13px; display: flex; flex-direction: column; gap: 9px; }.acp-panel h3 { margin: 0; font-size: 13px; }.acp-panel-head span,.acp-muted { color: var(--text-faint); font-size: 11px; }.acp-goal { margin: 0; }.acp-option,.acp-list { display: grid; gap: 8px; }.acp-option small,.acp-row small { display: block; color: var(--text-faint); font-size: 11px; margin-top: 3px; }.acp-row { display: flex; gap: 7px; padding-top: 7px; border-top: 1px solid var(--border); font-size: 12px; line-height: 1.5; }.acp-row em { font-size: 10px; color: var(--accent); font-style: normal; flex: 0 0 auto; }.acp-events { display: grid; gap: 5px; }.acp-events div { display: flex; justify-content: space-between; gap: 7px; font-size: 11px; color: var(--text-muted); }.acp-events small { color: var(--text-faint); }.acp-history { text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.acp-history small { display: block; color: var(--text-faint); }.acp-error { color: var(--danger) !important; }.acp-ok { color: var(--green) !important; }.acp-empty { padding: 35px; text-align: center; color: var(--text-faint); font-size: 12px; }
.acp-timeline { display: grid; gap: 5px; max-height: 360px; overflow: auto; }
.acp-timeline-item { border: 1px solid var(--border); border-radius: 6px; padding: 6px 7px; font-size: 11px; }
.acp-timeline-item summary { display: flex; justify-content: space-between; gap: 8px; cursor: pointer; color: var(--text-muted); }
.acp-timeline-item summary span { min-width: 0; display: flex; gap: 6px; overflow: hidden; }
.acp-timeline-item summary b { color: var(--text); font-weight: 600; }
.acp-timeline-item summary small { color: var(--accent); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.acp-timeline-item time { flex: 0 0 auto; color: var(--text-faint); font-size: 10px; }
.acp-timeline-item p { margin: 6px 0; color: var(--text-muted); line-height: 1.45; }
.acp-timeline-item button { padding: 4px 7px; font-size: 10px; }
.acp-empty-profile { max-width: 420px; margin: 16px auto 0; text-align: left; }
.acp-previews { display: grid; gap: 6px; }.acp-preview { border: 1px solid var(--border); border-radius: 7px; padding: 6px 8px; font-size: 11px; }.acp-preview summary { display: flex; gap: 7px; cursor: pointer; }.acp-preview summary b { color: var(--accent); font-weight: 600; }.acp-preview p { margin: 7px 0; }.acp-preview img,.acp-preview video { display: block; max-width: 100%; max-height: 180px; margin-top: 7px; border-radius: 5px; }.acp-preview audio { width: 100%; margin-top: 7px; }.acp-preview pre { max-height: 150px; overflow: auto; padding: 7px; white-space: pre-wrap; background: var(--bg-hover); }.acp-preview small { display: block; color: var(--text-faint); margin-top: 4px; overflow-wrap: anywhere; }
.acp-change-summary { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; font-size: 11px; color: var(--text-faint); }.acp-change-summary b { color: var(--text); margin-right: 3px; }
.acp-live-frame { position: relative; margin-top: 8px; max-width: 100%; border: 1px solid var(--border); border-radius: 7px; overflow: visible; background: #101521; }.acp-live-bar { display: flex; align-items: center; gap: 6px; padding: 6px 8px; color: #dbe5f5; font-size: 10px; }.acp-live-bar i { width: 7px; height: 7px; border-radius: 50%; background: #43d17a; /*__KEEP0__*/; }.acp-live-bar small { margin-left: auto; color: #93a4bd; }.acp-live-bar button { padding: 3px 7px; border-color: #52698b; color: #dbe5f5; background: #26344b; font-size: 10px; }.acp-live-bar .acp-vision-trigger { border-color: #5a9cff; color: #d9ebff; background: rgba(45, 113, 214, .45); }.acp-live-bar .acp-vision-trigger:disabled { opacity: .7; cursor: wait; }.acp-live-viewport { position: relative; max-width: 100%; min-height: 220px; }.acp-live-viewport iframe { display: block; width: 100%; height: 100%; min-height: 220px; border: 0; border-radius: 0 0 7px 7px; background: #000; }.acp-annotation-layer { position: absolute; inset: 0; cursor: crosshair; background: rgba(24, 39, 64, .22); touch-action: none; }.acp-annotation-box { position: absolute; border: 2px solid #55a8ff; background: rgba(85,168,255,.18); pointer-events: none; }.acp-resize-handle { position: absolute; right: -1px; bottom: -1px; width: 18px; height: 18px; padding: 0; border: 0; border-radius: 0 0 7px 0; background: linear-gradient(135deg, transparent 45%, #8fa4c4 46%, #8fa4c4 53%, transparent 54%, transparent 64%, #8fa4c4 65%, #8fa4c4 72%, transparent 73%); cursor: nwse-resize; }.acp-feedback-pop { position: absolute; z-index: 4; top: 35px; right: 8px; width: min(300px, calc(100% - 16px)); padding: 9px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--bg-raised); box-shadow: 0 12px 32px rgba(0,0,0,.28); }.acp-feedback-head,.acp-feedback-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; }.acp-feedback-head button { border: 0; background: transparent; padding: 0 3px; font-size: 16px; }.acp-feedback-pop textarea { width: 100%; box-sizing: border-box; margin: 8px 0; resize: vertical; border: 1px solid var(--border); border-radius: 6px; padding: 7px; color: var(--text); background: var(--bg); font: inherit; font-size: 11px; }.acp-feedback-foot small { color: var(--text-faint); font-size: 10px; }.acp-feedback-foot button { padding: 5px 8px; background: var(--accent); color: #fff; border-color: var(--accent); }
.acp-visual-inspect-status { margin: 8px 0 0; padding: 7px 9px; border: 1px solid rgba(74, 139, 228, .35); border-radius: 7px; color: #315b8f; background: rgba(74, 139, 228, .08); font-size: 11px; }
.acp-inspect-button { margin-left: auto; padding: 4px 9px; border: 1px solid rgba(66, 125, 213, .5); border-radius: 999px; color: #2f65a9; background: rgba(66, 125, 213, .08); font-size: 11px; }
.acp-inspect-button:disabled { opacity: .65; cursor: wait; }
.acp-live-vision-button { padding: 4px 9px; border: 1px solid rgba(33, 161, 113, .48); border-radius: 999px; color: var(--green); background: rgba(33, 161, 113, .09); font-size: 11px; }
.acp-live-vision-button.on, .acp-live-bar .acp-stream-trigger.on { border-color: #d48748; color: #fff; background: #a85723; }
.acp-live-vision-panel { margin-top: 9px; padding: 10px; border: 1px solid rgba(33, 161, 113, .34); border-radius: 9px; background: rgba(33, 161, 113, .06); }
.acp-visual-click-panel { display: grid; gap: 8px; margin-top: 10px; padding: 11px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-raised); }
.acp-visual-click-panel > summary { display: flex; align-items: center; justify-content: space-between; gap: 8px; cursor: pointer; }
.acp-visual-click-panel:not([open]) > :not(summary) { display: none; }
.acp-visual-click-panel b { font-size: 12px; }.acp-visual-click-panel small { color: var(--text-muted); font-size: 10px; }
.acp-visual-click-input { display: flex; gap: 7px; }.acp-visual-click-input input { flex: 1; min-width: 0; padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-raised); color: var(--text); font: inherit; font-size: 12px; }
.acp-visual-click-review { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(170px, .8fr); gap: 10px; }
.acp-visual-click-shot { position: relative; align-self: start; }.acp-visual-click-shot img { display: block; width: 100%; border-radius: 6px; }.acp-visual-click-shot span { position: absolute; box-sizing: border-box; border: 2px solid #26bd8b; background: rgba(38, 189, 139, .18); pointer-events: none; }
.acp-visual-click-detail { display: grid; align-content: start; gap: 6px; }.acp-visual-click-detail div { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 5px; }
.acp-visual-click-verification { display: grid; gap: 5px; padding: 9px; border: 1px solid var(--border); border-radius: 7px; }.acp-visual-click-verification p { margin: 0; font-size: 12px; }.acp-visual-click-verification[data-status="met"] { border-color: #26bd8b; }.acp-visual-click-verification[data-status="unmet"] { border-color: #e9a33d; }
.acp-visual-click-verification ol { max-height: 130px; overflow: auto; margin: 2px 0; padding-left: 18px; font-size: 11px; color: var(--text-muted); }
.acp-visual-click-verification li + li { margin-top: 4px; }
.acp-visual-click-after { display: grid; gap: 6px; }.acp-visual-click-after > div { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }.acp-visual-click-after figure { margin: 0; min-width: 0; }.acp-visual-click-after img { display: block; width: 100%; border-radius: 6px; }.acp-visual-click-after figcaption { margin-top: 4px; color: var(--text-muted); font-size: 11px; }
@media (max-width: 680px) { .acp-visual-click-review { grid-template-columns: 1fr; }.acp-visual-click-input { flex-wrap: wrap; }.acp-visual-click-input input { flex-basis: 100%; } }
.acp-live-vision-panel > div { display: flex; align-items: center; gap: 9px; }
.acp-live-vision-panel b { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; }
.acp-live-vision-panel i { width: 7px; height: 7px; border-radius: 50%; background: var(--green); }
.acp-live-vision-panel i.pulse { animation: acp-vision-pulse 1.3s ease-in-out infinite; }
.acp-live-vision-panel small { color: var(--text-muted); font-size: 10px; }
.acp-live-vision-panel button { padding: 3px 9px; font-size: 10px; }
.acp-live-vision-panel button:first-of-type { margin-left: auto; }
.acp-live-vision-panel > .acp-focus-controls { margin-top: 9px; }
.acp-focus-controls span { color: var(--text-muted); font-size: 11px; }
.acp-live-vision-panel > .acp-focus-viewport { display: block; max-height: 320px; margin-top: 7px; overflow: auto; }
.acp-focus-snapshot { position: relative; width: 100%; max-width: 720px; }
.acp-focus-snapshot.selecting { cursor: crosshair; touch-action: none; }
.acp-focus-snapshot img { display: block; width: 100%; height: auto; user-select: none; }
.acp-focus-region { position: absolute; box-sizing: border-box; border: 2px solid var(--green); background: rgba(38, 189, 139, .16); pointer-events: none; }
.acp-live-vision-panel p { margin: 7px 0 0; color: var(--text-muted); font-size: 11px; }
.acp-live-vision-mode { padding: 7px 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-raised); }
.acp-live-audio { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 7px 0 0; padding: 7px 8px; border: 1px dashed var(--border); border-radius: 6px; font-size: 11px; color: var(--text-muted); }
.acp-live-audio span { display: inline-flex; align-items: center; gap: 5px; color: var(--text); }
.acp-live-audio small { flex-basis: 100%; }
.acp-live-vision-mode b { color: var(--text); }
.acp-live-vision-limitations { margin: 7px 0 0; padding-left: 18px; color: var(--text-muted); font-size: 10px; line-height: 1.5; }
.acp-live-captions { margin: 9px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; max-height: 200px; overflow: auto; }
.acp-live-captions li { display: flex; gap: 7px; align-items: baseline; padding: 6px 8px; border-radius: 6px; background: var(--bg-raised); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.acp-live-captions b { flex: none; font-size: 10px; color: var(--text-muted); }
.acp-live-captions em { font-style: normal; color: var(--text-muted); font-size: 10px; }
.acp-caption-assistant { border-left: 2px solid var(--green); }
.acp-caption-user { border-left: 2px solid var(--accent); }
.acp-live-vision-panel pre { margin: 9px 0 0; padding: 9px; max-height: 210px; overflow: auto; white-space: pre-wrap; font: inherit; font-size: 11px; line-height: 1.6; color: var(--text); background: var(--bg-raised); border-radius: 6px; }
.acp-vision-timeline { margin: 9px 0 0; padding: 0 0 0 20px; max-height: 230px; overflow: auto; color: var(--text); font-size: 11px; line-height: 1.6; }
.acp-vision-timeline li { padding: 5px 0; overflow-wrap: anywhere; }
.acp-vision-timeline time { display: block; color: var(--text-muted); font-size: 10px; }
.acp-shared-video { display: block; width: 100%; max-height: 360px; margin-top: 9px; border-radius: 7px; object-fit: contain; background: #101521; }
@keyframes acp-vision-pulse { 50% { opacity: .3; transform: scale(.7); } }
@media (prefers-reduced-motion: reduce) { .acp-live-vision-panel i.pulse { animation: none; } }
.acp-feedback-history { display: grid; gap: 7px; margin-top: 4px; padding-top: 9px; border-top: 1px solid var(--border); }.acp-feedback-history h4 { margin: 0; font-size: 12px; }.acp-feedback-record { display: flex; gap: 7px; padding: 7px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-hover); }.acp-feedback-dot { flex: 0 0 auto; width: 7px; height: 7px; margin-top: 4px; border-radius: 50%; background: var(--amber); }.acp-feedback-record b { font-size: 11px; }.acp-feedback-record p { margin: 3px 0; color: var(--text-muted); font-size: 11px; line-height: 1.45; }.acp-feedback-record small { color: var(--text-faint); font-size: 10px; }
@media (max-width: 1050px) { .acp-grid { grid-template-columns: 1fr 1fr; }.acp-grid aside { grid-column: 1 / -1; } }@media (max-width: 700px) { .acp-grid { grid-template-columns: 1fr; }.acp-grid aside { grid-column: auto; } }
.acp-live-bar { flex-wrap: wrap; }
.acp-live-viewport:fullscreen { width: 100vw !important; height: 100vh !important; background: #000; }
.acp-resize-handle { z-index: 2; touch-action: none; }
.acp-feedback-record { display: block; }
.acp-feedback-record summary { display: flex; justify-content: space-between; gap: 8px; cursor: pointer; font-size: 11px; }
.acp-feedback-record summary span { color: var(--accent); }
.acp-feedback-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.acp-feedback-actions button { padding: 5px 7px; font-size: 11px; }
.acp-profile { display: grid; gap: 6px; padding: 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-hover); font-size: 11px; }
.acp-profile-head { display: flex; justify-content: space-between; gap: 8px; }.acp-profile-head span { color: var(--accent); font-size: 10px; }
.acp-profile-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; color: var(--text-muted); }.acp-profile-grid b { color: var(--text); }
.acp-profile details { border-top: 1px solid var(--border); padding-top: 5px; }.acp-profile summary { cursor: pointer; color: var(--text-muted); }.acp-profile details small { display: block; margin-top: 4px; overflow-wrap: anywhere; color: var(--text-faint); }
.acp-self-review { display: grid; gap: 6px; padding: 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-hover); font-size: 11px; }
.acp-self-review-head { display: flex; justify-content: space-between; gap: 8px; }.acp-self-review-head span { color: var(--accent); font-size: 10px; }.acp-self-review p { margin: 0; color: var(--text-muted); line-height: 1.45; }
.acp-self-review details { border-top: 1px solid var(--border); padding-top: 5px; }.acp-self-review summary { cursor: pointer; color: var(--text-muted); }.acp-self-review details small { display: block; margin-top: 4px; line-height: 1.4; overflow-wrap: anywhere; }.acp-self-review details:nth-of-type(3) small { color: var(--amber); }
.acp-snapshot-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; }
.acp-snapshot-pair figure { margin: 0; padding: 6px; border: 1px solid var(--border); border-radius: 6px; min-width: 0; }
.acp-snapshot-pair figcaption { margin-bottom: 5px; font-size: 11px; color: var(--text-muted); }
.acp-snapshot-pair img { width: 100%; display: block; }
.acp-snapshot-pair small { display: block; }
.acp-checkpoint { display: grid; gap: 4px; padding: 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-hover); font-size: 11px; }
.acp-checkpoint small { color: var(--text-faint); }
.acp-checkpoint button { justify-self: start; color: var(--danger); }
.acp-lease { display: grid; gap: 4px; padding: 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-hover); font-size: 11px; }
.acp-lease-head { display: flex; justify-content: space-between; gap: 8px; }
.acp-lease small { color: var(--text-faint); line-height: 1.45; }
.acp-recovery { display: grid; gap: 7px; padding: 9px; border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border)); border-radius: 8px; background: color-mix(in srgb, var(--danger) 5%, var(--bg-hover)); font-size: 11px; }
.acp-recovery-head { display: flex; justify-content: space-between; gap: 8px; }
.acp-recovery-head span { color: var(--danger); font-size: 10px; }
.acp-recovery p { margin: 0; }
.acp-recovery-warning { color: var(--amber); line-height: 1.45; }
.acp-recovery-option { display: grid; gap: 6px; padding-top: 7px; border-top: 1px solid var(--border); }
.acp-recovery-option b { display: block; }
.acp-recovery-option small { display: block; margin-top: 3px; color: var(--text-faint); line-height: 1.45; }
.acp-recovery-option button { justify-self: start; padding: 5px 8px; font-size: 11px; }
.acp-evaluation { display: grid; gap: 7px; padding: 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-hover); font-size: 11px; }
.acp-evaluation-head { display: flex; justify-content: space-between; gap: 8px; }
.acp-evaluation-score { display: flex; align-items: baseline; gap: 8px; }
.acp-evaluation-score strong { font-size: 21px; color: var(--accent); }
.acp-evaluation-score small { color: var(--text-faint); }
.acp-evaluation-metrics { display: flex; flex-wrap: wrap; gap: 6px; color: var(--text-muted); }
.acp-evaluation-metrics span { padding: 3px 5px; border-radius: 4px; background: var(--bg-raised); }
.acp-evaluation-checks { border-top: 1px solid var(--border); padding-top: 6px; }
.acp-evaluation-checks summary { cursor: pointer; color: var(--text-muted); }
.acp-evaluation-checks div { display: grid; grid-template-columns: 14px 1fr; gap: 3px; margin-top: 5px; }
.acp-evaluation-checks small { grid-column: 2; color: var(--text-faint); line-height: 1.35; }
.acp { position: relative; display: flex; flex-direction: column; min-height: 0; padding: 16px 18px; background: var(--bg); }
.acp-head { flex: 0 0 auto; align-items: center; margin-bottom: 14px; }
.acp-head h2 { font-size: 18px; letter-spacing: -.3px; }
.acp-head p { margin: 5px 0 0; }
.acp-head-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 7px; }
.acp-head-actions button { font-size: 11px; }
.acp-flow-rail {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  flex: 0 0 auto;
  margin: 0 0 14px;
  padding: 8px;
  border: 1px solid color-mix(in srgb, var(--border-strong) 72%, white);
  border-radius: 10px;
  background: rgba(255,255,255,.66);
}
.acp-flow-step { display: flex; align-items: center; gap: 8px; min-width: 0; padding: 7px 9px; border-radius: 7px; color: var(--text-faint); }
.acp-flow-step.active { color: var(--accent); background: rgba(47,111,237,.09); }
.acp-flow-step.complete { color: var(--green); }
.acp-flow-index { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; flex: 0 0 22px; border: 1px solid currentColor; border-radius: 50%; font-size: 11px; font-weight: 700; }
.acp-flow-copy { display: grid; min-width: 0; gap: 2px; }
.acp-flow-copy b { font-size: 12px; }
.acp-flow-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; color: var(--text-faint); }
/* 舞台 / 拉手 / 协作栏为同级 flex 项：舞台占满剩余空间，拉手固定宽，
   收起时只把协作栏 flex-basis 收到 0（内容裁剪不卸载，状态保留） */
.acp-studio { flex: 1; display: flex; gap: 12px; min-height: 0; }
.acp-stage { flex: 1 1 0; display: flex; flex-direction: column; min-width: 0; min-height: 0; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: var(--bg-raised); }
.acp-collab-rail {
  flex: 0 0 18px; align-self: stretch; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 7px;
  padding: 0; border: 1px solid var(--border); border-radius: 9px;
  background: var(--bg-raised); color: var(--text-faint);
  cursor: pointer; transition: color .16s, border-color .16s, background .16s;
}
.acp-collab-rail:hover { color: var(--accent); border-color: var(--border-2); background: #fff; }
.acp-collab-rail-label { writing-mode: vertical-rl; letter-spacing: 2px; font-size: 10px; }
.acp-stage-bar { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--border); background: var(--bg-raised); }
.acp-stage-tabs { display: flex; gap: 4px; }
.acp-stage-tabs button { border-color: transparent; color: var(--text-faint); background: transparent; }
.acp-stage-tabs button.on { color: var(--accent); background: color-mix(in srgb, var(--accent) 9%, transparent); }
.acp-stage-bar > button { font-size: 11px; }
.acp-stage-content { flex: 1; min-height: 0; overflow: auto; padding: 20px; background: radial-gradient(circle, #cbd5e1 1px, transparent 1px) 0 0 / 22px 22px, var(--accent-soft); }
.acp-stage-content { display: grid; align-content: start; gap: 18px; }
.acp-execute-region, .acp-preview-region { display: grid; gap: 9px; min-width: 0; }
.acp-region-placeholder { margin: 0; padding: 16px; border: 1px dashed var(--border-strong); border-radius: var(--radius-lg); background: rgba(255,255,255,.55); }
.acp-workflow-surface { display: grid; gap: 12px; max-width: 980px; margin: 0 auto 18px; color: var(--text); }
.acp-region-kicker { display: flex; align-items: center; gap: 8px; min-height: 26px; padding: 5px 8px; border-left: 3px solid var(--accent); color: var(--text); background: color-mix(in srgb, var(--accent) 6%, transparent); border-radius: 0 7px 7px 0; }
.acp-region-kicker > span { color: var(--accent); font: 700 10px var(--font-mono); }
.acp-region-kicker b { font-size: 12px; }
.acp-region-kicker small { color: var(--text-faint); font-size: 10px; }
.acp-region-preview { margin: 12px 0 8px; }
.acp-region-goal { margin-bottom: 1px; }
.acp-region-inline { display: inline-flex; align-items: baseline; gap: 8px; }
.acp-region-inline b { color: var(--text); font-size: 11px; }
.acp-region-inline small { color: var(--text-faint); font-size: 10px; font-weight: 400; }
.acp-surface-heading { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
.acp-surface-heading h3 { margin: 0; font-size: 14px; }
.acp-surface-heading p { margin: 4px 0 0; color: var(--text-muted); font-size: 11px; line-height: 1.5; }
.acp-surface-heading > span { color: var(--accent); font-size: 11px; white-space: nowrap; }
.acp-evidence-card { padding: 13px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-raised); font-size: 12px; }
.acp-evidence-card > p { margin: 9px 0 0; }
.acp-evidence-card button { white-space: nowrap; }
.acp-tool-call { border-top: 1px solid var(--border); padding: 8px 0; }
.acp-tool-call:first-of-type { margin-top: 9px; }
.acp-tool-call summary { display: flex; gap: 10px; cursor: pointer; }
.acp-tool-call summary span { color: var(--text-muted); overflow-wrap: anywhere; }
.acp-tool-call p { margin: 6px 0; color: var(--text-muted); }
.acp-report-row { display: grid; grid-template-columns: 36px 1fr; gap: 3px 7px; margin-top: 9px; }
.acp-report-row b { color: var(--accent); }
.acp-report-row small { grid-column: 2; color: var(--text-muted); }
.acp-chat-hint { margin: 0; padding: 7px; color: var(--accent); background: var(--bg-hover); border-radius: 7px; font-size: 11px; }
.acp-talk-button { align-self: start; }
.acp-stage-empty { min-height: 400px; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 24px; }
.acp-stage-glyph { font-size: 40px; line-height: 1; color: var(--accent); padding: 20px; margin-bottom: 18px; border: 1px solid #dbe4f0; border-radius: 22px; background: #fff; }
.acp-stage-empty h3 { margin: 0 0 12px; font-size: 23px; font-weight: 600; color: #23314c; }
.acp-stage-empty p { max-width: 460px; font-size: 13px; color: #64748b; line-height: 1.8; }
.acp-stage-empty small { max-width: 480px; margin-top: 20px; font-size: 11px; line-height: 1.7; color: #8291a8; }
.acp-stage-chips { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin: 8px 0 18px; }
.acp-stage-chips span { padding: 5px 10px; border: 1px solid #dce4ee; border-radius: 99px; font-size: 11px; background: var(--bg); color: #526480; }
.acp-stage-empty-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }
.acp-stage-empty-actions button:first-child { background: var(--accent); color: #fff; border-color: var(--accent); }
.acp-stage-request { border-left: 3px solid var(--accent); padding-left: 12px; text-align: left; }
.acp-stage-footer { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; padding: 9px 13px; border-top: 1px solid var(--border); color: var(--text-faint); font-size: 10px; }
.acp-stage-evidence .acp-preview { padding: 12px; background: var(--bg-raised); }
.acp-stage-evidence .acp-preview img,.acp-stage-evidence .acp-preview video { max-height: 540px; }
.acp-stage-evidence .acp-preview summary { font-size: 12px; }
.acp-stage-evidence .acp-live-viewport { min-height: 320px; }
.acp-runtime-slot { display: flex; flex-direction: column; flex: 1; min-height: 0; min-width: 0; overflow: auto; }
.acp-collaborator { flex: 0 1 390px; width: 390px; min-width: 0; min-height: 0; display: flex; flex-direction: column; gap: 10px; border: 1px solid var(--border); border-radius: 12px; padding: 12px; background: var(--bg-raised); overflow: hidden; transition: flex-basis .2s var(--ease-standard), width .2s var(--ease-standard), padding .2s var(--ease-standard), border-width .2s var(--ease-standard); }
/* 收起：宽度归零、内边距/边框同步收起；内部对话台等组件不卸载，展开即恢复 */
.acp-studio.is-collab-collapsed .acp-collaborator { flex: 0 0 0; width: 0; padding: 0; border-left-width: 0; border-right-width: 0; gap: 0; }
.acp-studio.is-collab-collapsed .acp-collab-rail { background: var(--accent); border-color: var(--accent); color: #fff; }
.acp-goal-region, .acp-acceptance-region { display: flex; min-height: 0; flex-direction: column; gap: 8px; }
/* 目标区按内容占位、空间不足时可压缩（内部 controls 自行滚动），不再把对话槽挤出；
   验收区保持自然高度、不贪婪抢占剩余空间，内容过高时内部滚动 */
.acp-goal-region { flex: 0 1 auto; min-height: 0; }
.acp-acceptance-region { flex: 0 1 auto; min-height: 0; overflow: auto; padding-top: 8px; border-top: 1px solid var(--border); }
.acp-acceptance-report { display: grid; gap: 8px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-hover); font-size: 11px; }
.acp-acceptance-report > p { margin: 0; color: var(--text-muted); }
.acp-collaborator-head { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
.acp-collaborator-head span { font-size: 11px; color: var(--text-faint); }
  /* 「正在做什么」：静态状态只说得出「持续执行中」，而实测 98% 的时间是静默的
  （每次静默都是一次模型调用）。这一行给出当前动作，超过阈值再补一句已等待秒数。 */
  .acp-activity { display: flex; align-items: center; gap: 6px; margin: 0 0 4px; font-size: 11px; color: var(--text-dim); line-height: 1.5; }
  .acp-activity-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); flex: 0 0 auto; animation: acp-activity-pulse 1.4s ease-in-out infinite; }
  .acp-activity.is-stale { color: var(--text-faint); }
  .acp-activity-wait { color: var(--accent); }
  @keyframes acp-activity-pulse { 0%, 100% { opacity: 0.35; } 50% { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) { .acp-activity-dot { animation: none; opacity: 1; } }
.acp-collaborator-head { flex: 0 0 auto; }
/* 不再用百分比 max-height（父级是内容高 flex 项时会按自身内容折算成很小的
   滚动窗，焦点一进来就把目标输入框滚走）；空间充足时完整展示，不足时才滚动 */
.acp-controls { flex: 0 1 auto; min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 8px; }
.acp-collaborator .acp-inline-link { align-self: flex-start; padding: 2px 0; border: 0; background: transparent; color: var(--accent); font-size: 11px; }
.acp-collaborator .acp-compose { display: flex; flex-direction: column; margin: 0; gap: 7px; }
.acp-collaborator .acp-compose textarea { min-height: 65px; font-size: 12px; }
.acp-collaborator .acp-compose small { font-size: 10px; color: var(--text-faint); }
.acp-current-goal p { margin: 0 0 8px; max-height: 64px; overflow: auto; font-size: 12px; line-height: 1.6; }
.acp-current-goal > div { display: flex; gap: 6px; }
.acp-current-goal button { font-size: 11px; padding: 5px 8px; }
.acp-approval-drawer { max-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: 7px; padding: 8px; font-size: 11px; flex: 0 0 auto; }
.acp-approval-drawer > summary { cursor: pointer; color: var(--text-muted); }
.acp-model-drawer { font-size: 11px; color: var(--text-muted); }
.acp-model-drawer > summary { cursor: pointer; padding: 4px 0; }
/* 对话槽独占剩余空间并保留保底高度：内部输入栏不可压缩，
   没有保底时消息区会被挤成 0 高度（对话被“挡”住） */
.acp-chat-slot { display: flex; flex-direction: column; flex: 1 1 0; min-height: 240px; min-width: 0; }
/* 对话台折叠时槽位收成 33px，把空间还给目标区/验收区 */
.acp-chat-slot:has(.cd-dock.cd-collapsed) { flex: 0 0 auto; min-height: 0; }
.acp-chat-slot :deep(.cd-dock) { flex: 1; width: 100%; min-height: 0; height: 100%; max-height: none; border-radius: 8px; border: 1px solid var(--border); }
.acp-chat-slot :deep(.cd-dock.cd-collapsed) { flex: 0 0 auto; height: 33px; min-height: 0; }
.acp-chat-slot :deep(.cd-head) { flex: 0 0 auto; flex-wrap: wrap; height: auto; min-height: 34px; gap: 5px; padding: 7px; }
.acp-chat-slot :deep(.cd-hint) { display: none; }
.acp-chat-slot :deep(.cd-body) { overflow: auto; }
.acp-chat-slot :deep(.cd-orphan), .acp-chat-slot :deep(.cd-inputbar) { flex-shrink: 0; }
.acp-chat-slot :deep(.cd-head-toggle) { min-width: 100px; }
/* 消息区保底可见高度，避免被不可压缩的输入栏吃成 0 */
.acp-chat-slot :deep(.cd-body) { padding: 8px 10px; min-height: 44px; }
/* 开发舱槽位专用紧凑密度：窄栏里工具芯片换行是输入区最大的纵向开销，
   只在槽位内覆写，不影响问答页与工作台底部对话台 */
.acp-chat-slot :deep(.cd-msg) { margin-bottom: 8px; }
.acp-chat-slot :deep(.cd-answer) { font-size: 12px; line-height: 1.65; }
.acp-chat-slot :deep(.cd-user-bubble) { font-size: 12px; padding: 5px 9px; }
.acp-chat-slot :deep(.cd-inputbar) { gap: 5px; padding: 6px 8px 7px; }
.acp-chat-slot :deep(.cd-tools) { gap: 4px; }
.acp-chat-slot :deep(.cd-chip) { height: 21px; padding: 0 7px; font-size: 10.5px; border-radius: 11px; }
.acp-chat-slot :deep(.cd-input) { padding: 6px 9px; font-size: 12px; }
.acp-chat-slot :deep(.cd-send) { height: 28px; }
.acp-inspector { position: absolute; inset: 82px 18px 18px; z-index: 8; overflow: auto; padding: 14px; background: var(--bg-raised); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 14px 40px #15264026; }
.acp-error-banner { padding: 8px 10px; margin: 0 0 10px; font-size: 12px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-raised); }
.acp-active-banner { display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 10px; padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-raised); font-size: 12px; }
.acp-active-banner span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.acp-active-banner button { flex-shrink: 0; }
.acp-history-dialog { width: min(700px, calc(100vw - 48px)); max-height: 78vh; overflow: auto; padding: 20px; border: 1px solid var(--border); border-radius: 14px; color: var(--text); background: var(--bg-raised); box-shadow: 0 20px 80px #0f172a40; }
.acp-history-dialog::backdrop { background: #0f172a55; }
.acp-history-dialog header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.acp-history-dialog h3 { margin: 0; font-size: 16px; }
.acp-history-dialog header p { margin: 7px 0; color: var(--text-faint); font-size: 12px; }
.acp-history-tools { display: flex; align-items: center; justify-content: space-between; padding: 12px 0; gap: 10px; font-size: 11px; color: var(--text-faint); }
.acp-history-row { display: flex; gap: 8px; align-items: center; padding: 8px 0; border-top: 1px solid var(--border); }
.acp-history-row .acp-history { flex: 1; min-width: 0; padding: 10px; line-height: 1.7; }
.acp-history-row .acp-history small { font-size: 11px; }
.acp-delete-history { color: var(--danger); }
@media (max-width: 1100px) { .acp-collaborator { flex-basis: 350px; width: 350px; }.acp-head { align-items: flex-start; }.acp-head-actions { max-width: 55%; }.acp-stage-content { padding: 12px; }.acp-stage-empty { padding: 16px; }.acp-stage-empty h3 { font-size: 20px; } }
@media (max-width: 760px) { .acp { padding: 12px; }.acp-head { flex-direction: column; }.acp-head-actions { max-width: 100%; justify-content: flex-start; }.acp-flow-rail { grid-template-columns: repeat(2, minmax(0, 1fr)); }.acp-studio { flex: none; flex-direction: column; }.acp-stage { flex: none; min-height: 460px; }.acp-collab-rail { display: none; }.acp-collaborator, .acp-studio.is-collab-collapsed .acp-collaborator { flex: 0 0 auto; width: auto; min-height: 540px; padding: 12px; border-width: 1px; }.acp-stage-bar { flex-wrap: wrap; }.acp-inspector { inset: 120px 12px 12px; }.acp-stage-empty { min-height: 300px; }.acp-history-tools { align-items: flex-start; flex-direction: column; } }

/* ---------- 开发舱视觉细化 ---------- */
.acp {
  background:
    radial-gradient(circle at 16% 0%, rgba(122,90,248,.08), transparent 34%),
    linear-gradient(135deg, #f2f5fa 0%, #edf1f7 100%);
  scrollbar-gutter: stable;
}
.acp-head { animation: acp-rise .3s var(--ease-spring) both; }
.acp-head h2 { letter-spacing: -.4px; }
.acp-state { background: rgba(255,255,255,.68); box-shadow: 0 2px 8px rgba(35,52,84,.05); }
.acp-studio { gap: 16px; }
.acp-stage,
.acp-collaborator {
  border-color: color-mix(in srgb, var(--border-strong) 72%, white);
  box-shadow: 0 10px 30px rgba(35,52,84,.07);
  animation: acp-rise .34s var(--ease-spring) both;
}
.acp-collaborator { animation-delay: .04s; }
.acp-stage-bar { background: rgba(255,255,255,.82); backdrop-filter: blur(10px); }
.acp-stage-tabs button { position: relative; transition: color .16s, background .16s, transform .16s var(--ease-spring); }
.acp-stage-tabs button:hover { transform: translateY(-1px); }
.acp-stage-tabs button.on::after {
  content: ''; position: absolute; left: 10px; right: 10px; bottom: 1px;
  height: 2px; border-radius: 2px; background: var(--accent);
  animation: acp-tab-in .2s var(--ease-spring) both;
}
.acp-stage-content { background: radial-gradient(circle, #c5cfde 1px, transparent 1px) 0 0 / 22px 22px, var(--accent-soft); }
.acp-workflow-surface { animation: acp-content-in .34s .06s var(--ease-spring) both; }
.acp-evidence-card,
.acp-preview,
.acp-profile,
.acp-self-review,
.acp-evaluation,
.acp-checkpoint,
.acp-lease {
  box-shadow: 0 4px 14px rgba(35,52,84,.045);
  transition: border-color .16s var(--ease-standard), box-shadow .18s var(--ease-spring), transform .18s var(--ease-spring);
}
.acp-evidence-card:hover,
.acp-preview:hover,
.acp-profile:hover,
.acp-self-review:hover,
.acp-evaluation:hover { border-color: var(--border-2); box-shadow: var(--shadow-hover); transform: translateY(-1px); }
.acp-stage-empty { animation: acp-content-in .4s var(--ease-spring) both; }
.acp-stage-glyph { box-shadow: 0 10px 24px rgba(47,111,237,.11); animation: acp-float 4s ease-in-out infinite; }
.acp-stage-chips span { transition: transform .16s var(--ease-spring), border-color .16s, background .16s; }
.acp-stage-chips span:hover { transform: translateY(-2px); border-color: var(--border-2); background: #fff; }
.acp-collaborator-head { padding-bottom: 9px; border-bottom: 1px solid var(--border); }
.acp-collaborator .acp-compose textarea { background: rgba(247,249,253,.86); transition: border-color .16s, box-shadow .16s, background .16s; }
.acp-collaborator .acp-compose textarea:focus { background: #fff; box-shadow: 0 0 0 3px rgba(47,111,237,.11); }
.acp-talk-button,
.acp-compose button { box-shadow: 0 5px 12px rgba(47,111,237,.18); }
.acp-talk-button:hover:not(:disabled),
.acp-compose button:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 8px 16px rgba(47,111,237,.23); }
.acp-approval-drawer { background: rgba(247,249,253,.72); }
.acp-chat-slot :deep(.cd-dock) { box-shadow: inset 0 1px 0 rgba(255,255,255,.7), 0 4px 14px rgba(35,52,84,.05); }
.acp-feedback-pop { animation: acp-pop .2s var(--ease-spring) both; }
.acp-live-frame { /*__KEEP13__*/; }

@keyframes acp-rise {
  from { opacity: 0; transform: translateY(7px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes acp-content-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes acp-tab-in {
  from { opacity: 0; transform: scaleX(.45); }
  to { opacity: 1; transform: scaleX(1); }
}
@keyframes acp-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-4px); }
}
@keyframes acp-pop {
  from { opacity: 0; transform: translateY(-4px) scale(.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
@media (prefers-reduced-motion: reduce) {
  .acp-stage-glyph { animation: none; }
}
</style>
