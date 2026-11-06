import { rawJson, request } from './_base'

export interface DelegationResult {
  status?: string
  conclusion?: string
  error?: string
  completed?: unknown[]
  resume_point?: string
  failure_reason?: string
  artifacts?: Array<Record<string, unknown>>
  file_changes?: Array<Record<string, unknown>>
  steps?: number
  [key: string]: unknown
}

export interface HeartbeatBeat {
  at: string
  epoch: number
  steps: number
  phase: string
  event: string
}

export interface NodeHeartbeat {
  state: 'alive' | 'stale' | 'dead' | 'none' | 'closed' | 'waiting'
  last_seen: string | null
  beats: number
  silence_seconds: number | null
  interval_seconds?: number
  progress: { steps?: number; phase?: string; changed_at?: string; changed_at_epoch?: number } | null
  progress_age_seconds: number | null
  progress_frozen: boolean
  recent_beats: HeartbeatBeat[]
  last_event: string
  reason: string
}

export interface HeartbeatConfig {
  interval_seconds: number
  stale_factor: number
  dead_factor: number
  progress_freeze_factor: number
  stale_after_seconds: number
  dead_after_seconds: number
  calibrated: boolean
  calibration_note: string
  source: string
}

export interface DelegationAlert {
  agent_id: string
  role?: string
  last_seen: string
  silence_seconds: number
  progress_frozen: boolean
  progress?: Record<string, unknown> | null
  detected_at: string
  received_by: string
  receiver: string
  delivery: string
}

export interface DelegationNode {
  id: string
  session_id: string
  parent_id: string
  role: string
  task: string
  depth: number
  status: string
  created_at?: string
  created_at_epoch?: number
  plan_id?: string
  task_id?: string
  ttl_seconds?: number
  requires_limit_confirmation?: boolean
  limit_reasons?: string[]
  impact?: { tools?: string[]; independent_context?: boolean; estimated?: string }
  result?: DelegationResult | null
  error?: string
  last_seen?: string
  last_seen_epoch?: number
  heartbeat_interval?: number
  beats?: number
  progress?: Record<string, unknown>
  recent_beats?: HeartbeatBeat[]
  last_event?: string
  heartbeat?: NodeHeartbeat
  children?: DelegationNode[]
}

export interface TaskAssistantReport {
  ok: boolean
  error?: string
  plan_id?: string
  tasks: Array<{ id: string; role: string; task: string; goal?: string; input?: string[]; outputs?: string[]; boundaries?: string[]; acceptance?: string[] }>
  results: Record<string, DelegationResult>
  merged: string
  assistant?: { orchestrator_only?: boolean; business_tools?: string[]; max_replans?: number }
}

export interface OrchestrationReport {
  plan_id?: string
  ok?: boolean
  tasks?: TaskAssistantReport['tasks']
  results?: Record<string, DelegationResult>
  merged?: string
}

export interface OrchestrationPlan {
  ok: boolean
  plan_id: string
  status?: string
  final?: boolean
  report?: OrchestrationReport
}

export interface DelegationTree {
  ok: boolean
  session_id: string
  root: DelegationNode
  nodes: DelegationNode[]
  pending: DelegationNode[]
  alerts?: DelegationAlert[]
  heartbeat_config?: HeartbeatConfig
  limits: { max_depth: number; max_branches: number; max_children: number }
  config: Record<string, number>
}

export const delegationApi = {
  tree(sessionId: string): Promise<DelegationTree> {
    return request(`/api/agent/delegations?session_id=${encodeURIComponent(sessionId)}`)
  },
  decide(sessionId: string, nodeId: string, approved: boolean): Promise<{ ok: boolean; error?: string; item?: DelegationNode }> {
    return rawJson(`/api/agent/delegations/${encodeURIComponent(nodeId)}/decision`, {
      session_id: sessionId, approved,
    })
  },
  taskAssist(sessionId: string, goal: string): Promise<TaskAssistantReport> {
    return rawJson('/api/agent/task-assistant', { session_id: sessionId, goal, max_replans: 1 })
  },
}

export const orchestrationApi = {
  /** 读取持久化编排计划；计划文件为 .docmind/orchestrations/<plan_id>.json。 */
  get(planId: string): Promise<OrchestrationPlan> {
    return request(`/api/orchestrate/${encodeURIComponent(planId)}`)
  },
}
