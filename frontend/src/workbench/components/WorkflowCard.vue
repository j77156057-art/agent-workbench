<script setup lang="ts">
// 对话流内的开发工作流卡片：方案选择/审批走 WorkflowGateDialog 模态（不打断对话滚动），
// 执行中每个子代理的 thought/action/observation 通过 SSE 实时追加、可折叠查看。
// 全部状态逻辑在 useWorkflowCard；展示常量在 workflow-format；本文件只做渲染。
import { useWorkflowCard } from '../composables/useWorkflowCard'
import { computed, ref, watch, nextTick } from 'vue'
import { reviewHeadlineOf } from '../composables/workflow-format'
import type { WorkflowState } from '../api'
import WorkflowGateDialog from './WorkflowGateDialog.vue'
import WorkflowPreview from './WorkflowPreview.vue'
import AdapterManagerPanel from './AdapterManagerPanel.vue'
import WorkflowMemberWindow from './WorkflowMemberWindow.vue'
import { agentApi } from '../api/agent'

const props = defineProps<{
  workflowId: string
  seed?: Partial<WorkflowState>
}>()
const emit = defineEmits<{
  (e: 'activity'): void
  (e: 'team', payload: { workflowId: string; active: boolean; members: Array<{ id: string; label: string; role: string; status: string; task: string }> }): void
  /** 人工审批门开合：父级据此锁定对话台的发送/切换等操作（聊天滚动不锁） */
  (e: 'gate', open: boolean): void
}>()

const {
  KIND_LABEL, EVALUATION_CHECK_LABELS, EVENT_LABELS,
  state, loadError, busy, gateDismissed, gateNeeded, gateOpen,
  finalNote, recoveryRiskAcknowledged, showAllTimeline, evaluation,
  rootEl, status, stages, stageClass, headDetail,
  displayStatus, statusLabel,
  members, completedCount, reviewFailures, uncertainTaskIds,
  timelineEvents, visibleTimeline,
  roleMeta, fmtMs,
  timelineDetail, timelineTime, timelineCanReplay, criterionTaskResult,
  interrupt, resume, taskStatusOf, toggleMember, memberOpen, retryMember,
  onFinalAcceptance, applyRecovery, focusTask,
  onChoose, onResearchSubmit, onResearchRun, onApprove, onReject,
  onRevise, onReviseApprove, onAcceptanceSave,
} = useWorkflowCard(props, emit)

/**
 * 复核区标题的取值与语气。判定逻辑在 `workflow-format.reviewHeadlineOf`
 * （后端已把 `agent_ok` / `project.ok` 分开存，前端只读不推断）；
 * 本文件只做渲染。
 */
const reviewHeadline = computed(() => reviewHeadlineOf(state.value))
const reviewHeadlineClass = computed(() => {
  const tone = reviewHeadline.value.tone
  return tone === 'ok' ? 'wf-ok' : tone === 'unverified' ? 'wf-unverified' : 'wf-bad'
})

/** 已弹出成员窗的级联序号：多个窗同时开时按弹出顺序错开左上角，避免完全重叠。 */
function popIndex(id: string): number {
  let n = 0
  for (const m of members.value) {
    if (!memberOpen(m.id)) continue
    if (m.id === id) return n
    n += 1
  }
  return 0
}

/**
 * 「算成功」的结局。后端从今天起给**三值** status
 * （`completed` / `completed_unverified` / `failed`），见
 * `game_workflow.settle_status`——只读工作流在独立复核为 `unverified` 时
 * 不再被压成 `failed`。
 *
 * `completed_unverified` **算成功**：活儿确实干完了（四任务全 ok、十项检查全
 * True），缺的只是「这次结果不构成独立验证」。因此用户**应当**能对它做最终
 * 验收——后端 `record_acceptance` 也已经放行；前端若还只认 `'completed'`，
 * 就会出现「后端允许、界面不给按钮」的死角，用户没法收尾。
 */
const isSuccessful = computed(() =>
  status.value === 'completed' || status.value === 'completed_unverified')

/**
 * 面向用户的「最终答复」正文。
 *
 * 为什么要有这一块：工作流真正的收尾答复一直**存在**，只是**没被渲染**——
 * ① DAG 末尾的 reporter 收尾节点产出的结论（`members` 里 role=reporter /
 *    id=report 那条的 `conclusion`），② 引擎对所有子任务结论做的整体合成
 *    （`state.results.merged`）。二者过去都只能靠点开某个成员小窗、或干脆
 *    （merged）无处可见——用户自然问「回答在哪」。
 *
 * 取值优先级：**reporter 节点的结论优先**（它就是我们专为「把四个子任务
 * 汇成一段人话」而加的收尾节点，最贴合用户要的答复）；缺失时回退到
 * `merged`（引擎级合成，语义等价但不带收尾节点的结构化排版）。都为空则
 * 返回空串，模板据此不渲染本块。
 */
const finalAnswer = computed(() => {
  const reporter = members.value.find(
    (m) => String(m.role || '').toLowerCase() === 'reporter' || m.id === 'report')
  const fromReporter = String(reporter?.conclusion || '').trim()
  if (fromReporter) return fromReporter
  const merged = (state.value?.results as Record<string, unknown> | undefined)?.merged
  return String(merged || '').trim()
})
const hasFinalAnswer = computed(() =>
  Boolean(finalAnswer.value) && isSuccessful.value)

/** 最终答复块的模板引用，用于完成时把它滚进视野。 */
const finalEl = ref<HTMLElement | null>(null)

/**
 * 工作流「实时」跑完时，把最终答复滚进视野——过去答案藏在卡片中段、
 * 上面还压着一堆时间线/复核块，用户很容易错过。
 *
 * 这里用的是**非 immediate** 的 watch：只有 `hasFinalAnswer` 在卡片存活期间
 * 从 false 翻到 true（即执行中 → 完成）才会触发一次滚动。
 * 打开历史会话时卡片一挂上来 `hasFinalAnswer` 就已是 true，不存在 false→true
 * 的变化，因此**不会**把页面猛地拉到旧答案——避免翻聊天记录时被乱滚。
 */
watch(hasFinalAnswer, (now, prev) => {
  if (!now || prev) return
  nextTick(() => {
    window.requestAnimationFrame(() => {
      finalEl.value?.scrollIntoView({ block: 'end', behavior: 'smooth' })
    })
  })
})

async function applyReviewedStage() {
  if (!state.value?.workflow_id || busy.value) return
  busy.value = true
  try {
    const result = await agentApi.workflowStageApply(state.value.workflow_id, true)
    if (result.ok) {
      emit('activity')
      window.setTimeout(() => window.location.reload(), 180)
    } else {
      loadError.value = result.error || '应用试做区改动失败'
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '应用试做区改动失败'
  } finally {
    busy.value = false
  }
}

async function reviewStageAgain() {
  if (!state.value?.workflow_id || busy.value) return
  busy.value = true
  try {
    const result = await agentApi.workflowStageReview(state.value.workflow_id)
    if (!result.ok) throw new Error(result.error || '重新复核失败')
    const refreshed = await agentApi.workflow(state.value.workflow_id)
    if (refreshed.workflow) state.value = refreshed.workflow
    emit('activity')
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '重新复核失败'
  } finally {
    busy.value = false
  }
}

async function discardStage() {
  if (!state.value?.workflow_id || busy.value || !window.confirm('丢弃这个任务的试做区文件？原项目不会改变，丢弃后不能再应用这些改动。')) return
  busy.value = true
  try {
    const result = await agentApi.workflowStageCleanup(state.value.workflow_id)
    if (!result.ok) throw new Error(result.error || '丢弃试做区失败')
    const refreshed = await agentApi.workflow(state.value.workflow_id)
    if (refreshed.workflow) state.value = refreshed.workflow
    emit('activity')
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '丢弃试做区失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div ref="rootEl" class="wf-card" :class="`wf-${status}`">
    <header class="wf-head">
      <span class="wf-ico">🧭</span>
      <b class="wf-title">开发工作流</b>
      <span class="wf-kind">{{ KIND_LABEL[state?.kind || seed?.kind || 'generic'] || '开发流程' }}</span>
      <span class="wf-status" :class="`wf-status-${displayStatus}`">{{ statusLabel }}</span>
      <span
        v-if="state?.tasks?.length && (status === 'executing' || status === 'completed' || displayStatus === 'completed_unverified')"
        class="wf-count"
      >{{ completedCount }}/{{ state.tasks.length }}</span>
      <span class="wf-sep">·</span>
      <span class="wf-event" :title="headDetail">{{ headDetail }}</span>
      <span class="wf-spacer" />
      <button
        v-if="gateNeeded"
        class="wf-gate-btn"
        @click="gateDismissed = false"
      >{{ status === 'awaiting_approval' || status === 'planned' ? '待审批' : '待选择' }} ›</button>
    </header>

    <div class="wf-body">
      <!-- 阶段条 -->
      <div class="wf-stages">
        <div v-for="(stage, i) in stages" :key="stage.key" class="wf-stage" :class="`wf-stage-${stageClass(i)}`">
          <span class="wf-stage-dot">{{ stageClass(i) === 'done' ? '✓' : i + 1 }}</span>
          <span class="wf-stage-label">{{ stage.label }}</span>
          <span v-if="i < stages.length - 1" class="wf-stage-line" :class="{ 'wf-stage-line-done': stageClass(i) === 'done' }" />
        </div>
      </div>

      <p v-if="loadError" class="wf-error">⚠ {{ loadError }}</p>

      <!-- 目标 -->
      <p v-if="state?.request" class="wf-goal">{{ state.request }}</p>
      <p v-if="state?.project_stage?.status === 'draft'" class="wf-stage-note">项目改动位于临时试做区 · {{ state.project_stage.process_backend === 'container' ? '容器隔离' : state.project_stage.process_backend === 'host_compat' ? '宿主兼容执行，未隔离文件与网络' : '命令执行待配置容器' }}</p>
      <p v-else-if="state?.project_stage?.status === 'applied'" class="wf-stage-note">已审核的改动已应用到项目</p>
      <p v-else-if="state?.project_stage?.status === 'discarded'" class="wf-stage-note">试做区已丢弃，原项目未应用这轮改动</p>

      <section v-if="status === 'awaiting_choice' && state?.options?.length" class="wf-inline-options">
        <div><b>请审核推进方案</b><small>选择方案只会生成执行计划；修改项目仍需你批准。</small></div>
        <div v-for="option in state.options" :key="option.id" class="wf-inline-option">
          <div><b>{{ option.title }}</b><small>{{ option.summary }}</small></div>
          <button class="wf-mini-btn" :disabled="busy" @click="option.id === 'custom' || option.id === 'web_research' ? gateDismissed = false : onChoose(option.id)">{{ option.id === 'custom' ? '补充目标' : option.id === 'web_research' ? '查看联网选项' : '选择并生成计划' }}</button>
        </div>
      </section>

      <!-- 进度/控制条 -->
      <div class="wf-meta">
        <span>{{ completedCount }}/{{ state?.tasks?.length || members.length }} 个任务完成</span>
        <span v-if="state?.steps">· {{ state.steps }} 步执行</span>
        <span v-if="state?.replans">· 重规划 {{ state.replans }} 次</span>
        <span v-if="state?.observability?.duration_ms">· 耗时 {{ fmtMs(state.observability.duration_ms) }}</span>
        <span class="wf-spacer" />
        <button v-if="['executing','generating_options','planning','researching','reviewing'].includes(status)" class="wf-mini-btn wf-mini-danger" :disabled="busy" @click="interrupt">中断</button>
        <button v-if="status === 'interrupted'" class="wf-mini-btn wf-mini-primary" :disabled="busy" @click="resume">恢复执行</button>
      </div>
      <div v-if="state?.tasks?.length" class="wf-progress" role="progressbar" :aria-valuenow="completedCount" aria-valuemin="0" :aria-valuemax="state.tasks.length">
        <span :style="{ width: `${Math.round((completedCount / state.tasks.length) * 100)}%` }" />
      </div>

      <div v-if="state?.interrupt_reason" class="wf-alert">{{ state.interrupt_reason }}</div>

      <!-- 任务 DAG -->
      <div v-if="state?.tasks?.length" class="wf-tasks">
        <div v-for="task in state.tasks" :key="String(task.id)" class="wf-task" :class="`wf-task-${taskStatusOf(String(task.id))}`">
          <span class="wf-task-dot" />
          <b class="wf-task-id">{{ task.id }}</b>
          <span class="wf-task-role">{{ roleMeta(String(task.role)).label }}</span>
          <span class="wf-task-text">{{ task.task }}</span>
          <em class="wf-task-state">{{ ({ pending: '待执行', running: '执行中', ok: '已完成', failed: '失败', blocked: '阻塞' } as Record<string, string>)[taskStatusOf(String(task.id))] || taskStatusOf(String(task.id)) }}</em>
        </div>
      </div>

      <!-- 成员实时轨迹 -->
      <div v-if="members.length" class="wf-members">
        <div class="wf-members-head">
          <b>团队成员（{{ members.length }}）</b>
          <small>点任意成员，把它单独的实时轨迹弹成一个小窗（可同时开多个、各自拖动）</small>
        </div>
        <div v-for="m in members" :key="m.id" class="wf-member" :class="`wf-member-${m.status}`" :data-task-id="m.id">
          <button class="wf-member-row" @click="toggleMember(m.id)">
            <span class="wf-member-avatar">{{ m.avatar }}</span>
            <span class="wf-member-main">
              <b>{{ m.label }}</b>
              <span class="wf-member-task">{{ m.task }}</span>
            </span>
            <span class="wf-member-side">
              <em class="wf-member-status">{{ ({ pending: '待执行', running: '执行中', ok: '已完成', failed: '失败', blocked: '阻塞' } as Record<string, string>)[m.status] || m.status }}</em>
              <small v-if="m.elapsedMs">{{ fmtMs(m.elapsedMs) }}</small>
            </span>
            <span class="wf-member-chevron" :title="memberOpen(m.id) ? '已弹出独立窗口（点此收起）' : '在独立小窗中查看该成员'">⧉</span>
          </button>
          <WorkflowMemberWindow
            v-if="memberOpen(m.id)"
            :member="m"
            :busy="busy"
            :index="popIndex(m.id)"
            @close="toggleMember(m.id)"
            @retry="retryMember"
          />
        </div>
      </div>

      <!-- 复核结果 -->
      <section v-if="state?.acceptance_contract?.items?.length" class="wf-acceptance">
        <b>验收条件</b>
        <span v-if="state.acceptance_contract.approved_revision" class="wf-acceptance-meta">已随计划确认 · 第 {{ state.acceptance_contract.approved_revision }} 版</span>
        <span v-else class="wf-acceptance-meta">等待计划审核</span>
        <div v-for="item in state.acceptance_contract.items" :key="item.id" class="wf-acceptance-item">
          <span>{{ item.required ? '必需' : '可选' }}</span>
          <div><b>{{ item.statement }}</b><small>{{ item.method }} · 预期证据：{{ item.evidence.join('、') || '待补充' }}</small></div>
          <small v-if="criterionTaskResult(item.evidence)" class="wf-acceptance-result">任务结果：{{ criterionTaskResult(item.evidence)?.status }} · {{ criterionTaskResult(item.evidence)?.conclusion }}</small>
        </div>
        <template v-if="isSuccessful">
          <p v-if="state.acceptance_contract.final_decision === 'accepted'" class="wf-ok">用户已验收通过</p>
          <p v-else-if="state.acceptance_contract.final_decision === 'rejected'" class="wf-bad">用户未通过验收：{{ state.acceptance_contract.final_note || '请继续说明不满足的效果' }}</p>
          <div v-else class="wf-acceptance-final">
            <p>工作流执行已结束。请按条件检查实际效果和证据，再做最终验收。</p>
            <textarea v-model="finalNote" rows="2" placeholder="未通过时请写明哪些效果需要继续修改；通过时可选填备注" />
            <div><button class="wf-mini-btn" :disabled="busy || !finalNote.trim()" @click="onFinalAcceptance(false)">未通过</button>
              <button class="wf-mini-btn wf-mini-primary" :disabled="busy" @click="onFinalAcceptance(true)">验收通过</button></div>
          </div>
        </template>
      </section>
      <div v-if="typeof state?.review?.ok === 'boolean'" class="wf-review">
        <b :class="reviewHeadlineClass">{{ reviewHeadline }}</b>
        <div v-for="(f, i) in reviewFailures" :key="i" class="wf-review-fail">
          <span>{{ f.message }}</span><small v-if="f.recovery">建议：{{ f.recovery }}</small>
        </div>
        <details v-if="state.review.project" class="wf-project-review">
          <summary>独立项目复核：{{ state.review.project.status }} · {{ state.review.project.change_count || 0 }} 个文件变化</summary>
          <p>{{ state.review.project.message }}</p>
          <button v-if="state.review.project.status === 'passed' && state.project_stage?.status === 'draft'" class="wf-mini-btn wf-mini-primary" :disabled="busy" @click="applyReviewedStage">将已验证改动应用到项目</button>
          <button v-if="state.project_stage?.status === 'draft' && (isSuccessful || status === 'failed' || status === 'interrupted')" class="wf-mini-btn" :disabled="busy" @click="reviewStageAgain">重新测试与复核</button>
          <button v-if="state.project_stage?.status === 'draft' && (isSuccessful || status === 'failed' || status === 'interrupted')" class="wf-mini-btn wf-mini-danger" :disabled="busy" @click="discardStage">丢弃试做区</button>
          <small v-if="state.review.project.checkpoint_skipped?.length">快照遗漏 {{ state.review.project.checkpoint_skipped.length }} 个文件，结果尚未完整验证。</small>
          <div v-for="(item, i) in state.review.project.tests || []" :key="i" class="wf-review-fail">
            <span>{{ item.ok ? '✓' : '✕' }} {{ item.command }} · {{ item.exit_code ?? item.error ?? '未运行' }}</span>
            <pre v-if="item.output || item.error">{{ item.output || item.error }}</pre>
          </div>
          <details v-if="state.review.project.diff"><summary>查看文件差异{{ state.review.project.diff_truncated ? '（部分）' : '' }}</summary><pre>{{ state.review.project.diff }}</pre></details>
        </details>
      </div>
      <WorkflowPreview v-if="state && (state.preview || state.visual_feedback?.length)" :key="state.workflow_id" :workflow="state" @activity="emit('activity')" />
      <details v-if="state && (state.preview || isSuccessful || status === 'failed' || status === 'interrupted')" class="wf-adapter-drawer"><summary>项目预览适配器与回滚设置</summary><AdapterManagerPanel :workflow="state" @activity="emit('activity')" /></details>
      <section v-if="state?.recovery?.status === 'required' && (status === 'failed' || status === 'interrupted')" class="wf-recovery">
        <div class="wf-recovery-head"><b>失败后的下一步</b><span>需要用户审核</span></div>
        <p>{{ state.recovery.summary || '执行未通过复核，请选择下一步。' }}</p>
        <div v-if="uncertainTaskIds.length" class="wf-recovery-risk">
          <p>有 {{ uncertainTaskIds.length }} 个任务的副作用尚未确认。重试前请先核对外部状态，避免重复执行。</p>
          <label><input v-model="recoveryRiskAcknowledged" type="checkbox" />我已核对这些任务的外部状态</label>
        </div>
        <div v-for="option in (state.recovery.options || [])" :key="option.id || option.title" class="wf-recovery-option">
          <div><b>{{ option.title }}</b><small>{{ option.detail }}</small></div>
          <button
            class="wf-mini-btn"
            :disabled="busy || (option.action === 'retry_failed' && (option.task_ids || []).some(id => uncertainTaskIds.includes(id)) && !recoveryRiskAcknowledged)"
            @click="applyRecovery(option)"
          >{{ option.action === 'retry_failed' ? '确认后重试' : option.action === 'rollback' ? '恢复执行前快照' : '查看任务轨迹' }}</button>
        </div>
      </section>
      <section v-if="evaluation" class="wf-evaluation">
        <div class="wf-evaluation-head">
          <b>工作流质量评估</b>
          <span :class="evaluation.passed ? 'wf-ok' : 'wf-bad'">{{ evaluation.passed ? '检查通过' : '有检查未通过' }}</span>
        </div>
        <div class="wf-evaluation-score">
          <strong>{{ Math.round((evaluation.score ?? 0) * 100) }}%</strong>
          <span>{{ evaluation.task_count || 0 }} 个任务 · {{ evaluation.steps || 0 }} 步 · 重规划 {{ evaluation.replans || 0 }} 次</span>
        </div>
        <div class="wf-evaluation-metrics">
          <span>失败 {{ evaluation.metrics?.failed_tasks || 0 }}</span>
          <span>阻塞 {{ evaluation.metrics?.blocked_tasks || 0 }}</span>
          <span>不确定副作用 {{ evaluation.metrics?.idempotency_in_doubt || 0 }}</span>
        </div>
        <details v-if="evaluation.checks?.length">
          <summary>查看检查项（{{ evaluation.checks.length }}）</summary>
          <div v-for="(check, i) in evaluation.checks" :key="i" class="wf-evaluation-check" :class="check.ok ? 'wf-ok' : 'wf-bad'">
            <span>{{ check.ok ? '✓' : '!' }}</span>
            <span>{{ EVALUATION_CHECK_LABELS[check.name || ''] || check.name || '检查项' }}</span>
          </div>
        </details>
      </section>
      <section v-if="state?.self_review?.status === 'ready'" class="wf-self-review">
        <div class="wf-self-review-head">
          <b>模型自我复盘</b>
          <span>{{ Math.round((state.self_review.confidence || 0) * 100) }}% 可信度</span>
        </div>
        <p>{{ state.self_review.summary }}</p>
        <details v-if="state.self_review.changed?.length">
          <summary>做了什么</summary>
          <small v-for="item in state.self_review.changed" :key="item">✓ {{ item }}</small>
        </details>
        <details v-if="state.self_review.verified?.length">
          <summary>验证了什么</summary>
          <small v-for="item in state.self_review.verified" :key="item">✓ {{ item }}</small>
        </details>
        <details v-if="state.self_review.uncertainties?.length" open>
          <summary>仍不确定</summary>
          <small v-for="item in state.self_review.uncertainties" :key="item">! {{ item }}</small>
        </details>
        <details v-if="state.self_review.next_steps?.length">
          <summary>建议下一步</summary>
          <small v-for="item in state.self_review.next_steps" :key="item">→ {{ item }}</small>
        </details>
      </section>

      <!-- 项目能力画像：只展示可复用能力摘要，不展示命令参数、环境变量或凭据 -->
      <section v-if="state?.project_profile" class="wf-project-profile">
        <div class="wf-project-profile-head">
          <b>项目能力画像</b>
          <span>{{ state.project_profile.kind || 'generic' }}</span>
        </div>
        <div class="wf-project-profile-counts">
          <span>工具 <b>{{ state.project_profile.tools?.length || 0 }}</b></span>
          <span>MCP <b>{{ state.project_profile.mcp?.length || 0 }}</b></span>
          <span>运行命令 <b>{{ state.project_profile.run_commands?.length || 0 }}</b></span>
          <span>预览适配器 <b>{{ state.project_profile.preview_adapters?.length || 0 }}</b></span>
        </div>
        <div v-if="state.project_profile.mcp?.length" class="wf-project-profile-list">
          <small class="wf-project-profile-label">项目 MCP</small>
          <div v-for="item in state.project_profile.mcp" :key="item.key" class="wf-project-profile-mcp">
            <b :class="{ 'wf-profile-disabled': item.enabled === false }">{{ item.name || item.key }}</b>
            <small v-if="item.enabled === false">已停用</small>
            <small v-else-if="item.capabilities?.length">{{ item.capabilities.join('、') }}</small>
          </div>
        </div>
        <div v-if="state.project_profile.preview_adapters?.length" class="wf-project-profile-list">
          <small class="wf-project-profile-label">预览能力</small>
          <div class="wf-project-profile-tags">
            <span v-for="item in state.project_profile.preview_adapters" :key="item">{{ item }}</span>
          </div>
        </div>
        <div v-if="state.project_profile.acceptance_methods?.length" class="wf-project-profile-list">
          <small class="wf-project-profile-label">验收方式</small>
          <div class="wf-project-profile-tags">
            <span v-for="item in state.project_profile.acceptance_methods" :key="item">{{ item }}</span>
          </div>
        </div>
      </section>

      <!-- 持久时间线优先；旧工作流回退到事件窗口 -->
      <details v-if="timelineEvents.length" class="wf-timeline">
        <summary>任务时间线（{{ timelineEvents.length }}）</summary>
        <div class="wf-timeline-list">
          <details v-for="(ev, i) in visibleTimeline" :key="ev.seq || String(ev.kind) + String(ev.ts) + i" class="wf-timeline-item">
            <summary>
              <span><b>{{ EVENT_LABELS[String(ev.kind)] || ev.kind || '事件' }}</b><small v-if="ev.task_id">{{ ev.task_id }}</small></span>
              <time v-if="timelineTime(ev)">{{ timelineTime(ev) }}</time>
            </summary>
            <div class="wf-timeline-detail">
              <p v-if="timelineDetail(ev)">{{ timelineDetail(ev) }}</p>
              <button v-if="timelineCanReplay(ev)" class="wf-mini-btn" :disabled="busy" @click.stop="retryMember(String(ev.task_id))">从此任务继续</button>
              <button v-else-if="ev.task_id" class="wf-mini-btn" @click.stop="focusTask(String(ev.task_id))">查看任务</button>
            </div>
          </details>
        </div>
        <button v-if="timelineEvents.length > 30" class="wf-mini-btn wf-timeline-more" @click.stop="showAllTimeline = !showAllTimeline">
          {{ showAllTimeline ? '只看最近 30 条' : '查看全部 ' + timelineEvents.length + ' 条' }}
        </button>
      </details>

      <!-- 最终答复：把 DAG 末尾 reporter 节点的收尾结论（或引擎整体合成）
           直接摊在卡片最底部——完成即最新、最该被看到的东西放在最后，
           不用点开成员小窗、也不用翻 merged 字段。 -->
      <section v-if="hasFinalAnswer" ref="finalEl" class="wf-final">
        <b class="wf-final-head">最终答复</b>
        <div class="wf-final-body">{{ finalAnswer }}</div>
      </section>
    </div>

    <WorkflowGateDialog
      v-if="state && gateOpen"
      :state="state"
      :busy="busy"
      @choose="onChoose"
      @research-submit="onResearchSubmit"
      @research-run="onResearchRun"
      @approve="onApprove"
      @reject="onReject"
      @revise="onRevise"
      @revise-approve="onReviseApprove"
      @acceptance-save="onAcceptanceSave"
      @dismiss="gateDismissed = true"
    />
  </div>
</template>

<style scoped>
.wf-card {
  margin: 4px 0 10px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-raised);
  overflow: hidden;
  transition: border-color .2s ease, box-shadow .2s ease, transform .2s ease;
}
.wf-card.wf-executing { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(37,96,212,.08), 0 10px 24px rgba(37,96,212,.06); }
.wf-card.wf-failed { border-color: rgba(214,78,78,.5); box-shadow: 0 8px 20px rgba(214,78,78,.06); }
.wf-card.wf-completed { border-color: rgba(52,168,112,.45); box-shadow: 0 8px 20px rgba(52,168,112,.06); }
/* 卡片描边同样别用红色——边框转红会让整张卡看起来像出了故障，
   而实际是「交付完成，独立验证那一步没做」。 */
.wf-card.wf-completed_unverified { border-color: rgba(224,162,48,.45); box-shadow: 0 8px 20px rgba(224,162,48,.06); }
.wf-head {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 12px; user-select: none;
  background: var(--bg-hover);
}
.wf-ico { font-size: var(--fs-md); }
.wf-title { font-size: var(--fs-md); font-weight: 700; color: var(--text); }
.wf-kind { font-size: var(--fs-cap); color: var(--text-faint); border: 1px solid var(--border); border-radius: 99px; padding: 1px 7px; }
.wf-status { font-size: var(--fs-cap); font-weight: 600; }
.wf-status-executing, .wf-status-generating_options, .wf-status-planning { color: var(--accent); }
.wf-status-completed { color: var(--green); }
.wf-status-failed { color: var(--danger); }
/* 「已完成 · 未做独立验证」——**琥珀色**：内容确实交付了，而独立验证那一条是
   **证据等级**的声明，不是失败。给绿色会和「已完成」混淆，给红色会误导成
   「有东西坏了」；琥珀色正好是「完成，但有一处需要你知道」的既有语义。 */
.wf-status-completed_unverified { color: var(--amber); }
.wf-status-awaiting_choice, .wf-status-awaiting_approval { color: var(--amber); }
.wf-status-executing::before { content: ''; display: inline-block; width: 6px; height: 6px; margin: 0 5px 1px 0; border-radius: 50%; background: currentColor; animation: wf-pulse 1.4s ease-in-out infinite; }
.wf-sep { color: var(--text-faint); font-size: var(--fs-cap); }
.wf-event { font-size: var(--fs-cap); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 0 1 auto; }
.wf-count {
  font-size: var(--fs-cap); font-weight: 600; color: var(--text-muted);
  font-variant-numeric: tabular-nums; flex: 0 0 auto;
}
.wf-spacer { flex: 1; }
.wf-gate-btn {
  border: 1px solid var(--amber); color: var(--amber);
  background: rgba(214,158,46,.08); border-radius: 99px;
  font-size: var(--fs-cap); font-weight: 600; padding: 2px 10px; cursor: pointer;
}
.wf-gate-btn:hover { background: rgba(214,158,46,.16); }
.wf-gate-btn { transition: background .16s ease, transform .16s ease, box-shadow .16s ease; }
.wf-gate-btn:hover { transform: translateY(-1px); box-shadow: 0 3px 8px rgba(214,158,46,.15); }
.wf-body { padding: 11px 13px 12px; display: grid; gap: 10px; }
.wf-inline-options { display: grid; gap: 8px; padding: 11px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-hover); }
.wf-inline-options > div:first-child { display: grid; gap: 3px; }
.wf-inline-options > div:first-child small,.wf-inline-option small { color: var(--text-muted); line-height: 1.45; }
.wf-inline-option { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-raised); }
.wf-inline-option > div { display: grid; gap: 3px; min-width: 0; }
.wf-inline-option button { flex: 0 0 auto; }
.wf-stages { display: flex; align-items: center; }
.wf-stage-note { margin: 8px 0; padding: 7px 9px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-hover); color: var(--text-muted); font-size: var(--fs-cap); }
.wf-stage { position: relative; display: flex; align-items: center; gap: 5px; flex: 1; }
.wf-stage-dot {
  width: 18px; height: 18px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: var(--fs-cap); font-weight: 700; flex: 0 0 auto;
  border: 1px solid var(--border-strong); color: var(--text-faint); background: var(--bg-raised);
}
.wf-stage-label { font-size: var(--fs-cap); color: var(--text-faint); white-space: nowrap; }
.wf-stage-line { flex: 1; height: 1px; background: var(--border); margin: 0 4px; min-width: 8px; }
.wf-stage-line-done { background: linear-gradient(90deg, var(--green), rgba(52,168,112,.35)); }
.wf-stage-active .wf-stage-dot { border-color: var(--accent); color: var(--accent-ink); background: var(--accent); }
.wf-stage-active .wf-stage-label { color: var(--accent); font-weight: 600; }
.wf-stage-done .wf-stage-dot { border-color: var(--green); background: var(--green); color: var(--green-ink); }
.wf-stage-done .wf-stage-label { color: var(--text-muted); }
.wf-stage:last-child .wf-stage-line { display: none; }
.wf-goal {
  margin: 0; padding: 8px 10px; font-size: var(--fs-sm); line-height: 1.6;
  border-left: 3px solid var(--accent); background: var(--bg-hover); border-radius: 0 8px 8px 0;
  color: var(--text);
}
.wf-acceptance { display: grid; gap: 7px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; font-size: var(--fs-sm); }
.wf-acceptance-meta, .wf-acceptance-item small { color: var(--text-faint); font-size: var(--fs-cap); }
.wf-acceptance-item { display: flex; gap: 8px; border-top: 1px solid var(--border); padding-top: 7px; }
.wf-acceptance-item > span { flex: 0 0 auto; color: var(--accent); font-size: var(--fs-cap); }
.wf-acceptance-item > div { display: grid; gap: 3px; }
.wf-acceptance-final { display: grid; gap: 7px; }
.wf-acceptance-final p { margin: 0; color: var(--text-muted); }
.wf-acceptance-final textarea { width: 100%; box-sizing: border-box; padding: 7px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-raised); color: var(--text); resize: vertical; }
.wf-acceptance-final > div { display: flex; gap: 7px; }
.wf-meta { display: flex; align-items: center; gap: 6px; font-size: var(--fs-cap); color: var(--text-faint); font-variant-numeric: tabular-nums; }
.wf-progress { height: 4px; border-radius: 99px; background: var(--bg-selected); overflow: hidden; }
.wf-progress > span { display: block; height: 100%; min-width: 0; border-radius: inherit; background: linear-gradient(90deg, var(--accent), #6f9cf5); transition: width .35s ease; }
.wf-error { margin: 0; color: var(--danger); font-size: var(--fs-sm); }
.wf-alert {
  margin: 0; padding: 6px 9px; font-size: var(--fs-sm);
  border: 1px solid rgba(214,158,46,.5); border-radius: 7px; background: rgba(214,158,46,.08); color: var(--amber);
}
.wf-mini-btn {
  border: 1px solid var(--border); border-radius: 6px; background: var(--bg-raised);
  color: var(--text-muted); font-size: var(--fs-cap); padding: 2px 9px; cursor: pointer;
  transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease;
}
.wf-mini-btn:hover:not(:disabled) { color: var(--text); border-color: var(--border-strong); transform: translateY(-1px); }
.wf-mini-btn:disabled { opacity: .5; cursor: default; }
.wf-mini-danger { color: var(--danger); border-color: var(--danger); }
.wf-mini-danger:hover:not(:disabled) { background: var(--danger); color: var(--danger-ink); }
.wf-mini-primary { color: var(--accent); border-color: var(--accent); }
.wf-tasks { display: grid; gap: 4px; }
.wf-task {
  display: flex; align-items: baseline; gap: 8px;
  padding: 6px 9px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg-hover); font-size: var(--fs-sm);
  transition: background .16s ease, border-color .16s ease, transform .16s ease;
}
.wf-task:hover { background: var(--bg-hover); border-color: var(--border-strong); transform: translateX(2px); }
.wf-task-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); flex: 0 0 auto; align-self: center; }
.wf-task-running .wf-task-dot { background: var(--accent); box-shadow: 0 0 0 3px rgba(37,96,212,.15); animation: wf-pulse 1.4s ease-in-out infinite; }
.wf-task-ok .wf-task-dot { background: var(--green); }
.wf-task-failed .wf-task-dot, .wf-task-blocked .wf-task-dot { background: var(--danger); }
.wf-task-id { font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--accent); }
.wf-task-role { font-size: var(--fs-cap); color: var(--text-faint); font-weight: 600; flex: 0 0 auto; }
.wf-task-text { flex: 1; min-width: 0; color: var(--text); line-height: 1.5; }
.wf-task-state { flex: 0 0 auto; font-style: normal; font-size: var(--fs-cap); color: var(--text-faint); }
.wf-task-running .wf-task-state { color: var(--accent); }
.wf-task-ok .wf-task-state { color: var(--green); }
.wf-task-failed .wf-task-state, .wf-task-blocked .wf-task-state { color: var(--danger); }
.wf-members { display: grid; gap: 6px; }
.wf-members-head { display: flex; align-items: baseline; gap: 8px; }
.wf-members-head b { font-size: var(--fs-sm); color: var(--text); }
.wf-members-head small { font-size: var(--fs-cap); color: var(--text-faint); }
.wf-member { border: 1px solid var(--border); border-radius: 9px; background: var(--bg-hover); transition: border-color .18s ease, box-shadow .18s ease, transform .18s ease; }
.wf-member:hover { border-color: var(--border-strong); }
.wf-member-running { border-color: rgba(37,96,212,.45); box-shadow: inset 3px 0 0 var(--accent); }
.wf-member-row {
  width: 100%; display: flex; align-items: center; gap: 9px;
  padding: 8px 10px; border: 0; background: transparent; cursor: pointer; text-align: left;
}
.wf-member-row:hover { background: var(--bg-hover); border-radius: 9px; }
.wf-member-avatar {
  width: 24px; height: 24px; border-radius: 7px; flex: 0 0 auto;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: var(--fs-cap); font-weight: 700;
  background: var(--bg-selected); color: var(--accent); border: 1px solid var(--border);
}
.wf-member-main { flex: 1; min-width: 0; display: grid; gap: 1px; }
.wf-member-main b { font-size: var(--fs-sm); color: var(--text); }
.wf-member-task { font-size: var(--fs-cap); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-member-side { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 6px; }
.wf-member-status { font-style: normal; font-size: var(--fs-cap); color: var(--text-faint); }
.wf-member-running .wf-member-status { color: var(--accent); }
.wf-member-ok .wf-member-status { color: var(--green); }
.wf-member-failed .wf-member-status, .wf-member-blocked .wf-member-status { color: var(--danger); }
.wf-member-side small { font-size: var(--fs-cap); color: var(--text-faint); font-variant-numeric: tabular-nums; }
.wf-member-chevron { color: var(--text-faint); font-size: var(--fs-cap); }
.wf-member-detail {
  margin: 0 10px 9px 42px;
  padding-left: 11px; border-left: 2px solid var(--border);
  display: grid; gap: 5px;
}
.wf-member-wait { font-size: var(--fs-sm); color: var(--text-faint); padding: 3px 0; }
.wf-step { display: flex; gap: 7px; position: relative; }
.wf-step-glyph {
  width: 18px; flex: 0 0 auto; text-align: center;
  font-size: var(--fs-cap); color: var(--text-faint); padding-top: 1px;
}
.wf-step-action .wf-step-glyph { color: var(--accent); }
.wf-step-observation .wf-step-glyph { color: var(--green); }
.wf-step-thought .wf-step-glyph { color: var(--text-muted); }
.wf-step-body { min-width: 0; display: grid; gap: 1px; }
.wf-step-title { font-size: var(--fs-cap); font-weight: 600; color: var(--text); }
.wf-step-summary {
  font-size: var(--fs-sm); line-height: 1.5; color: var(--text-muted);
  white-space: pre-wrap; word-break: break-word;
}
.wf-step-full {
  display: none; margin: 3px 0 0; padding: 7px 9px;
  background: var(--bg-raised); border: 1px solid var(--border); border-radius: 7px;
  font: inherit; font-size: var(--fs-cap); line-height: 1.55; color: var(--text-muted);
  white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow-y: auto;
}
.wf-step:hover .wf-step-full { display: block; }
.wf-step-hint { font-size: var(--fs-cap); color: var(--text-faint); }
.wf-member-conclusion, .wf-member-error {
  margin-top: 2px; padding: 7px 9px; border-radius: 7px;
  font-size: var(--fs-sm); line-height: 1.55; display: grid; gap: 3px;
}
.wf-member-conclusion b, .wf-member-error b { font-size: var(--fs-cap); }
.wf-member-conclusion { background: rgba(52,168,112,.08); color: var(--text); }
.wf-member-conclusion b { color: var(--green); }
.wf-member-error { background: rgba(214,78,78,.07); color: var(--danger); }
.wf-member-error .wf-mini-btn { justify-self: start; margin-top: 3px; }
.wf-final {
  display: grid; gap: 7px; padding: 11px 12px;
  border: 1px solid rgba(52,168,112,.3); border-left: 3px solid var(--green);
  border-radius: 9px; background: rgba(52,168,112,.05);
}
.wf-final-head { color: var(--green); font-size: var(--fs-sm); letter-spacing: .02em; }
.wf-final-body {
  font-size: var(--fs-sm); line-height: 1.6; color: var(--text);
  white-space: pre-wrap; overflow-wrap: anywhere;
  max-height: 460px; overflow: auto;
}
.wf-review {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  font-size: var(--fs-sm); padding: 9px 10px; border-radius: 8px; background: var(--bg-hover); border: 1px solid var(--border);
}
.wf-review:has(.wf-ok) { border-color: rgba(52,168,112,.3); background: rgba(52,168,112,.05); }
.wf-review:has(.wf-bad) { border-color: rgba(214,78,78,.3); background: rgba(214,78,78,.04); }
.wf-ok { color: var(--green); }
.wf-bad { color: var(--danger); }
/* 「内容交付了但这次结果不构成独立验证」——琥珀色，不该和真失败一样标红。
   与 .wf-status-completed_unverified 用同一个 --amber，两处语气保持一致。 */
.wf-unverified { color: var(--amber); }
.wf-review:has(.wf-unverified) { border-color: rgba(214,158,46,.3); background: rgba(214,158,46,.05); }
.wf-review-fail { display: grid; gap: 1px; width: 100%; font-size: var(--fs-cap); color: var(--text-muted); }
.wf-review-fail small { color: var(--text-faint); }
.wf-project-review { width: 100%; border-top: 1px solid var(--border); padding-top: 5px; color: var(--text-muted); }
.wf-project-review summary { cursor: pointer; }
.wf-project-review p { margin: 5px 0; }
.wf-project-review pre { max-height: 260px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: var(--fs-cap); }
.wf-recovery { display: grid; gap: 8px; padding: 10px; border: 1px solid var(--danger); border-radius: 9px; background: var(--bg-hover); font-size: var(--fs-cap); }
.wf-recovery-head { display: flex; justify-content: space-between; gap: 8px; }
.wf-recovery-head span { color: var(--danger); font-size: var(--fs-cap); }
.wf-recovery p { margin: 0; line-height: 1.5; color: var(--text-muted); }
.wf-recovery-risk { display: grid; gap: 6px; padding: 7px 8px; border-radius: 7px; background: rgba(214,158,46,.08); }
.wf-recovery-risk p { color: var(--amber); }
.wf-recovery-risk label { display: flex; align-items: center; gap: 5px; color: var(--text); cursor: pointer; }
.wf-recovery-risk input { margin: 0; }
.wf-recovery-option { display: flex; justify-content: space-between; align-items: center; gap: 8px; border-top: 1px solid var(--border); padding-top: 7px; }
.wf-recovery-option > div { display: grid; gap: 3px; min-width: 0; }
.wf-recovery-option b { color: var(--text); }
.wf-recovery-option small { color: var(--text-faint); line-height: 1.45; }
.wf-recovery-option button { flex: 0 0 auto; }
.wf-evaluation { display: grid; gap: 7px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-hover); font-size: var(--fs-cap); }
.wf-evaluation-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.wf-evaluation-score { display: flex; align-items: baseline; flex-wrap: wrap; gap: 7px; }
.wf-evaluation-score strong { font-size: var(--fs-xl); color: var(--text); font-variant-numeric: tabular-nums; }
.wf-evaluation-score span, .wf-evaluation-metrics { color: var(--text-faint); }
.wf-evaluation-metrics { display: flex; flex-wrap: wrap; gap: 6px 12px; }
.wf-evaluation details { border-top: 1px solid var(--border); padding-top: 6px; }
.wf-evaluation summary { cursor: pointer; color: var(--text-muted); }
.wf-evaluation-check { display: flex; gap: 7px; margin-top: 5px; }
.wf-self-review { display: grid; gap: 6px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-hover); font-size: var(--fs-cap); }
.wf-self-review-head { display: flex; justify-content: space-between; gap: 8px; }.wf-self-review-head span { color: var(--accent); font-size: var(--fs-cap); }
.wf-self-review p { margin: 0; color: var(--text-muted); line-height: 1.45; }.wf-self-review details { border-top: 1px solid var(--border); padding-top: 5px; }
.wf-self-review summary { cursor: pointer; color: var(--text-muted); }.wf-self-review details small { display: block; margin-top: 4px; line-height: 1.4; overflow-wrap: anywhere; }.wf-self-review details:nth-of-type(3) small { color: var(--amber); }
.wf-project-profile { display: grid; gap: 7px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-hover); font-size: var(--fs-cap); }
.wf-project-profile-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.wf-project-profile-head span { color: var(--accent); font-size: var(--fs-cap); border: 1px solid var(--border); border-radius: 99px; padding: 1px 7px; }
.wf-project-profile-counts { display: flex; flex-wrap: wrap; gap: 6px 12px; color: var(--text-faint); }
.wf-project-profile-counts span { white-space: nowrap; }
.wf-project-profile-counts b { color: var(--text-muted); font-variant-numeric: tabular-nums; }
.wf-project-profile-list { display: grid; gap: 4px; border-top: 1px solid var(--border); padding-top: 6px; }
.wf-project-profile-label { color: var(--text-faint); }
.wf-project-profile-mcp { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; min-width: 0; }
.wf-project-profile-mcp b { color: var(--text-muted); font-size: var(--fs-cap); font-weight: 600; }
.wf-project-profile-mcp small { color: var(--text-faint); overflow-wrap: anywhere; }
.wf-project-profile-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.wf-project-profile-tags span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border: 1px solid var(--border); border-radius: 99px; padding: 2px 7px; color: var(--text-muted); background: var(--bg-raised); }
.wf-project-profile-tags .wf-profile-disabled { opacity: .55; text-decoration: line-through; }
.wf-timeline { font-size: var(--fs-cap); }
.wf-timeline > summary { cursor: pointer; color: var(--text-faint); }
.wf-timeline-list { display: grid; gap: 5px; max-height: 360px; overflow-y: auto; margin-top: 7px; }
.wf-timeline-item { padding: 6px 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-hover); }
.wf-timeline-item > summary { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; cursor: pointer; }
.wf-timeline-item > summary span { display: flex; gap: 7px; min-width: 0; }
.wf-timeline-item b { color: var(--text-muted); font-weight: 600; }
.wf-timeline-item small { color: var(--accent); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-timeline-item time { color: var(--text-faint); font-size: var(--fs-cap); flex: 0 0 auto; }
.wf-timeline-detail { display: grid; justify-items: start; gap: 6px; padding-top: 6px; }
.wf-timeline-detail p { margin: 0; color: var(--text-faint); overflow-wrap: anywhere; }
.wf-timeline-more { margin-top: 7px; }
.wf-dots { animation: wf-blink 1.1s infinite; }
@keyframes wf-blink { 50% { opacity: .25; } }
@keyframes wf-pulse { 0%, 100% { opacity: .55; transform: scale(.86); } 50% { opacity: 1; transform: scale(1); } }

@media (max-width: 640px) {
  .wf-head { flex-wrap: wrap; gap: 6px; }
  .wf-event { order: 8; flex-basis: 100%; }
  .wf-stages { overflow-x: auto; padding-bottom: 2px; }
  .wf-stage { min-width: 78px; }
  .wf-stage-label { overflow: hidden; text-overflow: ellipsis; }
  .wf-task { align-items: flex-start; flex-wrap: wrap; }
  .wf-task-text { flex-basis: calc(100% - 24px); }
  .wf-task-state { margin-left: 15px; }
  .wf-member-side small { display: none; }
  .wf-recovery-option { align-items: flex-start; flex-direction: column; }
}

@media (prefers-reduced-motion: reduce) {
  .wf-card, .wf-gate-btn, .wf-mini-btn, .wf-task, .wf-member, .wf-progress > span { transition: none; }
  .wf-status-executing::before, .wf-task-running .wf-task-dot, .wf-dots { animation: none; }
}
</style>
