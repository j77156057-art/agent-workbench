// 对话消息统一契约：问答首页（AskApp）与工作台对话台（ChatDock）共用，
// 字段与 /api/chat 的 SSE 事件对齐。入口差异字段保持可选。
import type { WorkflowState } from '../api'

/** ReAct 执行轨迹：思考 → 工具动作 → 观察 → 复核 */
export type ChatTraceKind = 'thought' | 'action' | 'observation' | 'reflection'

export interface ChatTraceItem {
  type: ChatTraceKind
  text: string
  /** 追加时刻（action 耗时回填用）；历史回放重建的轨迹可能没有 */
  at?: number
  /** observation/reflection 回填前一个 action 的耗时（ms） */
  elapsedMs?: number
}

/**
 * 交错流节点：模型思考与工具调用按**发生顺序**排成一条故事线
 * （think → 调用 → 返回 → think → … → 答案），不再是「思考整块在上、
 * 调用整块在下」的两段式。
 *
 * - `think`：reasoning_content 的一个分段。ReAct 每轮先流思考再动作，
 *   轮次边界即切点（useChatStream 在 thought/action 事件处切断）。
 * - `thought`/`action`/`result`：trace 条目的引用（**下标**而非拷贝——
 *   observation 会回头回填前一个 action 的 elapsedMs，拷快照就丢了）。
 */
export type ChatFlowItem =
  | { kind: 'think'; text: string }
  | { kind: 'thought'; trace: number }
  | { kind: 'action'; trace: number }
  | { kind: 'result'; trace: number }

export type ChatMsgStatus = 'streaming' | 'done' | 'stopped' | 'error'

/** 挂在助手回合内的工作流卡片引用（卡片自己连 SSE 推进度） */
export interface ChatWorkflowRef {
  workflowId: string
  seed?: Partial<WorkflowState>
}

/** P1：错误分类 + 自检信号（final 事件下游消费）。 */
export type ChatErrorKind = 'permanent' | 'retried_failed' | string
export type ChatConsistency = 'ok' | 'warn' | string

/** 应用自身发起的活动（如「AI 浏览当前界面」）：渲染为居中小字条，
 *  不是用户气泡——其完整指令只作为系统上下文发给模型 */
export type ChatActivityKind = 'inspect'

/** 结构化「向用户提问 / 请求授权」载荷（P0）：后端在拿不准、歧义写授权、
 *  安全门拦下对外写时下发，前端渲染成可点选项框；点某项即把其 value 作为
 *  下一轮用户消息发回（复用 send(preset)）。 */
export interface ChatAskOption { label: string; value: string }
export interface ChatAsk {
  /** clarify = 请用户澄清目标；permission = 请用户授权一次写操作。 */
  kind: 'clarify' | 'permission' | string
  question: string
  options: ChatAskOption[]
  /** 是否同时允许用户忽略选项、自己打字回答。 */
  allow_free_text?: boolean
}

/**
 * 本轮产物：模型写的文件、截的图。
 *
 * 后端 observation 事件早就带 `artifacts`（工具落盘/截图那一刻自报，
 * 路径与标签都有），但前端此前完全没用 —— 于是产物只躺在磁盘上，
 * 用户得自己去路径里搜才能看到"模型到底生成了什么"。
 */
export interface ChatArtifact {
  id?: string
  /** 'image' | 'code' | 'text' | …（后端按来源/扩展名判定） */
  kind?: string
  /**
   * 项目**相对**路径 —— 图片据此拼预览 URL（`/api/assets/raw?path=`）。
   * 绝对路径在浏览器里没法用（用户只看到一串盘符），也不该外泄。
   */
  path?: string
  /** 展示名（文件名 / "游戏运行画面"这类说明） */
  label?: string
  /** 一句话说明（新建了多大、这张图是什么） */
  summary?: string
  bytes?: number
  /** 产出证据链（工具名、来源） */
  evidence?: string[]
  /** 产出适配器（game / browser / …） */
  adapter?: string
  /** 直接可用的资源地址（http/https；其余协议后端已滤掉） */
  uri?: string
  /** MIME */
  mime?: string
  /** 渲染方式：media / interactive / diff / table / code / text / generic */
  renderer?: string
  /** 改动了多少行（diff 类产物） */
  change_summary?: { added_lines?: number; removed_lines?: number }
}

export interface ChatMsg {
  id: number
  role: 'user' | 'assistant' | 'activity'
  text: string
  status: ChatMsgStatus
  /** ReAct 工具轨迹（问答页展示精简步骤，工作台展示完整时间线） */
  trace: ChatTraceItem[]
  /** 深度思考模型的 reasoning_content 流（整块；交错展示用 flow） */
  reasoning: string
  /** 思考与工具调用按发生顺序的交错流（流式期由 useChatStream 构建；
   *  历史/演示等绕开管线的路径为空，由 flowOf 兜底合成） */
  flow?: ChatFlowItem[]
  /** SSE notice 事件（提示性通知，如上下文自动压缩） */
  notices: string[]
  /** 运行期提示（后端标了 transient 的 notice）：只在流式中显示，回合结束即清 */
  progress?: string
  /** 计划模式步骤（plan 事件） */
  plan: string[]
  /** 本轮产物（模型写的文件、截的图）：由 observation 事件里的 artifacts 收集 */
  artifacts?: ChatArtifact[]
  /** 本回合发起的开发工作流：消息内直接挂卡片 */
  workflow?: ChatWorkflowRef
  startedAt?: number
  lastActivityAt?: number
  finishedAt?: number
  error?: string
  /** P1：final.error_kind 三态（permanent / retried_failed / 其他）。
   *  模板按此分配 css class：红条 / 黄条 / 灰条。 */
  errorKind?: string
  /** 问答页：用户消息附带的图片（objectURL/dataURL） */
  images?: string[]
  /** 工作台：失败回合允许「继续完成任务」续跑 */
  recoverable?: boolean
  /** 工作台：图片消息只显示数量（File 随请求发送，不留 URL） */
  imageCount?: number
  /** role==='activity' 时的活动类型 */
  activityKind?: ChatActivityKind
  /** P2：后端 SSE turn 事件分发的 turn_id，用于刷新后 GET partial 拉起。 */
  turnId?: string
  /** P0：摘要一致性自检结果（final.consistency 转发到此处，模板按此染色） */
  consistency?: ChatConsistency
  /** 一致性自检命中率（前端调试用，模板不必显示） */
  consistencyRatio?: number
  /** 与命中率正交的原因标签：'verbatim_dump' 等。模板据此显示语义正确的提示。 */
  consistencyReason?: string
  /** final：本轮是澄清反问（后端未起工具，直接请用户补充）。问答页据此
   *  抑制「猜你想问」chip（BUG-3）。 */
  clarificationRequired?: boolean
  /** final：本轮为降级收尾（信息不足 / 未核验等），非干净的完整交付。 */
  degraded?: boolean
  /** 结构化「向用户提问 / 请求授权」载荷：存在时渲染成可点选项框。 */
  ask?: ChatAsk
}

/** start_workflow 动作行与其观察由工作流卡片承载，交错流里不重复展示。 */
function isWorkflowStart(text: string): boolean {
  return /^start_workflow\s*\(/.test(text.trim())
}

export function flowKindOf(type: ChatTraceKind): 'thought' | 'action' | 'result' {
  if (type === 'action') return 'action'
  if (type === 'thought') return 'thought'
  return 'result'
}
/**
 * 渲染用的交错流。流式期 useChatStream 已按轮次切好 reasoning；历史回放、
 * 演示模式、中断恢复等绕开管线的路径没有 flow，在此按 trace 顺序兜底合成
 * （reasoning 只剩整块，当作第一个 think——分段信息没留下来，不能假装知道
 * 切点）。两条路都过滤 start_workflow 行。
 */
export function flowOf(msg: {
  trace: ChatTraceItem[]
  reasoning?: string
  flow?: ChatFlowItem[]
}): ChatFlowItem[] {
  const skip = new Set<number>()
  msg.trace.forEach((item, i) => {
    if (item.type === 'action' && isWorkflowStart(item.text)) {
      skip.add(i)
      if (msg.trace[i + 1]?.type === 'observation') skip.add(i + 1)
    }
  })
  if (msg.flow && msg.flow.length) {
    return msg.flow.filter((node) => node.kind === 'think' || !skip.has(node.trace))
  }
  const items: ChatFlowItem[] = []
  if (msg.reasoning) items.push({ kind: 'think', text: msg.reasoning })
  msg.trace.forEach((item, i) => {
    if (skip.has(i)) return
    items.push({ kind: flowKindOf(item.type), trace: i })
  })
  return items
}
