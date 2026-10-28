// 消息状态展示的唯一口径：只根据已有字段判断“这轮发生了什么”，
// 不推断任务是否验收、不补造进度或耗时。
import type { ChatMsg } from '../workbench/composables/chat-types'

export type MessageTone = 'neutral' | 'accent' | 'warning' | 'danger'

export interface MessagePresentation {
  label: string
  tone: MessageTone
}

export function getMessagePresentation(msg: ChatMsg, askAnswered = false): MessagePresentation {
  if (msg.status === 'error') return { label: '回答失败', tone: 'danger' }
  if (msg.status === 'stopped') return { label: '已停止', tone: 'neutral' }
  if (msg.status === 'streaming') {
    return { label: msg.text ? '正在回答' : '正在处理', tone: 'accent' }
  }
  if (msg.ask && !askAnswered) {
    return { label: msg.ask.kind === 'permission' ? '等待授权' : '等待补充', tone: 'warning' }
  }
  if (msg.ask && askAnswered) return { label: '已提交选择', tone: 'neutral' }
  if (msg.degraded === true) return { label: '未完成', tone: 'warning' }
  if (msg.consistencyReason === 'verbatim_dump') return { label: '未综合结论', tone: 'warning' }
  if (msg.consistency === 'warn') return { label: '需核对', tone: 'warning' }
  return { label: '已回复', tone: 'neutral' }
}
