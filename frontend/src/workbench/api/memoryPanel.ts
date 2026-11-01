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

export interface MemoryHealthWarning {
  code?: string
  message?: string
}

export interface MemoryHealthLayer {
  key?: string
  title?: string
  chars?: number
  budget?: number | null
}

export interface MemoryHealthMetrics {
  consolidate_calls?: number
  skipped?: number
  errors?: number
  tokens_in?: number
  tokens_out?: number
  p50_ms?: number
  p95_ms?: number
  promoted?: number
}

export interface MemoryFunnelStage {
  key?: string
  label?: string
  count?: number
  /** 这一级数的是什么：turn=回合 / item=条目 / session=会话。单位不同的两级不能比大小。 */
  unit?: 'turn' | 'item' | 'session' | string
  unit_label?: string
  scope?: string
  scope_label?: string
  /** 回合级：窗口内有多少条记录真的带这一级的判据字段；null 表示不适用。 */
  measurable?: number | null
  /** 「共享层回注」的事件数（一个回合可能注入多次），与会话数分开报。 */
  events?: number | null
  /** 条目级的存量（不受时间窗约束），与窗口内流量并列。 */
  stock?: number | null
}

export interface MemoryFunnelHandoff {
  from?: string
  to?: string
  from_label?: string
  to_label?: string
  comparable?: boolean
  reason?: string
}

export interface MemoryFunnel {
  days?: number
  counts?: Record<string, number>
  stages?: MemoryFunnelStage[]
  /** 第一个「上一级有量、这一级归零」的断点；跨单位处改用「交接处停在…」措辞。空串 = 全通。 */
  stalled_at?: string
  /** 后级多于前级的解释（缺字段 / 跨单位 / 正常形状），与断点分开显示。 */
  caveats?: string[]
  /** 单位或作用域不同的相邻级 —— 这些地方没有「子集」关系。 */
  handoffs?: MemoryFunnelHandoff[]
  /** 整级没有判据字段，不能当断点看。 */
  unmeasurable?: string[]
  skipped_breakpoints?: Array<{ at?: string; reason?: string }>
  missing_field?: Record<string, number>
  /** 采纳/晋升的窗口按 `updated` 卡（采纳本身无时间戳）。 */
  item_window_by?: string
  inject_events?: number
  inject_unattributed?: number
}

export interface MemoryHealth {
  ok?: boolean
  project_id?: string
  generated_at?: string
  funnel?: MemoryFunnel
  shared?: { active?: number; pinned?: number; superseded?: number }
  project?: {
    total?: number
    candidate?: number
    adopted?: number
    frozen?: number
    promoted?: number
    superseded?: number
    merged?: number
    by_status?: Record<string, number>
  }
  duplicates?: { project?: number; shared?: number; planned?: Array<Record<string, unknown>> }
  stale?: { retention_days?: number; superseded?: number }
  index?: { items?: number; vectors?: number; edges?: number; fts?: boolean; rebuilt_at?: string }
  context?: { total_chars?: number; layers?: MemoryHealthLayer[] }
  metrics?: MemoryHealthMetrics
  warnings?: MemoryHealthWarning[]
}

export interface MemoryMaintenanceResult {
  ok?: boolean
  apply?: boolean
  candidates?: Record<string, unknown>
  shared?: Record<string, unknown>
  gc?: Record<string, unknown>
  index?: Record<string, unknown>
}

/** 磁盘保留期：三类有界副本（试做区 / 项目快照 / dist 构建副本）的回收设置与计划。 */
export interface DiskRetentionSettings {
  /** 每隔多少天自动清一次；0 = 不自动清（默认），只能手动点。 */
  interval_days: number
  /** 闲置多少天才算过期。 */
  idle_days: number
  /** 永不删除的名字/前缀；内置项只加不减。 */
  whitelist: string[]
}

export interface DiskRetentionRanges {
  interval_days_min: number
  interval_days_max: number
  idle_days_min: number
  idle_days_max: number
  whitelist_max_entries: number
  max_items_per_run: number
}

export interface DiskRetentionPlan {
  reclaimable_bytes?: number
  reclaimable_items?: number
  blocked_by_whitelist_bytes?: number
  blocked_by_whitelist_items?: number
  needs_review_bytes?: number
  needs_review_items?: number
  kept_items?: number
  max_items_per_run?: number
  by_kind?: Record<string, number>
}

export interface DiskRetentionLastRun {
  ts?: number
  trigger?: string
  applied?: boolean
  freed_bytes?: number
  freed_items?: number
  failed_items?: number
  skipped_items?: number
  failures?: string[]
  plan_summary?: DiskRetentionPlan
}

export interface DiskRetentionState {
  ok: boolean
  enabled: boolean
  state_root?: string
  project_root?: string
  settings: DiskRetentionSettings
  ranges: DiskRetentionRanges
  builtin_whitelist: string[]
  schedule?: { interval_days?: number; due?: boolean; last_apply_ts?: number | null }
  last_run?: DiskRetentionLastRun
  plan?: DiskRetentionPlan
}

export interface DiskRetentionRow {
  kind?: string
  name?: string
  path?: string
  bytes?: number
  reason?: string
  whitelisted_by?: string
}

export interface DiskRetentionRunResult {
  ok: boolean
  applied: boolean
  dry_run: boolean
  plan?: DiskRetentionPlan
  items?: DiskRetentionRow[]
  blocked?: DiskRetentionRow[]
  result?: {
    removed?: Array<Record<string, unknown>>
    failed?: Array<Record<string, unknown>>
    skipped?: Array<Record<string, unknown>>
    freed_bytes?: number
    freed_items?: number
  }
  record?: DiskRetentionLastRun
  settings?: DiskRetentionSettings
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
  supersedeItem(itemId: string, by: string, confirm = true): Promise<Record<string, unknown>> {
    return call('POST', `/api/memory/items/${encodeURIComponent(itemId)}/supersede`, { by, confirm })
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
  health(): Promise<MemoryHealth> {
    return request('/api/memory/health')
  },
  maintain(payload: {
    apply?: boolean
    confirm?: boolean
    retention_days?: number
    threshold?: number
  }): Promise<MemoryMaintenanceResult> {
    return call('POST', '/api/memory/maintain', payload)
  },
  /** 磁盘回收：读当前设置 + 后端下发的边界 + 上次计划（不现场扫描，秒级返回）。 */
  diskRetention(): Promise<DiskRetentionState> {
    return request('/api/disk-retention')
  },
  saveDiskRetention(payload: Partial<DiskRetentionSettings>): Promise<DiskRetentionState> {
    return call('POST', '/api/disk-retention/settings', payload)
  },
  /** `apply=false` 只出计划（也是「重新扫描」）；真删除必须同时给 `confirm=true`。 */
  runDiskRetention(payload: {
    apply?: boolean
    confirm?: boolean
    include_orphans?: boolean
  }): Promise<DiskRetentionRunResult> {
    return call('POST', '/api/disk-retention/run', payload)
  },
}
