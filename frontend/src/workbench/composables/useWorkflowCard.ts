// WorkflowCard 的全部有状态逻辑：工作流状态 hydrate、SSE 订阅与退避重连、
// subagent_step 按 rAF 合批、人工门提交（乐观更新）、成员轨迹拼装、复核/验收/恢复操作。
// 组件只保留 props/emits 声明与模板渲染。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  agentApi, workflowEvents,
} from '../api'
import type { AcceptanceItem, WorkflowChildTrace, WorkflowEvaluation, WorkflowEvent, WorkflowState, WorkflowStepItem } from '../api'
import { appEvents } from '../eventBus'
import {
  DIRECT_GATE_STATUSES, EVALUATION_CHECK_LABELS, EVENT_LABELS, GATE_STATUSES, KIND_LABEL,
  STAGES, STATUS_LABEL, STEP_GLYPH, TERMINAL,
  criterionTaskResultOf, displayStatusOf, fmtMs, roleMeta, stageIndexOf, statusLabelOf,
  stepSummary, stepTitle,
  taskResults, timelineDetail, timelineTime,
  type MemberRow,
} from './workflow-format'

export interface WorkflowCardProps {
  workflowId: string
  seed?: Partial<WorkflowState>
}

export interface WorkflowTeamPayload {
  workflowId: string
  active: boolean
  members: Array<{ id: string; label: string; role: string; status: string; task: string }>
}
export type WorkflowCardEmit =
  & ((e: 'activity') => void)
  & ((e: 'team', payload: WorkflowTeamPayload) => void)
  & ((e: 'gate', open: boolean) => void)

export function useWorkflowCard(props: WorkflowCardProps, emit: WorkflowCardEmit) {
  // ---------------------------------------------------------------- 状态
  const state = ref<WorkflowState | null>(null)
  const loadError = ref('')
  const busy = ref(false)
  // 初始收起门弹窗：hydrate 得到真实状态后再武装（避免无 seed 时闪一下）。
  const gateDismissed = ref(true)
  const finalNote = ref('')
  const recoveryRiskAcknowledged = ref(false)
  const showAllTimeline = ref(false)
  const evaluation = ref<WorkflowEvaluation | null>(null)
  const rootEl = ref<HTMLElement | null>(null)
  /** 卡片头部状态行展示的最新一条实时动作（调用了什么工具 / 读写了什么）。 */
  const lastLive = ref<{ title: string; summary: string } | null>(null)

  /** SSE 实时轨迹（task_id -> steps），仅内存；终态后由 state.results[].trace 补全。 */
  const liveSteps = reactive(new Map<string, WorkflowStepItem[]>())
  /** subagent_start/complete 事件带来的临时成员信息（hydrate 间隙也能先展示）。 */
  const liveMeta = new Map<string, { role?: string; task?: string; status?: string; elapsedMs?: number; error?: string; conclusion?: string; trace?: WorkflowChildTrace }>()
  /** 已弹出独立小窗的成员 id 集合（点成员行才弹，非自动展开；见 WorkflowMemberWindow）。 */
  const openMembers = ref<Set<string>>(new Set())
  let lastSeq = 0
  let unsubscribe: (() => void) | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined
  let refreshTimer: ReturnType<typeof setTimeout> | undefined
  let destroyed = false

  // subagent_step 事件按帧合批：多代理并行时单帧可能到达十几条步骤，逐条写
  // reactive Map 会让卡片整子树逐事件重渲染，并向父级逐条 emit activity 触发滚动，
  // 这是工作流执行期对话卡顿的主要来源。队列在每帧只提交一次。
  interface QueuedStep { id: string; type: string; text: string }
  const stepQueue: QueuedStep[] = []
  let stepRaf = 0
  function flushSteps() {
    stepRaf = 0
    if (!stepQueue.length || destroyed) { stepQueue.length = 0; return }
    const queued = stepQueue.splice(0)
    for (const st of queued) {
      const list = liveSteps.get(st.id) || []
      list.push({ type: st.type, text: st.text })
      if (list.length > 240) list.splice(0, list.length - 240)
      liveSteps.set(st.id, list)
    }
    // 卡片头部始终展示最新一条动作（调用了什么工具、读写了什么文件）
    const newest = queued[queued.length - 1]
    if (newest) {
      const step = { type: newest.type, text: newest.text }
      lastLive.value = { title: stepTitle(step), summary: stepSummary(step) }
    }
    // 一帧最多通知父级跟随一次
    emit('activity')
  }
  function scheduleStepFlush() {
    if (!stepRaf) stepRaf = requestAnimationFrame(flushSteps)
  }

  // `status` 保留**后端原值**——它驱动终止判断、SSE 关流、恢复/丢弃等逻辑，
  // 一旦改写会让「已完成」被当成仍在运行。
  const status = computed(() => state.value?.status || String(props.seed?.status || 'preparing'))
  // 展示用status：`failed` 但「只是没做独立验证」时改写成 `completed_unverified`，
  // 让卡片不说「执行失败」。判据在后端已经算好的 `review.agent_ok` /
  // `review.project.ok` 上，前端不重新推断。详见 workflow-format.statusLabelOf。
  const displayStatus = computed(() => displayStatusOf(state.value))
  const statusLabel = computed(() => statusLabelOf(state.value))
  const terminal = computed(() => TERMINAL.has(status.value))
  const gateNeeded = computed(() => GATE_STATUSES.has(status.value) && !terminal.value)
  const gateOpen = computed(() => gateNeeded.value && !gateDismissed.value)
  watch(gateOpen, (open) => emit('gate', open), { immediate: true })
  /** 需要人工处理的门（选择/检索）立即弹窗；审批门在自动链路里只会毫秒穿过，延迟武装防闪。 */
  let gateArmTimer: ReturnType<typeof setTimeout> | undefined
  watch(status, (next, prev) => {
    if (next === prev) return
    if (gateArmTimer) { clearTimeout(gateArmTimer); gateArmTimer = undefined }
    // 进入新的人工门：重新武装弹窗（提交失败回滚 / 研究后回到选择门）
    if (DIRECT_GATE_STATUSES.has(next)) {
      gateDismissed.value = false
    } else if (next === 'planned' || next === 'awaiting_approval') {
      // 「选完自动开干」链路里规划→审批→执行在后台连续发生、毫秒穿过；
      // 只有真正停留（挂回恢复的待审批 / 回调缺失）才在 500ms 后弹窗。
      gateArmTimer = setTimeout(() => {
        gateArmTimer = undefined
        if (!destroyed && GATE_STATUSES.has(status.value)) gateDismissed.value = false
      }, 500)
    }
  }, { flush: 'post' })

  const stages = STAGES
  function stageClass(i: number): string {
    const cur = stageIndexOf(status.value)
    if (i < cur || cur === 4) return 'done'
    if (i === cur) return 'active'
    return 'todo'
  }

  const latestEventLabel = computed(() => {
    const events = state.value?.events
    const kind = events?.length ? String(events[events.length - 1].kind || '') : ''
    return EVENT_LABELS[kind] || kind || '准备中'
  })
  /** 头部状态行的动态信息：执行中优先显示最新工具动作（调用/读写了什么）。 */
  const headDetail = computed(() => {
    if (status.value === 'executing' && lastLive.value) {
      return `${lastLive.value.title} · ${lastLive.value.summary}`
    }
    return latestEventLabel.value
  })

  function prevOrLive(meta: { role?: string; task?: string; status?: string; elapsedMs?: number; error?: string; conclusion?: string; trace?: WorkflowChildTrace }): Partial<MemberRow> {
    return {
      role: meta.role,
      task: meta.task,
      status: meta.status,
      elapsedMs: meta.elapsedMs,
      error: meta.error,
      conclusion: meta.conclusion,
      trace: meta.trace,
    }
  }

  const members = computed<MemberRow[]>(() => {
    const wf = state.value
    const order: string[] = []
    const base = new Map<string, Partial<MemberRow>>()
    for (const task of wf?.tasks || []) {
      const id = String(task.id || '')
      if (!id) continue
      order.push(id)
      base.set(id, {
        id, role: String(task.role || 'coder'),
        task: String(task.task || task.description || ''),
      })
    }
    for (const sub of wf?.subagents || []) {
      const id = String(sub.id || '')
      if (!id) continue
      if (!order.includes(id)) order.push(id)
      base.set(id, {
        ...base.get(id),
        id, role: String(sub.role || base.get(id)?.role || 'coder'),
        task: base.get(id)?.task || String(sub.task || ''),
        status: String(sub.status || 'running'),
        elapsedMs: sub.elapsed_ms, error: sub.error, conclusion: sub.conclusion,
        trace: sub.trace,
      })
    }
    for (const [id, result] of Object.entries(taskResults(wf))) {
      if (!order.includes(id)) order.push(id)
      const prev = base.get(id) || {}
      base.set(id, {
        ...prev, id,
        status: String(result.status || prev.status || 'ok'),
        conclusion: String(result.conclusion || result.summary || prev.conclusion || ''),
        error: String(result.error || prev.error || ''),
        elapsedMs: typeof result.elapsed_ms === 'number' ? result.elapsed_ms : prev.elapsedMs,
        trace: (result.trace as WorkflowChildTrace | undefined) || prev.trace,
      })
    }
    // SSE 先行数据（state hydrate 之前）
    for (const id of liveMeta.keys()) {
      if (!order.includes(id)) order.push(id)
      const meta = liveMeta.get(id)!
      base.set(id, { ...base.get(id), id, ...prevOrLive(meta) })
    }
    for (const id of liveSteps.keys()) {
      if (!order.includes(id)) order.push(id)
    }
    return order.map((id) => {
      const b = base.get(id) || {}
      const meta = roleMeta(b.role)
      return {
        id,
        role: b.role || 'coder',
        label: meta.label,
        avatar: meta.avatar,
        task: b.task || liveMeta.get(id)?.task || '执行中…',
        status: b.status || (liveSteps.has(id) ? 'running' : 'pending'),
        elapsedMs: b.elapsedMs || liveMeta.get(id)?.elapsedMs,
        error: b.error || (liveMeta.get(id)?.status === 'failed' ? liveMeta.get(id)?.error : ''),
        conclusion: b.conclusion || (liveMeta.get(id)?.status === 'ok' ? liveMeta.get(id)?.conclusion : ''),
        steps: liveSteps.get(id) || [],
        trace: b.trace,
      }
    })
  })

  watch([members, terminal], () => {
    emit('team', {
      workflowId: props.workflowId,
      active: !terminal.value,
      members: members.value.map(m => ({ id: m.id, label: m.label, role: m.role, status: m.status, task: m.task })),
    })
  }, { immediate: true })

  const completedCount = computed(() => members.value.filter(m => m.status === 'ok').length)
  const reviewFailures = computed(() => {
    const failures = state.value?.review?.failures
    return Array.isArray(failures) ? failures as Array<{ kind?: string; message?: string; recovery?: string }> : []
  })
  const uncertainTaskIds = computed(() => state.value?.recovery?.evidence?.uncertain_task_ids || [])
  watch(() => state.value?.recovery?.generated_at, () => { recoveryRiskAcknowledged.value = false })
  const timelineEvents = computed(() => state.value?.timeline?.length ? state.value.timeline : (state.value?.events || []))
  const visibleTimeline = computed(() => timelineEvents.value.slice(showAllTimeline.value ? 0 : -30).reverse())
  const evaluationKey = computed(() => {
    const wf = state.value
    if (!wf || wf.workflow_id !== props.workflowId || !TERMINAL.has(wf.status)) return ''
    const lastEvent = wf.events?.[wf.events.length - 1]
    return [wf.workflow_id, wf.status, lastEvent?.seq ?? '', wf.steps ?? 0, wf.replans ?? 0].join(':')
  })
  let evaluationRequest = 0
  watch(evaluationKey, async key => {
    const ticket = ++evaluationRequest
    evaluation.value = null
    if (!key) return
    try {
      const result = await agentApi.workflowEvaluation(props.workflowId)
      if (!destroyed && ticket === evaluationRequest && result.ok && result.evaluation) {
        evaluation.value = result.evaluation
      }
    } catch { /* 评估是增强信息，读取失败不影响工作流操作 */ }
  })

  // ---------------------------------------------------------------- SSE + hydrate
  function setState(wf: WorkflowState | null | undefined) {
    if (!wf) return
    state.value = wf
    const lastEv = wf.events?.[wf.events.length - 1]
    if (typeof lastEv?.seq === 'number') lastSeq = Math.max(lastSeq, lastEv.seq)
  }

  async function hydrate(): Promise<boolean> {
    try {
      const r = await agentApi.workflow(props.workflowId)
      if (r.ok && r.workflow) { setState(r.workflow); return true }
      if (r.error) loadError.value = r.error
    } catch (e) {
      loadError.value = (e as Error).message || '工作流状态读取失败'
    }
    return false
  }

  function scheduleRefresh(delay = 600) {
    if (refreshTimer || destroyed) return
    refreshTimer = setTimeout(() => {
      refreshTimer = undefined
      void hydrate()
    }, delay)
  }

  // SSE 断连重连卫生：① 指数退避 2.5s→30s（旧实现固定 2.5s 无限重试，服务
  // 重启瞬间 N 个旧标签齐刷 GET/SSE）；② 后台标签不排重连，回到前台立即补连
  // （与 polling 治理的可见性感知一致）。收到任何事件说明连接健康，重置退避。
  let connected = false
  let reconnectDelay = 2500
  const RECONNECT_MAX_DELAY = 30000

  function connect() {
    unsubscribe?.()
    unsubscribe = workflowEvents(props.workflowId, lastSeq, {
      onEvent: (ev) => { reconnectDelay = 2500; handleEvent(ev) },
      onDone: () => {
        connected = false
        scheduleRefresh(0)
        scheduleReconnectStop()
      },
      onError: () => {
        connected = false
        if (!terminal.value && !destroyed) scheduleReconnect()
      },
    })
    connected = true
  }
  function scheduleReconnect() {
    if (reconnectTimer || destroyed) return
    if (typeof document !== 'undefined' && document.hidden) return
    const delay = reconnectDelay
    reconnectDelay = Math.min(RECONNECT_MAX_DELAY, reconnectDelay * 2)
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined
      if (destroyed) return
      // 排期后页面被切到后台：本次不连，等 visibilitychange 回前台再补
      if (typeof document !== 'undefined' && document.hidden) return
      void hydrate().then(ok => {
        if (destroyed) return
        if (ok) { reconnectDelay = 2500; connect() }
        else scheduleReconnect()
      })
    }, delay)
  }
  function onVisibilityResume() {
    if (typeof document === 'undefined' || document.hidden) return
    if (!destroyed && !connected && !terminal.value && !reconnectTimer) {
      void hydrate().then(ok => { if (ok && !destroyed && !connected) connect() })
    }
  }
  function scheduleReconnectStop() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = undefined }
  }

  function handleEvent(ev: WorkflowEvent) {
    if (typeof ev.seq === 'number') lastSeq = Math.max(lastSeq, ev.seq)
    const kind = String(ev.kind || '')
    if (kind === 'subagent_step') {
      const id = String(ev.task_id || '')
      if (!id) return
      // 入队，由 rAF 在每帧统一提交（见 stepQueue/flushSteps）
      stepQueue.push({ id, type: String(ev.step_type || 'thought'), text: String(ev.text || '') })
      scheduleStepFlush()
      return
    }
    if (kind === 'subagent_start') {
      liveMeta.set(String(ev.task_id || ''), { role: String(ev.role || ''), task: String(ev.task || ''), status: 'running' })
      scheduleRefresh()
    } else if (kind === 'subagent_complete') {
      liveMeta.set(String(ev.task_id || ''), {
        role: String(ev.role || ''),
        task: String(ev.task || ''),
        status: String(ev.status || 'ok') === 'ok' ? 'ok' : 'failed',
        elapsedMs: typeof ev.elapsed_ms === 'number' ? ev.elapsed_ms : undefined,
        conclusion: String(ev.conclusion || ''),
        error: String(ev.error || ''),
        trace: ev.trace,
      })
      scheduleRefresh()
    } else {
      if (ev.status) patchStatus(String(ev.status), ev.phase ? String(ev.phase) : undefined)
      // 门状态切换 / 波次 / 终态等关键事件后拉一次完整状态（任务表、results、review）
      if (/^(options|clarify|choice|plan|approval|execute|review|replan|dag_|complete|fail|interrupt|resume|auto_execute|subagent_retry)/.test(kind)) {
        scheduleRefresh(kind.startsWith('subagent') ? 700 : 350)
      }
    }
    if (!state.value) scheduleRefresh(0)
  }

  function patchStatus(next: string, phase?: string) {
    if (!state.value) return
    state.value = { ...state.value, status: next, phase: phase || state.value.phase }
  }

  // ---------------------------------------------------------------- 交互
  async function withBusy(fn: () => Promise<void>) {
    busy.value = true
    try { await fn() }
    catch (e) { loadError.value = (e as Error).message || '工作流操作失败' }
    finally { busy.value = false }
  }

  type GateResult = { ok?: boolean; workflow?: WorkflowState | null; error?: string } | null | undefined
  /**
   * 门控提交（选方案 / 提交检索 / 审批 / 改任务）：点击瞬间就收起弹窗并释放父级
   * 发送锁，请求在后台继续 —— 选择卡片只负责收集人的决定，绝不能把人锁在
   * 「提交中」。成功后若后端仍停在某个门（如自定义目标后重新出方案），再重新
   * 开门；失败则 hydrate 回真实状态、重新开门并在卡片上显示错误。
   */
  async function submitGate(fn: () => Promise<GateResult>) {
    gateDismissed.value = true
    try {
      const r = await fn()
      if (r?.workflow) setState(r.workflow)
      if (r && r.ok === false) throw new Error(r.error || '工作流操作失败')
      const next = r?.workflow?.status || status.value
      if (GATE_STATUSES.has(next)) gateDismissed.value = false
    } catch (e) {
      loadError.value = (e as Error).message || '工作流操作失败'
      const ok = await hydrate()
      if (ok && GATE_STATUSES.has(status.value)) gateDismissed.value = false
    }
  }
  function onChoose(choiceId: string, customText?: string) {
    // 普通方案：选完模型立刻开干。乐观进入规划态，弹窗即时关闭、卡片自动收起，
    // 后端在守护线程完成规划与执行，进度由 SSE 推进。
    if (choiceId !== 'custom' && choiceId !== 'web_research') {
      patchStatus('planning', 'plan')
    }
    void submitGate(() => agentApi.workflowChoice(props.workflowId, choiceId, customText || ''))
  }
  function onResearchSubmit(findings: string) {
    void submitGate(() => agentApi.workflowResearch(props.workflowId, findings))
  }
  function onResearchRun() {
    void submitGate(() => agentApi.workflowResearchRun(props.workflowId, state.value?.request))
  }
  function onApprove() {
    const wasWaiting = status.value === 'awaiting_approval'
    // 批准即开跑：乐观进入执行态（后端审批后由守护线程真正运行 DAG），
    // execute_start 等 SSE 事件随后把真实进度推到卡片。
    patchStatus('executing', 'execute')
    void submitGate(async () => {
      const id = props.workflowId
      if (wasWaiting) {
        const r = await agentApi.workflowApprove(id, true, true)
        // 后端秒回 planned（执行已转后台），不要用它盖掉乐观执行态。
        // 但无 workflow 时 SSE 可能没起来或服务端没回最新状态——
        // 显式 hydrate 一次拿真实进度，否则会卡在乐观 executing 永远
        // 看不到后续状态变化。
        if (!r.workflow) {
          await hydrate()
          return { ok: true }
        }
        return { ok: true }
      }
      // planned=首次执行：execute 先落审批门（快），再批准并后台开跑
      const r = await agentApi.workflowExecute(id)
      if (r.workflow && r.workflow.status === 'awaiting_approval') {
        const continued = await agentApi.workflowApprove(id, true, true)
        if (!continued.workflow) {
          await hydrate()
          return { ok: true }
        }
        return { ok: true }
      }
      if (!r.workflow) {
        await hydrate()
        return { ok: true }
      }
      return r.workflow ? { ok: true } : r
    })
  }
  function onReject() {
    void submitGate(() => agentApi.workflowApprove(props.workflowId, false))
  }
  function onRevise(taskId: string, task: string, deps: string[]) {
    const tasks = (state.value?.tasks || []).map(t =>
      String(t.id) === taskId ? { ...t, task, depends_on: deps } : t)
    void submitGate(() => agentApi.workflowRevise(props.workflowId, tasks))
  }
  function onReviseApprove(approved: boolean) {
    void submitGate(() => agentApi.workflowReviseApprove(props.workflowId, approved))
  }
  function onAcceptanceSave(items: AcceptanceItem[]) {
    void submitGate(() => agentApi.workflowAcceptance(props.workflowId, items))
  }
  async function onFinalAcceptance(approved: boolean) {
    await withBusy(async () => {
      const r = await agentApi.workflowAcceptanceDecide(props.workflowId, approved, finalNote.value)
      if (r.workflow) setState(r.workflow)
      else loadError.value = r.error || '记录验收结果失败'
    })
  }
  async function interrupt() {
    await withBusy(async () => {
      const r = await agentApi.workflowInterrupt(props.workflowId)
      if (r.workflow) setState(r.workflow)
    })
  }
  async function resume() {
    await withBusy(async () => {
      const r = await agentApi.workflowResume(props.workflowId)
      if (r.workflow) setState(r.workflow)
    })
  }
  async function retryTasks(taskIds: string[]) {
    if (busy.value) return
    const ids = [...new Set(taskIds.filter(Boolean))]
    if (!ids.length) return
    const uncertain = ids.filter(id => uncertainTaskIds.value.includes(id))
    if (uncertain.length && !recoveryRiskAcknowledged.value) {
      loadError.value = '请先核对不确定副作用的外部状态，并勾选确认后再重试。'
      return
    }
    if (!window.confirm(uncertain.length
      ? '已核对 ' + uncertain.length + ' 个任务的外部状态。确定按顺序重试这 ' + ids.length + ' 个任务吗？'
      : '确定按顺序重试 ' + ids.length + ' 个失败或阻塞任务吗？')) return
    loadError.value = ''
    await withBusy(async () => {
      for (const id of ids) {
        const r = await agentApi.workflowSubagentRetry(props.workflowId, id)
        if (!r.workflow || r.ok === false) throw new Error(r.error || '任务 ' + id + ' 重试失败')
        setState(r.workflow)
      }
      await hydrate()
    })
  }
  function retryMember(id: string) {
    void retryTasks([id])
  }
  type RecoveryOption = NonNullable<NonNullable<WorkflowState['recovery']>['options']>[number]
  function inspectRecovery(option: RecoveryOption) {
    const id = option.task_ids?.[0] || uncertainTaskIds.value[0]
    if (id) focusTask(id)
    else (rootEl.value?.querySelector('.wf-review, .wf-members, .wf-acceptance') || rootEl.value)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
  async function rollbackProject() {
    if (busy.value || !state.value?.project_checkpoint?.id) return
    if (!window.confirm('将恢复执行前快照中已有的项目文件；快照之后新建的文件会保留。确定继续吗？')) return
    loadError.value = ''
    await withBusy(async () => {
      const r = await agentApi.workflowProjectRollback(props.workflowId, true)
      if (!r.rollback || r.ok === false) throw new Error(r.error || '项目快照恢复失败')
      await hydrate()
      if (r.rollback.ok === false) {
        const count = Array.isArray(r.rollback.failed) ? r.rollback.failed.length : 0
        throw new Error('项目快照部分恢复失败（' + count + ' 个文件），请检查项目状态。')
      }
    })
  }
  function applyRecovery(option: RecoveryOption) {
    if (option.action === 'retry_failed') { void retryTasks(option.task_ids || []); return }
    if (option.action === 'rollback') { void rollbackProject(); return }
    inspectRecovery(option)
  }

  /** 点击成员行：开/关该成员的独立小窗（openMembers = 已弹出窗集合）。 */
  function toggleMember(id: string) {
    const next = new Set(openMembers.value)
    if (next.has(id)) next.delete(id); else next.add(id)
    openMembers.value = next
  }
  function memberOpen(id: string) {
    return openMembers.value.has(id)
  }
  function taskStatusOf(id: string): string {
    const r = taskResults(state.value)[id]
    if (r) return String(r.status || 'ok')
    return members.value.find(m => m.id === id)?.status || 'pending'
  }
  function criterionTaskResult(evidence: string[]) {
    return criterionTaskResultOf(evidence, taskResults(state.value))
  }
  function timelineCanReplay(ev: WorkflowEvent): boolean {
    const id = String(ev.task_id || '')
    const resultEvent = ['task_complete', 'task_blocked', 'subagent_complete', 'subagent_retry_complete']
      .includes(String(ev.kind || ''))
    return !!id && resultEvent && (status.value === 'failed' || status.value === 'interrupted')
      && ['failed', 'blocked'].includes(taskStatusOf(id))
  }
  function focusTask(id: string) {
    openMembers.value = new Set([...openMembers.value, id])
    requestAnimationFrame(() => {
      const target = [...(rootEl.value?.querySelectorAll<HTMLElement>('.wf-member') || [])]
        .find(element => element.dataset.taskId === id)
      ;(target || rootEl.value)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }
  function onFocusEvent(detail: { workflowId: string; taskId: string }) {
    if (detail.workflowId && detail.workflowId !== props.workflowId) return
    if (detail.taskId) focusTask(detail.taskId)
  }

  let offFocusMember = () => {}
  onMounted(async () => {
    offFocusMember = appEvents.on('docmind:wf-focus-member', onFocusEvent)
    document.addEventListener('visibilitychange', onVisibilityResume)
    if (props.seed) setState({ tasks: [], subagents: [], events: [], ...props.seed, workflow_id: props.workflowId } as WorkflowState)
    await hydrate()
    connect()
  })
  onBeforeUnmount(() => {
    destroyed = true
    unsubscribe?.()
    scheduleReconnectStop()
    if (gateArmTimer) clearTimeout(gateArmTimer)
    if (refreshTimer) clearTimeout(refreshTimer)
    if (stepRaf) { cancelAnimationFrame(stepRaf); stepRaf = 0 }
    stepQueue.length = 0
    offFocusMember()
    document.removeEventListener('visibilitychange', onVisibilityResume)
  })

  return {
    // 模板直接使用的常量
    KIND_LABEL, STATUS_LABEL, STEP_GLYPH, EVALUATION_CHECK_LABELS, EVENT_LABELS,
    // 展示层专用（见 workflow-format.statusLabelOf 的说明）
    displayStatus, statusLabel,
    // 状态与计算属性
    state, loadError, busy, gateDismissed, gateNeeded, gateOpen,
    finalNote, recoveryRiskAcknowledged, showAllTimeline, evaluation,
    rootEl, status, stages, stageClass, headDetail,
    members, completedCount, reviewFailures, uncertainTaskIds,
    timelineEvents, visibleTimeline,
    // 纯展示函数
    roleMeta, fmtMs, stepTitle, stepSummary,
    timelineDetail, timelineTime, timelineCanReplay, criterionTaskResult,
    // 交互
    interrupt, resume, taskStatusOf, toggleMember, memberOpen, retryMember,
    onFinalAcceptance, applyRecovery, focusTask,
    onChoose, onResearchSubmit, onResearchRun, onApprove, onReject,
    onRevise, onReviseApprove, onAcceptanceSave,
  }
}
