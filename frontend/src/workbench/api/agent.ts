// Agent 策略 / 审批 / 工作流全量类型与 API（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { getProjectId, rawJson, request, withProject } from './_base'
import { openSse } from './sse'

// ---------------------------------------------------------------- Agent 策略 / 审批（P4 收口）
// 对应后端 /api/agent/*。这些是**项目作用域**请求（审批/授权针对当前项目的外部文件），
// 必须带上 X-DocMind-Project（经 withProject）。统一走 rawJson 通道：不把 ok:false 抛错，
// 与原组件「读失败体字段（reason/recorded/approval）」的语义一致。
export interface AgentRoutingResp { ok?: boolean; auto_cloud_enabled?: boolean }
export interface AgentConnectorInfo { key: string; label: string; enabled: boolean }
export interface AgentConnectorsResp { ok?: boolean; connectors?: AgentConnectorInfo[] }
export interface AgentApproval { id: string; status: string; summary?: string; diff?: string; paths?: string[] }
export interface AgentApprovalsResp { ok?: boolean; approvals?: AgentApproval[] }
export interface AgentPermissionResp { ok?: boolean; recorded?: boolean; reason?: string }
export interface AgentApprovalCreateResp { ok?: boolean; approval?: AgentApproval; error?: string }
export interface AgentGateRequest { id: string; action: string; target: string; risk?: string; created_at?: string }
export interface AgentGateRequestsResp { ok?: boolean; items?: AgentGateRequest[]; error?: string }
export interface ProjectProfileMcp {
  key: string; name?: string; enabled?: boolean; transport?: string
  capabilities?: string[]; summary?: string
}
export interface ProjectProfile {
  version?: number; project_id?: string; project_root?: string; kind?: string
  tools?: string[]; mcp?: ProjectProfileMcp[]; run_commands?: string[]
  acceptance_methods?: string[]; acceptance_scripts?: string[]
  preview_adapters?: string[]; sources?: string[]; updated_at?: string
}
export interface ProjectProfileResp { ok?: boolean; profile?: ProjectProfile; error?: string }
export interface UserProfile {
  user_id?: string; display_name: string; language: string; timezone: string;
  location: string; preferences: Record<string, unknown>; goals: string[]; notes: string; updated?: string
}
export interface UserProfileResp { ok?: boolean; profile?: UserProfile; error?: string }
export interface MemoryItem { id: string; kind: string; title: string; content: string; evidence?: string; source?: string; repeats?: number; updated?: string }
export interface MemoryListResp { ok?: boolean; items?: MemoryItem[]; error?: string }
export interface PreviewAdapterInfo {
  id: string; label?: string; kind?: string; evidence?: string
  capture_adapter?: string; available?: boolean; requires_connector?: boolean
  connector_key?: string; transport?: string; project_scoped?: boolean
  generated?: boolean; status?: 'pending' | 'active' | 'rejected'
  refresh_tool?: string; connector_hint?: string; validation?: string[]
  runtime?: 'python' | 'node' | string; entrypoint?: string; module_path?: string; source_sha256?: string
  code_review?: 'pending' | 'active' | 'missing' | 'unavailable' | string
  source_preview?: string; source_truncated?: boolean; active_sha256?: string
  code_diff?: string; code_diff_truncated?: boolean
}
export interface PreviewAdapterCatalogResp { ok?: boolean; adapters?: PreviewAdapterInfo[]; count?: number; error?: string }
export interface PreviewAdapterConfigResp { ok?: boolean; profile?: ProjectProfile; error?: string }
export interface WorkflowAcceptanceReport {
  workflow_id?: string; status?: string; kind?: string; request?: string
  acceptance?: AcceptanceContract; evaluation?: WorkflowEvaluation
  review?: WorkflowState['review']
  preview?: WorkflowPreview; self_review?: WorkflowSelfReview; recovery?: WorkflowState['recovery']
  generated_at?: string
}

export interface WorkflowOption {
  id: string; title: string; summary: string; recommended?: boolean
  source?: string; requires_web?: boolean
}
export interface WorkflowEvent {
  ts?: string; kind?: string
  /** 持久事件的单调序号；SSE 重连按 after=seq 去重重放 */
  seq?: number
  // subagent_step / subagent_start / subagent_complete 实时轨迹字段
  task_id?: string; role?: string; step_type?: string; text?: string
  task?: string; status?: string; phase?: string; elapsed_ms?: number
    /**
     * 这条结论**不是模型自己的答案** —— harness 写的安全网兜底（连续空响应、
     * 清洗后的残句）。`status` 仍可能是 `ok`，所以不能靠它判断；此前后端压根没把
     * 这个字段发出来，时间线上「子代理完成」看起来和正常完成一模一样。
     */
    degraded?: boolean
  conclusion?: string; error?: string; trace?: WorkflowChildTrace
  /** 部分事件（如 langsmith 导出状态）直接携带 LangSmith 状态体 */
  langsmith?: LangSmithStatus
  [key: string]: unknown
}
export interface WorkflowTraceStep {
  action?: string; obs?: string; ok?: boolean; tool?: string; error?: string
  [key: string]: unknown
}
/** 子代理单条实时轨迹（step_sink → subagent_step SSE 事件），文本后端已按类型裁剪。 */
export interface WorkflowStepItem {
  type: 'thought' | 'action' | 'observation' | string
  text: string
}
/** 子代理完成时回传的有界轨迹（subagent_complete.trace / state.results[].trace）。 */
export interface WorkflowChildTrace {
  steps?: WorkflowTraceStep[]
  n_steps?: number
  thoughts?: string[]
  reflections?: string[]
  tokens?: { in?: number; out?: number; total?: number; [k: string]: unknown }
  elapsed_ms?: number
  cost?: number
  [key: string]: unknown
}
export interface WorkflowBackendStatus {
  ok?: boolean
  backend?: string
  langgraph_installed?: boolean
  checkpoint_backend?: string
  checkpoint_error?: string
  persistent_checkpoint?: boolean
  distributed_leases?: boolean
  checkpoint_health?: {
    backend?: string
    configured?: boolean
    required?: boolean
    healthy?: boolean
    state?: string
    schema_version?: number | null
    error?: string
  }
}
export interface LangSmithStatus {
  ok?: boolean
  installed?: boolean
  key_configured?: boolean
  enabled?: boolean
  realtime?: boolean
  project?: string
  endpoint?: string
  pending_exports?: number
  recent_export_errors?: Array<{ name?: string; error?: string }>
}
export interface WorkflowEvaluation {
  workflow_id?: string
  passed?: boolean
  score?: number
  checks?: Array<{ name?: string; ok?: boolean; detail?: string }>
  task_count?: number
  steps?: number
  replans?: number
  metrics?: Record<string, number | boolean>
}
export interface WorkflowSelfReview {
  status?: 'ready' | 'in_progress' | string
  generated_at?: string; source?: string; confidence?: number; summary?: string
  changed?: string[]; verified?: string[]; uncertainties?: string[]; next_steps?: string[]
}
export interface AcceptanceItem {
  id: string; statement: string; method: string; evidence: string[]
  required: boolean; user_approved: boolean
}
export interface AcceptanceContract {
  revision: number; approved_revision: number; items: AcceptanceItem[]
  final_decision: 'pending' | 'accepted' | 'rejected'; final_note: string
}
export interface WorkflowPreviewArtifact {
  id: string; kind: string; label: string; renderer?: string; adapter?: string; path?: string; uri?: string; mime?: string
  status?: string; summary?: string; before?: string; after?: string
  evidence?: string[]; metadata?: Record<string, string>
  change_summary?: { before_lines?: number; after_lines?: number; added_lines?: number; removed_lines?: number }
}
export interface WorkflowPreview {
  schema?: string; workflow_id?: string; generated_at?: string; status?: string
  summary?: string; artifacts?: WorkflowPreviewArtifact[]
  checks?: Array<{ name?: string; ok?: boolean; detail?: string }>
  counts?: { artifacts?: number; by_kind?: Record<string, number> }
  changes?: { total?: number; files?: Array<{ path?: string; kind?: string; artifact_id?: string; summary?: Record<string, number> }>; counts?: Record<string, number> }
}
export interface VisualFeedbackRecord {
  id: string; artifact_id?: string; label: string; note: string
  region?: { x: number; y: number; width: number; height: number } | null
  screenshot: boolean; sent_at: string; status?: string; detail?: string
  snapshots?: Partial<Record<'before' | 'after', { width: number; height: number; bytes: number; captured_at: string }>>
}
export interface WorkflowProjectReview {
  ok: boolean; status: string; independent: boolean; message: string
  change_count?: number; changes?: Array<{ path: string; status: string }>
  diff?: string; diff_truncated?: boolean
  checkpoint_skipped?: Array<{ path?: string; reason?: string }>
  tests?: Array<{ command: string; ok: boolean; exit_code?: number; output?: string; error?: string }>
}
export interface WorkflowState {
  workflow_id: string; status: string; phase: string; request?: string
  kind?: 'generic' | 'game' | 'eda' | string
  options?: WorkflowOption[]; selected_option?: WorkflowOption | null
  tasks?: Array<Record<string, unknown>>; pending_tasks?: Array<Record<string, unknown>>
  subagents?: Array<{
    id?: string; role?: string; status?: string; persona?: string
    tools?: string[]; mcp?: string; reflection?: boolean
    task_thread?: string
    /** 子代理当前任务描述与终态结论（对话流团队面板展示用） */
    task?: string; conclusion?: string; trace?: WorkflowChildTrace
    reflection_result?: { ok?: boolean; source?: string; issues?: string[]; next_step?: string }
    steps?: number; elapsed_ms?: number; error?: string
    retry_count?: number
    /**
     * 失败任务的补救留痕：`status` 仍是 `failed`（界面照旧标红），
     * 这两个字段说明后来由哪个任务补上了，别把失败历史抹掉。
     */
    remediated_by?: string; remediation_note?: string
  }>
  results?: Record<string, Record<string, unknown>>; events?: WorkflowEvent[]; timeline?: WorkflowEvent[]
  dispatches?: Array<{ planner?: string; added?: string[]; kind?: string }>
  review?: { ok?: boolean; project?: WorkflowProjectReview; [key: string]: unknown }; interrupt_reason?: string
  recovery?: {
    status?: string; generated_at?: string; summary?: string
    evidence?: { failed_task_ids?: string[]; uncertain_task_ids?: string[] }
    options?: Array<{ id?: string; action?: string; title?: string; detail?: string; task_ids?: string[]; requires_user?: boolean }>
  }
  acceptance_contract?: AcceptanceContract
  project_profile?: ProjectProfile
  self_review?: WorkflowSelfReview
  project_checkpoint?: {
    id?: string; created_at?: string; file_count?: number; bytes?: number
    skipped?: Array<{ path?: string; reason?: string }>
  }
  project_stage?: { status?: string; workspace_root?: string; file_count?: number; process_backend?: string }
  capability_lease?: {
    id?: string; capabilities?: string[]; status?: string
    created_at?: string; expires_at?: string; expires_at_epoch?: number
    released_at?: string; source?: string
  }
  visual_feedback?: VisualFeedbackRecord[]
  preview?: WorkflowPreview
  steps?: number; replans?: number; subagent_retries?: Record<string, number>; context_layers?: Record<string, unknown>
  langsmith_trace?: Record<string, unknown>
  observability?: {
    duration_ms?: number; elapsed_ms?: number; events?: number
    prompt_tokens?: number; completion_tokens?: number; total_tokens?: number
    tool_events?: number; mcp_events?: number; failures?: number
    subagents_started?: number; subagents_completed?: number
  }
}
export interface WorkflowResp { ok?: boolean; workflow?: WorkflowState; error?: string }
/** GET /api/agent/workflows 列表项：仅摘要，不含 tasks/options/events 大对象。 */
export interface WorkflowSummary {
  workflow_id: string
  project_id?: string; project_root?: string
  status: string; phase: string; kind?: string; request?: string
  task_count?: number; task_done?: number
  error?: string; interrupt_reason?: string
  created_at?: string; updated_at?: string
}
export interface WorkflowListResp { ok?: boolean; items?: WorkflowSummary[]; error?: string }
export interface WorkflowDeleteResp { ok?: boolean; workflow_id?: string; error?: string }
export interface RetrievalEvalCase {
  id?: string; query: string; relevant_ids?: string[]
  relevant_sources?: string[]; relevant_terms?: string[]
}
export interface RetrievalEvalReport {
  metrics?: Record<string, number>
  ks?: number[]
  items?: Array<Record<string, unknown>>
}
export interface RetrievalEvalResp {
  ok?: boolean; collection?: string; modes?: Record<string, RetrievalEvalReport>
  metric?: string
  comparison?: { metric?: string; best?: string; ranking?: Array<{ mode?: string; value?: number }> }
  error?: string
}
export interface RetrievalRuntimeStatus {
  ok?: boolean
  retrieval?: {
    backend?: string; collection?: string; top_k?: number
    bm25?: boolean; reranker?: string; reranker_model?: string
    lexical_persistence?: boolean; lexical_index?: Record<string, unknown>
    recent_events?: RetrievalTraceEvent[]
  }
}
export interface RetrievalTraceDocument {
  id?: string; source?: string; snippet?: string
  start_line?: number | string; end_line?: number | string
  dense_rank?: number | null; bm25_score?: number; hybrid_score?: number
  rerank_score?: number; distance?: number
}
export interface RetrievalTraceEvent {
  ts?: number; query?: string; collection?: string; mode?: string
  duration_ms?: number; candidates?: number; returned?: number
  reranker?: string; lexical_index?: string
  documents?: RetrievalTraceDocument[]
  [key: string]: unknown
}

export const agentApi = {
  /** Agent 路由策略（本地/云端、自动转云开关）。 */
  routing(): Promise<AgentRoutingResp> {
    return rawJson<AgentRoutingResp>('/api/agent/routing')
  },
  /** 已接入的外部工具（连接器）列表。 */
  connectors(): Promise<AgentConnectorsResp> {
    return rawJson<AgentConnectorsResp>('/api/agent/connectors')
  },
  /** 外部文件改动审批列表。 */
  approvals(): Promise<AgentApprovalsResp> {
    return rawJson<AgentApprovalsResp>('/api/agent/approvals')
  },
  /** 为「项目外文件」发起改动审批请求。 */
  requestApproval(paths: string[], summary: string): Promise<AgentApprovalCreateResp> {
    return rawJson<AgentApprovalCreateResp>('/api/agent/approvals', { paths, summary })
  },
  /** 记录/查询「允许修改项目外文件」的授权。 */
  permission(payload: { path: string; allow_external: boolean; approved: boolean }): Promise<AgentPermissionResp> {
    return rawJson<AgentPermissionResp>('/api/agent/permission', payload)
  },
  /** 审批决定：approved / rejected。 */
  decide(id: string, status: string): Promise<{ ok?: boolean }> {
    return rawJson<{ ok?: boolean }>('/api/agent/approvals/decide', { id, status })
  },
  /** 当前项目等待用户确认的工具动作（例如桌面点击/输入/保存）。 */
  approvalRequests(): Promise<AgentGateRequestsResp> {
    return rawJson<AgentGateRequestsResp>('/api/agent/approval-requests')
  },
  /** 通过或拒绝一个工具动作；通过后 Agent 必须用同样参数重试。 */
  decideApprovalRequest(id: string, approved: boolean): Promise<{ ok?: boolean; error?: string }> {
    return rawJson<{ ok?: boolean; error?: string }>('/api/agent/approval-requests/decide', { id, approved })
  },
  workflowBackend(): Promise<WorkflowBackendStatus> {
    return rawJson('/api/agent/workflow/backend')
  },
  retrievalEvaluate(payload: {
    cases: RetrievalEvalCase[]; collection?: string; top_k?: number
    modes?: string[]; metric?: string; ks?: number[]
  }): Promise<RetrievalEvalResp> {
    return rawJson('/api/agent/retrieval/evaluate', payload)
  },
  retrievalStatus(collection?: string, top_k = 5): Promise<RetrievalRuntimeStatus> {
    const query = new URLSearchParams({ top_k: String(top_k) })
    if (collection) query.set('collection', collection)
    return rawJson(`/api/agent/retrieval/status?${query.toString()}`)
  },
  langsmith(): Promise<LangSmithStatus> {
    return rawJson('/api/agent/langsmith')
  },
  workflowEvaluation(id: string): Promise<{ ok?: boolean; evaluation?: WorkflowEvaluation; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/evaluation`)
  },
  projectProfile(): Promise<ProjectProfileResp> {
    return rawJson('/api/agent/project-profile')
  },
  userProfile(): Promise<UserProfileResp> {
    return rawJson('/api/agent/profile')
  },
  updateUserProfile(patch: Partial<UserProfile>): Promise<UserProfileResp> {
    return request('/api/agent/profile', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
    })
  },
  memories(query = '', limit = 20): Promise<MemoryListResp> {
    const params = new URLSearchParams({ query, limit: String(limit) })
    return rawJson(`/api/agent/memory?${params.toString()}`)
  },
  deleteMemory(id: string): Promise<{ ok?: boolean; error?: string }> {
    return request(`/api/agent/memory/${encodeURIComponent(id)}`, { method: 'DELETE' })
  },
  workflowStart(
    prompt: string,
    options: { use_llm?: boolean; web_enabled?: boolean; kind?: 'generic' | 'game' | 'eda' } = {},
  ): Promise<WorkflowResp> {
    return rawJson('/api/agent/workflow/start', { prompt, ...options })
  },
  workflow(id: string): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}`)
  },
  /** 当前项目进程内未终结的工作流；无则 workflow=null（孤儿卡片发现用）。 */
  workflowActive(): Promise<{ ok?: boolean; workflow?: WorkflowState | null; error?: string }> {
    return rawJson('/api/agent/workflow/active')
  },
  /** 当前项目最近工作流摘要（工作台「工作流历史」）。 */
  workflowList(limit = 50): Promise<WorkflowListResp> {
    return rawJson(`/api/agent/workflows?limit=${encodeURIComponent(String(limit))}`)
  },
  /** 删除终态工作流并清理磁盘文件；运行中需先 workflowInterrupt。 */
  workflowDelete(id: string): Promise<WorkflowDeleteResp> {
    return request(`/api/agent/workflow/${encodeURIComponent(id)}`, { method: 'DELETE' })
  },
  workflowChoice(id: string, choice: string, custom_request = ''): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/choice`, { choice, custom_request })
  },
  workflowResearch(id: string, findings: string, source = 'web'): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/research`, { findings, source })
  },
  workflowResearchRun(id: string, query = ''): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/research/run`, { query })
  },
  workflowPlan(id: string, tasks?: Array<Record<string, unknown>>): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/plan`, { tasks })
  },
  workflowApprove(id: string, approved: boolean, auto_execute = false): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/approve`, { approved, auto_execute })
  },
  workflowPreview(id: string): Promise<{ ok?: boolean; preview?: WorkflowPreview; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/preview`)
  },
  previewAdapters(): Promise<PreviewAdapterCatalogResp> {
    return rawJson('/api/agent/preview-adapters')
  },
  configurePreviewAdapters(adapters: string[]): Promise<PreviewAdapterConfigResp> {
    return rawJson('/api/agent/preview-adapters/config', { adapters })
  },
  decidePreviewAdapter(id: string, approved: boolean, source_sha256 = ''): Promise<{ ok?: boolean; manifest?: PreviewAdapterInfo; error?: string }> {
    return rawJson(`/api/agent/preview-adapters/${encodeURIComponent(id)}/decision`, { approved, source_sha256 })
  },
  workflowAcceptanceReport(id: string): Promise<{ ok?: boolean; report?: WorkflowAcceptanceReport; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/acceptance-report`)
  },
  workflowAcceptance(id: string, items: AcceptanceItem[]): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/acceptance`, { items })
  },
  workflowAcceptanceDecide(id: string, approved: boolean, note = ''): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/acceptance/decide`, { approved, note })
  },
  workflowVisualFeedback(id: string, feedback: Record<string, unknown>, projectId = getProjectId()): Promise<{ ok?: boolean; feedback?: VisualFeedbackRecord; items?: VisualFeedbackRecord[]; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/visual-feedback`, feedback, undefined, projectId)
  },
  workflowFeedbackStatus(id: string, feedbackId: string, status: string, detail = '', projectId = getProjectId()): Promise<{ ok?: boolean; feedback?: VisualFeedbackRecord; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/visual-feedback/${encodeURIComponent(feedbackId)}/status`, { status, detail }, undefined, projectId)
  },
  workflowFeedbackSnapshot(id: string, feedbackId: string, phase: 'before' | 'after', image_base64: string, projectId = getProjectId()): Promise<{ ok?: boolean; feedback?: VisualFeedbackRecord; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/visual-feedback/${encodeURIComponent(feedbackId)}/snapshot/${phase}`, { image_base64 }, undefined, projectId)
  },
  workflowFeedbackSnapshotUrl(id: string, feedbackId: string, phase: 'before' | 'after', projectId = getProjectId()): string {
    return `/api/agent/workflow/${encodeURIComponent(id)}/visual-feedback/${encodeURIComponent(feedbackId)}/snapshot/${phase}?project_id=${encodeURIComponent(projectId)}`
  },
  workflowCheckpoint(id: string, approved?: boolean): Promise<{ ok?: boolean; checkpoint?: Record<string, unknown>; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/checkpoint`, { approved })
  },
  workflowProjectCheckpoint(id: string): Promise<{ ok?: boolean; checkpoint?: Record<string, unknown>; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/project-checkpoint`, {})
  },
  workflowProjectRollback(id: string, approved = false, paths: string[] = []): Promise<{ ok?: boolean; rollback?: Record<string, unknown>; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/project-rollback`, { approved, paths })
  },
  workflowStageApply(id: string, approved = false): Promise<{ ok?: boolean; result?: Record<string, unknown>; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/stage/apply`, { approved })
  },
  workflowStageReview(id: string): Promise<{ ok?: boolean; review?: WorkflowProjectReview; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/stage/review`, {})
  },
  workflowStageCleanup(id: string): Promise<{ ok?: boolean; result?: Record<string, unknown>; error?: string }> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/stage/cleanup`, {})
  },
  workflowInterrupt(id: string, reason = '用户请求中断'): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/interrupt`, { reason })
  },
  workflowResume(id: string): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/resume`, {})
  },
  workflowRevise(id: string, tasks: Array<Record<string, unknown>>): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/revise`, { tasks })
  },
  workflowReviseApprove(id: string, approved: boolean): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/revise/approve`, { approved })
  },
  workflowExecute(id: string, session_id = 'workflow'): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/execute`, { session_id })
  },
  workflowSubagentRetry(id: string, taskId: string, session_id = 'workflow'): Promise<WorkflowResp> {
    return rawJson(`/api/agent/workflow/${encodeURIComponent(id)}/subagents/${encodeURIComponent(taskId)}/retry`, { session_id })
  },
}

export interface WorkflowEventHandlers {
  onEvent: (ev: WorkflowEvent) => void
  /** 后端推完终态事件并发 __stream_done__ 自关时回调（卡片此时拉一次完整状态 hydrate）。 */
  onDone?: () => void
  /** 网络/服务错误；AbortError（主动退订）不上报。 */
  onError?: (e: unknown) => void
}

/**
 * 订阅工作流实时事件 SSE（GET /api/agent/workflow/{id}/events?after=seq）。
 * 连接先重放 seq>after 的持久事件，再推送实时事件；终态收到 __stream_done__ 后
 * 自动断流并回调 onDone。返回退订函数（AbortController 断开），可安全重复调用。
 */
export function workflowEvents(
  workflowId: string,
  after = 0,
  handlers: WorkflowEventHandlers,
): () => void {
  const ctrl = new AbortController()
  let alive = true
  const url = `/api/agent/workflow/${encodeURIComponent(workflowId)}/events?after=${Math.max(0, Math.floor(after) || 0)}`
  void openSse(url, ctrl.signal, (raw) => {
    const ev = raw as unknown as WorkflowEvent
    if (ev.kind === '__stream_done__') {
      alive = false
      ctrl.abort()
      handlers.onDone?.()
      return
    }
    handlers.onEvent(ev)
  }).catch((e: unknown) => {
    if ((e as Error)?.name === 'AbortError') return
    handlers.onError?.(e)
  })
  return () => {
    if (!alive) return
    alive = false
    ctrl.abort()
  }
}
