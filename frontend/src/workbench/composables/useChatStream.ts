// SSE 流式回答的接收管线：工作台对话台与问答首页共用同一份机械逻辑——
//  1) token / reasoning 增量按 rAF 帧合批提交（SSE 单帧常只有 1~2 个 token，
//     逐 token 触发响应式更新会让整段答案每 token 重渲染、强排滚动，造成「一卡一卡」）；
//  2) trace（thought/action/observation/reflection）追加并回填 action 耗时；
//  3) notice / plan / workflow / context / final 事件分类分发，业务差异通过钩子注入。
// 不持有 messages 数组、不绑定请求生命周期（abort/epoch 仍由调用方管理）。
import type { SseEvent } from '../api'
import type { BrowserEvidence, ChatFlowItem, ChatTraceItem, ChatTraceKind, ChatWorkflowRef, ChatAsk, ChatArtifact, FileDiffEntry } from './chat-types'
// ⚠ 相对导入必须带 `.ts` 扩展名：`frontend/tests/*.test.mjs` 用
// `node --experimental-strip-types` 直接跑生产模块（见 tests/test_frontend_node_tests.py），
// node 的 ESM 解析器不认无扩展名路径（Vite 认，所以只有 node 这条链会炸）。
import { flowKindOf } from './chat-types.ts'

export type { ChatTraceKind, ChatTraceItem, ChatMsg, ChatMsgStatus, ChatWorkflowRef } from './chat-types'

export interface ChatStreamTurn {
  id: number
  text: string
  reasoning: string
  notices: string[]
  /** transient notice 的落点：只在流式中显示的运行期提示，不进历史 */
  progress?: string
  trace: ChatTraceItem[]
  /** 思考与工具调用的交错流（think 节点随帧生长，动作事件处切断） */
  flow?: ChatFlowItem[]
  plan?: string[]
  workflow?: ChatWorkflowRef
  /** 本轮产物（模型写的文件、截的图）：由 observation 事件里的 artifacts 收集 */
  artifacts?: ChatArtifact[]
  status?: string
  error?: string
  /** P1：final.error_kind 三态（permanent / retried_failed / 其他）。
   *  模板按此分配 css class：红条 / 黄条 / 灰条。 */
  errorKind?: string
  /** P2：后端 turn 事件分发的 turn_id，用于刷新后 GET partial 拉起 */
  turnId?: string
  /** 精确断点续跑：后端 checkpoint id（final 事件写入）。 */
  checkpointId?: string
  /** P0：摘要一致性自检结果（final.consistency 转发到此处） */
  consistency?: 'ok' | 'warn' | string
  consistencyRatio?: number
  /** 与覆盖率正交的原因标签（final.consistency_reason 转发）：如 'verbatim_dump'。
   *  覆盖率只说「摘要漏没漏过程细节」，这里补一句「答案是不是通篇原文裸铺没归纳」，
   *  模板据此显示语义正确的提示，而非把 dump 误报成覆盖存疑。 */
  consistencyReason?: string
  /** final：本轮是澄清反问（后端未起工具，直接请用户补充）。供上层抑制
   *  「猜你想问」等把澄清当正常答案的渲染（BUG-3）。 */
  clarificationRequired?: boolean
  /** final：本轮为降级收尾（信息不足 / 未核验等），非干净的完整交付。 */
  degraded?: boolean
  /** 结构化「向用户提问 / 请求授权」载荷（来自 final 或 needs_confirmation 事件）。 */
  ask?: ChatAsk
}

export interface ChatStreamHooks<T extends ChatStreamTurn> {
  /** 每条绑定到 turn 的事件到达时（用于刷新 lastActivityAt 等） */
  onActivity?: (turn: T) => void
  /** 一帧内有正文增量提交到 turn.text（可在此做节流 markdown 重渲染） */
  onTextFlushed?: (turn: T) => void
  /** 一次 rAF 帧提交完成（每帧最多一次，用于滚动跟随） */
  onFrame?: () => void
  /** reasoning 事件到达（是否首次由调用方自行判重，如自动展开思考窗） */
  onReasoning?: (turn: T) => void
  /** context 事件（全局上下文用量等，不绑定具体回合） */
  onContext?: (ev: SseEvent) => void
  /** 结构性事件（trace/notice/plan/workflow/final）处理完后（跟随滚动） */
  onStructural?: () => void
  /** final 文本（缓冲已摘除；覆盖/守卫/持久化等策略由调用方决定） */
  onFinal?: (turn: T, text: string) => void
}

export function useChatStream<T extends ChatStreamTurn>(hooks: ChatStreamHooks<T>) {
  interface StreamBuffer { turn: T; text: string; reasoning: string; think: string }
  const buffers = new Map<number, StreamBuffer>()
  // 首条 final 守卫：终态可能重复下发（重试后的第二个 final、成功 final 后的
  // error final）。此前只在问答页调用方有一份守卫，工作台对话台没有 —— 第二条
  // 会覆盖已收到的答案/把成功翻成 error。下沉到这里，两条页面共用。
  const finalSeen = new WeakSet<object>()
  let raf = 0
  // 每个 turn 当前是否有一个「敞开的」think 节点（reasoning 分段进行中）。
  // 为什么不能等动作事件才整段落进 flow：那会让思考在动作发生前一直不可见，
  // 违背「思考是实时外露」；所以首帧就建节点，之后随帧 append。
  const thinkOpen = new Map<number, boolean>()

  /** 把本帧累积的思考文本结算进交错的 think 节点（没有就新建）。 */
  function syncThink(turn: T, text: string) {
    if (!text) return
    const flow = (turn.flow ??= [])
    if (thinkOpen.get(turn.id)) {
      const last = flow[flow.length - 1]
      if (last && last.kind === 'think') { last.text += text; return }
    }
    flow.push({ kind: 'think', text })
    thinkOpen.set(turn.id, true)
  }
  /** 切断当前思考分段：下一个 reasoning  chunk 将开新节点（动作/答案前的最后一跳）。 */
  function closeThink(turn: T) { thinkOpen.delete(turn.id) }

  function flush() {
    raf = 0
    if (!buffers.size) return
    for (const buf of buffers.values()) {
      if (buf.text) { buf.turn.text += buf.text; hooks.onTextFlushed?.(buf.turn) }
      if (buf.reasoning) buf.turn.reasoning += buf.reasoning
      if (buf.think) syncThink(buf.turn, buf.think)
      buf.think = ''
    }
    buffers.clear()
    // Vue 的 DOM 刷新也排在微任务里，挂在其后读到的是本帧最新布局
    hooks.onFrame?.()
  }
  function scheduleFlush() {
    if (!raf) raf = requestAnimationFrame(flush)
  }
  /** 流结束时同步排空（取消未触发的帧回调），保证 final 文本不丢。 */
  function drain() {
    if (raf) { cancelAnimationFrame(raf); raf = 0 }
    if (!buffers.size) return
    for (const buf of buffers.values()) {
      if (buf.text) buf.turn.text += buf.text
      if (buf.reasoning) buf.turn.reasoning += buf.reasoning
      if (buf.think) syncThink(buf.turn, buf.think)
      buf.think = ''
    }
    buffers.clear()
  }
  /** 放弃所有缓冲（切换/重置时调用） */
  function reset() {
    if (raf) { cancelAnimationFrame(raf); raf = 0 }
    buffers.clear()
    thinkOpen.clear()
  }

  function appendTrace(turn: T, type: ChatTraceKind, text: string, evidence?: BrowserEvidence, ok?: boolean, fileDiffs?: FileDiffEntry[]) {
    const at = Date.now()
    // 观察/反思通常是前一个工具动作的返回，将两者之间的时间显示为动作耗时。
    if (type === 'observation' || type === 'reflection') {
      const pending = [...turn.trace].reverse().find((item) => item.type === 'action' && !item.elapsedMs)
      if (pending?.at) pending.elapsedMs = Math.max(0, at - pending.at)
    }
    turn.trace.push({
      type, text, at,
      ...(evidence ? { evidence } : {}),
      ...(typeof ok === 'boolean' ? { ok } : {}),
      ...(fileDiffs && fileDiffs.length ? { file_diffs: fileDiffs } : {}),
    })
    // 轨迹事件即轮次边界：先切断思考分段，再把动作节点接进交错流
    closeThink(turn)
    const flow = (turn.flow ??= [])
    flow.push({ kind: flowKindOf(type), trace: turn.trace.length - 1 })
  }

  /**
   * 登记本轮产物。
   *
   * 去重按 path：同一个文件可能被反复报（改一次报一次、截一张报一张），
   * 卡片区要的是"一共产出了什么"，不是"报了几次"。
   */
  function collectArtifacts(turn: T, list: ChatArtifact[]) {
    const seen = new Set((turn.artifacts ?? []).map((a) => a.path || a.id || ''))
    for (const a of list) {
      if (!a || typeof a !== 'object') continue
      const key = a.path || a.id || ''
      if (!key || seen.has(key)) continue
      seen.add(key)
      ;(turn.artifacts ??= []).push(a)
    }
  }

  function bufferFor(turn: T): StreamBuffer {
    let buf = buffers.get(turn.id)
    if (!buf) { buf = { turn, text: '', reasoning: '', think: '' }; buffers.set(turn.id, buf) }
    return buf
  }
  /** 结构性事件（thought/action/…）到达前，先把未结算的思考文本落进节点。 */
  function settleThink(turn: T) {
    const buf = buffers.get(turn.id)
    if (buf?.think) { syncThink(turn, buf.think); buf.think = '' }
  }

  function dispatch(ev: SseEvent, turn: T | undefined) {
    // 上下文用量是全局指示，不挂在某条消息上
    if (ev.type === 'context') { hooks.onContext?.(ev); return }
    // turn 事件用于「本轮 ID」分发；不绑定到具体回合，只透传给上层
    if (ev.type === 'turn') {
      if (turn) turn.turnId = ev.turn_id || turn.turnId
      hooks.onStructural?.()
      return
    }
    if (!turn) return
    if (ev.type === 'final') {
      if (finalSeen.has(turn)) return
      finalSeen.add(turn)
    }
    hooks.onActivity?.(turn)
    // 结构化「提问 / 授权」载荷：终态澄清 final 与流中的 needs_confirmation 都可能带。
    // 抢先于下面的类型分支统一捕获，否则 needs_confirmation（无专门分支）会被整条丢弃。
    // 只在事件确实带了 ask 时才写，避免后续无 ask 的事件把先前的框擦掉。
    if (ev.ask) turn.ask = ev.ask
    // 运行期提示（「正在继续执行」这类现在时）到此作废：回合已经结束，再挂着它
    // 界面就是在宣称一件已经不做完的事。final / done 两条都判，因为出错收尾时
    // final 可能没有正文（下面那条 final 分支要求 ev.text 非空）。
    if (ev.type === 'final' || ev.type === 'done') {
      turn.progress = ''
      // done 兜底切断：abort 等路径没有 final，别把半段思考一直敞着
      if (ev.type === 'done') { settleThink(turn); closeThink(turn) }
    }
    if (ev.type === 'token' && typeof ev.text === 'string') {
      bufferFor(turn).text += ev.text
      scheduleFlush()
    } else if (ev.type === 'final' && typeof ev.text === 'string' && ev.text) {
      // 答案落定前，把最后一段思考（动作之后、答案之前的那一跳）收进 flow
      settleThink(turn)
      closeThink(turn)
      buffers.delete(turn.id)
      // P0：自检结果直接挂在回合上，模板按 consistency 分色
      if (ev.consistency === 'warn' || ev.consistency === 'ok') {
        turn.consistency = ev.consistency
        if (typeof ev.consistency_ratio === 'number') {
          turn.consistencyRatio = ev.consistency_ratio
        }
      }
      // 与命中率正交的原因标签（如逐行裸铺源码）：即便 consistency=ok 也要接，
      // 因为 dump 的关键词命中率≈100% 恰恰会被判成 ok。模板据此显示语义正确的提示。
      if (typeof ev.consistency_reason === 'string') {
        turn.consistencyReason = ev.consistency_reason
      }
      // 澄清 / 降级标志：后端说这轮不是干净的完整交付，上层据此抑制
      // 「猜你想问」等把澄清当正常答案的渲染（BUG-3）。
      if (ev.clarification_required === true) turn.clarificationRequired = true
      if (ev.degraded === true) turn.degraded = true
      // 精确断点续跑：后端给出 checkpoint id，失败回合的「继续」据此取回逐步现场。
      if (typeof ev.checkpoint_id === 'string' && ev.checkpoint_id) turn.checkpointId = ev.checkpoint_id
      hooks.onFinal?.(turn, ev.text)
      if (ev.status === 'error') {
        turn.status = 'error'
        turn.error = ev.error_kind || '请求失败'
        // P1：把 error_kind 分出来供模板染色
        turn.errorKind = ev.error_kind
      }
    } else if (ev.type === 'final' && ev.status === 'error') {
      // 错误收尾 final 可能 text 为空（被服务端「请求失败」剥离成空文本）；
      // 仍要让上层知道这是终态、并把 error_kind 分流。
      settleThink(turn)
      closeThink(turn)
      buffers.delete(turn.id)
      turn.status = 'error'
      turn.error = ev.error_kind || '请求失败'
      turn.errorKind = ev.error_kind
      if (typeof ev.checkpoint_id === 'string' && ev.checkpoint_id) turn.checkpointId = ev.checkpoint_id
    } else if (ev.type === 'reasoning' && typeof ev.text === 'string') {
      const buf = bufferFor(turn)
      buf.reasoning += ev.text
      buf.think += ev.text
      hooks.onReasoning?.(turn)
      scheduleFlush()
    } else if (ev.type === 'notice' && ev.text) {
      // 「这条是不是运行期进度」由**发 notice 的后端**用 transient 说，不由前端比文案猜。
      // 旧版这里是一份四条文案白名单（已进入模型处理 / 模型仍在处理 / 正在准备本轮上下文 /
      // 正在等待模型输出），与后端**零交集**：那 4 条后端一条都不发，而它实际发的 3 条
      // 一条都没被过滤掉 —— 于是「模型输出尚未结束，正在继续执行。」在任务完成后
      // 仍挂在横幅上。逐条列举已经失效过一次，所以不再回到那种写法。
      // 见 docs/known-defect-notice-whitelist-detached.md。
      if (ev.transient === true) turn.progress = ev.text
      else if (!turn.notices.includes(ev.text)) turn.notices.push(ev.text)
    } else if (ev.type === 'plan' && Array.isArray(ev.steps)) {
      turn.plan = ev.steps as string[]
    } else if (ev.type === 'thought' || ev.type === 'action'
               || ev.type === 'observation' || ev.type === 'reflection') {
      // 轮次边界：先结算未提交的思考文本（保住实时节奏），再切断分段、接动作节点
      if (ev.text) { settleThink(turn); appendTrace(turn, ev.type, ev.text, ev.browser_evidence, ev.ok, ev.file_diffs as FileDiffEntry[] | undefined) }
      // 产物：工具落盘/截图那一刻自报，跟着那一次观察走。
      // 不从 action 文本反推 —— 被安全拦截的调用同样带 path，反推会把没做的记成做了。
      if (ev.type === 'observation' && ev.artifacts?.length) {
        collectArtifacts(turn, ev.artifacts)
      }
    } else if (ev.type === 'workflow') {
      // start_workflow 已执行：卡片直接挂在当前助手回合内，后续由卡片自己连 SSE。
      const w = ev as unknown as { workflow_id?: string; status?: string; phase?: string; kind?: string; request?: string }
      if (w.workflow_id && !turn.workflow) {
        turn.workflow = {
          workflowId: w.workflow_id,
          seed: { status: w.status, phase: w.phase, kind: w.kind, request: w.request },
        } as T['workflow']
      }
    }
    // 结构类事件（计划/工具轨迹/通知）出现时跟随一次；token 滚动已在帧合批里处理
    if (ev.type !== 'token' && ev.type !== 'reasoning') hooks.onStructural?.()
  }

  return { scheduleFlush, drain, reset, appendTrace, dispatch }
}
