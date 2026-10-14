// /api/chat 问答流式入口（aiApi）（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { SelectionAiRequest, SseEvent, SseStreamHandlers, postSse } from './sse'
import { getSessionId } from './session'
import { withProject } from './_base'
import type { ChatUiContext } from '../previewFeedback'

export interface ActiveTurnRef {
  turn_id: string
  status: string
  created_at: number
  event_count: number
  has_final: boolean
}
export interface TurnPartialResponse {
  ok: boolean
  turn_id: string
  session_id: string
  project_id?: string | null
  status: string
  created_at: number
  events: SseEvent[]
  final?: SseEvent
}

export const aiApi = {
  /** 解释 / Review / 自由提问：走 ReAct agent（可 search_code/read_file/grep，引用文件行号）。
   *  web/thinking：逐请求的联网搜索 / 深度思考开关（后端默认均为关/模型默认）。
   *  session_id：当前标签页会话，用于隔离并发请求的逐请求开关。
   *  turn_id：可选；进页面后从 /api/turn/active 拿到 in-flight turn_id 后可继续用同一 id，
   *  便于刷新后 GET partial 拉到这条 turn 的所有事件。 */
  askGrounded(
    question: string,
    h: SseStreamHandlers,
    opts: { web?: boolean; thinking?: boolean | null; images?: Blob[]; sessionId?: string; uiContext?: ChatUiContext; visualTimeline?: { at: string; observation: string }[]; workflowId?: string; feedbackId?: string; turnId?: string } = {},
  ): Promise<void> {
    const fd = new FormData()
    fd.append('question', question)
    fd.append('session_id', opts.sessionId || getSessionId())
    if (opts.uiContext) fd.append('ui_context', opts.uiContext)
    // 语音轮次同样要看画面（「边看边聊」），所以与 cockpit_live_vision 一样带上时间线。
    if ((opts.uiContext === 'cockpit_live_vision' || opts.uiContext === 'cockpit_voice_turn')
      && opts.visualTimeline?.length) {
      fd.append('visual_timeline', JSON.stringify(opts.visualTimeline.slice(-5)))
    }
    // 桌面视觉复验：后端只在 ui_context=desktop_visual_review 时读取这两个编号对账。
    if (opts.uiContext === 'desktop_visual_review') {
      if (opts.workflowId) fd.append('workflow_id', opts.workflowId)
      if (opts.feedbackId) fd.append('feedback_id', opts.feedbackId)
    }
    fd.append('web_mode', opts.web ? '1' : '0')
    if (opts.thinking === true) fd.append('thinking_mode', '1')
    else if (opts.thinking === false) fd.append('thinking_mode', '0')
    // P2：复用既有 turn_id，让 GET partial 拉到这条 turn 的所有事件
    if (opts.turnId) fd.append('turn_id', opts.turnId)
    // 多模态：附带图片（后端按 filename 扩展名 + 文件头魔数双重校验）
    for (const img of opts.images || []) {
      fd.append('images', img, (img as File).name || 'image.png')
    }
    return postSse('/api/chat', { method: 'POST', body: fd }, h)
  },
  /** 改写：直连 LLM 快通道，强约束只产出可直接替换的纯代码。 */
  rewriteSelection(req: SelectionAiRequest, h: SseStreamHandlers): Promise<void> {
    return postSse(
      '/api/selection_ai',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...req, mode: 'rewrite' }),
      },
      h,
    )
  },
  /** P2：列出该会话当前所有 in-flight turn（用于刷新后 partial 拉起） */
  listActiveTurns(sessionId: string): Promise<{ ok: boolean; session_id: string; turns: ActiveTurnRef[] }> {
    const url = `/api/turn/active?session_id=${encodeURIComponent(sessionId || getSessionId())}`
    return fetch(url, withProject({ headers: { Accept: 'application/json' } }))
      .then(async (res) => {
        if (res.status === 404) return { ok: false, session_id: sessionId, turns: [] }
        const body = await res.json()
        return body
      })
  },
  /** P2：拉起一条 turn 的 partial（已发事件 + final） */
  async getTurnPartial(turnId: string): Promise<TurnPartialResponse> {
    const res = await fetch(`/api/turn/${encodeURIComponent(turnId)}/partial`,
      withProject({ headers: { Accept: 'application/json' } }))
    if (res.status === 404) {
      return { ok: false, turn_id: turnId, session_id: '', status: 'expired', created_at: 0, events: [] }
    }
    const body = await res.json()
    return body
  },
}

export const voiceApi = {
  status() { return fetch('/api/voice/status').then(r => r.json() as Promise<{ browser_fallback: boolean; stt_configured: boolean; tts_configured: boolean }>) },
  async transcribe(blob: Blob, language = ''): Promise<string> {
    const fd = new FormData(); fd.append('file', blob, 'voice.webm'); fd.append('language', language)
    const res = await fetch('/api/voice/transcribe', { method: 'POST', body: fd })
    const body = await res.json() as { ok?: boolean; text?: string; error?: string }
    if (!res.ok || body.ok === false) throw new Error(body.error || '语音识别失败')
    return body.text || ''
  },
  async dialogue(text: string, mainResult = '', forward = true): Promise<{ text: string; forward_to_main?: boolean }> {
    const res = await fetch('/api/voice/dialogue', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, main_result: mainResult, forward }),
    })
    const body = await res.json() as { ok?: boolean; text?: string; error?: string; forward_to_main?: boolean }
    if (!res.ok || body.ok === false) throw new Error(body.error || '语音协作失败')
    return { text: body.text || '', forward_to_main: body.forward_to_main }
  },
  async speech(text: string, voice = ''): Promise<Blob> {
    const res = await fetch('/api/voice/speech', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice }),
    })
    if (!res.ok) {
      const body = await res.json().catch(() => ({})) as { error?: string }
      throw new Error(body.error || '语音合成失败')
    }
    return res.blob()
  },
}
