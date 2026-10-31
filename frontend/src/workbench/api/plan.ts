// Plan 长跑：仅开发台表面可调用（后端校验 X-DocMind-Surface）。
import { request } from './_base'

export interface PlanTask {
  key?: string
  text?: string
  state?: string
  receipts?: string[]
}

export interface PlanLogRow {
  ts?: string
  kind?: string
  text?: string
  phase?: string
  iteration?: number
  rationale?: string
  task?: string
  actions?: string[]
  taskbook?: Record<string, unknown>
}

export interface PlanState {
  id?: string
  project?: string
  session?: string
  goal?: string
  status?: string
  phase?: string
  iteration?: number
  review_interval_minutes?: number
  next_review_at?: string
  last_final?: string
  last_error?: string
  stop_reason?: string
  user_stop?: boolean
  created?: string
  updated?: string
}

export interface PlanStatus {
  ok?: boolean
  active?: boolean
  state?: PlanState | null
  taskbook?: { items?: PlanTask[]; pending?: number; done?: number; total?: number }
  log?: PlanLogRow[]
}

function planRequest<T>(url: string, init: RequestInit): Promise<T> {
  const headers = new Headers(init.headers as HeadersInit | undefined)
  headers.set('X-DocMind-Surface', 'workbench')
  return request<T>(url, { ...init, headers })
}

export const planApi = {
  start(sessionId: string, goal: string, reviewIntervalMinutes = 30): Promise<PlanStatus> {
    return planRequest('/api/plan/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        goal,
        review_interval_minutes: reviewIntervalMinutes,
        max_iterations: 200,
      }),
    })
  },
  stop(sessionId: string): Promise<PlanStatus> {
    return planRequest('/api/plan/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, confirm: true }),
    })
  },
  status(sessionId: string): Promise<PlanStatus> {
    return planRequest(
      `/api/plan/status?${new URLSearchParams({ session_id: sessionId }).toString()}`,
      { method: 'GET' },
    )
  },
  log(sessionId: string, limit = 200): Promise<{ ok?: boolean; items?: PlanLogRow[] }> {
    return planRequest(
      `/api/plan/log?${new URLSearchParams({
        session_id: sessionId,
        limit: String(limit),
      }).toString()}`,
      { method: 'GET' },
    )
  },
}
