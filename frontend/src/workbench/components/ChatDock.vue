<script setup lang="ts">
// P0 底部 AI 对话台：全局代码问答/定位入口（ReAct agent，SSE）。
// - 答案中的文件引用渲染为可点击卡片：跳转代码行 + 文件树展开闪烁 + 分区高亮；
// - 引擎连接 / 会话历史 / 工作流历史均为独立弹层组件（components/*Popover.vue）；
// - 模型配置、审批门、孤儿工作流恢复、SSE 流式管线在 composables/ 与问答页共用。
import { nextTick, ref, watch, onMounted, onBeforeUnmount, computed, defineAsyncComponent } from 'vue'
// 直接依赖对话框域，避免经 workbench 聚合桶拖入编辑器链
import { askConfirm, askAlert } from '../composables/dialogs'
import { aiApi, voiceApi, agentApi, visionApi, contextApi, harnessApi, modelApi, browserLiveApi, getSessionId, getProjectId, setSessionId, startNewSession, startTabProbe } from '../api'
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
import { noteBrowserEvidence } from '../composables/browserLive'
import type { ChatActivityKind, ChatMsg, ChatTraceItem, BrowserEvidence } from '../composables/chat-types'
import { flowOf } from '../composables/chat-types'
import type { ChatArtifact } from '../composables/chat-types'
import ArtifactCards from './ArtifactCards.vue'
import { useAutoScroll } from '../composables/useAutoScroll'
import { useMessageRender } from '../composables/useMessageRender'
// 当前代码库：与资源管理器同源（tree 在切项目时会用新项目头重拉）。
// 不用 /api/config 的 code_root —— 那个字段是遗留指针，后端自己写明「切换项目后可能
// 滞后」（api.py 的 _project_root_or_error 注释），标错库比不标更糟。
import { useWorkbench } from '../composables/workbench'
// 展示偏好与问答页同源（一份 localStorage + 一个变更事件）：这里必须真的消费它，
// 否则「设置里关掉推理轨迹」在开发台对话台不生效。
import { prefs as displayPrefs } from '../../ask/askPrefs'
// 设置弹窗低频且体积大（117KB）：异步组件 + v-if，打开时才拉取
const ModelSettingsDialog = defineAsyncComponent(() => import('./ModelSettingsDialog.vue'))
const ModelSwitcherPopover = defineAsyncComponent(() => import('./ModelSwitcherPopover.vue'))
import SessionHistoryPopover from './SessionHistoryPopover.vue'
import WorkflowHistoryPopover from './WorkflowHistoryPopover.vue'
// 工作流总览卡片内联在对话流里（2026-10-22 回退：独立总览窗改回对话内展开）。
// 卡片仅在消息带工作流时按需异步加载；卡片内「点成员弹独立小窗」的逻辑不受影响。
const WorkflowCard = defineAsyncComponent(() => import('./WorkflowCard.vue'))
import WorkflowMembersDock from './WorkflowMembersDock.vue'
import ChatComposer from './ChatComposer.vue'
import type { WorkflowSummary, WorkflowState } from '../api'
import type { PreviewFeedbackRequest } from '../previewFeedback'

const props = withDefaults(defineProps<{ scope?: 'default' | 'cockpit' }>(), { scope: 'default' })
// 开发舱里侧栏文件树是隐藏的，「在问哪个库」只剩顶栏那颗项目名，而对话就在这块下方。
// 所以把代码库根目录搬到输入框同一行；普通对话台不显示（旁边就是资源管理器，重复了）。
const { tree, jumpToLine, revealPath } = useWorkbench()
const codeRoot = computed(() => tree.value?.code_root || '')
const codeRootLabel = computed(() => {
  const parts = codeRoot.value.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || ''
})
const voiceActive = ref(false)
// 语音是自主开发舱的协作通道；普通项目对话台不显示也不启动语音资源。
// 转写有两条独立通路：浏览器自带 Web Speech，或后端配了 STT（服务端转写）。
// 必须分开记 —— 合成一个布尔就没法在被禁时说清到底缺的是哪一条。
const hasWebSpeech = typeof window !== 'undefined'
  && ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window)
const voiceSttConfigured = ref(false)
// 2026-10-20：原来这里要求 scope==='cockpit'（自主开发舱），但那个 scope 已在 v2 重构里
// 随 AutonomousCockpit.vue 一起删掉了 —— 于是这个门控让**任何**挂载的对话台都拿不到
// 语音按钮，语音整段成了死代码（后端 /api/voice/* 一直好好的）。
// 语音能力（浏览器 Web Speech / 后端 STT+TTS）与 scope 无关，门控改成按能力本身判断。
const voiceSupported = computed(() => hasWebSpeech || voiceSttConfigured.value)
const voiceHint = computed(() => {
  if (voiceSupported.value) return ''
  const missing: string[] = []
  if (!hasWebSpeech) missing.push('本机浏览器没有 Web Speech（缺 SpeechRecognition）')
  if (!voiceSttConfigured.value) missing.push('后端也没配置语音转写（STT）')
  return `语音协作不可用：${missing.join('；')}。两条通路上打通任意一条即可（换 Edge / Chrome，或在设置里配好语音服务）。`
})
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
  if (!voiceSupported.value) return
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
  if (!voiceActive.value) return
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
  appEvents.emit('docmind:wf-focus-member', { workflowId: team.workflowId, taskId })
}

interface InterruptedRecovery {
  user: string
  assistant: string
  trace: ChatMsg['trace']
  reasoning: string
  plan: string[]
  startedAt?: number
  /** 精确断点续跑：失败消息上的后端 checkpoint id。 */
  checkpointId?: string
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
      checkpointId: typeof row.checkpointId === 'string' ? row.checkpointId : undefined,
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
/** 后端在回合异常/中断时落盘的人话占位：「（本轮未完成：原因）」。
 *  刷新后它曾被当成一条普通已完成回答，用户就再也看不到「继续」入口。 */
const INTERRUPTED_ANSWER_RE = /^（本轮未完成(?:[：:，]\s*([\s\S]*?))?）\s*$/
function interruptedReason(text: string): string {
  const hit = String(text || '').trim().match(INTERRUPTED_ANSWER_RE)
  if (!hit) return ''
  return (hit[1] || '').replace(/[。.\s]+$/, '') || '上一轮没有完成'
}
/** 点击「继续上次任务」：优先用页面里这条失败消息重建续接现场。
 *  刷新后 sessionStorage 已被 restoreInterrupted 消费掉，只发一句「继续」会让前端
 *  丢掉能看到的上下文；这里把用户问题与残留痕迹再塞回去，send() 才会走完整续接。 */
function resumeTurn(turn: ChatMsg) {
  if (sending.value) return
  if (!readInterruptedRecovery()) {
    const idx = messages.value.findIndex(m => m.id === turn.id)
    const user = idx > 0 ? messages.value[idx - 1] : undefined
    if (user?.role === 'user' && user.text) {
      try {
        sessionStorage.setItem(recoveryKey(), JSON.stringify({
          user: user.text,
          assistant: turn.text || '',
          trace: turn.trace?.slice(-24) || [],
          reasoning: turn.reasoning || '',
          plan: turn.plan?.slice(-12) || [],
          startedAt: turn.startedAt,
          checkpointId: turn.checkpointId,
        } satisfies InterruptedRecovery))
      } catch {}
    }
  }
  void send('继续完成任务')
}
// 兼容旧版会话：早期 API 曾把内部路由上下文拼在用户问题前并落盘。
// 仅清理从字符串开头出现的完整旧前缀，正文中提到“系统提示”不受影响。
const legacyPromptPrefix = /^\s*【系统提示】[\s\S]*?用户问题：\s*/
// 续接请求的完整包体（原始请求/上次进度/指令）只该给模型看；历史里只显示用户
// 真正敲下的那句「继续完成任务」，否则刷新后对话流里会冒出一大段内部提示。
const legacyContinuationPrefix = /^\s*这是上一个被页面切换或连接中断的回合的继续请求。[\s\S]*?用户续接指令：\s*/
function cleanLegacyPrompt(value: string): string {
  return String(value || '').replace(legacyPromptPrefix, '').replace(legacyContinuationPrefix, '').trimStart()
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
/** 答案下方引用 chips 上限（0 = 不限制），与问答页同一份偏好、同一个语义。
 *  截断不能是静默的：少掉的条数要数得出来、点得开，否则用户以为答案只引用了这些。 */
const refsExpanded = ref<Set<number>>(new Set())
function shownRefs(msg: ChatMsg) {
  const all = refsOf(msg)
  if (displayPrefs.maxRefs <= 0 || refsExpanded.value.has(msg.id)) return all
  return all.slice(0, displayPrefs.maxRefs)
}
function hiddenRefs(msg: ChatMsg) { return refsOf(msg).length - shownRefs(msg).length }
function toggleRefs(msg: ChatMsg) {
  const next = new Set(refsExpanded.value)
  if (next.has(msg.id)) next.delete(msg.id)
  else next.add(msg.id)
  refsExpanded.value = next
}
/** 交错流是否有可见节点（供模板决定是否渲染 .cd-flow 容器）：
 *  先看有没有节点——历史消息不落 trace/reasoning，flowOf 合成结果为空，
 *  早年只看 showTrace 会给每条历史消息渲染一个空容器（死 DOM）。
 *  有节点时：思考类恒显示，动作/返回行随「显示推理轨迹」偏好。 */
function cdHasFlow(msg: ChatMsg) {
  const items = flowOf(msg)
  if (!items.length) return false
  if (displayPrefs.showTrace) return true
  return items.some((n) => n.kind === 'think' || n.kind === 'thought')
}

/* 推理轨迹整块的展开状态（按消息 id，2026-10-20）。
   流式过程中自动展开 —— 用户要看到"AI 正在想什么"；
   回答结束后收成一行「推理过程 N 步」，需要时点开。
   与问答页的 flowOpen = streaming || expanded 同一套语义。 */
const flowExpanded = ref<Record<number, boolean>>({})
function flowOpen(msg: ChatMsg): boolean {
  if (msg.status === 'streaming') return true
  return !!flowExpanded.value[msg.id]
}
function toggleFlow(id: number) {
  flowExpanded.value = { ...flowExpanded.value, [id]: !flowExpanded.value[id] }
}
function onCardActivity() {
  // 卡片内部子代理步骤频率高：按帧合批跟随，不做每事件 nextTick
  scheduleFollow()
}

const composerEl = ref<InstanceType<typeof ChatComposer> | null>(null)
function focusComposer() { composerEl.value?.focus() }
/** 空态的快捷问题（2026-10-20）：点一下就落到输入框里，不自动发送 ——
 *  用户可能想先改一改措辞。与概览页「试试问 AI」同一批问题。 */
const QUICK_PROMPTS = [
  '这个项目的战斗数值是怎么算的？',
  '敌人 AI 的巡逻逻辑在哪个文件？',
  '这个函数被谁调用了？',
]
function askQuick(q: string) { input.value = q; focusComposer() }
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
  thinkingLevel, thinkingAdjustable, cycleThinking, toggleThinking,
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
  if (!q && !imgs.length) return false
  // ④ interrupt-and-redirect：运行时输入即打断当前轮并重定向（旧流失配由 chatEpoch 守卫停写）
  const redirecting = sending.value
  if (redirecting) {
    // 旧轮必须在此显式收尾：abort 之后旧 send() 的 catch 里 live() 已因 epoch
    // 前进失配（if (!t) return false），status 会永远停在 streaming——工作台
    // 表现为旧消息头顶「正在执行」+ elapsedLabel 跟着 nowTick 一直走字，
    // useMessageRender 的 500ms 计时器也永远不会停。
    finalizeRedirectedTurn()
    ++chatEpoch
    if (abortCtl) { abortCtl.abort(); abortCtl = null }
    sending.value = false
  }
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
  // 精确断点续跑：优先用这条失败消息上的 checkpoint id；刷新/断连导致前端
  // 没拿到 final 时退回 latest（后端只认本会话，并会校验之后没有更新的轮次）。
  const resumeCheckpoint = isContinuationRequest(q)
    ? (recovery?.checkpointId
       || [...messages.value].reverse().find(m => m.role === 'assistant' && m.checkpointId)?.checkpointId
       || 'latest')
    : undefined
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
    // 浏览器动作：实况窗立刻钉到这一页并展开；流程行也会拿到同一份证据。
    if (ev.browser_evidence) noteBrowserEvidence(ev.browser_evidence)
    dispatch(ev, live())
  }

  try {
    // 不支持思考的模型不传思考参数（null）；native 模型恒开，也无需显式传
    const thinkingOpt = thinkingSupported.value
      ? (thinkingNative.value ? null : thinkingOn.value)
      : null
    // 等级只在可调档的 toggle 家族下发；native/none 传 null，后端回落布尔
    const thinkingLevelOpt = (thinkingSupported.value && !thinkingNative.value && thinkingAdjustable.value)
      ? thinkingLevel.value
      : null
    await aiApi.askGrounded(request, { onEvent, signal: ac.signal }, {
      web: webOn.value,
      thinking: thinkingOpt,
      thinkingLevel: thinkingLevelOpt,
      images: imgs,
      sessionId: currentSessionId(),
      uiContext: props.scope === 'cockpit' && liveVisionTimeline.length && !uiContext ? 'cockpit_live_vision' : uiContext,
      visualTimeline: props.scope === 'cockpit' ? liveVisionTimeline : [],
      workflowId: reviewRef?.workflowId,
      feedbackId: reviewRef?.feedbackId,
      resumeCheckpoint,
    })
    drain()
    const t = live()
    if (t) {
      if (t.status !== 'error') t.status = t.text ? 'done' : 'stopped'
      t.finishedAt = Date.now()
      finishLiveMd(t.id, () => answerHtml(t))
      finished = !!t.text && t.status !== 'error'
      if (t.status === 'error') {
        t.recoverable = true
        if (!t.text) t.text = '本轮未完成，可核对现场后继续。'
        // 失败也存现场：刷新后靠这份快照把 trace/plan 栽回历史里的失败回合。
        preserveInterrupted()
      }
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
      t.recoverable = true
      if (!t.text) t.text = '本轮未完成，可核对现场后继续。'
      preserveInterrupted()
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

/** 重定向时收尾被抛弃的旧回合：落 stopped + 截止计时 + 空答案给一句人话。
 *  没有这一笔，旧消息会永远挂在「正在执行」，计时器还一直走字。 */
function finalizeRedirectedTurn() {
  const prev = messages.value[messages.value.length - 1]
  if (!prev || prev.role !== 'assistant' || prev.status !== 'streaming') return
  prev.status = 'stopped'
  prev.finishedAt = Date.now()
  if (!prev.text) prev.text = '（已切换到新问题，本轮未完成）'
  if (!prev.notices.includes('已切换到新问题，本轮回答中断；已执行的工具操作不会自动撤销。')) {
    prev.notices.push('已切换到新问题，本轮回答中断；已执行的工具操作不会自动撤销。')
  }
  finishLiveMd(prev.id, () => answerHtml(prev))
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
      checkpointId: assistant?.checkpointId,
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
    // 服务端历史里已有这条问题：若最后一条回答还是「本轮未完成」占位，说明失败
    // 的那一轮仍在等续接 —— 把快照里的 trace/plan/耗时栽回重建出来的那条消息，
    // 而不是再 append 一份重复的问答。
    const wanted = cleanLegacyPrompt(row.user)
    const matched = turns.filter(t => t && cleanLegacyPrompt(t.user || '') === wanted)
    const last = matched[matched.length - 1]
    if (last) {
      const failed = !!last.assistant && !!interruptedReason(last.assistant)
      const isLastTurn = turns[turns.length - 1] === last
      if (failed && isLastTurn) {
        for (let i = messages.value.length - 1; i > 0; i--) {
          const m = messages.value[i]
          if (m.role === 'assistant' && m.status === 'error'
              && messages.value[i - 1]?.role === 'user' && messages.value[i - 1].text === wanted) {
            if (Array.isArray(row.trace) && row.trace.length) m.trace = row.trace
            if (typeof row.reasoning === 'string' && row.reasoning) m.reasoning = row.reasoning
            if (Array.isArray(row.plan) && row.plan.length) m.plan = row.plan
            if (typeof row.startedAt === 'number') m.startedAt = row.startedAt
            if (typeof row.checkpointId === 'string' && row.checkpointId) m.checkpointId = row.checkpointId
            m.recoverable = true
            break
          }
        }
      }
      sessionStorage.removeItem(recoveryKey())
      return
    }
    const interruptedAct = activityFromHistory(row.user)
    messages.value.push(interruptedAct
      ? { id: msgSeq++, role: 'activity', text: interruptedAct.label, status: 'done', trace: [], reasoning: '', notices: [], plan: [], activityKind: interruptedAct.kind }
      : { id: msgSeq++, role: 'user', text: cleanLegacyPrompt(row.user), status: 'done', trace: [], reasoning: '', notices: [], plan: [] })
    messages.value.push({ id: msgSeq++, role: 'assistant', text: cleanLegacyPrompt(row.assistant), status: 'stopped',
      trace: row.trace || [], reasoning: row.reasoning || '', plan: row.plan || [], recoverable: true,
      checkpointId: row.checkpointId,
      notices: ['上次回答因离开页面或切换项目中断，已保留原始问题和执行上下文；可以点击“继续上次任务”或输入“继续”。'] })
  } catch {}
}
/** P2：进页面时拉 in-flight turn，把已退化的所有事件按 reducer 还原到 messages。
 *  保留 turn.turnId 供后续 send() 沿用；status=done/stopped/error 按原 final 收尾。 */
async function recoverActiveTurns() {
  const sid = currentSessionId()
  try {
    const list = await aiApi.listActiveTurns(sid)
    if (!list.ok || !Array.isArray(list.turns) || list.turns.length === 0) return
    // 只恢复**还在跑**的回合。服务端终态记录会在注册表里保留 60s 供回放，
    // 若把它们也当 in-flight 拉起，刷新后会凭空多出一条「正在执行」+ 续接按钮
    // （实测：回合刚结束后 60s 内刷新必现，60s 后记录下线才消失）。
    // 已完成的历史本来就会由 restoreHistory 从会话里回灌，不需要在这里补。
    const live = list.turns.filter(t => t?.status === 'streaming')
    if (!live.length) return
    // 同一会话并发 in-flight 罕见，多条都拉只会乱序，只取最后一条。
    const target = live[live.length - 1]
    const partial = await aiApi.getTurnPartial(target.turn_id)
    if (!partial.ok) return
    // 列出来到取回之间它可能刚好结束：终态同样交给会话历史，别再补一条幻影。
    if (partial.status && partial.status !== 'streaming') return
    const events = Array.isArray(partial.events) ? partial.events : []
    const finalEv = partial.final
    // 用户消息：turn event 之前的用户输入已在 sessionStorage / 历史里；这里只挂助手回合
    const turn: ChatMsg = {
      id: msgSeq++, role: 'assistant', text: '', status: 'streaming',
      trace: [], reasoning: '', notices: [], plan: [],
      startedAt: Date.now(), turnId: target.turn_id,
      recoverable: false,
    }
    messages.value.push(turn)
    // 按事件 reducer 回放，复用 useChatStream 的 dispatch（同一管线保证渲染口径一致）
    for (const ev of events) {
      dispatch(ev as unknown as SseEvent, turn)
    }
    // 终态收尾：有 final 就 stop sending，让用户能继续
    if (finalEv) {
      sending.value = false
      turn.recoverable = !finalEv.status || finalEv.status === 'error'
      const noticeMsg = '⚠ 上次页面刷新中断，已恢复到最后状态。'
      if (!turn.notices.includes(noticeMsg)) turn.notices.unshift(noticeMsg)
    } else {
      // 还在 in-flight：把它标 recoverable=true 让用户可以继续
      turn.recoverable = true
    }
    void nextTick(scrollToBottom)
  } catch (e) {
    // partial 拉起失败不影响主流程：用户可以正常发新消息
    console.warn('[recovery] failed to recover active turns', e)
  }
}
/** P1：把 final.error_kind 三态映射到模板 className。
 *  - permanent  → 红条
 *  - retried_failed → 黄条
 *  - 其他（兼容旧「服务异常」/未知字符串） → 灰条
 */
function errorKindClass(kind?: string): string {
  if (kind === 'permanent') return 'cd-error-permanent'
  if (kind === 'retried_failed') return 'cd-error-warn'
  return 'cd-error-soft'
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
function rebuildFromTurns(turns: { user: string; assistant: string; checkpoint_id?: string }[]) {
  const rebuilt: ChatMsg[] = []
  for (let i = 0; i < turns.length; i++) {
    const t = turns[i]
    if (!t) continue
    if (t.user) {
      const act = activityFromHistory(t.user)
      rebuilt.push(act
        ? { id: msgSeq++, role: 'activity', text: act.label, status: 'done', trace: [], reasoning: '', notices: [], plan: [], activityKind: act.kind }
        : { id: msgSeq++, role: 'user', text: cleanLegacyPrompt(t.user), status: 'done', trace: [], reasoning: '', notices: [], plan: [] })
    }
    if (t.assistant) {
      // 失败占位不是回答：如实显示「执行失败」，并且只有它确实是最后一轮时
      // 才给续接入口（用户后来已经继续过，就不该在历史中间再冒一个按钮）。
      const reason = interruptedReason(t.assistant)
      const isLastTurn = i === turns.length - 1
      // 后端在失败回合上落了 checkpoint_id：这也是一条可续跑信号，
      // 即便占位文案将来改了措辞，入口也不会丢。
      const recoverable = (!!reason || !!t.checkpoint_id) && isLastTurn
      rebuilt.push({
        id: msgSeq++, role: 'assistant',
        text: reason ? '' : cleanLegacyPrompt(t.assistant),
        status: reason ? 'error' : 'done',
        trace: [], reasoning: '',
        notices: recoverable
          ? ['上一轮没有完成；可以点击“继续上次任务”，或输入“继续”从中断处接着做。']
          : [],
        plan: [],
        recoverable,
        error: reason || undefined,
        checkpointId: t.checkpoint_id,
      })
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
    // P2：拉起 in-flight turn —— 服务端还有该会话正在跑的回合时按 partial 重建。
    // 必须在 restoreInterrupted 之后做：旧的 sessionStorage 兜底覆盖了部分场景，
    // 这里再补一次「服务端也认」的双重兜底，让刷新后的窗口期也能恢复。
    await recoverActiveTurns()
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
      // 演示也把产物摆出来：下面这段回答自己就写着"会出现可点击的文件卡片"，
      // 演示里却只有正文里的路径引用 —— 对不上。真实链路里产物走
      // observation 事件的 artifacts，这里照同构的形状补一份。
      t.artifacts = [{
        path: 'scripts/player/player_stats.gd',
        label: 'player_stats.gd',
        summary: '修改 scripts/player/player_stats.gd（1.2 KB）',
        bytes: 1229,
        evidence: ['demo'],
      }]
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
  // 语音能力探测不再按 scope 门控：cockpit scope 已不存在，
  // 只在 cockpit 下探测等于**永远探测不到**，后端配了 STT/TTS 也用不上。
  void voiceApi.status().then(status => {
    voiceTtsConfigured.value = !!status.tts_configured
    voiceSttConfigured.value = !!status.stt_configured
  }).catch(() => { voiceTtsConfigured.value = false })
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

// 消息展示层（流式 markdown 节流/成稿引用缓存/trace 步骤/计时）
const {
  scheduleLiveMd, finishLiveMd, streamHtmlOf,
  answerHtml, refsOf, webRefsOf, openRef, TRACE_GLYPH,
  toggleActivity, activityIsOpen, traceTitle, traceSummary,
  traceState, traceStateLabel,
  traceElapsed, elapsedLabel, resetRenderState,
} = useMessageRender<ChatMsg>(() =>
  sending.value
  || messages.value.some(m => m.status === 'streaming' || (m.workflow?.workflowId && !m.finishedAt))
  || activeWorkflowIds.value.length > 0)

/**
 * 点开一个文件产物：在工作台里定位到它（文件树展开 + 打开预览）。
 * 图片走 ArtifactCards 自己的新标签打开，不走这里。
 */
async function openArtifact(a: ChatArtifact) {
  if (!a?.path) return
  try {
    await jumpToLine(a.path, 1)
    revealPath(a.path)
  } catch {
    // 产物可能已被删/移走：让文件树去报，不在这里吞掉用户的操作意图
  }
}

// ---- 浏览器动作留痕（流程行展开后的"动作前 / 动作后"画面） ----
const EVIDENCE_EFFECT_LABEL: Record<string, string> = {
  changed: '页面有变化', navigated: '跳转', reloaded: '刷新', typed: '已输入',
  pixels: '画面变化', error_seen: '页面报错', no_change: '没有变化',
}
function evidenceOf(item: ChatTraceItem): BrowserEvidence | undefined {
  return item?.evidence
}
function evidenceEffect(ev?: BrowserEvidence): string {
  if (!ev) return ''
  const key = ev.effect || ''
  return EVIDENCE_EFFECT_LABEL[key] || key
}
function evidenceShot(path?: string): string {
  return path ? browserLiveApi.shotUrl(path) : ''
}

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
// （toggleThinking / cycleThinking 均来自 useModelConfig）。
const THINKING_TIER_TEXT: Record<string, string> = {
  off: '已关闭', low: '低档', medium: '中档', high: '高档',
}
const thinkingTitle = computed(() => {
  if (thinkingMode.value === 'unknown') return '深度思考能力待确认，请在模型设置中选择能力'
  if (!thinkingSupported.value) return '当前模型不支持深度思考，可点左侧模型芯片切换支持思考的模型'
  if (thinkingNative.value) return '该模型内置深度思考，始终开启'
  if (thinkingAdjustable.value) {
    const cur = THINKING_TIER_TEXT[thinkingLevel.value] || thinkingLevel.value
    return `深度思考：${cur}（点击切换到下一档）`
  }
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
      <!-- 开发舱专属：把当前代码库搬到对话台同一行（完整路径在 title 里） -->
      <span v-if="props.scope === 'cockpit' && codeRootLabel" class="cd-root" :title="`当前代码库：${codeRoot}`">
        <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true"><path d="M1 3 Q1 2.2 1.8 2.2 H4.6 L5.6 3.2 H10.2 Q11 3.2 11 4 V9 Q11 9.8 10.2 9.8 H1.8 Q1 9.8 1 9 Z" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>
        <span class="cd-root-name">{{ codeRootLabel }}</span>
      </span>
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

    <!-- 2026-10-19：<EngineConnectPopover /> 原挂在这里，靠顶栏「更多 → 游戏引擎连接」
         派发的 docmind:open-engine 事件唤起。那个入口已移进设置页的「运行与引擎」分区
         （内嵌渲染），事件也无人再派发，这个实例留在这里就是孤儿 → 删。 -->

    <!-- 折叠时不再卸载对话区：靠高度过渡 + 裁剪做顺滑展开/收起，状态与滚动位置保留 -->
    <!-- 2026-10-20：对话区加 aria-live —— 流式回答此前完全不被屏幕阅读器播报
         （451 个按钮里 0 个 aria-busy，对话区也没有 live 区域）。
         polite：等空闲再播，不打断当前朗读；additions text：只播新增内容。 -->
      <div :id="`docmind-chat-body-${props.scope}`" ref="scroller" class="cd-body"
           aria-live="polite" aria-relevant="additions text" @scroll="onScroll" @wheel="onWheel">
        <p v-if="historyError" role="alert">{{ historyError }}</p>
        <!-- 2026-10-20：对话台空态。此前 .cd-empty / .cd-quick 这套样式**写了却从未被渲染**
             （模板里没有引用），所以右栏一打开就是一片空白 —— 改三栏 IDE 之后这块
             空白更显眼。这里补上：一句引导 + 三个能直接问的快捷问题。 -->
        <div v-if="!messages.length" class="cd-empty">
          <div class="cd-empty-title">用大白话问代码</div>
          <div class="cd-quicks">
            <button
              v-for="q in QUICK_PROMPTS"
              :key="q"
              type="button"
              class="cd-quick"
              @click="askQuick(q)"
            >{{ q }}</button>
          </div>
        </div>
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
              <!-- degraded 是后端明确说的「这轮不是干净交付」。挂在绿色「已完成」下面
                   就是在宣称一件没做到的事，所以降级回合改显「未完成」。 -->
              <span class="cd-run-status wb-status-chip" :class="[`cd-run-${m.status}`, { 'cd-run-degraded': m.status === 'done' && m.degraded }]">
                {{ m.status === 'streaming' ? '正在执行' : m.status === 'done' ? (m.degraded ? '未完成' : '已完成') : m.status === 'error' ? '执行失败' : '已停止' }}
              </span>
              <span class="cd-run-time" v-if="elapsedLabel(m)">· {{ elapsedLabel(m) }}</span>
            </div>
            <!-- 运行期提示（后端标了 transient 的 notice）只活在这里：流式一结束就没了。
                 同一句话挂在「已完成」下面就是在宣称一件已经不做完的事。 -->
            <div v-if="m.progress && m.status === 'streaming'" class="cd-notice">
              <svg width="11" height="11" viewBox="0 0 11 11"><circle cx="5.5" cy="5.5" r="4.6" fill="none" stroke="currentColor" stroke-width="1"/><path d="M5.5 4.6 V7.6" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/><circle cx="5.5" cy="3" r=".75" fill="currentColor"/></svg>
              <span>{{ m.progress }}</span>
            </div>
            <div v-for="(n, i) in m.notices" :key="'n' + i" class="cd-notice">
              <svg width="11" height="11" viewBox="0 0 11 11"><circle cx="5.5" cy="5.5" r="4.6" fill="none" stroke="currentColor" stroke-width="1"/><path d="M5.5 4.6 V7.6" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/><circle cx="5.5" cy="3" r=".75" fill="currentColor"/></svg>
              <span>{{ n }}</span>
            </div>
            <button v-if="m.recoverable" class="cd-resume" :disabled="sending" @click="resumeTurn(m)">
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
            <!-- 2026-10-14：交错流——思考（reasoning 分段 / ReAct Thought）与
                 工具调用按发生顺序排成一条故事线：think → 调用 → 返回 → think
                 → … 答案之前全部可见、无边框（容器只有左侧一条虚线时间线）。
                 reasoning 的轮次切分在 useChatStream（动作事件即切点，思考随帧
                 实时生长）；历史/演示路径没有 flow，由 flowOf 兜底合成。
                 点动作/返回行看该事件全文；流式中最新一行自动摊开（偏好可关）。 -->
            <div v-if="cdHasFlow(m)" class="cd-flow">
              <!-- 折叠头（2026-10-20）：与问答页的「推理过程 N 步」同一套交互。
                   此前工作台的推理轨迹是**永远铺开**的 —— 思考正文、执行计划、
                   动作行全占位，正文被推到很下面；问答页那边却能一键收起。
                   现在：流式过程中自动展开（用户要看到实时思考），
                   回答结束后收成一行，需要时再点开。 -->
              <button
                class="cd-flow-toggle"
                :aria-expanded="flowOpen(m)"
                @click="toggleFlow(m.id)"
              >
                <span class="cd-flow-toggle-glyph" aria-hidden="true">◈</span>
                <span class="cd-flow-toggle-label">推理过程</span>
                <span class="cd-flow-toggle-count">{{ flowOf(m).length }} 步</span>
                <span class="cd-flow-toggle-chevron" :class="{ open: flowOpen(m) }">⌄</span>
              </button>
              <div v-show="flowOpen(m)" class="cd-flow-body">
              <div v-if="m.plan.length" class="cd-plan">
                <div class="cd-plan-title">执行计划</div>
                <div v-for="(s, i) in m.plan" :key="'p' + i" class="cd-plan-step">
                  <span class="cd-plan-idx">{{ i + 1 }}</span><span>{{ s }}</span>
                </div>
              </div>
              <template v-for="(node, i) in flowOf(m)" :key="i">
                <div v-if="node.kind === 'think' || node.kind === 'thought'" class="cd-flow-think">
                  <span class="cd-flow-dot" :class="{ on: m.status === 'streaming' && i === flowOf(m).length - 1 }"></span>
                  <div class="cd-flow-think-body">{{ node.kind === 'think' ? node.text : m.trace[node.trace].text.trim() }}</div>
                </div>
                <template v-else-if="displayPrefs.showTrace">
                  <button
                    class="cd-flow-row"
                    :class="[node.kind, `cd-flow-${traceState(m, m.trace[node.trace], node.trace)}`]"
                    :aria-expanded="activityIsOpen(m.id, node.trace, m)"
                    :aria-label="`${traceTitle(m.trace[node.trace])}，${traceStateLabel(traceState(m, m.trace[node.trace], node.trace), m.trace[node.trace])}，点击查看详情`"
                    @click="toggleActivity(m.id, node.trace, m)"
                  >
                    <span class="cd-flow-glyph">{{ node.kind === 'action' ? (TRACE_GLYPH[m.trace[node.trace].type] || '↗') : '↳' }}</span>
                    <span class="cd-flow-main">
                      <span class="cd-flow-title">{{ traceTitle(m.trace[node.trace]) }}</span>
                      <!-- 展开后事件详情就是全文，摘要行再留一份等于同一段字排两遍 -->
                      <span v-if="!activityIsOpen(m.id, node.trace, m)" class="cd-flow-summary">{{ traceSummary(m.trace[node.trace]) }}</span>
                    </span>
                    <img
                      v-if="node.kind === 'result' && evidenceOf(m.trace[node.trace])?.shot_after"
                      class="cd-flow-thumb"
                      :src="evidenceShot(evidenceOf(m.trace[node.trace])?.shot_after)"
                      alt="动作后画面"
                      loading="lazy"
                    />
                    <span class="cd-flow-meta">
                      <span class="cd-flow-state">{{ traceStateLabel(traceState(m, m.trace[node.trace], node.trace), m.trace[node.trace]) }}</span>
                      <span v-if="traceElapsed(m.trace[node.trace])" class="cd-flow-time">{{ traceElapsed(m.trace[node.trace]) }}</span>
                      <span class="cd-flow-chevron">{{ activityIsOpen(m.id, node.trace, m) ? '▾' : '▸' }}</span>
                    </span>
                  </button>
                  <div v-if="activityIsOpen(m.id, node.trace, m)" class="cd-flow-detail">
                    <span class="cd-flow-detail-label">事件详情</span>
                    <div v-if="evidenceOf(m.trace[node.trace])" class="cd-flow-evidence" data-testid="trace-evidence">
                      <div class="cd-flow-evidence-meta">
                        <span
                          class="cd-flow-evidence-effect"
                          :data-ok="evidenceOf(m.trace[node.trace])?.ok ? '1' : '0'"
                        >{{ evidenceEffect(evidenceOf(m.trace[node.trace])) }}</span>
                        <a
                          v-if="evidenceOf(m.trace[node.trace])?.url"
                          class="cd-flow-evidence-url"
                          :href="evidenceOf(m.trace[node.trace])?.url || undefined"
                          target="_blank"
                          rel="noopener noreferrer"
                        >{{ evidenceOf(m.trace[node.trace])?.url }}</a>
                      </div>
                      <div class="cd-flow-shots">
                        <figure v-if="evidenceOf(m.trace[node.trace])?.shot_before">
                          <img :src="evidenceShot(evidenceOf(m.trace[node.trace])?.shot_before)" alt="动作前画面" loading="lazy" />
                          <figcaption>动作前</figcaption>
                        </figure>
                        <figure v-if="evidenceOf(m.trace[node.trace])?.shot_after">
                          <img :src="evidenceShot(evidenceOf(m.trace[node.trace])?.shot_after)" alt="动作后画面" loading="lazy" />
                          <figcaption>动作后</figcaption>
                        </figure>
                      </div>
                    </div>
                    <code>{{ m.trace[node.trace].text }}</code>
                  </div>
                </template>
              </template>
              </div>
            </div>
            <!-- 流式中按 ~8fps 节流重渲染 markdown（格式边出边成型，又不逐 token 重建
                 DOM）；结束瞬间由 answerHtml 缓存接管成稿，视觉无跳变 -->
            <div v-if="m.text" class="ai-md cd-answer" :class="{ 'cd-answer-live': m.status === 'streaming' }">
              <!-- 「留地板、拆其余」重构：移除「覆盖存疑」「未综合结论」两条 nag 徽章
                   （它们把每个正常回答都标注得可疑，是工作台「事后打脸」感的来源）。
                   保留下面「未完成」徽章——那是诚实的失败终态，不是设卡。 -->
              <!-- 通用的「没答完」终态：后端 degraded=true 说明这一轮不是模型写出的
                   完整结论（兜底诊断 / 中间证据 / 达到步数上限）。此前前端只接了标志、
                   从不显示，于是任何兜底都挂在绿色「已完成」下面。 -->
              <div v-if="m.status === 'done' && m.degraded && m.consistencyReason !== 'verbatim_dump'" class="cd-consistency-warn" role="status">
                <span class="cd-warn-badge">未完成</span>
                本轮没有拿到可综合的完整答案——上面是兜底内容或中间证据，不是针对你问题的结论。
                展开上方「推理过程」可核对模型实际拿到的内容；建议重试、换个问法，或换一个模型。
              </div>
              <template v-if="m.status === 'streaming'"><div class="cd-answer-pending"><span class="cd-caret" aria-hidden="true" /> 正在生成回答…</div></template>
              <span v-else v-html="answerHtml(m)" />
            </div>
            <!-- 产物：本轮模型写下的文件、截到的图。直接摆在回答下面，
                 不用再去路径里自己搜。图片给缩略图，文件给可点的名字。 -->
            <ArtifactCards :artifacts="m.artifacts" :open="openArtifact" />
            <details v-if="m.status === 'done' && webRefsOf(m).length" class="cd-web-sources">
              <summary>联网来源（{{ webRefsOf(m).length }}）</summary>
              <a v-for="(s, i) in webRefsOf(m)" :key="s.url + i" class="cd-web-source" :href="s.url" target="_blank" rel="noopener noreferrer">
                <span class="cd-web-source-title">{{ s.title }}</span>
                <span class="cd-web-source-url">{{ s.url }}</span>
                <span v-if="s.score" class="cd-web-source-score">参考 {{ s.score }}</span>
              </a>
            </details>
            <div v-if="m.status === 'error'" class="cd-error" :class="errorKindClass(m.errorKind)">⚠ {{ m.error }}</div>
            <div v-if="m.status === 'done' && shownRefs(m).length" class="cd-refs">
              <button
                v-for="r in shownRefs(m)"
                :key="r.path + ':' + r.line"
                class="cd-ref"
                :title="'打开 ' + r.path + (r.line ? ':' + r.line : '')"
                @click="openRef(r)"
              >
                <svg width="11" height="11" viewBox="0 0 11 11"><path d="M2 1.5 H6.5 L9 4 V9.5 H2 Z M6.5 1.5 V4 H9" fill="none" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/></svg>
                <span class="cd-ref-path">{{ r.path }}</span>
                <span v-if="r.line" class="cd-ref-line">:{{ r.line }}</span>
              </button>
              <button v-if="hiddenRefs(m) > 0" type="button" class="cd-ref cd-ref-more"
                      :title="'还有 ' + hiddenRefs(m) + ' 处引用未显示'"
                      @click="toggleRefs(m)">+{{ hiddenRefs(m) }} 处</button>
              <button v-else-if="displayPrefs.maxRefs > 0 && refsOf(m).length > displayPrefs.maxRefs"
                      type="button" class="cd-ref cd-ref-more" @click="toggleRefs(m)">收起</button>
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
        :thinking-level="thinkingLevel"
        :thinking-adjustable="thinkingAdjustable"
        :thinking-title="thinkingTitle"
        :web-title="webTitle"
        :usage="usage"
        :usage-title="usageTitle"
        :voice-active="voiceActive"
        :voice-supported="voiceSupported"
        :voice-hint="voiceHint"
        @submit="send()"
        @stop="stop"
        @images-picked="onImagesPicked"
        @video-picked="onVideoPicked"
        @remove-image="removeImage"
        @update:web-on="webOn = $event"
        @toggle-thinking="toggleThinking"
        @cycle-thinking="cycleThinking"
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
  /* 2026-10-20：默认高度 62vh → 38vh。
     62vh（620px）把主区压到只剩 262px，整屏 2/3 是一个空对话框；试过"右侧常驻栏"
     也不行 —— 420px 栏里回答区只剩 389px、每行约 26 个字（中文舒适区 45–75 字/行），
     长回答被挤成电报体。38vh 让主区拿到 1312×706，回答区横跨全宽（阅读栏 680px、
     每行约 48 字）。「展开」态仍是 calc(100vh - --wb-main-min)，需要时能手动拉满。 */
  height: min(38vh, 520px);
  min-height: 300px;
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
.cd-title { font-size: var(--fs-sm); font-weight: 600; color: var(--text); }
.cd-hint { font-size: var(--fs-cap); color: var(--text-faint); }
/* 代码库标签：头部只有 32px 高、宽度随窗口变，目录名太长时要截断而不是把右边的按钮挤没 */
.cd-root {
  display: inline-flex; align-items: center; gap: 4px;
  min-width: 0; max-width: 260px; font-size: var(--fs-cap); color: var(--text-muted);
}
.cd-root svg { flex: 0 0 auto; }
.cd-root-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-live { font-size: var(--fs-cap); color: var(--amber); margin-left: 4px; }
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
  border: 1px solid var(--edge-ink-soft, var(--border)); border-radius: 5px;
  background: transparent; color: var(--text-muted);
  font-size: var(--fs-cap); cursor: pointer;
}
.cd-btn:hover { color: var(--text); border-color: var(--border-strong); background: var(--bg-hover); }
.cd-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.cd-btn:active { transform: translateY(1px); }
.cd-btn-on { color: var(--accent); border-color: var(--accent); background: var(--bg-selected); }

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
.cd-pop-title { font-size: var(--fs-sm); font-weight: 600; color: var(--text); margin-bottom: 8px; }
.cd-mini {
  height: 21px; padding: 0 9px; font-size: var(--fs-cap);
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
  background: var(--bg-raised); color: var(--accent); font-size: var(--fs-cap);
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
.cd-orphan-glyph { color: var(--amber); font-size: var(--fs-md); line-height: 1; flex: none; }
.cd-orphan-body { flex: 1 1 auto; min-width: 0; }
.cd-orphan-title { font-size: var(--fs-sm); font-weight: 600; color: var(--text); }
.cd-orphan-desc {
  font-size: var(--fs-cap); color: var(--text-muted); margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cd-orphan-err { font-size: var(--fs-cap); color: var(--danger); margin-top: 2px; }
.cd-orphan .cd-mini { flex: none; white-space: nowrap; }
.cd-body { flex: 1 1 auto; overflow-y: auto; padding: 8px 14px 4px; min-height: 90px; }
/* 2026-10-20：对话台回到**底部抽屉**形态（38vh）后，宽度横跨全屏 ——
   空态那句引导和 3 个快捷问题如果也满宽，就会孤零零挤在左边、右侧一大片空白。
   把空态收进与答案区同宽的阅读栏（--measure），空态与正式回答左对齐在同一列。 */
.cd-empty { padding: 14px 6px; color: var(--text-muted); max-width: var(--measure); }
.cd-empty-title { font-size: var(--fs-sm); margin: 0 0 10px; color: var(--text-muted); }
.cd-quicks { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.cd-quick {
  text-align: left; font-size: var(--fs-sm); padding: 5px 10px;
  border: 1px solid var(--edge-ink-soft, var(--border)); border-radius: 12px;
  background: transparent; color: var(--text-muted); cursor: pointer; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease;
}
.cd-quick:hover { color: var(--accent); border-color: var(--accent); background: var(--bg-selected); transform: translateX(2px); }

.cd-msg { margin-bottom: 10px; }
.cd-user-bubble {
  /* 与问答页同一口径：气泡不横跨整个通栏（82% × 1312px ≈ 1075px 太宽，
     一条气泡占满一行反而难读），封顶 560px。 */
  display: inline-block; max-width: min(560px, 82%);
  margin-left: auto;
  padding: 6px 10px; border-radius: 9px 9px 2px 9px;
  background: var(--bg-selected); color: var(--text);
  font-size: var(--fs-sm); white-space: pre-wrap;
}
.cd-msg-user { display: flex; justify-content: flex-end; }
/* 应用自发起活动字条：居中、弱化，不是用户气泡也不是 AI 回复 */
.cd-msg-activity { display: flex; justify-content: center; }
.cd-act-note {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px; border-radius: 99px;
  background: var(--bg-hover); color: var(--text-faint);
  font-size: var(--fs-cap); line-height: 1.5;
}
.cd-act-note small { color: var(--text-faint); opacity: .8; }
/* 答案区：与问答页统一到**同一把阅读栏尺**。
   实测（1600px 视口）：工作台对话台是 1312px 通栏，而原来答案是 12px + max-width:96%
   ≈ 1259px 一行 —— 每行 100+ 个汉字，阅读行长严重超标，扫行很累。
   问答页那边一直有 max-width: var(--measure)（840px，60 字/行），两页观感因此不一致。
   现在工作台也用 --measure（本页 680px，约 48 字/行，IDE 里更紧凑合适），
   字号同时提到 --fs-body（14px，与问答页一致）。
   word-break/overflow-wrap：长 URL、长路径、不带空格的标识符在窄栏里会顶破面板。 */
.cd-answer {
  font-size: var(--fs-body);
  max-width: var(--measure);
  word-break: break-word;
  overflow-wrap: anywhere;
}
/* 流式期的节流 markdown 与成稿同一样式；光标在块级内容后自然落到新行 */
.cd-caret {
  display: inline-block; width: 6px; height: 1.05em; margin-left: 2px;
  vertical-align: -0.18em; border-radius: 1px;
  background: var(--accent); animation: cd-caret 1.05s steps(1, end) infinite;
}
@keyframes cd-caret { 0%, 55% { opacity: 1; } 56%, 100% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .cd-caret { animation: none; } }
/* 答案未出结果前的占位：不再逐字吐正文（看着像卡在最后一行被截断），完成才整块显示 */
.cd-answer-pending {
  display: inline-flex; align-items: center; gap: 2px;
  font-size: var(--fs-sm); color: var(--text-faint);
}
.cd-thinking, .cd-error { font-size: var(--fs-sm); color: var(--text-muted); padding: 6px 9px; border-radius: 7px; background: var(--bg-hover); }
.cd-thinking { border-left: 2px solid var(--accent); }
.cd-slow-notice { margin: 6px 0; padding: 7px 9px; border: 1px solid color-mix(in srgb, var(--amber) 45%, var(--border)); border-radius: 6px; color: var(--text-muted); background: color-mix(in srgb, var(--amber) 10%, transparent); font-size: var(--fs-cap); line-height: 1.45; }
.cd-error { color: var(--danger); border: 1px solid color-mix(in srgb, var(--danger) 22%, transparent); background: color-mix(in srgb, var(--danger) 5%, transparent); }
/* P1 重试层错误染色：按 final.error_kind 三态分流。
 *   cd-error-permanent  — 不可恢复（fallback 也失败 / 非网络错）→ 红条
 *   cd-error-warn       — 重试用尽 + 无 fallback → 黄条（温和但可见）
 *   cd-error-soft       — 旧的「服务异常」/未识别 → 灰条（不显眼）
 */
.cd-error-permanent { color: var(--danger); border: 1px solid color-mix(in srgb, var(--danger) 22%, transparent); background: color-mix(in srgb, var(--danger) 5%, transparent); }
.cd-error-warn { color: var(--amber); border: 1px solid color-mix(in srgb, var(--amber) 28%, transparent); background: color-mix(in srgb, var(--amber) 6%, transparent); }
.cd-error-soft { color: var(--text-muted); border: 1px solid color-mix(in srgb, var(--text-muted) 22%, transparent); background: var(--bg-hover); }
/* P0 一致性自检提示：observation 关键词命中率 < 50% 时给用户一个不挡眼但可见的提示。
 * 位置在 .cd-answer 顶部第一行；不挡后续内容、不改 status。 */
.cd-consistency-warn {
  font-size: var(--fs-sm);
  color: var(--amber);
  background: color-mix(in srgb, var(--amber) 6%, transparent);
  border: 1px solid color-mix(in srgb, var(--amber) 22%, transparent);
  border-radius: 6px;
  padding: 4px 8px;
  margin: 0 0 8px;
  line-height: 1.45;
}
.cd-warn-badge {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border: 1px solid color-mix(in srgb, var(--amber) 50%, transparent);
  border-radius: 4px;
  font-size: var(--fs-cap);
  font-weight: 700;
  letter-spacing: .02em;
  color: var(--amber);
  background: color-mix(in srgb, var(--amber) 12%, transparent);
  vertical-align: baseline;
}

.cd-run-head {
  display: flex; align-items: center; gap: 6px;
  min-height: 22px; margin: 1px 0 5px;
  color: var(--text-muted); font-size: var(--fs-cap);
}
.cd-run-avatar {
  width: 17px; height: 17px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--accent-ink); background: var(--accent); font-size: var(--fs-cap);
}
.cd-run-role { color: var(--text); font-weight: 600; }
.cd-run-status { color: var(--text-faint); }
.cd-run-streaming { color: var(--accent); }
.cd-run-done { color: var(--green); }
/* 降级回合：状态虽已结束，但不是干净交付，用琥珀色与「已完成」的绿色区分开。 */
.cd-run-degraded { color: var(--amber); }
.cd-run-error { color: var(--danger); }
.cd-run-stopped { color: var(--amber); }
.cd-run-streaming::before { content: ''; display: inline-block; width: 9px; height: 9px; margin: 0 6px 0 0; border: 1.5px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: cd-run-spin .8s linear infinite; }
@keyframes cd-run-spin { to { transform: rotate(360deg); } }
.cd-run-time { color: var(--text-faint); font-variant-numeric: tabular-nums; }

.cd-wf-pending {
  display: flex; align-items: center; gap: 9px;
  margin: 4px 0 8px; padding: 10px 12px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-hover); color: var(--text-muted); font-size: var(--fs-sm);
}
.cd-wf-spinner {
  width: 13px; height: 13px; border-radius: 50%; flex: 0 0 auto;
  border: 2px solid var(--border-strong); border-top-color: var(--accent);
  animation: cd-wf-spin .7s linear infinite;
}
@keyframes cd-wf-spin { to { transform: rotate(360deg); } }

/* 交错流（2026-10-14）：思考 ↔ 工具调用按发生顺序的一条故事线。
   无边框——容器只有左侧一条虚线时间线；节点全是文字行。思考（reasoning
   分段 / ReAct Thought）实时外露随帧生长，动作/返回行一行摘要、点开看全文。
   替代了旧的「cd-reason 块 + 可折叠过程流（cd-process-stream）」两段式。 */
.cd-flow {
  display: flex; flex-direction: column;
  margin: 4px 0 6px 3px; padding-left: 12px;
  border-left: 1px dashed color-mix(in srgb, var(--accent) 25%, var(--border));
  /* 与答案区同一把阅读栏尺：轨迹行在 1312px 通栏里太长，扫读困难 */
  max-width: var(--measure);
}
/* 折叠头：与问答页 .flow-toggle 同一套（◈ 推理过程 / N 步 / ⌄） */
.cd-flow-toggle {
  align-self: flex-start;
  display: inline-flex; align-items: center; gap: 7px;
  border: 1px dashed var(--border);
  background: var(--bg-hover);
  border-radius: var(--r-sm);
  padding: 3px 10px;
  cursor: pointer;
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-size: var(--fs-cap);
  transition: color .15s ease, border-color .15s ease, background .15s ease;
}
.cd-flow-toggle:hover { color: var(--text); border-color: var(--border-strong); background: var(--bg-raised); }
.cd-flow-toggle:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 2px; }
.cd-flow-toggle-glyph { color: var(--accent-2); font-size: var(--fs-cap); }
.cd-flow-toggle-label { font-weight: 700; color: var(--text); }
.cd-flow-toggle-count {
  color: var(--text-faint);
  border-left: 1px solid var(--border);
  padding-left: 7px;
}
.cd-flow-toggle-chevron { color: var(--text-faint); font-size: var(--fs-cap); transition: transform .18s var(--ease-standard); }
.cd-flow-toggle-chevron.open { transform: rotate(180deg); }
.cd-flow-body { display: flex; flex-direction: column; margin-top: 6px; }
.cd-flow-think { display: flex; gap: 8px; padding: 2px 0; }
.cd-flow-dot {
  flex: 0 0 6px; width: 6px; height: 6px; margin-top: 6px;
  border-radius: 50%; background: var(--text-faint);
}
.cd-flow-dot.on { background: var(--accent); animation: cd-run-pulse 1.1s infinite; }
.cd-flow-think-body {
  min-width: 0; flex: 1;
  font-size: var(--fs-sm); line-height: 1.6;
  color: color-mix(in srgb, var(--accent) 42%, var(--ink));
  padding-left: 9px; border-left: 2px solid color-mix(in srgb, var(--accent) 55%, transparent);
  white-space: pre-wrap; overflow-wrap: anywhere;
  max-height: 220px; overflow: auto;
}

/* 动作/返回行：一行摘要（工具名 + 参数/结果），点开看该事件全文 */
.cd-flow-row {
  width: 100%; min-width: 0; display: flex; align-items: center; gap: 7px;
  border: 0; background: transparent; padding: 4px 0; text-align: left;
  color: var(--text-muted); cursor: pointer;
}
.cd-flow-row:hover { color: var(--text); }
.cd-flow-row:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.cd-flow-glyph {
  flex: 0 0 15px; width: 15px; text-align: center;
  color: var(--text-faint); font-size: var(--fs-md); line-height: 1;
}
.cd-flow-running .cd-flow-glyph { color: var(--accent); }
.cd-flow-ok .cd-flow-glyph { color: var(--green); }
.cd-flow-warn .cd-flow-glyph { color: var(--amber); }
.cd-flow-error .cd-flow-glyph { color: var(--danger); }
.cd-flow-main { min-width: 0; flex: 1; display: flex; align-items: baseline; gap: 7px; }
.cd-flow-title { flex: 0 0 auto; color: var(--text); font-size: var(--fs-sm); font-weight: 650; white-space: nowrap; }
.cd-flow-summary {
  min-width: 0; flex: 1;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--text-muted); font-size: var(--fs-sm);
}
/* 返回行缩进挂在对应动作行下，读作「这次调用的结果」 */
.cd-flow-row.result { padding-left: 20px; }
.cd-flow-row.result .cd-flow-summary { color: var(--text-faint); font-size: var(--fs-sm); }
.cd-flow-meta { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-cap); }
.cd-flow-state { padding: 2px 6px; border-radius: 999px; color: var(--text-faint); background: var(--bg-hover); white-space: nowrap; }
.cd-flow-running .cd-flow-state { color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); }
.cd-flow-ok .cd-flow-state { color: var(--green); background: color-mix(in srgb, var(--green) 9%, transparent); }
.cd-flow-warn .cd-flow-state { color: var(--amber); background: color-mix(in srgb, var(--amber) 11%, transparent); }
.cd-flow-error .cd-flow-state { color: var(--danger); background: color-mix(in srgb, var(--danger) 10%, transparent); }
.cd-flow-time { color: var(--text-faint); font-variant-numeric: tabular-nums; }
.cd-flow-chevron { color: var(--text-faint); font-size: var(--fs-cap); }
.cd-flow-detail {
  margin: 0 0 4px 20px; padding: 3px 0 3px 8px;
  border-left: 2px solid var(--border);
  color: var(--text-dim); font: 10.5px/1.5 var(--font-mono, monospace);
  white-space: pre-wrap; overflow-wrap: anywhere; max-height: 130px; overflow: auto;
}
.cd-flow-detail-label { display: block; margin-bottom: 3px; color: var(--text-faint); font: 9.5px/1.2 var(--font-ui); letter-spacing: .04em; }
.cd-flow-detail code { display: block; color: inherit; font: inherit; white-space: pre-wrap; overflow-wrap: anywhere; }

/* 浏览器动作：折叠行上给一张"动作后"小图，一眼就知道它当时看到了什么 */
.cd-flow-thumb {
  flex: 0 0 auto;
  width: 46px; height: 30px;
  object-fit: cover;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--bg-hover);
}
/* 有前后截图的详情比纯文本高得多，单独放宽（:has 不影响其他事件） */
.cd-flow-detail:has(.cd-flow-evidence) { max-height: 420px; }
.cd-flow-evidence { display: flex; flex-direction: column; gap: 6px; margin: 2px 0 7px; font-family: var(--font-ui); }
.cd-flow-evidence-meta { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.cd-flow-evidence-effect {
  padding: 1px 6px; border-radius: 999px;
  color: var(--green); background: color-mix(in srgb, var(--green) 9%, transparent);
  font-size: var(--fs-cap);
}
.cd-flow-evidence-effect[data-ok="0"] { color: var(--danger); background: color-mix(in srgb, var(--danger) 10%, transparent); }
.cd-flow-evidence-url {
  min-width: 0; max-width: 100%;
  color: var(--accent); font: var(--fs-cap)/1.4 var(--font-mono);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cd-flow-shots { display: flex; gap: 8px; flex-wrap: wrap; }
.cd-flow-shots figure { margin: 0; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.cd-flow-shots img {
  display: block;
  width: 200px; max-width: 100%;
  border: 1px solid var(--border); border-radius: 3px;
  background: var(--bg-hover);
}
.cd-flow-shots figcaption { color: var(--text-faint); font-size: 9.5px; }

.cd-refs { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.cd-ref {
  display: inline-flex; align-items: center; gap: 4px;
  height: 22px; padding: 0 8px;
  border: 1px solid var(--border-2); border-radius: 5px;
  /* color 不能取 var(--accent)：那是填色令牌，压在 --accent-soft 底上 paper 实测
     约 2.5:1。仓库约定纸底强调字走 --accent-text。 */
  background: var(--accent-soft); color: var(--accent-text);
  font-family: var(--font-mono); font-size: var(--fs-cap); cursor: pointer;
}
.cd-ref:hover { background: var(--bg-selected); border-color: var(--accent); }
.cd-ref-more { font-weight: 700; }
.cd-ref-line { color: var(--amber); }
.cd-web-sources { margin-top: 8px; border-top: 1px solid var(--border); padding-top: 5px; max-width: 96%; }
.cd-web-sources summary { cursor: pointer; color: var(--text-muted); font-size: var(--fs-cap); }
.cd-web-source { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 8px; margin-top: 5px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 5px; color: var(--text); text-decoration: none; background: var(--bg); }
.cd-web-source:hover { border-color: var(--accent); background: var(--bg-selected); }
.cd-web-source-title { font-size: var(--fs-cap); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-web-source-url { grid-column: 1 / -1; color: var(--accent); font-size: var(--fs-cap); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-web-source-score { color: var(--text-faint); font-size: var(--fs-cap); }





/* 系统通知（上下文压缩等） */
.cd-notice {
  display: flex; align-items: flex-start; gap: 5px;
  margin: 2px 0 5px; padding: 4px 8px;
  border-radius: 5px; background: var(--bg-hover);
  font-size: var(--fs-cap); color: var(--text-dim); line-height: 1.5;
}
.cd-notice svg { flex: 0 0 auto; margin-top: 2px; color: var(--text-faint); }
.cd-resume {
  display: inline-flex; align-items: center; gap: 5px;
  margin: 1px 0 7px; padding: 5px 9px;
  border: 1px solid var(--accent); border-radius: 5px;
  background: color-mix(in srgb, var(--accent) 8%, transparent); color: var(--accent);
  font-size: var(--fs-cap); cursor: pointer;
}
.cd-msg-attachment { display: inline-flex; margin-right: 6px; color: var(--accent); font-size: var(--fs-cap); }
.cd-resume:hover { background: color-mix(in srgb, var(--accent) 15%, transparent); }
.cd-resume:disabled { opacity: .45; cursor: default; }
.cd-resume:disabled:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }

/* 计划步骤 */
/* 执行计划：2026-10-14 去方框，退化为带序号圆点的纯文字列表 */
.cd-plan {
  margin: 2px 0 6px; padding: 0;
  border: none; border-radius: 0; background: transparent;
}
.cd-plan-title { font-size: var(--fs-cap); font-weight: 600; color: var(--text-muted); margin-bottom: 5px; }
.cd-plan-step { display: flex; gap: 7px; align-items: flex-start; font-size: var(--fs-cap); color: var(--text-dim); line-height: 1.55; }
.cd-plan-step + .cd-plan-step { margin-top: 3px; }
.cd-plan-idx {
  flex: 0 0 15px; width: 15px; height: 15px; margin-top: 1px;
  border-radius: 50%; background: var(--accent); color: var(--accent-ink);
  font-size: var(--fs-cap); display: inline-flex; align-items: center; justify-content: center;
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
  background: linear-gradient(180deg, color-mix(in srgb, var(--bg-raised) 98%, transparent), color-mix(in srgb, var(--bg-raised) 90%, transparent));
  box-shadow: 0 -8px 26px rgba(35,52,84,.06);
}
.cd-head {
  background: linear-gradient(180deg, color-mix(in srgb, var(--bg-raised) 96%, transparent), color-mix(in srgb, var(--bg-raised) 88%, transparent));
  border-bottom: 1px solid color-mix(in srgb, var(--border) 82%, var(--bg-raised));
}
.cd-body {
  scroll-behavior: smooth;
  background: linear-gradient(180deg, color-mix(in srgb, var(--bg-raised) 50%, transparent), color-mix(in srgb, var(--bg-raised) 80%, transparent));
}
.cd-msg { animation: cd-message-in .22s var(--ease-spring) both; }
.cd-user-bubble {
  border: 1px solid color-mix(in srgb, var(--accent) 10%, transparent);
  box-shadow: 0 3px 10px color-mix(in srgb, var(--accent) 8%, transparent);
}
.cd-answer { line-height: 1.7; padding: 0; background: transparent; border: none; box-shadow: none; }
/* 交错流的行悬停 feedback：极淡的底色 + 2px 右移，不搞卡片感 */
.cd-flow-row { border-radius: 6px; padding: 4px 6px; margin: 0 -6px; transition: background .16s, color .16s, transform .16s var(--ease-spring); }
.cd-flow-row:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); transform: translateX(2px); }
.cd-flow-detail { animation: cd-detail-in .18s var(--ease-spring) both; }
.cd-notice { border: 1px solid var(--border); background: color-mix(in srgb, var(--bg-raised) 80%, transparent); }
.cd-inputbar { background: linear-gradient(180deg, color-mix(in srgb, var(--bg-raised) 88%, transparent), color-mix(in srgb, var(--bg-raised) 96%, transparent)); border-top: 1px solid var(--border); }
.cd-input { box-shadow: inset 0 1px 2px color-mix(in srgb, var(--ink) 3%, transparent); transition: border-color .16s, box-shadow .16s, background .16s; }
.cd-input:focus { background: var(--bg-raised); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 11%, transparent), inset 0 1px 2px color-mix(in srgb, var(--ink) 3%, transparent); }
.cd-send { box-shadow: 0 4px 11px color-mix(in srgb, var(--accent) 18%, transparent); transition: transform .16s var(--ease-spring), box-shadow .16s, filter .16s; }
.cd-send:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 7px 15px color-mix(in srgb, var(--accent) 24%, transparent); filter: brightness(1.03); }
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
  /* 窄屏：动作行的标题与摘要换行，meta 靠右 */
  .cd-flow-main { flex-wrap: wrap; }
  .cd-flow-summary { flex-basis: 100%; }
  .cd-flow-meta { margin-left: auto; }
  .cd-pop { left: 6px; bottom: 52px; width: calc(100vw - 12px); }
  .cd-orphan { align-items: flex-start; flex-wrap: wrap; margin-inline: 8px; }
  .cd-orphan-body { flex-basis: calc(100% - 28px); }
  .cd-orphan .cd-mini { margin-left: 22px; }
}
@media (prefers-reduced-motion: reduce) {
  .cd-mini, .cd-jump-bottom, .cd-quick, .cd-flow-row { transition: none; }
  .cd-pop, .cd-run-streaming::before { animation: none; }
}
</style>
