<script setup lang="ts">
// P0 底部 AI 对话台：全局代码问答/定位入口（ReAct agent，SSE）。
// - 答案中的文件引用渲染为可点击卡片：跳转代码行 + 文件树展开闪烁 + 分区高亮；
// - 引擎连接 / 会话历史 / 工作流历史均为独立弹层组件（components/*Popover.vue）；
// - 模型配置、审批门、孤儿工作流恢复、SSE 流式管线在 composables/ 与问答页共用。
import { nextTick, ref, watch, onMounted, onBeforeUnmount, computed, defineAsyncComponent } from 'vue'
// 直接依赖对话框域，避免经 workbench 聚合桶拖入编辑器链
import { askConfirm, askAlert } from '../composables/dialogs'
import { aiApi, voiceApi, agentApi, visionApi, contextApi, harnessApi, modelApi, getSessionId, getProjectId, setSessionId, startNewSession, startTabProbe } from '../api'
import type { ContextUsage, SessionInfo } from '../api'
import type { SseEvent } from '../api'
import { appEvents } from '../eventBus'
import type { ChatUiContext } from '../previewFeedback'
import type { FocusChatDetail } from '../eventBus'
import { demoMode } from '../composables/demo'
import { useModelConfig } from '../composables/useModelConfig'
import { useWorkflowGate } from '../composables/useWorkflowGate'
import { useOrphanWorkflow } from '../composables/useOrphanWorkflow'
import { useChatStream } from '../composables/useChatStream'
import type { ChatActivityKind, ChatMsg } from '../composables/chat-types'
import { useAutoScroll } from '../composables/useAutoScroll'
import { useMessageRender } from '../composables/useMessageRender'
// 设置弹窗低频且体积大（117KB）：异步组件 + v-if，打开时才拉取
const ModelSettingsDialog = defineAsyncComponent(() => import('./ModelSettingsDialog.vue'))
const ModelSwitcherPopover = defineAsyncComponent(() => import('./ModelSwitcherPopover.vue'))
import EngineConnectPopover from './EngineConnectPopover.vue'
import SessionHistoryPopover from './SessionHistoryPopover.vue'
import WorkflowHistoryPopover from './WorkflowHistoryPopover.vue'
// 工作流卡片仅在工作流消息中渲染：异步按需加载
const WorkflowCard = defineAsyncComponent(() => import('./WorkflowCard.vue'))
import WorkflowMembersDock from './WorkflowMembersDock.vue'
import ChatComposer from './ChatComposer.vue'
import type { WorkflowSummary } from '../api'
import type { PreviewFeedbackRequest } from '../previewFeedback'

const props = withDefaults(defineProps<{ scope?: 'default' | 'cockpit' }>(), { scope: 'default' })
const voiceActive = ref(false)
// 语音是自主开发舱的协作通道；普通项目对话台不显示也不启动语音资源。
const voiceSupported = ref(props.scope === 'cockpit' && typeof window !== 'undefined' && ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window))
const voiceTtsConfigured = ref(false)
const voiceForwardQueue = ref<string[]>([])
const voiceCompanionBusy = ref(false)
const voiceMainResult = ref('')
let liveVisionFrame: Blob | null = null
let liveVisionTimeline: { at: string; observation: string }[] = []
let voiceRecognition: any = null
let voiceRecorder: MediaRecorder | null = null
let voiceAudio: HTMLAudioElement | null = null
const voiceAudioQueue: string[] = []
const spokenChars = new Map<number, number>()
function shouldForwardVoice(text: string): boolean {
  return /这里不对|不对|有问题|改一下|修改一下|继续|重做|不符合|错了|不正确/.test(text)
}
async function speakVoiceText(text: string) {
  const chunk = String(text || '').trim()
  if (!chunk) return
  if (voiceTtsConfigured.value) {
    try {
      const blob = await voiceApi.speech(chunk)
      voiceAudioQueue.push(URL.createObjectURL(blob))
      if (!voiceAudio) {
        voiceAudio = new Audio()
        voiceAudio.onended = () => { if (voiceAudioQueue.length) { voiceAudio!.src = voiceAudioQueue.shift()!; void voiceAudio!.play() } else { voiceAudio = null } }
        voiceAudio.onerror = () => voiceAudio?.onended?.(new Event('ended'))
        voiceAudio.src = voiceAudioQueue.shift()!
        void voiceAudio.play()
      }
      return
    } catch { /* 服务端 TTS 失败时回退浏览器语音 */ }
  }
  if (typeof window !== 'undefined' && window.speechSynthesis) {
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(chunk))
  }
}
async function companionVoice(text: string) {
  if (voiceCompanionBusy.value) return
  voiceCompanionBusy.value = true
  try {
    const visualContext = liveVisionTimeline.length
      ? `\n最近画面观察：${liveVisionTimeline.slice(-3).map(item => `${item.at} ${item.observation.slice(-500)}`).join('；')}`
      : ''
    const r = await voiceApi.dialogue(text, voiceMainResult.value + visualContext, false)
    await speakVoiceText(r.text)
  } catch {
    await speakVoiceText('我先记下你的问题，主 Agent 完成本轮后会继续处理。')
  } finally { voiceCompanionBusy.value = false }
}
function toggleVoice() {
  if (props.scope !== 'cockpit' || !voiceSupported.value) return
  if (voiceRecognition) { voiceRecognition.stop(); voiceRecognition = null; voiceActive.value = false; return }
  const Recognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
  if (!Recognition && typeof navigator !== 'undefined' && navigator.mediaDevices?.getUserMedia) {
    void navigator.mediaDevices.getUserMedia({ audio: true }).then(stream => {
      const chunks: Blob[] = []
      voiceRecorder = new MediaRecorder(stream)
      voiceRecorder.ondataavailable = event => { if (event.data?.size) chunks.push(event.data) }
      voiceRecorder.onstop = async () => {
        stream.getTracks().forEach(track => track.stop())
        voiceRecorder = null; voiceRecognition = null; voiceActive.value = false
        try {
          const phrase = await voiceApi.transcribe(new Blob(chunks, { type: 'audio/webm' }), 'zh')
          if (phrase) { if (sending.value && !shouldForwardVoice(phrase)) await companionVoice(phrase); else if (sending.value) voiceForwardQueue.value.push(phrase); else void send(phrase) }
        } catch { await speakVoiceText('语音识别失败，请重试。') }
      }
      voiceRecorder.start(); voiceRecognition = voiceRecorder; voiceActive.value = true
    }).catch(() => { voiceActive.value = false })
    return
  }
  const rec = new Recognition(); rec.lang = 'zh-CN'; rec.continuous = true; rec.interimResults = true
  rec.onresult = (event: any) => {
    let interim = ''
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const phrase = String(event.results[i][0].transcript || '').trim()
      if (event.results[i].isFinal && phrase) {
        if (sending.value) {
          if (shouldForwardVoice(phrase)) {
            voiceForwardQueue.value.push(phrase)
            void speakVoiceText('已转交主 Agent。')
          } else void companionVoice(phrase)
        } else void send(phrase)
      }
      else interim += phrase
    }
    if (interim) input.value = interim
  }
  rec.onerror = () => { voiceActive.value = false; voiceRecognition = null }
  rec.onend = () => { voiceActive.value = false; voiceRecognition = null }
  rec.start(); voiceRecognition = rec; voiceActive.value = true
}
function speakDelta(turn: ChatMsg) {
  voiceMainResult.value = turn.text.slice(-6000)
  if (props.scope !== 'cockpit' || !voiceActive.value) return
  const previous = spokenChars.get(turn.id) || 0
  const text = turn.text.slice(previous)
  const boundary = Math.max(text.lastIndexOf('。'), text.lastIndexOf('！'), text.lastIndexOf('？'), text.lastIndexOf('.'), text.lastIndexOf('!'), text.lastIndexOf('?'))
  if (boundary < 0) return
  const chunk = text.slice(0, boundary + 1).trim()
  if (!chunk) return
  spokenChars.set(turn.id, previous + boundary + 1)
  void speakVoiceText(chunk)
}
const scopedSessionKey = () => `docmind.cockpitSession:${getProjectId()}`
const fallbackCockpitSessions = new Map<string, string>()
function makeCockpitSessionId(): string {
  const id = globalThis.crypto?.randomUUID?.().replace(/-/g, '') || `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`
  return `dev-${id.slice(0, 32)}`
}
function currentSessionId(): string {
  if (props.scope === 'default') return getSessionId()
  try {
    const saved = sessionStorage.getItem(scopedSessionKey())
    if (saved?.startsWith('dev-')) return saved
    const created = makeCockpitSessionId()
    sessionStorage.setItem(scopedSessionKey(), created)
    return created
  } catch {
    const key = scopedSessionKey()
    if (!fallbackCockpitSessions.has(key)) fallbackCockpitSessions.set(key, makeCockpitSessionId())
    return fallbackCockpitSessions.get(key)!
  }
}
async function selectSession(id: string): Promise<boolean> {
  if (props.scope === 'default') return setSessionId(id)
  if (!id.startsWith('dev-')) return false
  try { sessionStorage.setItem(scopedSessionKey(), id); return true }
  catch { fallbackCockpitSessions.set(scopedSessionKey(), id); return true }
}
async function createSession(): Promise<string> {
  if (props.scope === 'default') return startNewSession()
  const id = makeCockpitSessionId()
  return await selectSession(id) ? id : ''
}

// ---------------------------------------------------------------- 对话状态
// ChatMsg 契约在 composables/chat-types.ts，与问答首页共用同一份模型

/** 活跃工作流团队（左下角成员抽屉数据源，终态自动移除） */
interface WfTeam {
  workflowId: string
  active: boolean
  members: Array<{ id: string; label: string; role: string; status: string; task: string }>
}
const wfTeams = ref<WfTeam[]>([])
const wfActiveTeam = computed(() => {
  const live = [...wfTeams.value].reverse().find(t => t.active && t.members.length)
  return live || null
})
// 所有未终结的工作流（含停在方案门、成员尚为空的）——并发拦截与离开确认都以此为准
const activeWorkflowIds = computed(() =>
  wfTeams.value.filter(t => t.active).map(t => t.workflowId))
// 审批门互斥与离开确认抽到 useWorkflowGate（问答页共用）：门开期间锁操作区，
// 离开对话时确认并中断未终结工作流，避免卡片卸载后后端工作流停在门里成为孤儿。
const { gateBlocking, onWfGate, resetGates, confirmLeaveWorkflows } = useWorkflowGate(
  () => activeWorkflowIds.value)
function onWfTeam(payload: WfTeam) {
  const rest = wfTeams.value.filter(t => t.workflowId !== payload.workflowId)
  if (payload.active) rest.push(payload)
  wfTeams.value = rest
  // 工作流终结：给对应回合盖结束时间，让秒表定格（计时钟也随之停走）
  if (!payload.active) {
    const turn = messages.value.find(m => m.workflow?.workflowId === payload.workflowId)
    if (turn && turn.startedAt && !turn.finishedAt) turn.finishedAt = Date.now()
    if (orphanWf.value?.workflow_id === payload.workflowId) orphanWf.value = null
  }
}

// 自动滚动（贴底跟随/上滚暂停/rAF 合批）抽到 useAutoScroll
const {
  scroller, stickToBottom, onScroll, onWheel,
  followBottom, scrollToBottom, scheduleFollow, resumeAutoScroll,
} = useAutoScroll()

// ---------------------------------------------------------------- 孤儿工作流恢复
// 探测/挂回/中断抽到 useOrphanWorkflow（问答页共用）：页面刷新后卡片丢失但后端
// 同项目互斥仍存活时，给出恢复条避免新请求被拒且无操作入口。
const {
  orphanWf, orphanBusy, orphanError, orphanStatusLabel,
  detectOrphanWorkflow, reattachOrphan, interruptOrphan,
} = useOrphanWorkflow({
  hasWorkflowCard: (id) => messages.value.some(m => m.workflow?.workflowId === id),
  appendWorkflowCard: (wf) => {
    messages.value.push({
      id: msgSeq++, role: 'assistant', text: '', status: 'done',
      trace: [], reasoning: '', notices: [], plan: [],
      workflow: { workflowId: wf.workflow_id, seed: wf },
      startedAt: Date.now(),
    })
  },
  onReattached: () => { stickToBottom.value = true; void scrollToBottom() },
})
async function focusWfMember(taskId: string) {
  const team = wfActiveTeam.value
  if (!team) return
  // 面板折叠时先展开（卡片常驻未卸载），下一帧再定位，否则 scrollIntoView
  // 落在被 overflow:hidden 裁掉的容器里用户看不到
  if (collapsed.value) {
    collapsed.value = false
    await nextTick()
  }
  appEvents.emit('docmind:wf-focus-member', { workflowId: team.workflowId, taskId })
}

interface InterruptedRecovery {
  user: string
  assistant: string
  trace: ChatMsg['trace']
  reasoning: string
  plan: string[]
  startedAt?: number
}

let msgSeq = 1
const messages = ref<ChatMsg[]>([])
let chatProject = getProjectId(), chatSession = currentSessionId(), chatEpoch = 0
const draftKey = () => 'docmind.workbenchChatDraft:' + chatProject + ':' + chatSession
const recoveryKey = () => 'docmind.interrupted:' + chatProject + ':' + chatSession
const historyError = ref('')
const sessionItems = ref<SessionInfo[]>([])
const sessionOpen = ref(false)
const sessionBusy = ref(false)
const sessionError = ref('')
function readDraft() { try { return sessionStorage.getItem(draftKey()) || '' } catch { return '' } }
function readInterruptedRecovery(): InterruptedRecovery | null {
  try {
    const row = JSON.parse(sessionStorage.getItem(recoveryKey()) || 'null') as Partial<InterruptedRecovery> | null
    if (!row || typeof row.user !== 'string' || typeof row.assistant !== 'string') return null
    return {
      user: row.user,
      assistant: row.assistant,
      trace: Array.isArray(row.trace) ? row.trace.filter(item => item && typeof item.text === 'string').slice(-24) : [],
      reasoning: typeof row.reasoning === 'string' ? row.reasoning : '',
      plan: Array.isArray(row.plan) ? row.plan.filter(item => typeof item === 'string').slice(-12) : [],
      startedAt: typeof row.startedAt === 'number' ? row.startedAt : undefined,
    }
  } catch { return null }
}
function isContinuationRequest(value: string): boolean {
  const normalized = value.trim().replace(/[。！!？?，,、；;：:"“”‘’\s]/g, '')
  return /^(继续|继续完成任务|继续上次任务|继续回答|继续执行|接着做|接着完成|恢复任务|恢复回答|从中断处继续)$/.test(normalized)
}
function continuationQuestion(request: string, recovery: InterruptedRecovery): string {
  const trace = recovery.trace.map(item => `${item.type}: ${item.text}`).join('\n')
  const plan = recovery.plan.join('\n')
  return [
    '这是上一个被页面切换或连接中断的回合的继续请求。不要把“继续”当成新的独立问题。',
    `原始用户请求：${recovery.user}`,
    `上次已收到的助手内容：${recovery.assistant}`,
    trace ? `上次已记录的执行步骤：\n${trace}` : '',
    plan ? `上次执行计划：\n${plan}` : '',
    '请先依据以上上下文判断已经完成的步骤，再从中断处继续；不要重复已经确认完成的有副作用工具调用。若原始请求只是询问能力，请直接继续回答原始问题。',
    `用户续接指令：${request}`,
  ].filter(Boolean).join('\n\n')
}
// 兼容旧版会话：早期 API 曾把内部路由上下文拼在用户问题前并落盘。
// 仅清理从字符串开头出现的完整旧前缀，正文中提到“系统提示”不受影响。
const legacyPromptPrefix = /^\s*【系统提示】[\s\S]*?用户问题：\s*/
function cleanLegacyPrompt(value: string): string {
  return String(value || '').replace(legacyPromptPrefix, '').trimStart()
}
/** 应用自发起活动（非用户消息）在会话历史里的标记 → 字条类型/文案。
 *  标记由后端写入历史 user 字段（见 api.py app_guided_inspect）。 */
const ACTIVITY_MARKERS: Array<{ marker: string; kind: ChatActivityKind; label: string }> = [
  { marker: '【界面观察】', kind: 'inspect', label: 'AI 浏览当前界面' },
]
function activityFromHistory(text: string): { kind: ChatActivityKind; label: string } | null {
  const raw = String(text || '').trimStart()
  const hit = ACTIVITY_MARKERS.find(a => raw.startsWith(a.marker))
  return hit ? { kind: hit.kind, label: hit.label } : null
}
const input = ref(readDraft())
watch(input, v => { try { sessionStorage.setItem(draftKey(), v) } catch {} }, { flush: 'sync' })
const sending = ref(false)
let abortCtl: AbortController | null = null
/** 工作流主 Agent 的 start_workflow 动作行与其观察不在对话重复展示（卡片即反馈）。 */
function visibleTrace(msg: ChatMsg) {
  const hidden = new Set<number>()
  msg.trace.forEach((item, i) => {
    if (item.type === 'action' && /^start_workflow\s*\(/.test(item.text.trim())) {
      hidden.add(i)
      if (msg.trace[i + 1]?.type === 'observation') hidden.add(i + 1)
    }
  })
  return msg.trace.map((item, index) => ({ item, index })).filter(x => !hidden.has(x.index))
}
const traceSegmentOpen = ref<Set<number>>(new Set())
function toggleTraceSegment(id: number) {
  const next = new Set(traceSegmentOpen.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  traceSegmentOpen.value = next
}
function traceSegmentIsOpen(msg: ChatMsg) {
  return traceSegmentOpen.value.has(msg.id) || msg.status === 'streaming'
}
function traceSegmentTitle(msg: ChatMsg) {
  const stages: string[] = []
  for (const row of visibleTrace(msg)) {
    if (row.item.type !== 'action') continue
    const stage = traceGroup(row.item)
    if (stages[stages.length - 1] !== stage) stages.push(stage)
  }
  return stages.length ? stages.join(' → ') : '分析与复核'
}
function onCardActivity() {
  // 卡片内部子代理步骤频率高：按帧合批跟随，不做每事件 nextTick
  scheduleFollow()
}

const composerEl = ref<InstanceType<typeof ChatComposer> | null>(null)
function focusComposer() { composerEl.value?.focus() }
const pendingImages = ref<File[]>([])
const attachmentError = ref('')
const videoBusy = ref(false)

// ---------------------------------------------------------------- 折叠
const dockStateKey = (part: 'collapsed' | 'expanded') => props.scope === 'default'
  ? `docmind.chatDock${part === 'collapsed' ? 'Collapsed' : 'Expanded'}`
  : `docmind.chatDock.cockpit.${part}`
const collapsed = ref(window.localStorage.getItem(dockStateKey('collapsed')) === '1')
const expanded = ref(window.localStorage.getItem(dockStateKey('expanded')) === '1')
watch(collapsed, (v) => window.localStorage.setItem(dockStateKey('collapsed'), v ? '1' : '0'))
watch(expanded, (v) => window.localStorage.setItem(dockStateKey('expanded'), v ? '1' : '0'))
function toggleDock() {
  // 审批门打开时禁止折叠（pointer-events 已挡鼠标，这里挡已聚焦元素的键盘触发），
  // 否则 33px 裁剪会把居中审批模态吞掉
  if (gateBlocking.value) return
  collapsed.value = !collapsed.value
  if (!collapsed.value) nextTick(focusComposer)
}

// ---------------------------------------------------------------- 模型 / 联网 / 思考
// 模型配置、联网/思考开关及本地持久化抽到 useModelConfig（问答页共用）；
// 后端切换模型会清空多轮上下文，保存后同步清空本地消息避免张冠李戴。
const {
  modelConfig, settingsOpen, webOn, thinkingOn,
  modelLabel, thinkingMode, thinkingSupported, thinkingNative, thinkingEffective,
  visionLabel,
  loadModelConfig, openSettings, onModelSaved,
} = useModelConfig({
  onSaved: () => {
    clearMessages()
    // 切模型后窗口可能大变（如 16k → 256k）：立即重拉用量，避免仍显示旧窗口的剩余量
    void loadContextUsage()
  },
})

// 模型芯片只负责切换已保存预设；完整的新增/编辑入口留在设置页。
const modelSwitcherOpen = ref(false)
const modelSwitchError = ref('')
async function openModelSwitcher() {
  if (demoMode.value) return
  modelSwitchError.value = ''
  try {
    const current = await modelApi.get()
    modelConfig.value = current
    // 旧版本已选中的模型未自动生成预设；首次打开切换器时补录一次。
    const exists = current.model_presets?.some(p => p.provider === current.llm_provider
      && p.model === current.llm_model
      && p.base_url === (current.llm_provider === 'custom' ? current.custom_base_url : ''))
    if (!exists && current.llm_provider !== 'mock') {
      const saved = await modelApi.savePreset({
        label: `${current.provider_meta?.[current.llm_provider]?.label || current.llm_provider} · ${current.llm_model}`,
        provider: current.llm_provider,
        model: current.llm_model,
        base_url: current.llm_provider === 'custom' ? current.custom_base_url : '',
        context_window: current.context_window_override || 0,
      })
      if (saved.ok === false || !saved.presets) modelSwitchError.value = saved.error || '保存当前模型到切换列表失败'
      else modelConfig.value = { ...current, model_presets: saved.presets }
    }
  } catch (e) {
    modelSwitchError.value = (e as { message?: string }).message || '读取模型列表失败'
  }
  modelSwitcherOpen.value = true
}
function openModelSettings() {
  modelSwitcherOpen.value = false
  openSettings()
}
function openUnifiedModelSettings() {
  modelSwitcherOpen.value = false
  window.dispatchEvent(new CustomEvent('docmind:open-settings', { detail: { tab: 'model' } }))
}

// ---------------------------------------------------------------- 上下文窗口用量
// 后端按当前模型真实窗口估算（含系统提示/历史摘要/历史回放/当前问题），
// 每轮问答开工与收尾各推一次 SSE context 事件；刷新页面走 GET /api/context 恢复。
const usage = ref<ContextUsage | null>(null)
function applyUsage(ev: SseEvent) {
  if (typeof ev.percent !== 'number' || typeof ev.context_window !== 'number') return
  usage.value = {
    used_tokens: ev.used_tokens ?? 0,
    context_window: ev.context_window,
    prompt_budget: ev.prompt_budget ?? 0,
    percent: Math.max(0, Math.min(100, ev.percent)),
    level: (ev.level as ContextUsage['level']) || 'ok',
    history_tokens: ev.history_tokens,
    compact_trigger_tokens: ev.compact_trigger_tokens,
    compact_percent: typeof ev.compact_percent === 'number'
      ? Math.max(0, Math.min(100, ev.compact_percent))
      : undefined,
  }
}
function fmtTokens(n: number): string {
  if (n >= 1000) {
    const k = n / 1000
    return `${k >= 100 ? Math.round(k) : k.toFixed(k >= 10 ? 0 : 1)}k`
  }
  return String(n)
}
const usageTitle = computed(() => {
  const u = usage.value
  if (!u) return ''
  // 百分比按「可用 prompt 额度」计（模型窗口扣除输出预留）；level 现由「历史 / 压缩触发线」口径分级
  const tail = u.level === 'high'
    ? '（历史已达压缩触发线，早期对话会被自动压缩为摘要）'
    : u.level === 'warn'
      ? '（历史接近压缩触发线，即将自动压缩早期对话）'
      : '（历史达到触发线后早期对话会自动压缩为摘要，不影响新问答）'
  let line = `上下文已用 ${fmtTokens(u.used_tokens)} / 可用额度 ${fmtTokens(u.prompt_budget)} tokens`
    + `（模型窗口 ${fmtTokens(u.context_window)}，已预留输出空间）${tail}`
  if (typeof u.compact_percent === 'number') {
    line += `\n压缩进度：历史 ${fmtTokens(u.history_tokens ?? 0)}`
      + ` / 触发线 ${fmtTokens(u.compact_trigger_tokens ?? 0)} tokens（${u.compact_percent}%）`
  }
  return line
})

// ---------------------------------------------------------------- 发送 / 停止
async function send(text?: string, attached?: File[], onAccepted?: () => void, uiContext?: ChatUiContext, activity?: { kind: ChatActivityKind; label: string }, reviewRef?: { workflowId?: string; feedbackId?: string }, onResult?: (text: string) => void): Promise<boolean> {
  const q = (text ?? input.value).trim()
  const imgs = attached ? attached.slice() : pendingImages.value.slice()
  // 共享屏幕进行中对两种对话都附加最新帧：用户切页后落到的普通工作台对话同样在"看着"，
  // 不再出现"刚共享完换个地方问就说看不到"。手动附过图（attached/pendingImages）则不覆盖。
  if (!attached && liveVisionFrame && imgs.length === 0) {
    const extension = liveVisionFrame.type === 'image/jpeg' ? 'jpg' : 'png'
    imgs.push(new File([liveVisionFrame], `current-view-${Date.now()}.${extension}`, { type: liveVisionFrame.type || 'image/png' }))
  }
  if ((!q && !imgs.length) || sending.value) return false
  // 审批门打开期间禁止发起新问答（CSS 已挡鼠标，这里挡 Ctrl+Enter 键盘发送）
  if (gateBlocking.value) {
    void askAlert({
      title: '工作流等待你的处理',
      message: '请先在居中的审批窗口中选择方案、批准或调整任务（也可中断该工作流），再继续提问。',
    })
    return false
  }
  const recovery = readInterruptedRecovery()
  const isResume = !!recovery && isContinuationRequest(q)
  const request = isResume ? continuationQuestion(q, recovery!) : q
  const epoch = ++chatEpoch
  historyError.value = ''
  try { sessionStorage.removeItem(recoveryKey()) } catch {}
  if (isResume) {
    const previous = messages.value[messages.value.length - 1]
    if (previous?.recoverable) previous.recoverable = false
  }
  if (attached === undefined) {
    input.value = ''
    pendingImages.value = []
    attachmentError.value = ''
  }
  stickToBottom.value = true  // 用户主动发送，恢复贴底自动滚动
  if (activity) {
    // 应用自发起活动（如 AI 浏览当前界面）：完整指令只作为系统上下文发给后端，
    // 对话流里只显示一张居中小字条，不伪装成用户说的话
    messages.value.push({ id: msgSeq++, role: 'activity', text: activity.label, status: 'done', trace: [], reasoning: '', notices: [], plan: [], activityKind: activity.kind, imageCount: imgs.length || undefined })
  } else {
    messages.value.push({ id: msgSeq++, role: 'user', text: q || '请分析附件图片', status: 'done', trace: [], reasoning: '', notices: [], plan: [], imageCount: imgs.length })
  }
  const turn: ChatMsg = {
    id: msgSeq++, role: 'assistant', text: '', status: 'streaming', trace: [], reasoning: '', notices: [], plan: [],
    startedAt: Date.now(),
  }
  messages.value.push(turn)
  sending.value = true
  onAccepted?.()
  if (demoMode.value) {
    await nextTick(scrollToBottom)
    await demoAnswer(turn.id, q)
    sending.value = false
    await nextTick(scrollToBottom)
    return true
  }
  const ac = new AbortController()
  abortCtl = ac
  await nextTick(scrollToBottom)
  if (epoch !== chatEpoch) return false
  let finished = false
  let receivedFinal = false

  const live = () => epoch === chatEpoch ? messages.value.find((m) => m.id === turn.id) : undefined
  // 事件分类与帧合批在 useChatStream 中（token/reasoning/plan/trace/workflow/context…）
  const onEvent = (ev: SseEvent) => {
    if (epoch !== chatEpoch) return
    if (ev.type === 'final' && typeof ev.text === 'string' && ev.text) receivedFinal = true
    dispatch(ev, live())
  }

  try {
    // 不支持思考的模型不传思考参数（null）；native 模型恒开，也无需显式传
    const thinkingOpt = thinkingSupported.value
      ? (thinkingNative.value ? null : thinkingOn.value)
      : null
    await aiApi.askGrounded(request, { onEvent, signal: ac.signal }, {
      web: webOn.value,
      thinking: thinkingOpt,
      images: imgs,
      sessionId: currentSessionId(),
      uiContext: props.scope === 'cockpit' && liveVisionTimeline.length && !uiContext ? 'cockpit_live_vision' : uiContext,
      visualTimeline: props.scope === 'cockpit' ? liveVisionTimeline : [],
      workflowId: reviewRef?.workflowId,
      feedbackId: reviewRef?.feedbackId,
    })
    drain()
    const t = live()
    if (t) {
      if (t.status !== 'error') t.status = t.text ? 'done' : 'stopped'
      t.finishedAt = Date.now()
      finishLiveMd(t.id, () => answerHtml(t))
      finished = !!t.text && t.status !== 'error'
      if (t.status === 'error') t.recoverable = true
      if (finished && receivedFinal) onResult?.(t.text)
      if (onResult && !receivedFinal) finished = false
    }
  } catch (e) {
    drain()
    const t = live()
    if (!t) return false
    finishLiveMd(t.id, () => answerHtml(t))
    if ((e as Error).name === 'AbortError') {
      t.status = 'stopped'
      t.finishedAt = Date.now()
      if (!t.text) t.text = '（已停止）'
    } else {
      t.status = 'error'
      t.finishedAt = Date.now()
      t.error = (e as { message?: string }).message || '请求失败'
    }
  } finally {
    if (epoch === chatEpoch) {
      sending.value = false; abortCtl = null
      const queuedVoice = voiceForwardQueue.value.shift()
      if (queuedVoice) void send(queuedVoice)
      // 问答可能由主 Agent 经 start_workflow 工具触发工作流；若撞上同项目互斥，
      // 错误只体现在回答文本里，这里补一次探测，让孤儿恢复条给出可操作入口。
      if (props.scope === 'default') void detectOrphanWorkflow()
    }
    void refreshSessionList()
    await nextTick(scrollToBottom)
  }
  return finished
}

function stop() {
  // 主动停止也保存现场，便于用户之后点击“继续上次任务”。
  preserveInterrupted()
  drain()
  const turn = messages.value[messages.value.length - 1]
  if (turn?.status === 'streaming' && !turn.workflow) turn.recoverable = true
  if (turn) finishLiveMd(turn.id, () => answerHtml(turn))
  if (turn?.status === 'streaming') {
    turn.status = 'stopped'
    turn.notices.push(turn.workflow
      ? '工作流启动已取消；若服务端已经创建，会自动中断。'
      : '回答已中断；已执行的工具操作不会自动撤销。')
  }
  ++chatEpoch
  abortCtl?.abort()
  abortCtl = null
  sending.value = false
}
function preserveInterrupted() {
  if (!sending.value) return
  const assistant = messages.value[messages.value.length - 1]
  const user = messages.value[messages.value.length - 2]
  try {
    sessionStorage.setItem(recoveryKey(), JSON.stringify({
      user: user?.text || '', assistant: assistant?.text || '',
      trace: assistant?.trace?.slice(-24) || [], reasoning: assistant?.reasoning || '',
      plan: assistant?.plan?.slice(-12) || [], startedAt: assistant?.startedAt,
    } satisfies InterruptedRecovery))
  } catch {}
}

function addImages(files: FileList | File[]) {
  const incoming = Array.from(files || [])
  const valid = incoming.filter((f) => /^image\/(png|jpeg|webp|gif)$/i.test(f.type))
  if (valid.length !== incoming.length) attachmentError.value = '仅支持 PNG、JPEG、WEBP、GIF 图片。'
  const merged = [...pendingImages.value, ...valid].slice(0, 4)
  if (pendingImages.value.length + valid.length > 4) attachmentError.value = '单条消息最多附带 4 张图片。'
  pendingImages.value = merged
}
function removeImage(index: number) { pendingImages.value.splice(index, 1) }
/** ChatComposer 选图/粘贴/拖拽统一上抛文件列表，校验与暂存仍在父级 */
function onImagesPicked(files: FileList | File[]) { addImages(files) }
async function onVideoPicked(file: File) {
  if (videoBusy.value) return
  videoBusy.value = true
  attachmentError.value = ''
  try {
    const result = await visionApi.analyzeVideo(file)
    if (!result.ok) throw new Error(result.error || '视频分析失败')
    const observation = (result.context || []).join('\n\n')
    input.value = observation
    await nextTick(focusComposer)
  } catch (e) {
    attachmentError.value = (e as { message?: string }).message || '视频分析失败'
  } finally {
    videoBusy.value = false
  }
}
function restoreInterrupted(turns: { user: string; assistant: string }[]) {
  try {
    const row = JSON.parse(sessionStorage.getItem(recoveryKey()) || 'null')
    if (!row || typeof row.user !== 'string' || typeof row.assistant !== 'string') return
    if (turns.some(t => t.user === row.user && t.assistant)) { sessionStorage.removeItem(recoveryKey()); return }
    const interruptedAct = activityFromHistory(row.user)
    messages.value.push(interruptedAct
      ? { id: msgSeq++, role: 'activity', text: interruptedAct.label, status: 'done', trace: [], reasoning: '', notices: [], plan: [], activityKind: interruptedAct.kind }
      : { id: msgSeq++, role: 'user', text: cleanLegacyPrompt(row.user), status: 'done', trace: [], reasoning: '', notices: [], plan: [] })
    messages.value.push({ id: msgSeq++, role: 'assistant', text: cleanLegacyPrompt(row.assistant), status: 'stopped',
      trace: row.trace || [], reasoning: row.reasoning || '', plan: row.plan || [], recoverable: true,
      notices: ['上次回答因离开页面或切换项目中断，已保留原始问题和执行上下文；可以点击“继续上次任务”或输入“继续”。'] })
  } catch {}
}
function resetChatContext() {
  liveVisionFrame = null
  liveVisionTimeline = []
  preserveInterrupted()
  stop()
  chatProject = getProjectId(); chatSession = currentSessionId()
  messages.value = []; usage.value = null; historyError.value = ''
  orphanWf.value = null
  input.value = readDraft()
  void restoreHistory(true).then(() => { if (props.scope === 'default') void detectOrphanWorkflow() })
}
function onPageHide() { preserveInterrupted(); stop() }
function onBeforeLeave(e: BeforeUnloadEvent) { if (sending.value) { preserveInterrupted(); e.preventDefault(); e.returnValue = '' } }

function clearMessages() {
  if (sending.value) stop()
  messages.value = []
  usage.value = null
  resetRenderState()
  traceSegmentOpen.value = new Set()
  // 卡片随消息一起卸载，不会再发 team 事件；这里同步清掉左下角成员抽屉，
  // 否则会残留指向已消失卡片的幽灵成员
  wfTeams.value = []
  resetGates()
}

/** 清空本地消息，并删除当前标签页会话在磁盘上的多轮历史。
 *  会话 id 每标签页独立（见 api.ts::getSessionId），故这里只清理本标签页自己的会话，
 *  不影响其它标签页；删除失败（无会话文件 / 服务未启动）不阻塞清空 UI。
 *  @returns 是否实际完成清空（用户在工作流确认框中点取消则为 false） */
async function clearConversation(): Promise<boolean> {
  if (!(await confirmLeaveWorkflows())) return false
  try { sessionStorage.removeItem(recoveryKey()) } catch {}
  clearMessages()
  if (demoMode.value) return true
  try {
    await harnessApi.deleteSession(currentSessionId())
  } catch {
    /* 会话文件不存在或服务不可达：忽略，本地已清空 */
  }
  void refreshSessionList()
  return true
}

/** 会话弹层内清空当前对话：完成后收起弹层。 */
async function clearCurrentFromPop() {
  if (await clearConversation()) sessionOpen.value = false
}


async function refreshSessionList() {
  if (demoMode.value) return
  try {
    const result = await harnessApi.sessions()
    // 会话按页面域隔离：问答页会话 id 为 ask- 前缀，工作台历史不展示它们
    sessionItems.value = (Array.isArray(result.items) ? result.items : [])
      .filter(i => props.scope === 'cockpit'
        ? String(i.session_id || '').startsWith('dev-')
        : !/^(ask-|dev-)/.test(String(i.session_id || '')))
    sessionError.value = ''
  } catch (e) {
    sessionError.value = (e as Error).message || '会话历史加载失败'
  }
}
async function prepareSessionChange(action: string): Promise<boolean> {
  if (!(await confirmLeaveWorkflows())) return false
  if (!sending.value && !messages.value.length) return true
  return askConfirm({
    title: action,
    message: `当前对话会保留在会话历史中，${action}后将显示另一段对话。继续？`,
    confirmText: '继续',
  })
}
async function switchSession(id: string) {
  const target = String(id || '').trim()
  if (!target || target === chatSession || sessionBusy.value) {
    sessionOpen.value = false
    return
  }
  if (!(await prepareSessionChange('切换会话'))) return
  sessionBusy.value = true
  sessionError.value = ''
  stop()
  try {
    const claimed = await selectSession(target)
    if (!claimed) throw new Error('这个会话正在其他标签页使用，无法在当前标签切换。')
    chatProject = getProjectId()
    chatSession = target
    clearMessages()
    try { sessionStorage.removeItem(recoveryKey()) } catch {}
    input.value = readDraft()
    sessionOpen.value = false
    await restoreHistory(true)
    await loadContextUsage()
  } catch (e) {
    sessionError.value = (e as Error).message || '切换会话失败'
  } finally {
    sessionBusy.value = false
  }
}
async function createConversation() {
  if (!(await prepareSessionChange('新建对话'))) return
  sessionBusy.value = true
  sessionError.value = ''
  stop()
  try {
    const next = await createSession()
    if (!next) throw new Error('无法创建新的独立会话，请刷新页面重试。')
    chatProject = getProjectId()
    chatSession = next
    clearMessages()
    input.value = ''
    pendingImages.value = []
    attachmentError.value = ''
    try { sessionStorage.removeItem(recoveryKey()) } catch {}
    sessionOpen.value = false
    orphanWf.value = null
    await refreshSessionList()
    if (props.scope === 'default') void detectOrphanWorkflow()
    await nextTick(focusComposer)
  } catch (e) {
    sessionError.value = (e as Error).message || '新建对话失败'
  } finally {
    sessionBusy.value = false
  }
}

async function loadContextUsage() {
  if (demoMode.value) return
  try {
    const u = await contextApi.get(currentSessionId())
    if (u) usage.value = u
  } catch {
    /* 未启动/无会话时不显示指示即可 */
  }
}

/** 删除会话历史中的某一条；当前会话走 clearConversation（含工作流离开确认）。 */
async function deleteSessionItem(id: string) {
  const target = String(id || '').trim()
  if (!target) return
  if (target === chatSession) {
    sessionOpen.value = false
    await clearConversation()
    return
  }
  if (sessionBusy.value) return
  const ok = await askConfirm({
    title: '删除对话',
    message: '确定删除这段对话历史？此操作不可恢复。',
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  sessionBusy.value = true
  sessionError.value = ''
  try {
    const r = await harnessApi.deleteSession(target)
    if (r?.ok === false) throw new Error('服务端拒绝删除')
    await refreshSessionList()
  } catch (e) {
    sessionError.value = (e as Error).message || '删除失败'
  } finally {
    sessionBusy.value = false
  }
}

// ---------------------------------------------------------------- 工作流历史
// 快捷任务（不走对话 turns）与已结束工作流只落盘在 .docmind/workflows/，
// 会话历史里永远找不到它们。这里提供独立弹层：查看（挂回卡片）、中断运行中、
// 删除终态并清理磁盘文件。
const wfHistoryOpen = ref(false)
const wfHistoryItems = ref<WorkflowSummary[]>([])
const wfHistoryBusy = ref(false)
const wfHistoryError = ref('')
const wfHistoryActingId = ref('')

async function refreshWfHistory() {
  if (demoMode.value) return
  wfHistoryBusy.value = true
  wfHistoryError.value = ''
  try {
    const r = await agentApi.workflowList()
    wfHistoryItems.value = Array.isArray(r.items) ? r.items : []
  } catch (e) {
    wfHistoryError.value = (e as Error).message || '工作流历史加载失败'
  } finally {
    wfHistoryBusy.value = false
  }
}
function openWfHistory() {
  wfHistoryOpen.value = true
  void refreshWfHistory()
}
/** 把历史工作流挂回当前对话：先拉完整状态，再插入一张只读/可继续的卡片。 */
async function reattachWfHistory(item: WorkflowSummary) {
  if (wfHistoryActingId.value) return
  wfHistoryActingId.value = item.workflow_id
  try {
    const r = await agentApi.workflow(item.workflow_id)
    const wf = r.workflow
    if (r.ok === false || !wf) throw new Error(r.error || '工作流状态读取失败')
    wfHistoryOpen.value = false
    if (!messages.value.some(m => m.workflow?.workflowId === item.workflow_id)) {
      messages.value.push({
        id: msgSeq++, role: 'assistant', text: '', status: 'done',
        trace: [], reasoning: '', notices: [], plan: [],
        workflow: { workflowId: item.workflow_id, seed: wf },
        startedAt: Date.now(),
      })
      stickToBottom.value = true
      await nextTick(scrollToBottom)
    }
  } catch (e) {
    wfHistoryError.value = (e as Error).message || '挂回失败'
  } finally {
    wfHistoryActingId.value = ''
  }
}
async function interruptWfHistory(item: WorkflowSummary) {
  if (wfHistoryActingId.value) return
  const ok = await askConfirm({
    title: '中断工作流',
    message: '确定中断这个工作流？已产生的文件改动不会自动回滚。',
    confirmText: '中断',
    danger: true,
  })
  if (!ok) return
  wfHistoryActingId.value = item.workflow_id
  wfHistoryError.value = ''
  try {
    const r = await agentApi.workflowInterrupt(item.workflow_id, '用户在工作流历史中中断')
    if (r.ok === false) throw new Error(r.error || '中断失败')
    await refreshWfHistory()
    if (props.scope === 'default') void detectOrphanWorkflow()
  } catch (e) {
    wfHistoryError.value = (e as Error).message || '中断失败'
  } finally {
    wfHistoryActingId.value = ''
  }
}
async function deleteWfHistory(item: WorkflowSummary) {
  if (wfHistoryActingId.value) return
  const ok = await askConfirm({
    title: '删除工作流记录',
    message: '将删除该工作流的状态记录与磁盘文件，此操作不可恢复。确定删除？',
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  wfHistoryActingId.value = item.workflow_id
  wfHistoryError.value = ''
  try {
    const r = await agentApi.workflowDelete(item.workflow_id)
    if (r.ok === false) throw new Error(r.error || '删除失败')
    wfHistoryItems.value = wfHistoryItems.value.filter(i => i.workflow_id !== item.workflow_id)
  } catch (e) {
    wfHistoryError.value = (e as Error).message || '删除失败'
  } finally {
    wfHistoryActingId.value = ''
  }
}

// ---------------------------------------------------------------- 历史回灌
/** 把服务端 turns 按「用户→助手」交替重建成消息列表（历史消息均为已完成态）。 */
function rebuildFromTurns(turns: { user: string; assistant: string }[]) {
  const rebuilt: ChatMsg[] = []
  for (const t of turns) {
    if (!t) continue
    if (t.user) {
      const act = activityFromHistory(t.user)
      rebuilt.push(act
        ? { id: msgSeq++, role: 'activity', text: act.label, status: 'done', trace: [], reasoning: '', notices: [], plan: [], activityKind: act.kind }
        : { id: msgSeq++, role: 'user', text: cleanLegacyPrompt(t.user), status: 'done', trace: [], reasoning: '', notices: [], plan: [] })
    }
    if (t.assistant) {
      rebuilt.push({ id: msgSeq++, role: 'assistant', text: cleanLegacyPrompt(t.assistant), status: 'done', trace: [], reasoning: '', notices: [], plan: [] })
    }
  }
  // msgSeq 已随分配递增，天然大于历史消息最大 id，后续新消息 id 不会冲突。
  messages.value = rebuilt
  return rebuilt.length
}

/** 只恢复当前项目的当前会话，绝不自动续接其他历史。 */
async function restoreHistory(force = false) {
  if (demoMode.value) return
  if (sending.value) return
  if (!force && messages.value.length) return
  // 记住进入时的消息数：await 期间用户可能已发送新消息（SSE 正在流），
  // 重建会整体覆盖 messages 并让 live() 匹配不到、静默丢流，故 await 后需复检。
  const project = getProjectId(), session = currentSessionId(), epoch = chatEpoch
  const before = messages.value.length
  try {
    const detail = await harnessApi.sessionDetail(session)
    if (project !== getProjectId() || session !== currentSessionId() || epoch !== chatEpoch) return
    const turns = detail.turns || []
    // Never silently load a different conversation into a fresh session.
    if (force && !turns.length && !sending.value) messages.value = []
    if (turns.length) {
      // 竞态兜底：await 窗口内若有新消息涌入（或正在发送），放弃本次回灌，
      // 绝不覆盖用户正在进行的对话；force 切换只受 sending 拦截。
      if (sending.value || (!force && messages.value.length !== before)) return
      rebuildFromTurns(turns)
      stickToBottom.value = true
      await nextTick(scrollToBottom)
    }
    restoreInterrupted(turns)
  } catch (e) {
    if (epoch === chatEpoch) { historyError.value = '历史加载失败：' + (e as Error).message; restoreInterrupted([]) }
  }
}

// ---------------------------------------------------------------- 离线演示问答
function demoAnswer(turnId: number, q: string): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(() => {
      const t = messages.value.find((m) => m.id === turnId)
      if (!t) return resolve()
      t.trace = [
        { type: 'thought', text: '先理解问题涉及的关键词，再到代码库里检索相关文件。' },
        { type: 'action', text: 'search_code：玩家受伤 / take_damage / health' },
        { type: 'observation', text: '命中 4 个文件，逐个阅读最相关的 2 个。' },
      ]
      t.text = demoReply(q)
      t.status = 'done'
      resolve()
    }, 900)
  })
}
function demoReply(q: string): string {
  if (q.includes('数值') || q.includes('受伤') || q.includes('血') || q.includes('伤害')) {
    return [
      '**（这是演示回答）** 连接你的真实项目后，AI 会先在代码库里检索，再给出文件+行号。',
      '',
      '以一个典型 Godot 项目为例，玩家受伤数值通常在这几处：',
      '',
      '1. **生命值定义**：`player_stats.gd` 里的 `max_health / health`（角色初始数值）',
      '2. **受伤计算**：`damage_calc.gd` 的 `take_damage(amount)`，先扣护甲、再扣血',
      '3. **数值配置**：策划常把数值放在 `resources/player_values.tres`，改数字不用改代码',
      '',
      '真实使用时，下面会出现可点击的文件卡片，点一下直接跳到对应行。',
    ].join('\n')
  }
  if (q.includes('结构') || q.includes('梳理')) {
    return [
      '**（演示回答）** 连接真实项目后，AI 会按目录和入口梳理出：',
      '',
      '- **入口**：主场景与初始化脚本',
      '- **核心系统**：角色、背包、战斗、存档各自的目录',
      '- **数据配置**：数值表 / 资源文件放在哪',
      '- 还可以点顶部「代码地图」看函数调用关系图',
    ].join('\n')
  }
  return [
    '**（演示回答）** 在本地版中，AI 会带着这个问题去搜你的代码库：读文件、追调用链，',
    '然后用大白话解释，并附上可点击的文件与行号。',
    '',
    '你可以换个更具体的问题试试，比如「玩家受伤扣多少血在哪算的？」。',
  ].join('\n')
}

// 概览页「去问问 / 试试问 AI」：展开对话台、（可选）预填问题、定位输入框
// 运行台「继续这段对话」：detail.reload=true 时按当前存储的会话 id 重新回灌历史
function onFocusChat(detail: FocusChatDetail) {
  if ((detail?.target || 'default') !== props.scope) return
  const q = detail?.q
  collapsed.value = false
  if (detail?.reload) {
    resetChatContext()
    void nextTick(focusComposer)
    return
  }
  nextTick(() => {
    if (q) input.value = q
    else if (detail?.qIfEmpty && !input.value.trim()) input.value = detail.qIfEmpty
    focusComposer()
  })
}
async function onSendChat(detail: PreviewFeedbackRequest) {
  if ((detail?.target || 'default') !== props.scope) return
  const prompt = detail?.prompt?.trim()
  if (!prompt) { detail?.onStatus?.('failed', '反馈不能为空'); return }
  if (detail.projectId !== getProjectId()) { detail.onStatus?.('pending', '项目已切换，请回到原项目重试'); return }
  if (sending.value || gateBlocking.value) {
    detail.onStatus?.('pending', sending.value ? 'AI 正忙，反馈已保存，请稍后重试' : '请先完成当前审核，再重试反馈')
    return
  }
  collapsed.value = false
  const images = (detail?.images || []).filter((image): image is Blob => image instanceof Blob)
  const files = images.map((image, index) => new File(
    [image], `live-preview-${Date.now()}-${index}.png`, { type: image.type || 'image/png' }))
  await nextTick()
  if (detail.projectId !== getProjectId() || sending.value || gateBlocking.value) {
    detail.onStatus?.('pending', '当前对话暂不可接收，请稍后在原项目重试')
    return
  }
  try {
    // 「浏览当前界面」是应用发起的活动：对话流只显示小字条，完整指令由后端转系统上下文
    const activity = detail.uiContext === 'app_interface_inspect'
      ? { kind: 'inspect' as const, label: 'AI 浏览当前界面' }
      : undefined
    const finished = await send(prompt, files, () => detail.onStatus?.('processing'), detail.uiContext, activity,
      detail.uiContext === 'desktop_visual_review'
        ? { workflowId: detail.workflowId, feedbackId: detail.feedbackId }
        : undefined, text => {
          if (detail.projectId === getProjectId()) detail.onResult?.(text)
        })
    detail.onStatus?.(finished ? 'awaiting_review' : 'failed', finished ? 'AI 回复已结束，请检查预览效果' : '本次对话未完成，已执行操作不会自动撤销，请检查后重试')
  } catch {
    detail.onStatus?.('failed', '本次对话未完成，请检查项目当前状态后重试')
  }
}
let offFocusChat = () => {}
let offSendChat = () => {}
let offContextChanged = () => {}
let offOpenModelSettings = () => {}
let offLiveVisionFrame = () => {}
onMounted(() => {
  if (props.scope === 'cockpit') {
    void voiceApi.status().then(status => {
      voiceTtsConfigured.value = !!status.tts_configured
      voiceSupported.value = voiceSupported.value || !!status.stt_configured
    }).catch(() => { voiceTtsConfigured.value = false })
  }
  // 全局事件统一走类型化事件总线（eventBus.ts），返回值即取消订阅
  offFocusChat = appEvents.on('docmind:focus-chat', onFocusChat)
  offSendChat = appEvents.on('docmind:send-chat', onSendChat)
  offContextChanged = appEvents.on('docmind:project-context-changed', resetChatContext)
  offOpenModelSettings = appEvents.on('docmind:open-model-settings', () => openModelSettings())
  // 两种 scope 都订阅最新帧（附加发送已放开给普通对话）；时间线仍只有驾驶舱消费。
  offLiveVisionFrame = appEvents.on('docmind:live-vision-frame', detail => {
    if (detail.projectId !== getProjectId()) return
    liveVisionFrame = detail.image
    if (props.scope === 'cockpit') liveVisionTimeline = detail.image ? detail.timeline || [] : []
  })
  window.addEventListener('pagehide', onPageHide)
  window.addEventListener('beforeunload', onBeforeLeave)
  if (props.scope === 'default') startTabProbe()   // 普通工作台沿用跨标签探测
  void loadModelConfig()
  void loadContextUsage()
  void refreshSessionList()
  // 历史回灌完成后再探测孤儿：避免本会话卡片已随历史逻辑存在时误报
  void restoreHistory().then(() => { if (props.scope === 'default') void detectOrphanWorkflow() })
})
onBeforeUnmount(() => {
  if (voiceAudio) { voiceAudio.pause(); voiceAudio = null }
  voiceAudioQueue.splice(0)
  onPageHide()
  offFocusChat()
  offSendChat()
  offContextChanged()
  offOpenModelSettings()
  offLiveVisionFrame()
  window.removeEventListener('pagehide', onPageHide)
  window.removeEventListener('beforeunload', onBeforeLeave)
})

// 消息展示层（流式 markdown 节流/成稿引用缓存/trace 步骤/计时/思考折叠）
const {
  scheduleLiveMd, finishLiveMd, streamHtmlOf,
  answerHtml, refsOf, webRefsOf, openRef, TRACE_GLYPH,
  toggleActivity, activityIsOpen, traceTitle, traceSummary, traceGroup, traceGroupLabel, traceGroupStart, traceState, traceStateLabel,
  traceElapsed, elapsedLabel, reasonOpen, toggleReason, resetRenderState,
} = useMessageRender<ChatMsg>(() =>
  sending.value
  || messages.value.some(m => m.status === 'streaming' || (m.workflow?.workflowId && !m.finishedAt))
  || activeWorkflowIds.value.length > 0)

// 流式接收管线（帧合批/trace 计时/事件分类）抽到 useChatStream，问答页共用同一实现。
const { drain, appendTrace, dispatch } = useChatStream<ChatMsg>({
  onActivity: (t) => { t.lastActivityAt = Date.now() },
  // 帧内正文增量提交后挂 125ms 节流的 markdown 重渲染
  onTextFlushed: (t) => { scheduleLiveMd(t); speakDelta(t) },
  // 一次 rAF 帧提交后跟随一次（微任务里读到本帧最新布局），不逐 token 强排
  onFrame: () => queueMicrotask(followBottom),
  // 深度思考默认折叠：用户点「深度思考」开关自行展开，避免长推理轨迹未请求即铺开（也更省空间）。
  // 流式期间该开关仍显示「…」提示模型正在思考，无需自动展开。
  onContext: (ev) => applyUsage(ev),
  onStructural: () => scheduleFollow(),
  onFinal: (t, text) => {
    t.text = text
    // 立即停止节流渲染并预热完成态 HTML，避免最后一帧与成稿之间格式闪一下
    finishLiveMd(t.id, () => answerHtml(t))
  },
})

// 思考芯片：仅 toggle 家族可点；native 恒开；none 不渲染可点按钮
function toggleThinking() {
  if (!thinkingSupported.value || thinkingNative.value) return
  thinkingOn.value = !thinkingOn.value
}
const thinkingTitle = computed(() => {
  if (thinkingMode.value === 'unknown') return '深度思考能力待确认，请在模型设置中选择能力'
  if (!thinkingSupported.value) return '当前模型不支持深度思考，可点左侧模型芯片切换支持思考的模型'
  if (thinkingNative.value) return '该模型内置深度思考，始终开启'
  return thinkingOn.value ? '深度思考已开启：回答前先推理，耗时更长' : '点击开启深度思考'
})
const webTitle = computed(() => webOn.value
  ? '联网搜索已开启：AI 可检索互联网获取最新信息'
  : '联网搜索已关闭：仅检索当前代码库，点击开启')

// ---------------------------------------------------------------- 引擎 / MCP 弹层
</script>

<template>
  <section
    class="cd-dock"
    :class="{
      'cd-collapsed': collapsed,
      'cd-expanded': expanded && !collapsed,
      'cd-clipping': collapsed,
      'cd-gate-blocked': gateBlocking,
    }"
  >
    <header class="cd-head">
      <!-- 折叠/收起只响应明确的标题热区，避免选中提示文字等误触把面板收起来 -->
      <span
        class="cd-head-toggle"
        role="button"
        :aria-expanded="!collapsed"
        :aria-controls="`docmind-chat-body-${props.scope}`"
        :title="collapsed ? '展开对话台' : '收起对话台'"
        tabindex="0"
        @click="toggleDock"
        @keydown.enter.prevent="toggleDock"
        @keydown.space.prevent="toggleDock"
      >
        <span class="cd-chevron" :class="{ rotated: !collapsed }">
          <svg width="9" height="9" viewBox="0 0 9 9"><path d="M2 1.5 L5.5 4.5 L2 7.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" /></svg>
        </span>
        <span class="cd-title">{{ props.scope === 'cockpit' ? '开发舱对话' : 'AI 助手' }}</span>
        <span v-if="sending" class="cd-live">AI 正在查代码<span class="cd-dots">…</span></span>
        <!-- 折叠态也要感知后台进展：工作流执行期 sending 已释放，靠成员抽屉状态补指示 -->
        <span v-if="collapsed && activeWorkflowIds.length" class="cd-live cd-live-wf">
          <i class="cd-live-dot" aria-hidden="true" />工作流进行中
        </span>
      </span>
      <span class="cd-hint">用大白话问代码：数值在哪 · 逻辑怎么走 · 报错怎么改</span>
      <span class="cd-spacer" />
      <button
        class="cd-btn"
        :class="{ 'cd-btn-on': sessionOpen }"
        :aria-pressed="sessionOpen"
        aria-label="会话历史"
        title="查看会话历史或开始新对话"
        @click.stop="sessionOpen = !sessionOpen; sessionOpen && refreshSessionList()"
      >
        <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
          <path d="M2 2.2h9v6.4H6.4L3.2 11V8.6H2z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>
          <path d="M4.1 4.4h4.8M4.1 6.2h3.5" stroke="currentColor" stroke-width="1" stroke-linecap="round"/>
        </svg>
        <span>会话</span>
      </button>
      <button v-if="props.scope === 'default'"
        class="cd-btn"
        :class="{ 'cd-btn-on': wfHistoryOpen }"
        :aria-pressed="wfHistoryOpen"
        aria-label="工作流历史"
        title="工作流历史：查看进度、中断运行中的工作流、删除已结束记录"
        @click.stop="wfHistoryOpen ? (wfHistoryOpen = false) : openWfHistory()"
      >
        <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
          <path d="M2.5 3.2h8v3.2h-8z M2.5 8h5v2.6h-5z" fill="none" stroke="currentColor" stroke-width="1.05" stroke-linejoin="round"/>
          <path d="M8.7 8.2l2.1 1.2-2.1 1.2z" fill="currentColor"/>
        </svg>
        <span>工作流</span>
      </button>
      <button
        class="cd-btn cd-size-btn"
        :class="{ 'cd-btn-on': expanded }"
        :aria-pressed="expanded"
        :title="expanded ? '收窄对话区' : '展开对话区，查看更多回答内容'"
        @click.stop="expanded = !expanded"
      >
        <svg v-if="!expanded" width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
          <path d="M2 5 V2 H5 M8 2 H11 V5 M11 8 V11 H8 M5 11 H2 V8" fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <svg v-else width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
          <path d="M5 2 H2 V5 M8 2 H11 V5 M11 8 V11 H8 M5 11 H2 V8" fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>{{ expanded ? '收窄' : '展开' }}</span>
      </button>
    </header>


    <SessionHistoryPopover
      :open="sessionOpen" :items="sessionItems" :busy="sessionBusy" :error="sessionError"
      :current-id="chatSession" :can-clear="!!messages.length || demoMode"
      @close="sessionOpen = false" @refresh="refreshSessionList" @create="createConversation"
      @select="switchSession" @delete="deleteSessionItem" @clear="clearCurrentFromPop"
    />
    <WorkflowHistoryPopover
      :open="wfHistoryOpen" :items="wfHistoryItems" :busy="wfHistoryBusy" :error="wfHistoryError"
      :acting-id="wfHistoryActingId"
      @close="wfHistoryOpen = false" @refresh="refreshWfHistory" @reattach="reattachWfHistory"
      @interrupt="interruptWfHistory" @delete="deleteWfHistory"
    />

    <EngineConnectPopover />

    <!-- 折叠时不再卸载对话区：靠高度过渡 + 裁剪做顺滑展开/收起，状态与滚动位置保留 -->
      <div :id="`docmind-chat-body-${props.scope}`" ref="scroller" class="cd-body" @scroll="onScroll" @wheel="onWheel">
        <p v-if="historyError" role="alert">{{ historyError }}</p>
        <div v-for="m in messages" :key="m.id" class="cd-msg" :class="`cd-msg-${m.role}`">
          <div v-if="m.role === 'activity'" class="cd-act-note">
            <svg v-if="m.activityKind === 'inspect'" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
              <path d="M1 5.5 C2.6 3.4 4.2 2.6 6 2.6 C7.8 2.6 9.4 3.4 11 5.5 C9.4 7.6 7.8 8.4 6 8.4 C4.2 8.4 2.6 7.6 1 5.5 Z" fill="none" stroke="currentColor" stroke-width="1"/>
              <circle cx="6" cy="5.5" r="1.5" fill="none" stroke="currentColor" stroke-width="1"/>
            </svg>
            <span>{{ m.text }}</span>
            <small v-if="m.imageCount">· 已附当前画面</small>
          </div>
          <div v-else-if="m.role === 'user'" class="cd-user-bubble">
            <span v-if="m.imageCount" class="cd-msg-attachment">{{ m.imageCount }} 张图片</span>
            <span>{{ m.text }}</span>
          </div>
          <template v-else>
            <div v-if="m.trace.length || m.reasoning || m.status === 'streaming'" class="cd-run-head">
              <span class="cd-run-avatar">✦</span>
              <span class="cd-run-role">交付代理</span>
              <span class="cd-run-status wb-status-chip" :class="`cd-run-${m.status}`">
                {{ m.status === 'streaming' ? '正在执行' : m.status === 'done' ? '已完成' : m.status === 'error' ? '执行失败' : '已停止' }}
              </span>
              <span class="cd-run-time" v-if="elapsedLabel(m)">· {{ elapsedLabel(m) }}</span>
            </div>
            <div v-for="(n, i) in m.notices" :key="'n' + i" class="cd-notice">
              <svg width="11" height="11" viewBox="0 0 11 11"><circle cx="5.5" cy="5.5" r="4.6" fill="none" stroke="currentColor" stroke-width="1"/><path d="M5.5 4.6 V7.6" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/><circle cx="5.5" cy="3" r=".75" fill="currentColor"/></svg>
              <span>{{ n }}</span>
            </div>
            <button v-if="m.recoverable" class="cd-resume" @click="send('继续完成任务')">
              ↻ 继续上次任务
            </button>
            <WorkflowCard
              v-if="m.workflow?.workflowId"
              :key="m.workflow.workflowId"
              :workflow-id="m.workflow.workflowId"
              :seed="m.workflow.seed"
              @activity="onCardActivity"
              @team="onWfTeam"
              @gate="(open: boolean) => onWfGate(m.workflow!.workflowId, open)"
            />
            <div v-else-if="m.workflow && m.status === 'streaming'" class="cd-wf-pending">
              <span class="cd-wf-spinner" aria-hidden="true" />
              正在创建工作流，方案马上在对话中展开…
            </div>
            <div v-if="m.plan.length" class="cd-plan">
              <div class="cd-plan-title">执行计划</div>
              <div v-for="(s, i) in m.plan" :key="'p' + i" class="cd-plan-step">
                <span class="cd-plan-idx">{{ i + 1 }}</span><span>{{ s }}</span>
              </div>
            </div>
            <div v-if="m.reasoning" class="cd-reason">
              <button class="cd-reason-head" @click="toggleReason(m.id)">
                {{ reasonOpen.has(m.id) ? '▾ 深度思考' : '▸ 深度思考（点击展开）' }}
                <span v-if="m.status === 'streaming'" class="cd-dots">模型思考中…</span>
              </button>
              <div v-if="reasonOpen.has(m.id)" class="cd-reason-body">{{ m.reasoning }}</div>
            </div>
            <div v-if="visibleTrace(m).length" class="cd-activity">
              <button class="cd-activity-segment" :aria-expanded="traceSegmentIsOpen(m)" @click="toggleTraceSegment(m.id)">
                <span class="cd-activity-segment-copy"><b>本轮任务动作</b><span>{{ traceSegmentTitle(m) }}</span></span>
                <span class="cd-activity-segment-count">{{ visibleTrace(m).length }} 条记录</span>
                <span aria-hidden="true">{{ traceSegmentIsOpen(m) ? '▾' : '▸' }}</span>
              </button>
              <div v-if="traceSegmentIsOpen(m)" class="cd-activity-steps">
              <div v-for="row in visibleTrace(m)" :key="row.index" class="cd-activity-item" :class="`cd-activity-${traceState(m, row.item, row.index)}`">
                <div v-if="traceGroupStart(m, row.index)" class="cd-activity-group-label">{{ traceGroupLabel(row.item) }}</div>
                <button
                  class="cd-activity-row"
                  :aria-expanded="activityIsOpen(m.id, row.index, m)"
                  :aria-label="`${traceTitle(row.item)}，${traceStateLabel(traceState(m, row.item, row.index))}，点击查看详情`"
                  @click="toggleActivity(m.id, row.index)"
                >
                  <span class="cd-activity-glyph">{{ TRACE_GLYPH[row.item.type] || '·' }}</span>
                  <span class="cd-activity-main">
                    <span class="cd-activity-kicker">{{ row.item.type === 'action' ? '工具调用' : row.item.type === 'observation' ? '工具返回' : row.item.type === 'reflection' ? '执行提示' : '执行记录' }}</span>
                    <span class="cd-activity-title">{{ traceTitle(row.item) }}</span>
                    <span class="cd-activity-summary">{{ traceSummary(row.item) }}</span>
                  </span>
                  <span class="cd-activity-meta">
                    <span class="cd-activity-state">{{ traceStateLabel(traceState(m, row.item, row.index), row.item) }}</span>
                    <span v-if="traceElapsed(row.item)" class="cd-activity-time">{{ traceElapsed(row.item) }}</span>
                    <span class="cd-activity-chevron">{{ activityIsOpen(m.id, row.index, m) ? '▾' : '▸' }}</span>
                  </span>
                </button>
                <div v-if="activityIsOpen(m.id, row.index, m)" class="cd-activity-detail">
                  <span class="cd-activity-detail-label">事件详情</span>
                  <code>{{ row.item.text }}</code>
                </div>
              </div>
              </div>
            </div>
            <!-- 流式中按 ~8fps 节流重渲染 markdown（格式边出边成型，又不逐 token 重建
                 DOM）；结束瞬间由 answerHtml 缓存接管成稿，视觉无跳变 -->
            <div v-if="m.text" class="ai-md cd-answer" :class="{ 'cd-answer-live': m.status === 'streaming' }">
              <template v-if="m.status === 'streaming'"><span v-html="streamHtmlOf(m)" /><span class="cd-caret" aria-hidden="true" /></template>
              <span v-else v-html="answerHtml(m)" />
            </div>
            <details v-if="m.status === 'done' && webRefsOf(m).length" class="cd-web-sources">
              <summary>联网来源（{{ webRefsOf(m).length }}）</summary>
              <a v-for="(s, i) in webRefsOf(m)" :key="s.url + i" class="cd-web-source" :href="s.url" target="_blank" rel="noopener noreferrer">
                <span class="cd-web-source-title">{{ s.title }}</span>
                <span class="cd-web-source-url">{{ s.url }}</span>
                <span v-if="s.score" class="cd-web-source-score">参考 {{ s.score }}</span>
              </a>
            </details>
            <div v-if="m.status === 'error'" class="cd-error">⚠ {{ m.error }}</div>
            <div v-if="m.status === 'done' && refsOf(m).length" class="cd-refs">
              <button
                v-for="r in refsOf(m)"
                :key="r.path + ':' + r.line"
                class="cd-ref"
                :title="'打开 ' + r.path + (r.line ? ':' + r.line : '')"
                @click="openRef(r)"
              >
                <svg width="11" height="11" viewBox="0 0 11 11"><path d="M2 1.5 H6.5 L9 4 V9.5 H2 Z M6.5 1.5 V4 H9" fill="none" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/></svg>
                <span class="cd-ref-path">{{ r.path }}</span>
                <span v-if="r.line" class="cd-ref-line">:{{ r.line }}</span>
              </button>
            </div>
          </template>
        </div>
      </div>
      <button v-if="sending && !stickToBottom" class="cd-jump-bottom" title="恢复跟随最新输出" @click="resumeAutoScroll">
        ↓ 回到底部
      </button>

      <!-- 孤儿工作流恢复条：后端未终结但当前对话没有卡片（多为刷新后），不处理会持续拦截新请求 -->
      <div v-if="orphanWf && props.scope === 'default'" class="cd-orphan" role="alert">
        <span class="cd-orphan-glyph" aria-hidden="true">⚠</span>
        <div class="cd-orphan-body">
          <div class="cd-orphan-title">
            该项目有一个未结束的工作流（{{ orphanStatusLabel }}），当前对话里没有它的审批卡片
          </div>
          <div class="cd-orphan-desc">
            它可能创建于页面刷新前；在处理它之前，新的工作流请求会被拦截。ID：{{ orphanWf.workflow_id }}
          </div>
          <div v-if="orphanError" class="cd-orphan-err">{{ orphanError }}</div>
        </div>
        <button class="cd-mini" :disabled="orphanBusy" @click="reattachOrphan">挂回对话继续</button>
        <button class="cd-mini cd-mini-danger" :disabled="orphanBusy" @click="interruptOrphan">
          {{ orphanBusy ? '处理中…' : '中断它' }}
        </button>
      </div>

      <ChatComposer
        ref="composerEl"
        v-model="input"
        :sending="sending"
        :demo-mode="demoMode"
        :video-busy="videoBusy"
        :pending-images="pendingImages"
        :attachment-error="attachmentError"
        :model-label="modelLabel"
        :vision-label="visionLabel"
        :web-on="webOn"
        :thinking-supported="thinkingSupported"
        :thinking-effective="thinkingEffective"
        :thinking-native="thinkingNative"
        :thinking-mode="thinkingMode"
        :thinking-title="thinkingTitle"
        :web-title="webTitle"
        :usage="usage"
        :usage-title="usageTitle"
        :voice-active="voiceActive"
        :voice-supported="props.scope === 'cockpit' && voiceSupported"
        @submit="send()"
        @stop="stop"
        @images-picked="onImagesPicked"
        @video-picked="onVideoPicked"
        @remove-image="removeImage"
        @update:web-on="webOn = $event"
        @toggle-thinking="toggleThinking"
        @open-model-switcher="openModelSwitcher"
        @toggle-voice="toggleVoice"
      />

    <WorkflowMembersDock
      :active="!!wfActiveTeam"
      :members="wfActiveTeam?.members || []"
      @focus="focusWfMember"
    />
    <ModelSettingsDialog
      v-if="settingsOpen"
      :visible="settingsOpen"
      :config="modelConfig"
      @close="settingsOpen = false"
      @saved="onModelSaved"
    />
    <ModelSwitcherPopover
      :visible="modelSwitcherOpen"
      :config="modelConfig"
      :load-error="modelSwitchError"
      @close="modelSwitcherOpen = false"
      @manage="openUnifiedModelSettings"
      @saved="onModelSaved"
    />
  </section>
</template>

<style scoped>
.cd-dock {
  position: relative;
  flex: 0 0 auto;
  border-top: 1px solid var(--border);
  background: var(--bg-raised);
  display: flex;
  flex-direction: column;
  height: min(62vh, 820px);
  min-height: 380px;
  max-height: calc(100vh - 86px);
  /* 高度是布局属性，动画期间只改高度一个变量；曲线先快后稳，配合 chevron 同步翻转 */
  transition: height .24s cubic-bezier(.32, .72, 0, 1),
              min-height .24s cubic-bezier(.32, .72, 0, 1);
}
/* 展开态高度 = 视口 - 上方「顶栏 + 编辑器区」的最小高度（--wb-main-min）。
   旧值写死 100vh-86px：实测上方最小是 118px（窄屏换行后 126/143px），86 偏小 32，
   于是展开后 dock 把输入栏底部顶出视口 32px —— 输入框被裁掉一截（"对话框被挤没了"）。
   现在按宽度分档把 --wb-main-min 调在实测值之上（124/132/150），
   展开时上方恰好压到最小、dock 底边贴视口底且余量恒 >= 0，任何尺寸下都不裁输入栏。 */
.cd-dock.cd-expanded {
  height: calc(100vh - var(--wb-main-min, 124px));
  max-height: calc(100vh - var(--wb-main-min, 124px));
  min-height: 380px;
}
/* 折叠态用显式高度（=头部 32 + 顶边 1），auto 无法参与过渡；
   双类名保证在矮屏媒体查询里也能覆盖 .cd-dock 的高度声明 */
.cd-dock.cd-collapsed { height: 33px; min-height: 0; max-height: none; }
/* 折叠收起时裁掉对话区与输入栏（弹层已 Teleport 到 body，不受裁剪影响） */
.cd-dock.cd-clipping { overflow: hidden; }
/* 人工审批门打开：锁操作区与头部，但不锁聊天滚动（遮罩 pointer-events:none）。
   头部必须整体锁定（含折叠点击）：否则折叠后 33px 裁剪会把居中审批模态吞掉。 */
.cd-dock.cd-gate-blocked .cd-inputbar,
.cd-dock.cd-gate-blocked .cd-head {
  pointer-events: none;
}
.cd-dock.cd-gate-blocked .cd-inputbar { opacity: .55; }
@media (max-height: 620px) {
  .cd-dock {
    height: min(52vh, 340px);
    min-height: 240px;
    max-height: calc(100vh - 86px);
  }
  /* 矮屏：上方最小高度不随视口高度变化，继续复用 --wb-main-min */
  .cd-dock.cd-expanded { height: calc(100vh - var(--wb-main-min, 124px)); }
}

.cd-head {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 10px;
  user-select: none;
}
/* 唯一的折叠热区：箭头 + 标题 + 状态指示；提示文字与按钮不触发折叠 */
.cd-head-toggle {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 6px; margin-left: -6px;
  border-radius: 6px; cursor: pointer; user-select: none;
}
.cd-head-toggle:hover { background: var(--bg-hover); }
.cd-head-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.cd-head-toggle .cd-live-wf { cursor: inherit; }
.cd-head-toggle .cd-chevron { flex: 0 0 auto; }
.cd-chevron {
  display: flex; color: var(--text-muted);
  transform-origin: center;
  transition: transform .24s cubic-bezier(.32, .72, 0, 1);
  will-change: transform;
}
.cd-chevron.rotated { transform: rotate(90deg); }
.cd-title { font-size: 12px; font-weight: 600; color: var(--text); }
.cd-hint { font-size: 11px; color: var(--text-faint); }
.cd-live { font-size: 11px; color: var(--amber); margin-left: 4px; }
/* 折叠态工作流后台指示：脉冲点 + 文字，位于标题折叠热区内 */
.cd-live-wf {
  display: inline-flex; align-items: center; gap: 5px;
  color: var(--accent); cursor: pointer;
}
.cd-live-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent);
  animation: cd-live-pulse 1.4s ease-in-out infinite;
}
@keyframes cd-live-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: .35; transform: scale(.7); }
}
@media (prefers-reduced-motion: reduce) { .cd-live-dot { animation: none; } }
.cd-spacer { flex: 1; }
.cd-btn {
  display: inline-flex; align-items: center; gap: 5px;
  height: 22px; padding: 0 8px;
  border: 1px solid var(--border); border-radius: 5px;
  background: transparent; color: var(--text-muted);
  font-size: 11px; cursor: pointer;
}
.cd-btn:hover { color: var(--text); border-color: var(--border-strong); }
.cd-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.cd-btn:active { transform: translateY(1px); }
.cd-btn-on { color: var(--accent); border-color: var(--accent); }

/* 弹层 */
.cd-pop-mask {
  position: fixed;
  inset: 0;
  z-index: 59;
}
.cd-pop {
  position: fixed;
  /* 26px 底部状态栏 + 原相对面板的 38px 间距 */
  left: 10px; bottom: 64px;
  width: 460px; max-width: calc(100vw - 24px);
  max-height: 60vh; overflow-y: auto;
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  box-shadow: 0 14px 38px rgba(35,52,84,.2);
  padding: 10px 12px;
  z-index: 60;
  animation: cd-pop-in .18s var(--ease-spring) both;
}
.cd-pop-title { font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 8px; }
.cd-mini {
  height: 21px; padding: 0 9px; font-size: 11px;
  border: 1px solid var(--border-strong); border-radius: 5px;
  background: transparent; color: var(--text-muted); cursor: pointer; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease;
}
.cd-mini:hover:not(:disabled) { color: var(--text); border-color: var(--accent); transform: translateY(-1px); }
.cd-mini:disabled { opacity: .45; cursor: default; }
.cd-mini-danger:hover:not(:disabled) { color: var(--danger); border-color: var(--danger); }

/* 消息区 */
.cd-jump-bottom {
  position: absolute; right: 14px; bottom: 58px; z-index: 3;
  border: 1px solid var(--border-2); border-radius: 999px; padding: 4px 9px;
  background: var(--bg-raised); color: var(--accent); font-size: 10.5px;
  box-shadow: 0 2px 8px rgba(30, 50, 90, .14); cursor: pointer;
}
.cd-jump-bottom:hover { background: var(--bg-selected); transform: translateY(-1px); }
/* 孤儿工作流恢复条：贴在输入框上沿，琥珀色提示但不遮挡消息流 */
.cd-orphan {
  display: flex; align-items: center; gap: 8px;
  margin: 6px 12px 0; padding: 8px 10px;
  border: 1px solid var(--amber); border-radius: 8px;
  background: color-mix(in srgb, var(--amber) 10%, var(--bg-raised));
}
.cd-orphan-glyph { color: var(--amber); font-size: 13px; line-height: 1; flex: none; }
.cd-orphan-body { flex: 1 1 auto; min-width: 0; }
.cd-orphan-title { font-size: 11.5px; font-weight: 600; color: var(--text); }
.cd-orphan-desc {
  font-size: 10.5px; color: var(--text-muted); margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cd-orphan-err { font-size: 10.5px; color: var(--danger); margin-top: 2px; }
.cd-orphan .cd-mini { flex: none; white-space: nowrap; }
.cd-body { flex: 1 1 auto; overflow-y: auto; padding: 8px 14px 4px; min-height: 90px; }
.cd-empty { padding: 14px 6px; color: var(--text-muted); }
.cd-empty-title { font-size: 12px; margin: 0 0 10px; color: var(--text-muted); }
.cd-quicks { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.cd-quick {
  text-align: left; font-size: 11.5px; padding: 5px 10px;
  border: 1px solid var(--border); border-radius: 12px;
  background: transparent; color: var(--text-muted); cursor: pointer; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease;
}
.cd-quick:hover { color: var(--accent); border-color: var(--accent); background: var(--bg-selected); transform: translateX(2px); }

.cd-msg { margin-bottom: 10px; }
.cd-user-bubble {
  display: inline-block; max-width: 82%;
  margin-left: auto;
  padding: 6px 10px; border-radius: 9px 9px 2px 9px;
  background: var(--bg-selected); color: var(--text);
  font-size: 12.5px; white-space: pre-wrap;
}
.cd-msg-user { display: flex; justify-content: flex-end; }
/* 应用自发起活动字条：居中、弱化，不是用户气泡也不是 AI 回复 */
.cd-msg-activity { display: flex; justify-content: center; }
.cd-act-note {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px; border-radius: 99px;
  background: var(--bg-hover); color: var(--text-faint);
  font-size: 11px; line-height: 1.5;
}
.cd-act-note small { color: var(--text-faint); opacity: .8; }
.cd-answer { font-size: 12.5px; max-width: 96%; }
/* 流式期的节流 markdown 与成稿同一样式；光标在块级内容后自然落到新行 */
.cd-caret {
  display: inline-block; width: 6px; height: 1.05em; margin-left: 2px;
  vertical-align: -0.18em; border-radius: 1px;
  background: var(--accent); animation: cd-caret 1.05s steps(1, end) infinite;
}
@keyframes cd-caret { 0%, 55% { opacity: 1; } 56%, 100% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .cd-caret { animation: none; } }
.cd-thinking, .cd-error { font-size: 12px; color: var(--text-muted); padding: 6px 9px; border-radius: 7px; background: var(--bg-hover); }
.cd-thinking { border-left: 2px solid var(--accent); }
.cd-slow-notice { margin: 6px 0; padding: 7px 9px; border: 1px solid color-mix(in srgb, var(--amber) 45%, var(--border)); border-radius: 6px; color: var(--text-muted); background: color-mix(in srgb, var(--amber) 10%, transparent); font-size: 11px; line-height: 1.45; }
.cd-error { color: var(--danger); border: 1px solid rgba(214,78,78,.22); background: rgba(214,78,78,.05); }

.cd-run-head {
  display: flex; align-items: center; gap: 6px;
  min-height: 22px; margin: 1px 0 5px;
  color: var(--text-muted); font-size: 11px;
}
.cd-run-avatar {
  width: 17px; height: 17px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  color: #fff; background: var(--accent); font-size: 10px;
}
.cd-run-role { color: var(--text); font-weight: 600; }
.cd-run-status { color: var(--text-faint); }
.cd-run-streaming { color: var(--accent); }
.cd-run-done { color: var(--green); }
.cd-run-error { color: var(--danger); }
.cd-run-stopped { color: var(--amber); }
.cd-run-streaming::before { content: ''; display: inline-block; width: 9px; height: 9px; margin: 0 6px 0 0; border: 1.5px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: cd-run-spin .8s linear infinite; }
@keyframes cd-run-spin { to { transform: rotate(360deg); } }
.cd-run-time { color: var(--text-faint); font-variant-numeric: tabular-nums; }

.cd-wf-pending {
  display: flex; align-items: center; gap: 9px;
  margin: 4px 0 8px; padding: 10px 12px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-hover); color: var(--text-muted); font-size: 12.5px;
}
.cd-wf-spinner {
  width: 13px; height: 13px; border-radius: 50%; flex: 0 0 auto;
  border: 2px solid var(--border-strong); border-top-color: var(--accent);
  animation: cd-wf-spin .7s linear infinite;
}
@keyframes cd-wf-spin { to { transform: rotate(360deg); } }

/* 聊天内的纵向活动流：动作按 SSE 到达顺序实时追加，详情按行折叠。 */
.cd-activity {
  position: relative; margin: 6px 0 9px;
  border: 1px solid var(--border); border-radius: var(--radius-lg);
  background: var(--bg-raised); overflow: hidden;
}
.cd-activity-segment { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; border: 0; background: var(--bg-hover); padding: 8px 10px; text-align: left; color: var(--text-muted); cursor: pointer; }
.cd-activity-segment:hover { background: var(--bg-selected); }
.cd-activity-segment:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.cd-activity-segment-copy { display: grid; min-width: 0; flex: 1; gap: 2px; }
.cd-activity-segment-copy b { color: var(--text); font-size: 11px; }
.cd-activity-segment-copy span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--accent); font-size: 11px; }
.cd-activity-segment-count { white-space: nowrap; color: var(--text-faint); font-size: 10px; }
.cd-activity-steps { margin: 7px 10px 9px 17px; padding-left: 16px; border-left: 1px solid var(--border); }
.cd-activity-item { position: relative; margin: 0; }
.cd-activity-group-label {
  margin: 8px 0 2px -2px;
  color: var(--text-faint);
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: .05em;
}
.cd-activity-item:first-child .cd-activity-group-label { margin-top: 2px; }
.cd-activity-item::before {
  content: ''; position: absolute; left: -20px; top: 9px;
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-faint); border: 2px solid var(--bg-raised);
  box-sizing: content-box;
}
.cd-activity-running::before { background: var(--accent); box-shadow: 0 0 0 3px rgba(37,96,212,.12); }
.cd-activity-ok::before { background: var(--green); }
.cd-activity-warn::before { background: var(--amber); }
.cd-activity-error::before { background: var(--danger); }
.cd-activity-row {
  width: 100%; min-width: 0; display: flex; align-items: center; gap: 7px;
  border: 0; background: transparent; padding: 4px 0; text-align: left;
  color: var(--text-muted); cursor: pointer;
}
.cd-activity-row:hover { color: var(--text); }
.cd-activity-row:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.cd-activity-glyph {
  flex: 0 0 15px; width: 15px; text-align: center;
  color: var(--text-faint); font-size: 13px; line-height: 1;
}
.cd-activity-running .cd-activity-glyph { color: var(--accent); }
.cd-activity-ok .cd-activity-glyph { color: var(--green); }
.cd-activity-warn .cd-activity-glyph { color: var(--amber); }
.cd-activity-error .cd-activity-glyph { color: var(--danger); }
.cd-activity-main { min-width: 0; flex: 1; display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: baseline; column-gap: 7px; row-gap: 1px; }
.cd-activity-kicker { grid-column: 1 / -1; color: var(--text-faint); font-size: 9.5px; line-height: 1.2; letter-spacing: .04em; text-transform: uppercase; }
.cd-activity-title { min-width: 0; color: var(--text); font-size: 12.5px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cd-activity-summary {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--text-muted); font-size: 12px;
}
.cd-activity-meta { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 5px; font-size: 10.5px; }
.cd-activity-state { padding: 2px 6px; border-radius: 999px; color: var(--text-faint); background: var(--bg-hover); white-space: nowrap; }
.cd-activity-running .cd-activity-state { color: var(--accent); background: rgba(47,111,237,.1); }
.cd-activity-ok .cd-activity-state { color: var(--green); background: rgba(28,158,102,.09); }
.cd-activity-warn .cd-activity-state { color: var(--amber); background: rgba(200,129,28,.11); }
.cd-activity-error .cd-activity-state { color: var(--danger); background: rgba(224,72,79,.1); }
.cd-activity-time { color: var(--text-faint); font-variant-numeric: tabular-nums; }
.cd-activity-chevron { color: var(--text-faint); font-size: 10px; }
.cd-activity-detail {
  margin: 0 0 4px 22px; padding: 5px 8px;
  border-left: 2px solid var(--border); background: var(--bg-hover);
  color: var(--text-dim); font: 10.5px/1.5 var(--font-mono, monospace);
  white-space: pre-wrap; overflow-wrap: anywhere; max-height: 130px; overflow: auto;
}
.cd-activity-detail-label { display: block; margin-bottom: 3px; color: var(--text-faint); font: 9.5px/1.2 var(--font-ui); letter-spacing: .04em; }
.cd-activity-detail code { display: block; color: inherit; font: inherit; white-space: pre-wrap; overflow-wrap: anywhere; }

.cd-refs { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.cd-ref {
  display: inline-flex; align-items: center; gap: 4px;
  height: 22px; padding: 0 8px;
  border: 1px solid var(--border-2); border-radius: 5px;
  background: var(--accent-soft); color: var(--accent);
  font-family: var(--font-mono); font-size: 11px; cursor: pointer;
}
.cd-ref:hover { background: var(--bg-selected); border-color: var(--accent); }
.cd-ref-line { color: var(--amber); }
.cd-web-sources { margin-top: 8px; border-top: 1px solid var(--border); padding-top: 5px; max-width: 96%; }
.cd-web-sources summary { cursor: pointer; color: var(--text-muted); font-size: 11px; }
.cd-web-source { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 8px; margin-top: 5px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 5px; color: var(--text); text-decoration: none; background: var(--bg); }
.cd-web-source:hover { border-color: var(--accent); background: var(--bg-selected); }
.cd-web-source-title { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-web-source-url { grid-column: 1 / -1; color: var(--accent); font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-web-source-score { color: var(--text-faint); font-size: 10px; }





/* 系统通知（上下文压缩等） */
.cd-notice {
  display: flex; align-items: flex-start; gap: 5px;
  margin: 2px 0 5px; padding: 4px 8px;
  border-radius: 5px; background: var(--bg-hover);
  font-size: 10.5px; color: var(--text-dim); line-height: 1.5;
}
.cd-notice svg { flex: 0 0 auto; margin-top: 2px; color: var(--text-faint); }
.cd-resume {
  display: inline-flex; align-items: center; gap: 5px;
  margin: 1px 0 7px; padding: 5px 9px;
  border: 1px solid var(--accent); border-radius: 5px;
  background: rgba(47,111,237,.08); color: var(--accent);
  font-size: 11px; cursor: pointer;
}
.cd-msg-attachment { display: inline-flex; margin-right: 6px; color: var(--accent); font-size: 10.5px; }
.cd-resume:hover { background: rgba(47,111,237,.15); }

/* 计划步骤 */
.cd-plan {
  margin: 2px 0 6px; padding: 7px 9px;
  border: 1px solid var(--border); border-radius: 7px; background: var(--bg-hover);
}
.cd-plan-title { font-size: 11px; font-weight: 600; color: var(--text-muted); margin-bottom: 5px; }
.cd-plan-step { display: flex; gap: 7px; align-items: flex-start; font-size: 11px; color: var(--text-dim); line-height: 1.55; }
.cd-plan-step + .cd-plan-step { margin-top: 3px; }
.cd-plan-idx {
  flex: 0 0 15px; width: 15px; height: 15px; margin-top: 1px;
  border-radius: 50%; background: var(--accent); color: #fff;
  font-size: 9.5px; display: inline-flex; align-items: center; justify-content: center;
}

/* 深度思考窗口（reasoning_content 流） */
.cd-reason {
  margin: 2px 0 6px; border: 1px solid var(--border);
  border-left: 2px solid var(--tool);
  border-radius: 6px; background: var(--bg-hover); overflow: hidden;
}
.cd-reason-head {
  width: 100%; text-align: left;
  border: none; background: none; padding: 5px 9px; cursor: pointer;
  font-size: 11px; color: var(--tool);
}
.cd-reason-head:hover { color: var(--text); }
.cd-reason-body {
  padding: 2px 10px 8px 20px;
  font-size: 11px; line-height: 1.65; color: var(--text-dim);
  white-space: pre-wrap; word-break: break-word;
  max-height: 220px; overflow-y: auto;
}

/* 新功能按钮增多后，窄宽度下逐级收起装饰性文字，保证头部不溢出 */
@media (max-width: 1280px) {
  .cd-hint { display: none; }
}
@media (max-width: 1020px) {
  .cd-live { display: none; }
  .cd-btn { padding: 0 7px; }
  .cd-btn span { display: none; }
  .cd-size-btn span { display: none; }
}
/* 尊重系统「减少动态效果」：高度/箭头翻转改为瞬时，光标不闪烁 */
@media (prefers-reduced-motion: reduce) {
  .cd-dock { transition: none; }
  .cd-chevron { transition: none; }
}

/* ---------- 对话区视觉细化 ---------- */
.cd-dock {
  background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(248,250,254,.98));
  box-shadow: 0 -8px 26px rgba(35,52,84,.06);
}
.cd-head {
  background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(247,249,253,.92));
  border-bottom: 1px solid color-mix(in srgb, var(--border) 82%, white);
}
.cd-body {
  scroll-behavior: smooth;
  background: linear-gradient(180deg, rgba(248,250,253,.5), rgba(255,255,255,.8));
}
.cd-msg { animation: cd-message-in .22s var(--ease-spring) both; }
.cd-user-bubble {
  border: 1px solid rgba(47,111,237,.1);
  box-shadow: 0 3px 10px rgba(47,111,237,.08);
}
.cd-answer { line-height: 1.7; }
.cd-activity {
  padding-top: 3px;
  padding-bottom: 3px;
  border-left-color: color-mix(in srgb, var(--accent) 25%, var(--border));
}
.cd-activity-row { border-radius: 6px; padding: 5px 6px; transition: background .16s, color .16s, transform .16s var(--ease-spring); }
.cd-activity-row:hover { background: rgba(47,111,237,.06); transform: translateX(2px); }
.cd-activity-detail { animation: cd-detail-in .18s var(--ease-spring) both; border-radius: 0 5px 5px 0; }
.cd-notice { border: 1px solid rgba(152,163,180,.22); background: rgba(244,247,251,.8); }
.cd-inputbar { background: linear-gradient(180deg, rgba(250,252,255,.88), rgba(245,248,252,.96)); border-top: 1px solid var(--border); }
.cd-input { box-shadow: inset 0 1px 2px rgba(35,52,84,.03); transition: border-color .16s, box-shadow .16s, background .16s; }
.cd-input:focus { background: #fff; box-shadow: 0 0 0 3px rgba(47,111,237,.11), inset 0 1px 2px rgba(35,52,84,.03); }
.cd-send { box-shadow: 0 4px 11px rgba(47,111,237,.18); transition: transform .16s var(--ease-spring), box-shadow .16s, filter .16s; }
.cd-send:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 7px 15px rgba(47,111,237,.24); filter: brightness(1.03); }
.cd-chip { transition: transform .16s var(--ease-spring), border-color .16s, background .16s, color .16s; }
.cd-chip:hover:not(:disabled):not(.cd-chip-off) { transform: translateY(-1px); }

@keyframes cd-message-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes cd-detail-in {
  from { opacity: 0; transform: translateY(-3px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes cd-pop-in { from { opacity: 0; transform: translateY(5px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@keyframes cd-run-pulse { 0%, 100% { opacity: .6; transform: scale(.85); } 50% { opacity: 1; transform: scale(1); } }
@media (max-width: 560px) {
  .cd-head { gap: 5px; padding-inline: 7px; }
  .cd-head-toggle { margin-left: -4px; padding-inline: 4px; }
  .cd-title { max-width: 76px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cd-btn { height: 24px; padding-inline: 6px; }
  .cd-body { padding-left: 9px; padding-right: 9px; }
  .cd-user-bubble { max-width: 94%; }
  .cd-answer { max-width: 100%; }
  .cd-activity-main { grid-template-columns: 1fr; gap: 1px; }
  .cd-activity-summary { grid-column: 1; }
  .cd-activity-meta { margin-left: auto; }
  .cd-pop { left: 6px; bottom: 52px; width: calc(100vw - 12px); }
  .cd-orphan { align-items: flex-start; flex-wrap: wrap; margin-inline: 8px; }
  .cd-orphan-body { flex-basis: calc(100% - 28px); }
  .cd-orphan .cd-mini { margin-left: 22px; }
}
@media (prefers-reduced-motion: reduce) {
  .cd-mini, .cd-jump-bottom, .cd-quick, .cd-activity-row { transition: none; }
  .cd-pop, .cd-run-streaming::before { animation: none; }
}
</style>
