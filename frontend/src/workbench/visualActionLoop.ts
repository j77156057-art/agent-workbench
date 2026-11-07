export type VisualActionPhase = 'locating' | 'awaiting_confirmation' | 'needs_review' | 'awaiting_user' | 'completed' | 'stopped'
export type VisualActionStatus = 'met' | 'unmet' | 'uncertain' | 'unavailable'

export interface VisualActionStep {
  at: string
  target: string
  status: VisualActionStatus
  evidence: string
  nextTarget: string
}

export interface VisualActionLoop {
  id: string
  projectId: string
  workflowId: string
  goal: string
  phase: VisualActionPhase
  steps: VisualActionStep[]
  lastTarget: string
  note: string
}

export function beginVisualActionLoop(projectId: string, workflowId: string, goal: string, target: string): VisualActionLoop {
  return { id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`, projectId, workflowId, goal: goal.trim(), phase: 'locating',
    steps: [], lastTarget: target.trim(), note: '正在定位第一步。' }
}

/** A repeated suggestion requires review instead of another automatic proposal. No click happens here. */
export function advanceVisualActionLoop(loop: VisualActionLoop, step: VisualActionStep): VisualActionLoop {
  const steps = [...loop.steps, step]
  if (step.status === 'met') return { ...loop, steps, lastTarget: step.target,
    phase: 'awaiting_user', note: 'AI 认为画面达成目标，请你检查后验收。' }
  if (step.status !== 'unmet') return { ...loop, steps, lastTarget: step.target,
    phase: 'needs_review', note: '视觉证据不足，已停止自动建议；请检查画面或让 AI 排查。' }
  const nextTarget = step.nextTarget.trim()
  if (!nextTarget) return { ...loop, steps, lastTarget: step.target,
    phase: 'needs_review', note: '目标尚未达成，AI 没有可靠的下一控件建议。' }
  const normalized = nextTarget.replace(/\s+/g, '').toLowerCase()
  const repeated = steps.some(item => item.target.replace(/\s+/g, '').toLowerCase() === normalized)
  if (repeated) return { ...loop, steps, lastTarget: step.target,
    phase: 'needs_review', note: 'AI 连续建议已尝试的控件，已暂停以避免重复点击。' }
  return { ...loop, steps, lastTarget: nextTarget, phase: 'locating', note: `正在定位下一步：${nextTarget}` }
}

export function restoreVisualActionLoop(raw: string | null, projectId: string, workflowId: string): VisualActionLoop | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as VisualActionLoop
    if (value.projectId !== projectId || value.workflowId !== workflowId || !value.id || !value.goal || !Array.isArray(value.steps)) return null
    if (!['locating', 'awaiting_confirmation', 'needs_review', 'awaiting_user', 'completed', 'stopped'].includes(value.phase)) return null
    // Proposals expire and depend on a captured window. A reload always requires fresh localization.
    return value.phase === 'locating' || value.phase === 'awaiting_confirmation'
      ? { ...value, phase: 'needs_review', note: '页面已重载，请重新定位下一控件并确认。' } : value
  } catch { return null }
}

// ---------------------------------------------------------------- 单次视觉点击审批
// HTTP 202 只是登记审批，绝不是点击成功。此控制器与 Vue 无关，便于用契约测试
// 直接验证「同提案重试、重复批准不重复请求、409 不冒充成功」。
export interface VisualClickProposal {
  proposal_id: string
  image: string
  bbox: number[]
  point: { x: number; y: number }
  label: string
  evidence: string
  expires_in: number
}

export interface VisualClickExecution {
  ok: boolean
  httpStatus?: number
  state?: string
  pending?: boolean
  executed?: boolean
  ticket_id?: string
  action?: string
  target?: string
  idempotent?: boolean
  renewed?: boolean
  renewal?: number
  renewal_message?: string
  error?: string
  before?: string | null
  after?: string | null
  label?: string
  verification?: {
    status: 'met' | 'unmet' | 'uncertain' | 'unavailable'
    evidence: string
    confidence?: number
    next_target?: string
  }
}

export type VisualClickPhase =
  'idle' | 'ready' | 'executing' | 'pending' | 'approving' | 'rejecting' | 'completed' | 'error'

export interface VisualClickViewState {
  phase: VisualClickPhase
  proposal: VisualClickProposal | null
  result: VisualClickExecution | null
  ticketId: string
  action: string
  target: string
  renewed: boolean
  renewal: number
  error: string
  notice: string
}

export interface VisualClickFlowDeps {
  execute(proposalId: string): Promise<VisualClickExecution>
  listApprovals(): Promise<{ items?: Array<{ id: string; action: string; target: string }> }>
  decide(id: string, approved: boolean): Promise<{ ok?: boolean; error?: string }>
}

export function initialVisualClickState(notice = ''): VisualClickViewState {
  return { phase: 'idle', proposal: null, result: null, ticketId: '', action: '', target: '',
    renewed: false, renewal: 0, error: '', notice }
}

export function createVisualClickFlow(
  deps: VisualClickFlowDeps,
  onChange: (state: VisualClickViewState) => void = () => {},
) {
  let state = initialVisualClickState()
  let generation = 0

  function snapshot() { return { ...state } }
  function update(next: VisualClickViewState) {
    state = next
    onChange(snapshot())
  }
  function reset(notice = '') {
    generation++
    update(initialVisualClickState(notice))
  }
  function propose(proposal: VisualClickProposal) {
    generation++
    update({ ...initialVisualClickState(), phase: 'ready', proposal,
      notice: '已定位目标；提交后仍需通过桌面动作审批。' })
  }
  function pendingState(result: VisualClickExecution): VisualClickViewState {
    return { ...state, phase: 'pending', result: null, error: '',
      ticketId: result.ticket_id || state.ticketId,
      action: result.action || state.action,
      target: result.target || state.target,
      renewed: !!result.renewed, renewal: result.renewal || 0,
      notice: result.renewed
        ? '提案已过有效期，已重新签票；请再次批准，无需重新定位。'
        : '待审批，尚未点击。批准后会使用同一个提案重试。' }
  }
  function applyExecution(result: VisualClickExecution) {
    if (result.httpStatus === 202 || result.state === 'pending' || result.pending) {
      update(pendingState(result))
      return
    }
    if (result.httpStatus === 404) {
      update({ ...initialVisualClickState(), phase: 'error',
        error: `提案已失效，必须重新定位。${result.error ? ` ${result.error}` : ''}` })
      return
    }
    if (result.httpStatus === 409) {
      update({ ...initialVisualClickState(), phase: 'error',
        error: `提案已消费或失效（HTTP 409），不能重复点击；请重新定位。${result.error ? ` ${result.error}` : ''}` })
      return
    }
    if (!result.ok || !result.executed || result.state !== 'completed') {
      update({ ...initialVisualClickState(), phase: 'error', error: result.error || '点击未执行，请重新定位后再试。' })
      return
    }
    update({ ...initialVisualClickState(), phase: 'completed', result,
      notice: result.idempotent ? '' : '点击已执行；请核对前后画面和视觉复验结果。' })
  }
  async function run(ticket: number) {
    const proposalId = state.proposal?.proposal_id
    if (!proposalId) return
    try {
      const result = await deps.execute(proposalId)
      if (ticket === generation) applyExecution(result)
    } catch (cause) {
      if (ticket !== generation) return
      const message = (cause as Error).message || '点击请求失败'
      if (state.ticketId) update({ ...state, phase: 'pending', result: null, error: message })
      else update({ ...initialVisualClickState(), phase: 'error', error: message })
    }
  }
  async function execute() {
    if (state.phase !== 'ready' || !state.proposal) return snapshot()
    const ticket = generation
    update({ ...state, phase: 'executing', error: '', notice: '正在提交桌面动作审批…' })
    await run(ticket)
    return snapshot()
  }
  async function retry() {
    if (state.phase !== 'pending' || !state.proposal) return snapshot()
    const ticket = generation
    update({ ...state, phase: 'executing', error: '', notice: '正在用同一个提案重试…' })
    await run(ticket)
    return snapshot()
  }
  async function findApproval() {
    const response = await deps.listApprovals()
    const row = (response.items || []).find(item => item.action === state.action && item.target === state.target)
    if (!row) throw new Error('审批队列里没有找到这次点击；它可能已被处理，请直接重试或重新定位。')
    return row
  }
  async function approve() {
    if (state.phase !== 'pending' || !state.proposal) return snapshot()
    const ticket = generation
    update({ ...state, phase: 'approving', error: '', notice: '正在记录批准；随后只重试一次。' })
    try {
      const row = await findApproval()
      if (ticket !== generation) return snapshot()
      const decision = await deps.decide(row.id, true)
      if (!decision.ok) throw new Error(decision.error || '批准未保存')
      if (ticket !== generation) return snapshot()
    } catch (cause) {
      if (ticket === generation) update({ ...state, phase: 'pending', error: (cause as Error).message || '批准失败' })
      return snapshot()
    }
    update({ ...state, phase: 'executing', error: '', notice: '批准已记录，正在用同一个提案重试…' })
    await run(ticket)
    return snapshot()
  }
  async function reject() {
    if (state.phase !== 'pending' || !state.proposal) return snapshot()
    const ticket = generation
    update({ ...state, phase: 'rejecting', error: '', notice: '正在记录拒绝…' })
    try {
      const row = await findApproval()
      if (ticket !== generation) return snapshot()
      const decision = await deps.decide(row.id, false)
      if (!decision.ok) throw new Error(decision.error || '拒绝未保存')
      if (ticket === generation) reset('已拒绝这次点击，未执行物理动作；可以重新定位。')
    } catch (cause) {
      if (ticket === generation) update({ ...state, phase: 'pending', error: (cause as Error).message || '拒绝失败' })
    }
    return snapshot()
  }

  return { snapshot, propose, execute, retry, approve, reject, reset }
}
