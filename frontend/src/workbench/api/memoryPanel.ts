// 记忆 v2 · 上下文面板 API（查看各层注入内容、直接编辑）。
import { request } from './_base'

export interface MemoryPanelEdge { type?: string; to?: string; direction?: string }
export interface MemoryPanelItem {
  id?: string
  kind?: string
  title?: string
  body?: string
  pinned?: boolean
  confidence?: number
  importance?: number
  edges?: MemoryPanelEdge[]
  [key: string]: unknown
}

export interface MemoryPanelHook {
  id?: string
  kind?: string
  text?: string
  pinned?: boolean
  verified?: boolean
  [key: string]: unknown
}

export interface MemoryPanelTaskItem {
  key?: string
  text?: string
  state?: string
  receipts?: string[]
  user_override?: boolean
  [key: string]: unknown
}

export interface MemoryLayer {
  key: string
  title: string
  text: string
  chars: number
  budget?: number | null
  editable: boolean
  source: string
  kind: string
  raw?: string
  version?: number
  hash_ok?: boolean
  gates_ok?: boolean
  missing_gates?: string[]
  error?: string
  path?: string
  profile?: Record<string, unknown>
  learned?: Record<string, unknown>
  items?: Array<Record<string, unknown>>
  errors?: Array<Record<string, unknown>>
  project_errors?: Array<Record<string, unknown>>
  next?: string
  book?: { id?: string; title?: string; status?: string }
  retrievable?: number
  retrievable_items?: Array<Record<string, unknown>>
}

export interface MemoryContextResult {
  ok?: boolean
  enabled?: boolean
  error?: string
  mode?: string
  session?: string
  turn_id?: string
  ts?: string
  layers?: MemoryLayer[]
  total_chars?: number
}

function call<T>(method: string, url: string, payload?: unknown): Promise<T> {
  return request<T>(url, {
    method,
    headers: payload === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  })
}

export const memoryPanelApi = {
  context(sessionId = '', mode: 'next' | 'last' = 'next'): Promise<MemoryContextResult> {
    const query = new URLSearchParams()
    if (sessionId) query.set('session_id', sessionId)
    query.set('mode', mode)
    return request(`/api/memory/context?${query.toString()}`)
  },
  saveConstitution(text: string, baseVersion?: number): Promise<{ ok?: boolean; version?: number; hash_ok?: boolean }> {
    return call('PUT', '/api/memory/constitution', { text, base_version: baseVersion })
  },
  saveProfile(patch: Record<string, unknown>): Promise<Record<string, unknown>> {
    return call('PUT', '/api/memory/profile', patch)
  },
  patchItem(itemId: string, payload: { text?: string; pinned?: boolean }): Promise<Record<string, unknown>> {
    return call('PATCH', `/api/memory/items/${encodeURIComponent(itemId)}`, payload)
  },
  promoteItem(itemId: string): Promise<{ ok?: boolean; promoted?: boolean; reasons?: string[]; pinned?: boolean }> {
    return call('POST', `/api/memory/items/${encodeURIComponent(itemId)}/promote`)
  },
  adoptItem(itemId: string): Promise<{ ok?: boolean; promoted?: boolean; reasons?: string[]; pinned?: boolean }> {
    return call('POST', `/api/memory/items/${encodeURIComponent(itemId)}/adopt`)
  },
  linkItem(itemId: string, to: string, type: string): Promise<Record<string, unknown>> {
    return call('POST', `/api/memory/items/${encodeURIComponent(itemId)}/links`, { to, type })
  },
  unlinkItem(itemId: string, to: string, type: string): Promise<Record<string, unknown>> {
    const query = new URLSearchParams({ to, type })
    return request(`/api/memory/items/${encodeURIComponent(itemId)}/links?${query.toString()}`, { method: 'DELETE' })
  },
  deleteItem(itemId: string): Promise<Record<string, unknown>> {
    return request(`/api/memory/items/${encodeURIComponent(itemId)}?confirm=true`, { method: 'DELETE' })
  },
  addHook(payload: { session_id: string; kind: string; text: string; pinned?: boolean }): Promise<Record<string, unknown>> {
    return call('POST', '/api/memory/hooks', payload)
  },
  patchHook(itemId: string, payload: { session_id: string; text?: string; pinned?: boolean }): Promise<Record<string, unknown>> {
    return call('PATCH', `/api/memory/hooks/${encodeURIComponent(itemId)}`, payload)
  },
  deleteHook(itemId: string, sessionId: string): Promise<Record<string, unknown>> {
    const query = new URLSearchParams({ session_id: sessionId, confirm: 'true' })
    return request(`/api/memory/hooks/${encodeURIComponent(itemId)}?${query.toString()}`, { method: 'DELETE' })
  },
  editTaskItem(key: string, text: string): Promise<Record<string, unknown>> {
    return call('PATCH', `/api/memory/taskbook/items/${encodeURIComponent(key)}`, { text })
  },
  completeTaskItem(key: string, payload: { override?: boolean; merge?: string; receipt_ids?: string[] }): Promise<Record<string, unknown>> {
    return call('POST', `/api/memory/taskbook/items/${encodeURIComponent(key)}/complete`, payload)
  },
  reopenTaskItem(key: string): Promise<Record<string, unknown>> {
    return call('POST', `/api/memory/taskbook/items/${encodeURIComponent(key)}/reopen`)
  },
  reindex(): Promise<Record<string, unknown>> {
    return call('POST', '/api/memory/reindex')
  },
}
