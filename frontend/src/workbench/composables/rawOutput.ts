// 原始输出缓冲：把 SSE 事件**原样**留在内存里，供右侧浮层的「原始输出」页签查看。
//
// 为什么只留响应侧：DocMind 的隐私口径是「prompt / response 原文永不落盘」
// （agent_trace.py 只记动作名/字数/耗时/成败）。这里沿用同一口径 ——
//   · 缓冲只在**内存**里，刷新页面即清空，不写任何文件；
//   · 不记用户提问与代码原文；
//   · 模型回包（token / trace / final 等）按原样显示，便于调试「到底收到了什么」。
//
// 连续的 token 增量合并成一条：逐 token 一行会把面板刷爆，而调试要看的是**事件序列**。
import { ref } from 'vue'
import type { SseEvent } from '../api'

export interface RawOutputEvent {
  id: number
  at: number
  type: string
  /** 事件来自哪个会话（底部主对话台 / 右侧浮层各有各的会话） */
  session: string
  /** 一行摘要：折叠时看这个 */
  summary: string
  /** 事件原样（JSON） */
  data: unknown
}

const LIMIT = 400
const events = ref<RawOutputEvent[]>([])
let seq = 0

export function rawOutputEvents(): RawOutputEvent[] {
  return events.value
}

export function clearRawOutput(): void {
  events.value = []
}

function summarize(ev: SseEvent): string {
  const type = String(ev?.type || 'unknown')
  if (type === 'token') {
    const n = typeof ev.text === 'string' ? ev.text.length : 0
    return `${n} 字`
  }
  if (type === 'final') {
    const n = typeof ev.text === 'string' ? ev.text.length : 0
    return `最终答案 ${n} 字`
  }
  const text = (ev as { text?: string }).text
  if (typeof text === 'string' && text.trim()) {
    const one = text.replace(/\s+/g, ' ').trim()
    return one.length > 90 ? one.slice(0, 90) + '…' : one
  }
  try {
    const raw = JSON.stringify(ev)
    return raw.length > 90 ? raw.slice(0, 90) + '…' : raw
  } catch {
    return type
  }
}

/** 记录一条 SSE 事件。连续 token 会并进上一条。 */
export function recordRawEvent(ev: SseEvent, session = ''): void {
  if (!ev || typeof ev !== 'object') return
  const type = String(ev.type || 'unknown')
  const last = events.value[events.value.length - 1]
  if (type === 'token' && last && last.type === 'token') {
    const prev = (last.data as { text?: string })?.text || ''
    const next = typeof ev.text === 'string' ? ev.text : ''
    last.data = { text: prev + next }
    last.summary = `${(prev + next).length} 字`
    last.at = Date.now()
    return
  }
  const next: RawOutputEvent = {
    id: ++seq,
    at: Date.now(),
    type,
    session,
    summary: summarize(ev),
    data: ev,
  }
  const list = events.value.length >= LIMIT
    ? events.value.slice(events.value.length - LIMIT + 1)
    : events.value
  events.value = [...list, next]
}
