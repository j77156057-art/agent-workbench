<script setup lang="ts">
// AI 问答首页（/）：用大白话提问，AI 自动检索当前项目代码/资料后流式作答。
// 与代码工作台共享 api.ts / markdown / 主题变量 / 会话 session.js；
// 重操作（建索引、分区开发）统一收口到工作台，本页只保留问答必需能力。
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  aiApi, projectApi, kbApi, promptApi, harnessApi, contextApi,
  getProjectId, setSessionId, startTabProbe, probeSoleDocMindTab,
} from '../workbench/api'
import type { ProjectInfo, SseEvent, SessionInfo, ContextUsage } from '../workbench/api'
import { demoMode, probeBackend } from '../workbench/composables/demo'
import { useModelConfig } from '../workbench/composables/useModelConfig'
import { useWorkflowGate } from '../workbench/composables/useWorkflowGate'
import { useOrphanWorkflow } from '../workbench/composables/useOrphanWorkflow'
import { useChatStream } from '../workbench/composables/useChatStream'
import AskSidebar from './components/AskSidebar.vue'
import AskMessage from './components/AskMessage.vue'
import FileViewer from './components/FileViewer.vue'
// 设置弹窗体积大（117KB）且低频：异步组件 + v-if，打开时才拉取
const ModelSettingsDialog = defineAsyncComponent(() => import('../workbench/components/ModelSettingsDialog.vue'))
import type { ChatMsg, ChatTraceItem } from '../workbench/composables/chat-types'
import { prefs } from './askPrefs'

// ---------------- 全局状态 ----------------
const projects = ref<ProjectInfo[]>([])
const currentPid = ref(getProjectId())

const messages = ref<ChatMsg[]>([])
let msgSeq = 1
const sending = ref(false)
const historyLoading = ref(false)

const draft = ref('')
const attachmentError = ref('')
interface PendingImg { file: File; url: string }
const pendingImages = ref<PendingImg[]>([])

// 模型配置 / 联网 / 思考（与工作台对话台同一 composable，偏好本机共享）
const {
  modelConfig: config,
  settingsOpen: settingsVisible,
  webOn, thinkingOn,
  thinkingSupported, thinkingNative, thinkingEffective, visionSupported,
  toggleThinking, setThinkingLevel,
  loadModelConfig: loadConfig, onModelSaved,
} = useModelConfig({
  onSaved: (info) => {
    usage.value = null
    void loadAskContext()
    // 只有「对话模型真的不可用」才值得打断式提示。仅检索降级时弹「Ollama 状态需要关注」
    // 是又一次误导——embedding_provider 默认 ollama，云端用户每存一次设置就被告知
    // 有一件他并不需要关心的事坏了。
    if (info.ollama_status?.guidance) {
      showToast(info.ollama_status.blocks_conversation
        ? '设置已保存，但对话模型所在的 Ollama 不可用'
        : '设置已保存（知识库检索暂不可用，不影响问答）')
    } else showToast('模型设置已保存')
  },
})

// 问答「默认开关」：设置里勾的「深度思考/联网搜索默认开启」写进 docmind.chatThinking / chatWeb
// （useModelConfig 自己存的那份，问答页与工作台对话台共享），这里把 chip 状态跟着翻。
// 正在回答时不搅 —— 改设置不该影响已经在跑的这一轮。
// 绑共享 prefs 而不是自己挂 ASK_PREFS_EVENT：只有这两个值真的变了才翻 chip，
// 改「引用条数上限」之类无关偏好不再顺带搅动输入区。
watch(() => [prefs.defaultThinking, prefs.defaultWeb] as const, ([t, w]) => {
  if (sending.value) return
  setThinkingLevel(t ? 'medium' : 'off')
  webOn.value = w
})

const viewer = ref<{ path: string; line: number } | null>(null)

const ingestBusy = ref(false)
const ingestMsg = ref('')

const scrollEl = ref<HTMLElement | null>(null)
const stick = ref(true)
let toastTimer = 0
const toast = ref('')

const SUGGESTIONS = [
  { title: '读懂项目', hint: '快速理解结构与关键逻辑', icon: 'code', prompt: '梳理当前项目的目录结构、主要模块和入口文件。' },
  { title: '梳理资料', hint: '提炼要点，找到有据可循的答案', icon: 'file', prompt: '帮我总结已上传资料的核心内容，并标注信息来源。' },
  { title: '解决问题', hint: '分析报错，找到修改方向', icon: 'terminal', prompt: '帮我分析下面的问题，并给出修复建议：\n' },
]

const sidebarOpen = ref(false)
const sidebarRef = ref<InstanceType<typeof AskSidebar> | null>(null)
const sidebarToggle = ref<HTMLButtonElement | null>(null)
const mobileNavigation = window.matchMedia('(max-width: 920px)')
function onNavigationResize() {
  if (!mobileNavigation.matches) sidebarOpen.value = false
}
onMounted(() => mobileNavigation.addEventListener('change', onNavigationResize))
onUnmounted(() => mobileNavigation.removeEventListener('change', onNavigationResize))
function closeSidebar() {
  if (!sidebarOpen.value) return
  sidebarOpen.value = false
  void nextTick(() => sidebarToggle.value?.focus())
}
async function newSidebarChat() {
  await newChat()
  closeSidebar()
}
async function selectSidebarSession(id: string) {
  await switchAskSession(id)
  if (sid() === id) closeSidebar()
}
function openSidebarSettings() {
  closeSidebar()
  openSettings()
}
function focusSettingsDialog() {
  document.querySelector<HTMLElement>('.ms-box button:not(:disabled), .ms-box input:not(:disabled)')?.focus()
}
watch(sidebarOpen, async (open) => {
  if (!open) return
  await nextTick()
  const el = sidebarRef.value?.$el as HTMLElement | undefined
  el?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
})
function onShellKeydown(event: KeyboardEvent) {
  if (!sidebarOpen.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    closeSidebar()
  }
  if (event.key !== 'Tab') return
  const el = sidebarRef.value?.$el as HTMLElement | undefined
  const items = Array.from(el?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not([hidden])') || [])
    .filter((item) => item.getClientRects().length > 0)
  const target = event.shiftKey ? items.at(-1) : items[0]
  if ((event.shiftKey && document.activeElement === items[0]) || (!event.shiftKey && document.activeElement === items.at(-1))) {
    event.preventDefault()
    target?.focus()
  }
}
function useCapability(prompt: string) {
  draft.value = prompt
  inputFocus.value = true
  void nextTick(() => { autosize(); textareaEl.value?.focus() })
}

const inputFocus = ref(false)

// 回答结束后「猜你想问」第二触点：从最后一条已完成回答启发式抽取文件引用生成相关追问
const FILE_REF_RE = /[A-Za-z0-9_.\-/\\]+\.(gd|gdshader|cs|cpp|cc|c|h|hpp|py|ts|tsx|js|jsx|vue|java|go|rs|rb|php|md|json|toml|yaml|yml|sql|sh|bat|ps1|txt)\b(?::\d+)?/g
const FOLLOWUP_GENERIC = [
  '用大白话再解释一遍',
  '这块逻辑有没有边界情况或坑？',
  '给我一个最小可运行示例',
  '如果要改这里，风险点在哪里？',
]
function deriveFollowups(text: string): string[] {
  const files = new Set<string>()
  if (text) {
    let m: RegExpExecArray | null
    FILE_REF_RE.lastIndex = 0
    while ((m = FILE_REF_RE.exec(text)) !== null && files.size < 2) {
      files.add(m[0].replace(/:\d+$/, ''))
    }
  }
  const out: string[] = []
  for (const f of files) {
    out.push(`通读 ${f} 并总结它负责什么`)
    out.push(`在 ${f} 里还有哪些相关函数？`)
  }
  for (const g of FOLLOWUP_GENERIC) {
    if (out.length >= 4) break
    out.push(g)
  }
  return out.slice(0, 4)
}

// ---------------- 派生 ----------------
const guidance = computed(() => config.value?.ollama_status?.guidance || '')
// 后端已把「挡住对话」和「仅检索降级」分开；这里据此选横幅语气。
const guidanceBlocks = computed(() => config.value?.ollama_status?.blocks_conversation === true)
const canSend = computed(() => !historyLoading.value && !gateBlocking.value
  && (draft.value.trim().length > 0 || pendingImages.value.length > 0))

// 最后一条已完成的助手回答 → 相关追问（仅对话态、无草稿且未在发送时显示）
// BUG-3 终态门：只有「真正交付了实质答案」的回合才给「猜你想问」。
// 澄清反问 / 降级收尾 / 摘要自检告警 / 正文过空的假完成残句，一律不弹 chip——
// 否则一个「？」「（没有返回内容）」或 19 字空壳底下照样摆一排追问，误导用户。
const FOLLOWUP_MIN_ANSWER_CHARS = 24
const followups = computed<string[]>(() => {
  const last = messages.value[messages.value.length - 1]
  if (!last || last.role !== 'assistant' || last.status !== 'done') return []
  if (last.clarificationRequired || last.degraded) return []
  if (last.consistency === 'warn') return []
  if ((last.text || '').trim().length < FOLLOWUP_MIN_ANSWER_CHARS) return []
  return deriveFollowups(last.text)
})

// ---------------- 工作流门控 / 孤儿恢复 ----------------
// 审批门互斥与孤儿工作流恢复均复用工作台 composable，保持两入口行为一致
const { gateBlocking, onWfGate } = useWorkflowGate()
function onWfActivity() { void nextTick(followScroll) }

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
  onReattached: () => { stick.value = true; void scrollToBottom() },
})

function sid(): string { return window.DocMindSession.get() }
function interruptedKey(): string { return `docmind.interrupted:${currentPid.value}:${sid()}` }
function draftKey(): string { return `docmind.questionDraft:${currentPid.value}` }
// 本项目「最近一次问答会话」指针：刷新/重开浏览器后自动续接（仅单标签时，防串台）
function lastSessionKey(): string { return `docmind_session_last:ask:${currentPid.value}` }
function persistLastSession(id?: string) {
  try { localStorage.setItem(lastSessionKey(), id || sid()) } catch { /* ignore */ }
}
function clearLastSession() {
  try { localStorage.removeItem(lastSessionKey()) } catch { /* ignore */ }
}

// ---------------- 会话历史（左侧栏列表）----------------
const sessionItems = ref<SessionInfo[]>([])
const sessionBusy = ref(false)
const currentProject = computed(() =>
  projects.value.find((p) => p.project_id === currentPid.value) || null,
)
const workspaceLabel = computed(() =>
  demoMode.value ? '示例游戏项目' : currentProject.value?.name || '未绑定项目',
)
const workspaceRoot = computed(() => currentProject.value?.root || '')
const documents = computed(() => config.value?.ingested_files || [])
const docCount = computed(() => documents.value.length)
const modelName = computed(() => config.value?.llm_model || '未配置')

// ---------------- 上下文窗口用量（与工作台同口径，后端按当前模型真实窗口估算）----------------
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
      ? Math.max(0, Math.min(100, ev.compact_percent)) : undefined,
  }
}
function fmtTokens(n: number): string {
  if (n >= 1000) {
    const k = n / 1000
    return `${k >= 100 ? Math.round(k) : k.toFixed(k >= 10 ? 0 : 1)}k`
  }
  return String(n)
}
const usageLabel = computed(() => {
  const u = usage.value
  if (!u) return ''
  return `上下文 ${fmtTokens(u.used_tokens)}/${fmtTokens(u.prompt_budget)}`
})
const usageTitle = computed(() => {
  const u = usage.value
  if (!u) return ''
  const tail = u.level === 'high'
    ? '（历史已达压缩触发线，早期对话会被自动压缩为摘要）'
    : u.level === 'warn'
      ? '（历史接近压缩触发线，即将自动压缩早期对话）'
      : '（历史达到触发线后早期对话会自动压缩为摘要，不影响新问答）'
  return `上下文已用 ${fmtTokens(u.used_tokens)} / 可用额度 ${fmtTokens(u.prompt_budget)} tokens`
    + `（模型窗口 ${fmtTokens(u.context_window)}，已预留输出空间）${tail}`
})
async function loadAskContext() {
  if (demoMode.value) return
  try {
    const u = await contextApi.get()
    usage.value = u
  } catch { /* 未启动/无会话时不显示 */ }
}

async function refreshSessionList() {
  if (demoMode.value) return
  try {
    const result = await harnessApi.sessions()
    // 问答页只列 ask- 域会话（工作台 web- 域不混入），后端已按当前项目分桶
    sessionItems.value = (Array.isArray(result.items) ? result.items : [])
      .filter(i => String(i.session_id || '').startsWith('ask-'))
  } catch (e) {
    showToast((e as Error).message || '问答历史加载失败')
  }
}
async function switchAskSession(id: string) {
  const target = String(id || '').trim()
  if (!target || target === sid() || sessionBusy.value) return
  if (messages.value.length
      && !window.confirm('当前对话会保留在历史中，切换后将显示另一段对话。继续？')) return
  sessionBusy.value = true
  stop()
  try {
    const claimed = await setSessionId(target)
    if (!claimed) throw new Error('这个会话正在其他标签页使用，无法切换。')
    messages.value = []
    usage.value = null
    pendingImages.value = []
    try { sessionStorage.removeItem(interruptedKey()) } catch { /* ignore */ }
    await restoreHistory()
    persistLastSession(target)
    await loadAskContext()
  } catch (e) {
    showToast((e as Error).message || '切换会话失败')
  } finally {
    sessionBusy.value = false
  }
}
async function deleteAskSession(id: string) {
  const target = String(id || '').trim()
  if (!target || sessionBusy.value) return
  if (target === sid()) {
    // 删除当前问答：清磁盘 + 开新会话（等同「新会话」并删掉旧记录）
    if (messages.value.length && !window.confirm('确定清空当前问答吗？此操作不可恢复。')) return
    sessionBusy.value = true
    try {
      try { await harnessApi.deleteSession(target) } catch { /* 文件可能已不存在 */ }
      clearLastSession()
      await newSession()
      await refreshSessionList()
    } finally {
      sessionBusy.value = false
    }
    return
  }
  if (!window.confirm('确定删除这段问答历史？此操作不可恢复。')) return
  sessionBusy.value = true
  try {
    const r = await harnessApi.deleteSession(target)
    if (r?.ok === false) throw new Error('服务端拒绝删除')
    await refreshSessionList()
  } catch (e) {
    showToast((e as Error).message || '删除失败')
  } finally {
    sessionBusy.value = false
  }
}

// 单标签且本项目有「最近问答」时自动续接（多标签保守不接，避免两个标签共用会话）
async function resumeLastSession(): Promise<boolean> {
  let last = ''
  try { last = localStorage.getItem(lastSessionKey()) || '' } catch { /* ignore */ }
  if (!last || last === sid()) return false
  if (!(await probeSoleDocMindTab())) return false
  try {
    const detail = await harnessApi.sessionDetail(last)
    if (!(detail.turns || []).length) return false
    const claimed = await setSessionId(last)
    if (!claimed) return false
    await restoreHistory()
    return true
  } catch {
    return false
  }
}

// ---------------- 通用提示 ----------------
function showToast(msg: string) {
  toast.value = msg
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.value = '' }, 5000)
}

// ---------------- 启动加载 ----------------
// 2026-10-19：问答页不再拉取 /api/model_status（显存与驻留已搬到工作台设置的
// 「模型」分区）。原先每 20 秒轮询一次、每轮对话后再刷一次，现在整个问答页
// 少了一个周期性后端请求。
async function loadProjects() {
  const r = await projectApi.list()
  projects.value = r.projects || []
  // 问答页不强制选项目：currentPid 仅读已存偏好（可为空 = 无项目对话模式）。
  // 选择 / 切换项目统一在代码工作台进行，本页只展示当前项，不在本页切换。
  currentPid.value = getProjectId()
}
async function reloadAll() {
  await Promise.all([loadConfig(), loadProjects()])
}

function setupDemoSidebar() {
  currentPid.value = 'demo'
  projects.value = [{ project_id: 'demo', root: 'D:/Games/MyGame', name: '示例游戏项目' }]
  config.value = {
    llm_provider: 'ollama', llm_model: 'qwen2.5:7b', embedding_provider: 'local',
    has_key: false, providers: [], provider_meta: {}, custom_base_url: '',
    capability: { context_window: 32768, thinking: 'toggle', vision: 'native', video: 'none', cloud: false },
    ingested_files: [],
  }
}

// ---------------- 会话历史 ----------------
async function restoreHistory() {
  historyLoading.value = true
  try {
    const detail = await harnessApi.sessionDetail(sid())
    if (messages.value.length) return
    if ((detail.turns || []).length) persistLastSession(detail.session_id || sid())
    for (const turn of detail.turns || []) {
      if (turn.user) {
        messages.value.push({ id: msgSeq++, role: 'user', text: turn.user, status: 'done', trace: [], reasoning: '', notices: [], plan: [] })
      }
      if (turn.assistant) {
        messages.value.push({ id: msgSeq++, role: 'assistant', text: turn.assistant, status: 'done', trace: Array.isArray(turn.trace) ? turn.trace : [], reasoning: '', notices: [], plan: [] })
      }
    }
    // 中断未恢复的半条回答：只回放内容，不自动重试
    try {
      const partial = JSON.parse(sessionStorage.getItem(interruptedKey()) || 'null') as
        { user?: string; assistant?: string } | null
      if (partial && typeof partial.user === 'string' && typeof partial.assistant === 'string') {
        const dup = (detail.turns || []).some((t) => t.user === partial.user && t.assistant)
        if (dup) {
          sessionStorage.removeItem(interruptedKey())
        } else {
          messages.value.push({ id: msgSeq++, role: 'user', text: partial.user, status: 'done', trace: [], reasoning: '', notices: [], plan: [] })
          messages.value.push({
            id: msgSeq++, role: 'assistant',
            text: partial.assistant + '\n\n（上次回答已中断，仅恢复已收到的内容，不会自动重试。）',
            status: 'stopped', trace: [], reasoning: '', notices: [], plan: [],
          })
        }
      }
    } catch { /* 会话存储不可用时忽略 */ }
    await nextTick(scrollToBottom)
  } catch {
    showToast('历史暂未加载，可刷新重试')
  } finally {
    historyLoading.value = false
  }
}

async function newSession() {
  const next = await window.DocMindSession.create()
  if (!next) { showToast('这个会话正在其他标签页使用，无法新建'); return }
  messages.value = []
  usage.value = null
  pendingImages.value = []
  try { sessionStorage.removeItem(interruptedKey()) } catch { /* ignore */ }
  orphanWf.value = null
  void detectOrphanWorkflow()
  await nextTick(scrollToBottom)
}
// 侧栏「＋ 新对话」：新建后刷新历史列表，使新会话在列表中可见/置顶
async function newChat() {
  await newSession()
  await refreshSessionList()
  // 乐观插入新会话占位：后台此时尚无内容，发首条消息后由真实列表覆盖。
  // 即时可见反馈，避免「点了新对话却像什么都没发生」。
  const id = sid()
  sessionItems.value = [
    { session_id: id, title: '未命名对话', updated_at: new Date().toISOString(), preview: '' } as SessionInfo,
    ...sessionItems.value.filter((s) => s.session_id !== id),
  ]
}

// ---------------- SSE 流式问答 ----------------
let chatEpoch = 0
let abortCtl: AbortController | null = null
let partial: { user: string; assistant: string } | null = null
// final 守卫：后端收尾异常时可能补发第二条非空 final（晚于真实 final 到达），
// 只接受第一条作为正文，后续降级为通知，避免覆盖正确答案。每轮发送重置。
let finalApplied = false

// P2/P5：与 ChatDock 同款的中断识别 —— 用户在恢复行写「继续」/「继续完成任务」
// 等字样时，把本轮当作「续发」处理，让后端用 continuationQuestion 拼新 prompt，
// 而不是当成一条新独立问题。否则模型会把「继续」理解成「继续做什么」答非所问。
function isContinuationRequest(value: string): boolean {
  const normalized = value.trim().replace(/[。！!？?，,、；;：:"“”‘’\s]/g, '')
  return /^(继续|继续完成任务|继续上次任务|继续回答|继续执行|接着做|接着完成|恢复任务|恢复回答|从中断处继续)$/.test(normalized)
}

// 流式接收管线与工作台共用 useChatStream：rAF 帧合批、trace 计时、事件分类
const { drain, appendTrace, dispatch } = useChatStream<ChatMsg>({
  onFrame: () => followScroll(),
  onStructural: () => followScroll(),
  onContext: (ev) => applyUsage(ev),
  // 中断快照要跟正文走，不能只跟 final 走：savePartial() 的守卫读的就是这个字段，
  // 只在 onFinal 里赋值等于"只有回答完整结束才允许恢复中断"，恰好错过它存在的场景。
  onTextFlushed: (t) => { if (partial) partial.assistant = t.text },
  onFinal: (t, text) => {
    drain()
    if (finalApplied) { t.notices.push(text); return }
    finalApplied = true
    t.text = text
    if (partial) partial.assistant = text
    followScroll()
  },
})

function savePartial() {
  if (partial?.assistant) {
    try { sessionStorage.setItem(interruptedKey(), JSON.stringify(partial)) } catch { /* ignore */ }
  }
}

/** 重定向时收尾被抛弃的旧回合：落 stopped + 截止计时 + 空答案给一句人话。
 *  没有这一笔，旧消息会永远挂在 streaming（typing 动画不停、计时器不走完）。 */
function finalizeRedirectedTurn() {
  const prev = messages.value[messages.value.length - 1]
  if (!prev || prev.role !== 'assistant' || prev.status !== 'streaming') return
  prev.status = 'stopped'
  prev.finishedAt = Date.now()
  if (!prev.text) prev.text = '（已切换到新问题，本轮未完成）'
  if (!prev.notices.includes('已切换到新问题，本轮回答中断；已执行的工具操作不会自动撤销。')) {
    prev.notices.push('已切换到新问题，本轮回答中断；已执行的工具操作不会自动撤销。')
  }
}

async function send(preset?: string) {
  if (historyLoading.value) return
  // ④ interrupt-and-redirect：运行时输入即打断当前轮并重定向（旧流失配由 chatEpoch 守卫停写）
  const redirecting = sending.value
  if (redirecting) {
    // 旧轮必须在此显式收尾：abort 之后旧 send() 的 catch 里 live() 已因 epoch
    // 前进失配（if (!t) return），status 会永远停在 streaming——问答页表现为
    // 旧消息的 typing 动画永不停，useMessageRender 的计时器也永不关。
    finalizeRedirectedTurn()
    ++chatEpoch
    if (abortCtl) { abortCtl.abort(); abortCtl = null }
    sending.value = false
  }
  if (gateBlocking.value) { showToast('工作流正在等待你的选择或审批，请先在卡片中处理'); return }
  const q = (preset ?? draft.value).trim()
  if (!q && pendingImages.value.length === 0) return
  const imgs = pendingImages.value.slice()
  const userMsg: ChatMsg = {
    id: msgSeq++, role: 'user', text: q || '请分析附件图片', status: 'done',
    images: imgs.map((i) => i.url), trace: [], reasoning: '', notices: [], plan: [],
  }
  // 关键：把流式回答对象创建为 reactive 代理，否则流式直写 turn.text/status
  // 是绕过代理改原始对象，computed 依赖（如 followups 读 last.status）永不失效。
  const turn = reactive<ChatMsg>({
    id: msgSeq++, role: 'assistant', text: '', status: 'streaming',
    trace: [], reasoning: '', notices: [], plan: [],
    startedAt: Date.now(),
  })
  messages.value.push(userMsg, turn)
  pendingImages.value = []
  attachmentError.value = ''
  // 关键硬化：清掉 sessionStorage 里的中断现场，避免「点继续」后又叠出半条 user 消息
  try { sessionStorage.removeItem(interruptedKey()) } catch { /* ignore */ }
  // 标记上一条「recoverable」已消费：续发之后那条就不能再点继续（避免循环）
  if (isContinuationRequest(q)) {
    const prev = messages.value[messages.value.length - 2]  // 跳过刚 push 的 user/assistant
    if (prev?.recoverable) prev.recoverable = false
  }
  draft.value = ''
  autosize()
  sending.value = true
  // 发送时不强制把视口拽到底：仅当用户本就在底部附近才跟随，
  // 避免「结果还没出来就被强制拉到最低」打断正在上方阅读的用户。
  const se0 = scrollEl.value
  stick.value = se0 ? se0.scrollHeight - se0.scrollTop - se0.clientHeight < 80 : true
  if (stick.value) await nextTick(scrollToBottom)

  if (demoMode.value) {
    await demoAnswer(turn, q)
    return
  }

  const epoch = ++chatEpoch
  finalApplied = false
  const ac = new AbortController()
  abortCtl = ac
  partial = { user: userMsg.text, assistant: '' }
  try { sessionStorage.removeItem(interruptedKey()) } catch { /* ignore */ }

  const live = () => (epoch === chatEpoch ? turn : undefined)
  // 事件分类/帧合批/首条 final 守卫都在 useChatStream（finalApplied/partial 经钩子闭包）
  const onEvent = (ev: SseEvent) => {
    if (epoch !== chatEpoch) return
    dispatch(ev, live())
  }

  try {
    const thinkingOpt = thinkingSupported.value
      ? (thinkingNative.value ? null : thinkingOn.value)
      : null
    await aiApi.askGrounded(q, { onEvent, signal: ac.signal }, {
      web: webOn.value,
      thinking: thinkingOpt,
      images: imgs.map((i) => i.file),
    })
    drain()
    if (epoch === chatEpoch) {
      if (turn.status !== 'error') turn.status = turn.text ? 'done' : 'stopped'
      if (!turn.text) turn.text = '（没有返回内容）'
      // 一轮问答落盘后记录「最近会话」：刷新/重开浏览器可自动续接
      if (turn.status === 'done') persistLastSession()
    }
  } catch (e) {
    drain()
    if (epoch !== chatEpoch) return
    if ((e as Error).name === 'AbortError') {
      turn.status = 'stopped'
      if (!turn.text) turn.text = '（已停止）'
      savePartial()
    } else {
      turn.status = 'error'
      turn.error = (e as Error).message || '请求失败'
    }
  } finally {
    if (epoch === chatEpoch) {
      sending.value = false
      abortCtl = null
    }
    partial = null
    // 本轮可能发起了工作流：补发事件若因竞态未挂上卡片，靠 orphan 探测兜底
    void detectOrphanWorkflow()
    await nextTick(followScroll)
  }
}

function stop() {
  abortCtl?.abort()
  chatEpoch++
  drain()
  const t = messages.value[messages.value.length - 1]
  if (t && t.role === 'assistant' && t.status === 'streaming') {
    t.status = 'stopped'
    if (!t.text) t.text = '（已停止）'
  }
  savePartial()
  sending.value = false
  abortCtl = null
  followScroll()
}

// ---------------- 演示模式脚本回答 ----------------
async function demoAnswer(turn: ChatMsg, q: string) {
  const epoch = ++chatEpoch
  const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms))
  const steps: Array<[ChatTraceItem['type'], string]> = [
    ['thought', `分析问题：在示例项目里定位「${q.slice(0, 24) || '该功能'}」相关实现。`],
    ['action', `search_code(query="${q.slice(0, 30) || '伤害逻辑'}", k=8)`],
    ['observation', '命中 8 个切片，最相关：scripts/combat/damage_calc.gd:31 calc_final_damage；scripts/player/player_stats.gd:14 take_damage。'],
    ['action', 'read_file(path="scripts/combat/damage_calc.gd", start_line=1, end_line=80)'],
    ['observation', '已读取文件：伤害公式在第 31 行，玩家扣血入口在 player_stats.gd:14。'],
  ]
  for (const [type, text] of steps) {
    await sleep(260)
    if (epoch !== chatEpoch) { turn.status = 'stopped'; sending.value = false; return }
    appendTrace(turn, type, text)
    await nextTick(followScroll)
  }
  await sleep(200)
  const answer =
    '根据当前项目代码，伤害计算位于 scripts/combat/damage_calc.gd:31 的 calc_final_damage()：\n\n'
    + '- 基础伤害 = 攻击力 × 暴击倍率\n- 防御减伤按 `defense / (defense + 100)` 收敛\n'
    + '- 玩家实际扣血入口在 scripts/player/player_stats.gd:14\n\n'
    + '建议先看这两个函数；需要调整数值或修复逻辑，可以直接在这里告诉我。'
  const chunks = answer.match(/.{1,4}/g) || []
  for (const c of chunks) {
    if (epoch !== chatEpoch) { turn.status = 'stopped'; sending.value = false; return }
    turn.text += c
    await sleep(16)
    followScroll()
  }
  turn.status = 'done'
  sending.value = false
  await nextTick(followScroll)
}

// ---------------- 输入与附件 ----------------
const textareaEl = ref<HTMLTextAreaElement | null>(null)
const imgInputEl = ref<HTMLInputElement | null>(null)
function autosize() {
  const el = textareaEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}
function onKeydown(e: KeyboardEvent) {
  if (e.isComposing) return
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    void send()
  }
}
function addImages(files: FileList | File[]) {
  const incoming = Array.from(files || [])
  const valid = incoming.filter((f) => /^image\/(png|jpeg|webp|gif)$/i.test(f.type))
  if (valid.length !== incoming.length) attachmentError.value = '仅支持 PNG、JPEG、WEBP、GIF 图片。'
  else attachmentError.value = ''
  const merged = [...pendingImages.value, ...valid.map((f) => ({ file: f, url: URL.createObjectURL(f) }))].slice(0, 4)
  if (pendingImages.value.length + valid.length > 4) attachmentError.value = '单条消息最多附带 4 张图片。'
  pendingImages.value = merged
}
function removeImage(i: number) {
  const [item] = pendingImages.value.splice(i, 1)
  if (item) URL.revokeObjectURL(item.url)
}
function onImagePick(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files) addImages(input.files)
  input.value = ''
}
function onPaste(e: ClipboardEvent) {
  const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith('image/'))
  if (files.length) { e.preventDefault(); addImages(files) }
}
function onDrop(e: DragEvent) {
  e.preventDefault()
  if (e.dataTransfer?.files?.length) addImages(e.dataTransfer.files)
}

async function enhance() {
  const d = draft.value.trim()
  if (!d) { showToast('请先写下要增强的提问草稿'); return }
  if (demoMode.value) { showToast('示例演示模式下无法增强，请在本机启动 Agent 工作台 后使用'); return }
  try {
    const r = await promptApi.enhance(d)
    if (r.ok && r.enhanced) {
      draft.value = r.enhanced
      autosize()
      showToast(r.mode === 'llm' ? '已用模型重写提问，可编辑后发送' : '已本地优化提问，可编辑后发送')
    } else {
      showToast('增强失败：' + (r.error || '未知错误'))
    }
  } catch (e) {
    showToast('增强请求失败：' + (e as Error).message)
  }
}

// ---------------- 侧栏操作 ----------------
const homeFileInput = ref<HTMLInputElement | null>(null)
function onHomeFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) void onIngest(file)
  input.value = ''
}
async function onIngest(file: File) {
  ingestBusy.value = true
  ingestMsg.value = `正在摄取：${file.name} …`
  try {
    const r = await kbApi.ingest(file)
    if (r.ok) {
      ingestMsg.value = `已摄取 ${file.name}（${r.chunks ?? 0} 个切片）`
      await loadConfig()
    } else {
      ingestMsg.value = '摄取失败：' + (r.error || '未知错误')
    }
  } catch (e) {
    ingestMsg.value = '摄取失败：' + (e as Error).message
  } finally {
    ingestBusy.value = false
    window.setTimeout(() => { ingestMsg.value = '' }, 8000)
  }
}

async function openSettings() {
  if (demoMode.value) { showToast('示例演示模式下不可修改模型设置'); return }
  try {
    await loadConfig()
    settingsVisible.value = true
  } catch (e) {
    showToast('读取模型配置失败：' + (e as Error).message)
  }
}
function openFile(path: string, line: number) {
  viewer.value = { path, line }
}

// ---------------- 滚动 ----------------
function onScroll() {
  const el = scrollEl.value
  if (!el) return
  stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}
function scrollToBottom() {
  const el = scrollEl.value
  if (!el) return
  const bottom = el.scrollHeight - el.clientHeight
  // 已在底部则跳过写入，避免流式逐帧重复设置 scrollTop 引发重排抖动
  if (el.scrollTop < bottom - 1) el.scrollTop = bottom
}
function followScroll() {
  if (stick.value) scrollToBottom()
}
const showJumpToLatest = computed(() => messages.value.length > 0 && !stick.value)
function jumpToLatest() {
  stick.value = true
  scrollToBottom()
}

// ---------------- 生命周期 ----------------
onMounted(async () => {
  startTabProbe()
  try { draft.value = sessionStorage.getItem(draftKey()) || '' } catch { /* ignore */ }
  window.addEventListener('pagehide', () => {
    try { sessionStorage.setItem(draftKey(), draft.value) } catch { /* ignore */ }
    if (sending.value) { savePartial(); abortCtl?.abort() }
  })
  // 2026-10-19：原先这里每 20 秒轮询一次 /api/model_status（显存占用），
  // 问答页不再展示显存，该轮询与可见性恢复钩子一并删掉。

  const online = await probeBackend()
  if (demoMode.value || !online) {
    setupDemoSidebar()
    return
  }
  try {
    await reloadAll()
    await refreshSessionList()
    await restoreHistory()
    // 当前 sid 没有内容（新标签/重开浏览器）时，单标签下自动续接本项目最近问答
    if (!messages.value.length) await resumeLastSession()
    await loadAskContext()
    void detectOrphanWorkflow()
  } catch (e) {
    showToast('读取配置失败：' + (e as Error).message)
  }
})

</script>

<template>
  <div class="ask-shell" @keydown="onShellKeydown">
    <button v-if="sidebarOpen" class="sidebar-backdrop" aria-label="关闭导航" tabindex="-1" @click="closeSidebar" />
    <AskSidebar
      id="ask-navigation"
      ref="sidebarRef"
      :class="{ 'is-open': sidebarOpen }"
      @close="closeSidebar"
      :projects="projects"
      :current-pid="currentPid"
      :config="config"
      :demo="demoMode"
      :ingest-busy="ingestBusy"
      :ingest-msg="ingestMsg"
      :sessions="sessionItems"
      :current-session-id="sid()"
      :session-busy="sessionBusy"
      :home="!messages.length"
      @ingest="onIngest"
      @open-settings="openSidebarSettings"
      @new-session="newSidebarChat"
      @switch-session="selectSidebarSession"
      @delete-session="deleteAskSession"
    />

    <main class="ask-main" :class="{ 'is-empty': !messages.length }" :inert="sidebarOpen">
      <header class="ask-topbar">
        <div class="tb-left">
          <button ref="sidebarToggle" class="sidebar-toggle" aria-label="打开导航" aria-controls="ask-navigation" :aria-expanded="sidebarOpen" @click="sidebarOpen = true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/></svg>
          </button>
          <h1 class="tb-title">AI 问答</h1>
          <span class="tb-sub">你的项目思考伙伴</span>
        </div>
        <div class="tb-right">
          <span v-if="usageLabel" class="tb-usage"
                :class="{ 'is-warn': usage?.level === 'warn', 'is-high': usage?.level === 'high' }"
                :title="usageTitle">{{ usageLabel }}</span>
          <a class="tb-btn primary" href="/workbench">
            代码工作台
            <svg viewBox="0 0 24 24" width="13" height="13"><path fill="currentColor" d="M8.5 16.5L16 9H9V7h10v10h-2v-7l-7.5 7.5z"/></svg>
          </a>
        </div>
      </header>

      <div v-if="demoMode" class="banner info">
        示例演示模式：当前未连接本地 Agent 工作台 服务，以下回答为演示数据。启动本机服务后刷新即可使用真实问答。
      </div>
      <!-- 只有「对话模型」不可用才用告警语气。仅检索不可用时用 info：
           embedding_provider 默认就是 ollama，若一律弹黄色横幅，云端对话用户
           每次都会看到「当前模型配置依赖本机 Ollama / 或改用 qwen、deepseek」，
           而他明明已经在用 deepseek——同一个误导信号的第二半。 -->
      <div v-else-if="guidance" class="banner" :class="guidanceBlocks ? 'warn' : 'info'">
        <span>{{ guidance }}</span>
      </div>

      <div class="chat-stage">
        <div ref="scrollEl" class="chat-scroll" @scroll="onScroll">
          <div class="chat-col" :class="{ 'is-welcome': !messages.length }">
          <div v-if="!messages.length" class="home-layout">
            <section class="welcome" aria-labelledby="welcome-title">
              <div class="welcome-eyebrow">
                <span class="welcome-mark" aria-hidden="true"><svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1Z"/><path d="M12 6v14M6 9h3M15 9h3M6 13h3M15 13h3"/></svg></span>
                <span>从一个好问题开始</span>
              </div>
              <h2 id="welcome-title" class="welcome-title">让想法，更进一步。</h2>
              <p class="welcome-desc">读懂代码，梳理资料。和 AI 一起，把问题变成清晰的下一步。</p>
              <div class="suggestions">
                <button v-for="s in SUGGESTIONS" :key="s.title" class="suggestion" type="button" @click="useCapability(s.prompt)">
                  <svg class="suggestion-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path v-if="s.icon === 'code'" d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16"/>
                    <path v-else-if="s.icon === 'file'" d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Zm0 0v6h6M8 13h8M8 17h5"/>
                    <path v-else d="M4 4h16v16H4zM8 9l3 3-3 3m5 0h3"/>
                  </svg>
                  <span class="suggestion-copy"><strong>{{ s.title }}</strong><span>{{ s.hint }}</span></span>
                  <span class="suggestion-arrow" aria-hidden="true">↗</span>
                </button>
              </div>
            </section>

            <aside class="home-rail" aria-label="工作区上下文">
              <section class="rail-block">
                <header class="rail-head"><h3>当前工作区</h3></header>
                <div class="rail-workspace">
                  <strong>{{ workspaceLabel }}</strong>
                  <span v-if="workspaceRoot" class="rail-path" :title="workspaceRoot">{{ workspaceRoot }}</span>
                </div>
                <dl class="rail-meta">
                  <dt>资料</dt><dd>{{ docCount }} 份</dd>
                  <dt>模型</dt><dd>{{ modelName }}</dd>
                </dl>
                <a class="rail-link" href="/workbench">管理工作区 <span aria-hidden="true">↗</span></a>
              </section>

              <section class="rail-block">
                <header class="rail-head">
                  <h3>资料文档 <span class="rail-count">{{ docCount }}</span></h3>
                  <button class="rail-upload" type="button" :disabled="demoMode || ingestBusy" @click="homeFileInput?.click()">
                    {{ ingestBusy ? '处理中' : '上传' }}
                  </button>
                </header>
                <input ref="homeFileInput" type="file" accept=".pdf,.md,.txt" hidden @change="onHomeFileChange">
                <p v-if="ingestMsg" class="rail-status" role="status">{{ ingestMsg }}</p>
                <div v-if="documents.length" class="rail-docs">
                  <div v-for="name in documents.slice(0, 4)" :key="name" class="rail-doc" :title="name">
                    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Zm0 0v6h6" />
                    </svg>
                    <span>{{ name }}</span>
                  </div>
                  <span v-if="documents.length > 4" class="rail-empty">还有 {{ documents.length - 4 }} 份资料</span>
                </div>
                <p v-else class="rail-empty">尚未上传资料文档。</p>
              </section>
            </aside>
          </div>

          <template v-else>
            <AskMessage
              v-for="m in messages" :key="m.id" :msg="m"
              @open-file="openFile"
              @ask="(v: string) => send(v)"
              @wf-gate="(open: boolean) => onWfGate(m.workflow!.workflowId, open)"
              @wf-activity="onWfActivity"
            />
          </template>

          <div v-if="followups.length && !draft.trim() && !sending" class="followups">
            <span class="followups-label"><span class="followups-ico" aria-hidden="true">✦</span>猜你想问</span>
            <div class="followups-row">
              <button v-for="(q, i) in followups" :key="i" class="followup" type="button" @click="send(q)"><span class="followup-arrow" aria-hidden="true">→</span><span class="followup-label">{{ q }}</span></button>
            </div>
          </div>
          </div>
        </div>
        <button v-if="showJumpToLatest" class="jump-latest" type="button" @click="jumpToLatest">
          回到最新回答 <span aria-hidden="true">↓</span>
        </button>
      </div>

      <div v-if="orphanWf" class="wf-orphan" role="alert">
        <span class="wf-orphan-glyph" aria-hidden="true">⚠</span>
        <div class="wf-orphan-body">
          <div>该项目有一个未结束的工作流（{{ orphanStatusLabel }}），当前对话里没有它的审批卡片。</div>
          <div v-if="orphanError" class="wf-orphan-err">{{ orphanError }}</div>
        </div>
        <button class="wf-orphan-btn" :disabled="orphanBusy" @click="reattachOrphan">挂回对话继续</button>
        <button class="wf-orphan-btn danger" :disabled="orphanBusy" @click="interruptOrphan">
          {{ orphanBusy ? '处理中…' : '中断它' }}
        </button>
      </div>

      <div v-if="gateBlocking" class="gate-hint">工作流正在等待你的选择或审批，处理后才能继续提问</div>

      <footer class="composer-wrap">
        <div class="composer" :class="{ blocked: gateBlocking }">
          <div v-if="pendingImages.length" class="img-preview">
            <div v-for="(img, i) in pendingImages" :key="img.url" class="img-thumb">
              <img :src="img.url" :alt="img.file.name">
              <button class="img-x" @click="removeImage(i)">×</button>
            </div>
          </div>
          <div class="composer-input">
            <span v-show="!draft && !inputFocus" class="ghost-text" aria-hidden="true">描述你的问题，或从下面的建议开始…</span>
            <textarea
              ref="textareaEl"
              v-model="draft"
              rows="1"
              aria-label="向 AI 提问，支持检索当前项目代码与资料（Enter 发送，Shift+Enter 换行）"
              @keydown="onKeydown"
              @input="autosize"
              @focus="inputFocus = true"
              @blur="inputFocus = false"
              @paste="onPaste"
              @dragover.prevent
              @drop="onDrop"
            ></textarea>
          </div>
          <div v-if="attachmentError" class="attach-err">{{ attachmentError }}</div>
          <div class="composer-bar">
            <div class="bar-left">
              <template v-if="visionSupported || demoMode">
                <button class="tool-btn" title="附加图片（PNG/JPEG/WEBP/GIF，最多 4 张，支持粘贴/拖拽）" @click="imgInputEl?.click()">
                  <svg viewBox="0 0 24 24" width="15" height="15"><path fill="currentColor" d="M4 5h16a2 2 0 012 2v10a2 2 0 01-2 2H4a2 2 0 01-2-2V7a2 2 0 012-2zm4 4a2 2 0 100 4 2 2 0 000-4zm12 7l-5-5-9 9H4v-2l8-8 6 6h2z"/></svg>
                </button>
                <input ref="imgInputEl" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden @change="onImagePick">
              </template>
              <button
                v-if="thinkingSupported"
                class="chip"
                :class="{ on: thinkingEffective, native: thinkingNative }"
                :aria-pressed="thinkingEffective"
                :disabled="thinkingNative"
                :title="thinkingNative ? '该模型内置深度思考，始终开启' : '回答前先推理，耗时更长但更稳'"
                @click="toggleThinking()"
              >深度思考</button>
              <button
                class="chip"
                :class="{ on: webOn }"
                :aria-pressed="webOn"
                title="开启后 AI 可检索互联网获取最新信息"
                @click="webOn = !webOn"
              >联网搜索</button>
              <button class="chip ghost" title="把草稿重写为更清晰的提问" @click="enhance">增强提问</button>
            </div>
            <div class="bar-right">
              <button v-if="sending" class="send-btn stop" title="停止回答" @click="stop">
                <span class="stop-ico"></span> 停止
              </button>
              <button class="send-btn" :disabled="!canSend" title="发送（Enter）；运行中发送＝打断当前轮并重定向" @click="send()">
                发送
                <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>
              </button>
            </div>
          </div>
        </div>
        <div class="composer-note"><span>回答可能有误，请核对重要信息</span><span class="composer-shortcut">Enter 发送 · Shift + Enter 换行</span></div>
      </footer>

      <transition name="toast">
        <!-- role=status + aria-live：toast 此前是纯视觉提示，屏幕阅读器完全读不到
             （"模型设置已保存"、"操作失败"这类信息对盲用户是不可见的）。 -->
        <div v-if="toast" class="toast" role="status" aria-live="polite">{{ toast }}</div>
      </transition>

      <ModelSettingsDialog
        v-if="settingsVisible"
        @vue:mounted="focusSettingsDialog"
        :visible="settingsVisible"
        :config="config"
        @close="settingsVisible = false"
        @saved="onModelSaved"
      />
      <FileViewer v-if="viewer" :path="viewer.path" :line="viewer.line" @close="viewer = null" />
    </main>
  </div>
</template>

<style scoped>
.ask-shell { display: flex; height: 100dvh; overflow: hidden; background: var(--bg); }
.ask-main { flex: 1; min-width: 0; display: flex; flex-direction: column; height: 100dvh; background: var(--surface); }
.ask-topbar { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 64px; padding: 12px 32px; border-bottom: 1px solid var(--border); }
.tb-left { display: flex; align-items: center; gap: 16px; min-width: 0; }
.tb-title { margin: 0; font-family: var(--font-ui); font-size: var(--fs-body); font-weight: 600; color: var(--ink); white-space: nowrap; }
.tb-sub { padding-left: 16px; border-left: 1px solid var(--border); font-size: var(--fs-sm); color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tb-right { display: flex; align-items: center; gap: 12px; flex: 0 0 auto; }
.tb-usage { font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--text-muted); white-space: nowrap; }
.tb-usage.is-warn { color: var(--amber); }
.tb-usage.is-high { color: var(--danger); }
.tb-btn { display: inline-flex; align-items: center; gap: 8px; min-height: 36px; border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--surface); color: var(--ink); font-size: var(--fs-sm); font-weight: 500; padding: 6px 12px; cursor: pointer; text-decoration: none; transition: background 150ms; }
.tb-btn:hover { background: var(--bg-hover); }
.sidebar-toggle { display: none; align-items: center; justify-content: center; flex-shrink: 0; width: 40px; height: 40px; border: 0; border-radius: var(--r-sm); background: transparent; color: var(--ink); cursor: pointer; }
.sidebar-toggle:hover { background: var(--bg-hover); }
.sidebar-backdrop { position: fixed; inset: 0; border: 0; background: var(--scrim); z-index: 59; }

/* 横幅：粗边框卡片 */
.banner {
  flex: 0 0 auto;
  margin: 16px 28px 0;
  padding: 12px 16px;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  background: var(--bg-raised);
  box-shadow: var(--sh-hard-sm);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.banner.info { background: color-mix(in srgb, var(--accent) 10%, transparent); border-color: var(--accent-2); color: var(--accent-2); }
.banner.warn { background: color-mix(in srgb, var(--amber) 10%, transparent); border-color: var(--amber); color: var(--amber); }

.chat-stage { flex: 1; min-height: 0; display: flex; flex-direction: column; position: relative; }
.chat-scroll { flex: 1; min-height: 0; overflow-y: auto; scrollbar-gutter: stable; }
.jump-latest {
  position: absolute;
  left: 50%;
  bottom: 16px;
  transform: translateX(-50%);
  z-index: 6;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 36px;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: var(--r-full);
  background: var(--bg-raised);
  color: var(--text);
  font-family: var(--font-ui);
  font-size: var(--fs-sm);
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0, 0, 0, .14);
}
.jump-latest:hover { background: var(--bg-hover); }
.chat-col { width: min(840px, 100%); margin: 0 auto; padding: 32px; display: flex; flex-direction: column; gap: 32px; }
.ask-main.is-empty { overflow-y: auto; }
.is-empty .chat-stage { display: contents; }
.is-empty .chat-scroll { flex: 0 0 auto; overflow: visible; margin-top: 0; scrollbar-gutter: auto; }
.chat-col.is-welcome { width: 100%; padding: 0; }
.home-layout {
  width: min(1180px, 100%);
  margin: 0 auto;
  padding: 56px 40px 8px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 56px;
  align-items: start;
}
.welcome { min-width: 0; text-align: left; }
.welcome-eyebrow { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; color: var(--accent-text); font-size: var(--fs-md); font-weight: 500; }
.welcome-mark { display: grid; place-items: center; width: 36px; height: 36px; border-radius: var(--r-md); background: var(--accent-soft); color: var(--accent-text); }
.welcome-title { margin: 0 0 16px; font-family: var(--font-ui); font-size: clamp(28px, 3vw, 40px); font-weight: 600; line-height: 1.3; letter-spacing: -.04em; color: var(--ink); }
.welcome-desc { margin: 0; font-size: var(--fs-h3); line-height: 1.8; color: var(--text-muted); }
.suggestions { display: flex; flex-direction: column; gap: 8px; margin: 28px 0 0; }
.suggestion { display: flex; align-items: center; gap: 12px; position: relative; text-align: left; border: 1px solid var(--border); border-radius: var(--r-md); background: var(--surface); color: var(--text); padding: 14px 16px; cursor: pointer; box-shadow: var(--shadow-panel, none); transition: background 150ms, border-color 150ms; }
.suggestion:hover { background: var(--bg-hover); border-color: var(--border-strong); }
.suggestion:hover .suggestion-copy > span,
.suggestion:hover .suggestion-arrow { color: color-mix(in srgb, var(--ink) 72%, var(--bg-hover)); }
.suggestion-icon { color: var(--accent-text); flex: 0 0 auto; margin-top: 2px; }
.suggestion-copy { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.suggestion-copy strong { font-size: var(--fs-body); font-weight: 600; }
.suggestion-copy > span { font-size: var(--fs-sm); line-height: 1.6; color: var(--text-muted); }
.suggestion-arrow { color: var(--text-muted); margin-left: auto; font-size: var(--fs-body); }
.home-rail {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 28px;
  padding-left: 32px;
  border-left: 1px solid var(--border);
}
.rail-block { min-width: 0; }
.rail-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.rail-head h3 { margin: 0; color: var(--text-muted); font-size: var(--fs-cap); font-weight: 600; letter-spacing: .04em; }
.rail-count { color: var(--text-muted); font-size: var(--fs-cap); font-variant-numeric: tabular-nums; }
.rail-workspace { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.rail-workspace strong { font-size: var(--fs-md); font-weight: 600; color: var(--text); }
.rail-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font-family: var(--font-mono); font-size: var(--fs-cap); }
.rail-docs { display: flex; flex-direction: column; gap: 2px; }
.rail-doc { display: flex; align-items: center; gap: 8px; min-width: 0; padding: 6px 0; color: var(--text); font-size: var(--fs-sm); }
.rail-doc svg { flex: none; color: var(--text-muted); }
.rail-doc span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rail-empty { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.7; }
.rail-upload {
  min-height: 28px;
  padding: 4px 8px;
  border: 0;
  border-radius: var(--r-sm);
  background: none;
  color: var(--accent-text);
  font-family: var(--font-ui);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.rail-upload:hover:not(:disabled) { background: var(--bg-hover); }
.rail-upload:disabled { color: var(--text-muted); cursor: default; }
.rail-status { margin: 0 0 6px; color: var(--text-muted); font-size: var(--fs-cap); line-height: 1.6; }
.rail-meta { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 14px; margin: 0; font-size: var(--fs-sm); line-height: 1.6; }
.rail-meta dt { color: var(--text-muted); }
.rail-meta dd { margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.rail-link { display: inline-flex; margin-top: 14px; color: var(--accent-text); font-size: var(--fs-sm); text-decoration: none; }
.rail-link:hover { text-decoration: underline; text-underline-offset: 3px; }
.composer-wrap { flex: 0 0 auto; width: min(840px, 100%); align-self: center; padding: 16px 32px 20px; }
.is-empty .composer-wrap { width: min(1180px, 100%); margin-bottom: 0; padding: 24px 40px 48px; }
.is-empty .composer { max-width: none; }
.composer { background: var(--bg-raised); border: 1px solid var(--border); border-radius: var(--r-lg); padding: 16px; box-shadow: var(--shadow-panel, var(--sh-hard)); transition: border-color 150ms, box-shadow 150ms; }
.composer:focus-within { border-color: var(--accent-text); box-shadow: 0 0 0 2px var(--accent-soft); }
.composer-input { position: relative; }
.composer textarea { display: block; width: 100%; min-height: 56px; border: none; outline: none; resize: none; background: transparent; font-family: var(--font-ui); font-size: var(--fs-h2); line-height: 1.75; color: var(--text); padding: 4px; max-height: 160px; }
.composer textarea::placeholder { color: var(--text-muted); }
.ghost-text { position: absolute; inset: 0; padding: 4px; font-size: var(--fs-h2); line-height: 1.75; color: var(--text-muted); pointer-events: none; }
.composer-note { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 12px 4px 0; color: var(--text-muted); font-size: var(--fs-sm); }
.followups { display: flex; flex-direction: column; gap: 8px; margin-top: 6px; padding: 8px 4px 0; border-top: 1px dashed var(--border); }
.followups-label { display: inline-flex; align-items: center; gap: 5px; font-family: var(--font-mono); font-size: var(--fs-cap); font-weight: 700; color: var(--text-muted); letter-spacing: .04em; }
.followups-ico { color: var(--accent-2); font-size: var(--fs-cap); }
.followups-row { display: flex; flex-wrap: wrap; gap: 8px; }
.followup {
  display: inline-flex; align-items: flex-start; gap: 6px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-sm); background: var(--bg); color: var(--text);
  font-family: var(--font-sans); font-size: var(--fs-md);
  padding: 7px 13px; cursor: pointer; text-align: left; line-height: 1.4;
  transition: transform 130ms cubic-bezier(.2,.8,.2,1), background 130ms, color 130ms, border-color 130ms;
}
.followup-arrow { flex: none; color: var(--accent-2); font-weight: 700; transition: color 130ms; }
.followup-label { min-width: 0; overflow-wrap: anywhere; }
.followup:hover { transform: translate(-1px, -1px); --btn-fill: var(--accent); background: var(--accent); color: var(--accent-ink); border-color: var(--border-strong); }
.followup:hover .followup-arrow { color: var(--accent-ink); }

.img-preview { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 6px 8px; }
.img-thumb { position: relative; }
.img-thumb img { width: 64px; height: 64px; object-fit: cover; border: var(--edge-w-strong) solid var(--edge-ink);
border-radius: var(--r-sm); }
.img-x {
  position: absolute; top: -8px; right: -8px;
  width: 18px; height: 18px;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm); background: var(--ink); color: var(--bg);
  font-size: var(--fs-sm); line-height: 1; cursor: pointer;
  display: grid; place-items: center;
}
.attach-err { padding: 0 6px 6px; font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--danger); }

.composer-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 12px; }
.bar-left { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.bar-right { display: flex; gap: 8px; flex: 0 0 auto; }
.tool-btn { width: 36px; height: 36px; border: 0; border-radius: var(--r-sm); background: transparent; color: var(--text-muted); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; }
.tool-btn:hover { color: var(--accent-text); background: var(--bg-hover); }
.chip { display: inline-flex; align-items: center; gap: 6px; min-height: 36px; border: 1px solid transparent; border-radius: var(--r-sm); background: transparent; color: var(--text-muted); font-size: var(--fs-sm); font-weight: 500; padding: 6px 10px; cursor: pointer; transition: background 150ms, color 150ms; }
.chip:hover { background: var(--bg-hover); color: var(--ink); }
.chip.on { background: var(--accent-soft); color: var(--accent-text); }
.chip.on::before { content: ''; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.chip.native { cursor: default; }
.send-btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 38px; border: 1px solid transparent; border-radius: var(--r-md); background: var(--accent); color: var(--accent-ink); font-size: var(--fs-md); font-weight: 600; padding: 8px 16px; cursor: pointer; transition: background 150ms; }
.send-btn:hover:not(:disabled) { background: var(--accent-deep); }
.send-btn:disabled { background: var(--bg-hover); color: var(--text-muted); cursor: not-allowed; }
.send-btn.stop { background: var(--bg-raised); color: var(--danger); border-color: var(--danger); }
.stop-ico { width: 10px; height: 10px; background: currentColor; }

/* toast */
.toast {
  position: fixed; top: 18px; left: 50%; transform: translateX(-50%);
  background: var(--ink); color: var(--bg);
  font-family: var(--font-mono); font-size: var(--fs-sm);
  padding: 10px 18px; border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm); box-shadow: var(--sh-hard-sm);
  z-index: 200; max-width: 70vw;
}
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translate(-50%, -8px); }
.toast-enter-active, .toast-leave-active { transition: all .2s cubic-bezier(.2,.8,.2,1); }

/* 孤儿工作流恢复条 / 审批门提示 */
.wf-orphan {
  flex: 0 0 auto;
  display: flex; align-items: center; gap: 12px;
  margin: 0 28px 10px; padding: 12px 16px;
  border: 2px solid var(--amber); background: color-mix(in srgb, var(--amber) 10%, transparent);
  font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--text);
}
.wf-orphan-glyph { color: var(--amber); font-size: var(--fs-h3); }
.wf-orphan-body { flex: 1; min-width: 0; }
.wf-orphan-err { color: var(--danger); margin-top: 3px; }
.wf-orphan-btn {
  flex: 0 0 auto;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm); background: var(--bg-raised); color: var(--text);
  font-family: var(--font-mono); font-size: var(--fs-sm); padding: 6px 12px; cursor: pointer;
  transition: background 130ms, color 130ms, border-color 130ms;
}
.wf-orphan-btn:hover { background: var(--ink); color: var(--bg); }
.wf-orphan-btn.danger:hover { background: var(--danger); border-color: var(--danger); color: var(--danger-ink); }
.wf-orphan-btn:disabled { opacity: .55; cursor: default; }
.gate-hint { flex: 0 0 auto; margin: 0 28px 8px; text-align: center; font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--amber); }
.composer.blocked { opacity: .7; }
.composer.blocked textarea { pointer-events: none; }

@media (max-width: 1100px) and (min-width: 921px) {
  .home-layout { grid-template-columns: minmax(0, 1fr) 240px; gap: 32px; padding: 40px 32px 8px; }
  .home-rail { padding-left: 24px; }
  .is-empty .composer-wrap { padding: 20px 32px 40px; }
  .suggestion-arrow { display: none; }
}
@media (max-width: 920px) {
  .home-layout { grid-template-columns: minmax(0, 1fr); gap: 24px; padding: 40px 32px 8px; }
  .home-rail { display: none; }
  .is-empty .composer-wrap { padding: 20px 32px 40px; }
  .suggestion-arrow { display: none; }
}
@media (max-width: 920px) {
  .sidebar-toggle { display: inline-flex; }
  .ask-topbar { padding: 8px 20px; }
}
@media (max-width: 600px) {
  .ask-topbar { padding: 8px 12px; gap: 8px; }
  .tb-left { gap: 8px; }
  .tb-sub, .tb-usage, .composer-shortcut { display: none; }
  .chat-col { padding: 24px 20px; }
  .home-layout { padding: 28px 20px 8px; gap: 24px; }
  .home-rail { display: none; }
  .welcome-title { font-size: 30px; }
  .welcome-desc { font-size: 14px; }
  .composer-wrap { padding: 12px 16px 16px; }
  .is-empty .composer-wrap { padding: 16px 20px 32px; }
  .composer { padding: 12px; }
  .composer-bar { flex-wrap: nowrap; gap: 6px; }
  .bar-left { flex: 1; min-width: 0; gap: 2px; }
  .bar-right { margin-left: auto; }
  .chip { min-height: 44px; padding: 6px 5px; font-size: var(--fs-sm); }
  .tool-btn { width: 34px; }
  .send-btn { min-height: 44px; padding: 8px 12px; }
  .jump-latest { min-height: 44px; }
  .suggestions { grid-template-columns: 1fr; gap: 8px; margin-top: 24px; }
  .suggestion { padding: 14px 16px; align-items: center; }
  .suggestion-copy { gap: 2px; }
  .suggestion-arrow { display: inline; }
  .banner { margin: 12px 16px 0; }
  .wf-orphan { margin-inline: 16px; flex-wrap: wrap; }
}
</style>
