// 运维层：预算 / 追踪 / 会话 / 技能（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { postJson, request, withProject } from './_base'

// ---------------------------------------------------------------- harness 运维层
// 对应后端 agent_trace / sessions / pricing / hooks / skills（纯只读查看 + 预算/热重载操作）。

export interface BudgetStatus {
  global_limit: number
  global_spent: number
  day: string
  day_spent: number
  per_minute_calls_limit: number
  per_minute_cost_limit: number
  minute_calls: number
  minute_cost: number
  sessions: Record<string, { spent: number; limit: number }>
  pricing_file: string
}
export interface BudgetCheck {
  ok: boolean
  reason: string
  global_limit: number
  global_spent: number
  session_limit: number
  session_spent: number
  day_spent: number
  minute_calls: number
  minute_cost: number
  per_minute_calls_limit: number
  per_minute_cost_limit: number
}
export interface TraceStep {
  i: number
  action: string
  arg_chars?: number
  latency_ms?: number
  obs_chars?: number
  ok?: boolean
}
/**
 * trace 记录级 `error` 字段：
 * - 普通 Agent 回合：错误原文（字符串，来源 agent_trace）；
 * - flow 回合（AI 工具流编排器）：按隐私边界（方案 §9.5）已脱敏为元数据对象
 *   `{ error_kind, chars }`，**绝不**含原文 / 文件路径 / 代码。
 */
export type TraceError = string | { error_kind?: string; chars?: number }

/**
 * 统一格式化 trace 的 `error` 字段，供各面板渲染，避免对象被直接渲染成 `[object Object]`。
 * - 字符串 → 原样返回（普通回合的原文错误）；
 * - 对象 → 显示可读中文（flow 回合的脱敏元数据）；
 * - 空 → 返回空串。
 */
export function fmtTraceError(err: TraceError | null | undefined): string {
  if (!err) return ''
  if (typeof err === 'string') return err
  return `已脱敏（错误类型：${err.error_kind || 'unknown'}）`
}

export interface TraceItem {
  turn_id: string
  ts: string
  session_id: string
  provider: string
  model: string
  route: string | null
  question_chars: number
  messages_count: number
  prompt_tokens: number
  completion_tokens: number
  cache_read_tokens?: number
  cache_creation_tokens?: number
  total_tokens: number
  task_budget?: {
    limit?: number | null
    prompt_tokens?: number
    completion_tokens?: number
    total_tokens?: number
    remaining?: number | null
    calls?: number
    exhausted?: boolean
  } | null
  cost_cny: number
  llm_calls: number
  llm_ms: number
  elapsed_ms: number
  outcome: string
  finish_reason?: string | null
  aborted: boolean
  error: TraceError
  final_chars?: number
  steps: TraceStep[]
}
export interface TraceSummary {
  turns: number
  prompt_tokens: number
  completion_tokens: number
  cache_read_tokens?: number
  cache_creation_tokens?: number
  /** 缓存命中省下的钱（后端算，前端只显示——单价和阶梯档都在后端）。 */
  cache_saved_cny?: number
  /** 命中过的回合数。与 `cache_read_tokens` 一起才看得出"跨回合到底有没有复用"。 */
  cache_hit_turns?: number
  /** 命中 token / 输入 token。长回合逐步重发天然会推高它，别拿它当"缓存生效"。 */
  cache_hit_rate?: number
  /** 命中回合 / 总回合。实测本机 88% 对 13% —— 这条才是跨回合复用的真信号。 */
  cache_hit_turn_share?: number
  total_tokens: number
  total_cost_cny: number
  avg_elapsed_ms: number
  aborted: number
  errors: number
  by_provider: Record<string, {
    turns: number
    prompt_tokens: number
    completion_tokens: number
    cache_read_tokens?: number
    cache_creation_tokens?: number
    tokens: number
    cost_cny: number
    /** 后端判定的计价状态：free / local / unknown / known。
     *  用来区分「都是 0 但含义不同」的三种情况，别在前端用 cost===0 反推。 */
    pricing_state?: string
    /** 该 provider 的缓存命中省下的钱（后端按每一行自己的 model 折算）。 */
    cache_saved_cny?: number
    cache_hit_turns?: number
    cache_hit_rate?: number
    cache_hit_turn_share?: number
    model?: string
    provider?: string
  }>
}
export interface SessionInfo {
  session_id: string
  turns: number
  has_summary: boolean
  updated_at: string
  title?: string
  preview?: string
}
export interface SkillInfo {
  name: string
  description: string
  when_to_use: string
  path: string
  source?: string
  version?: string
  checksum?: string
  history_versions?: Array<{ version?: string; checksum?: string }>
  stats?: { uses?: number; successes?: number; failures?: number; last_score?: number }
}

export const harnessApi = {
  budget(): Promise<{ ok: boolean; status: BudgetStatus; check: BudgetCheck }> {
    return request('/api/budget')
  },
  setBudgetLimit(limitCny: number): Promise<{ ok: boolean; check?: BudgetCheck; error?: string }> {
    return postJson('/api/budget', { limit_cny: limitCny })
  },
  setUsageLimits(limitCny: number, perMinuteCalls: number, perMinuteCost: number): Promise<{
    ok: boolean
    check?: BudgetCheck
    rate?: { per_minute_calls_limit: number; per_minute_cost_limit: number }
    error?: string
  }> {
    return postJson('/api/budget', {
      limit_cny: limitCny,
      per_minute_calls: perMinuteCalls,
      per_minute_cost: perMinuteCost,
    })
  },
  resetBudget(): Promise<{ ok: boolean; check?: BudgetCheck }> {
    return postJson('/api/budget', { reset: true })
  },
  sessions(): Promise<{ ok: boolean; items: SessionInfo[] }> {
    return request('/api/sessions')
  },
  /** 取回某会话的完整问答历史（供刷新/重开后回灌对话）。会话不存在时返回空 turns。 */
  sessionDetail(id: string): Promise<{
    ok: boolean
    session_id: string
    turns: {
      user: string
      assistant: string
      ts?: string
      turn_id?: string
      checkpoint_id?: string
      trace?: {
        type: 'thought' | 'action' | 'observation' | 'reflection'
        text: string
        at?: number
        elapsedMs?: number
        ok?: boolean
      }[]
    }[]
    summary?: string
    updated_at?: string
  }> {
    return request(`/api/sessions/${encodeURIComponent(id)}`)
  },
  deleteSession(id: string): Promise<{ ok: boolean }> {
    return fetch(`/api/sessions/${encodeURIComponent(id)}`, withProject({ method: 'DELETE' })).then((r) => r.json())
  },
  trace(limit = 30): Promise<{ ok: boolean; items: TraceItem[]; summary: TraceSummary }> {
    return request(`/api/trace?limit=${limit}`)
  },
  clearTrace(): Promise<{ ok: boolean }> {
    return postJson('/api/trace/clear', {})
  },
  skills(): Promise<{ ok: boolean; skills_dir: string; exists: boolean; count: number; errors: string[]; items: SkillInfo[] }> {
    return request('/api/skills')
  },
  reloadSkills(): Promise<{ ok: boolean; count?: number; errors?: string[] }> {
    return postJson('/api/skills/reload', {})
  },
  rollbackSkill(name: string): Promise<{ ok: boolean; rolled_back?: boolean; restored_previous?: boolean; approval_required?: boolean; error?: string }> {
    return postJson('/api/skills/rollback', { name })
  },
  hooks(): Promise<{ ok: boolean; hooks_dir: string; exists: boolean; counts: Record<string, number>; workflow_counts?: Record<string, number>; workflow_kinds?: string[]; breakpoints?: Record<string, { enabled?: boolean; block?: boolean; match?: string; reason?: string }>; sources: Record<string, unknown>; errors: string[] }> {
    return request('/api/hooks')
  },
  reloadHooks(): Promise<{ ok: boolean; errors?: string[] }> {
    return postJson('/api/hooks/reload', {})
  },
  setHookBreakpoint(payload: { kind: string; enabled?: boolean; block?: boolean; match?: string; reason?: string }): Promise<{ ok: boolean; breakpoint?: Record<string, unknown>; error?: string }> {
    return request('/api/hooks/breakpoints', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  },
  removeHookBreakpoint(kind: string): Promise<{ ok: boolean; breakpoint?: Record<string, unknown>; error?: string }> {
    return request(`/api/hooks/breakpoints/${encodeURIComponent(kind)}`, { method: 'DELETE' })
  },
}
