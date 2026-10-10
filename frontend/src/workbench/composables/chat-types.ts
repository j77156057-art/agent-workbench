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

export type ChatMsgStatus = 'streaming' | 'done' | 'stopped' | 'error'

/** 挂在助手回合内的工作流卡片引用（卡片自己连 SSE 推进度） */
export interface ChatWorkflowRef {
  workflowId: string
  seed?: Partial<WorkflowState>
}

/** 应用自身发起的活动（如「AI 浏览当前界面」）：渲染为居中小字条，
 *  不是用户气泡——其完整指令只作为系统上下文发给模型 */
export type ChatActivityKind = 'inspect'

export interface ChatMsg {
  id: number
  role: 'user' | 'assistant' | 'activity'
  text: string
  status: ChatMsgStatus
  /** ReAct 工具轨迹（问答页展示精简步骤，工作台展示完整时间线） */
  trace: ChatTraceItem[]
  /** 深度思考模型的 reasoning_content 流 */
  reasoning: string
  /** SSE notice 事件（提示性通知，如上下文自动压缩） */
  notices: string[]
  /** 运行期提示（后端标了 transient 的 notice）：只在流式中显示，回合结束即清 */
  progress?: string
  /** 计划模式步骤（plan 事件） */
  plan: string[]
  /** 本回合发起的开发工作流：消息内直接挂卡片 */
  workflow?: ChatWorkflowRef
  startedAt?: number
  lastActivityAt?: number
  finishedAt?: number
  error?: string
  /** 问答页：用户消息附带的图片（objectURL/dataURL） */
  images?: string[]
  /** 工作台：失败回合允许「继续完成任务」续跑 */
  recoverable?: boolean
  /** 工作台：图片消息只显示数量（File 随请求发送，不留 URL） */
  imageCount?: number
  /** role==='activity' 时的活动类型 */
  activityKind?: ChatActivityKind
}
