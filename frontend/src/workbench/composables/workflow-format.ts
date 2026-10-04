// 工作流卡片的展示常量与纯格式化函数（无状态），供 useWorkflowCard 与模板使用。
import type { WorkflowChildTrace, WorkflowEvent, WorkflowState, WorkflowStepItem } from '../api'

export const TERMINAL = new Set(['completed', 'failed', 'interrupted'])
export const GATE_STATUSES = new Set(['awaiting_choice', 'generating_options', 'awaiting_research', 'awaiting_approval', 'planned'])
export const DIRECT_GATE_STATUSES = new Set(['awaiting_choice', 'generating_options', 'awaiting_research'])

export const KIND_LABEL: Record<string, string> = { generic: '通用开发', game: '游戏开发', eda: '电子设计' }
export const STATUS_LABEL: Record<string, string> = {
  awaiting_choice: '等待选择', generating_options: '正在生成方案', awaiting_research: '等待检索',
  planned: '待执行', awaiting_approval: '等待审核', executing: '执行中',
  planning: '规划中', interrupted: '已暂停', completed: '已完成', failed: '执行失败',
  // **纯展示态**，后端不产生这个 status。
  //
  // 实测（`wf-e042489bf0fe`）：四个子代理全 `ok`、十项复核检查全绿、
  // `evidence_verified: true`、144 条引用 0 条不过、内容 4133 字全部正确交付——
  // 而 `status` 是 `failed`，因为 `game_workflow.py:3245` 把两个维度压成一个布尔：
  //
  //     state.review["agent_ok"] = ...                 // 十项检查 + 证据核验
  //     state.review["ok"] = agent_ok and project["ok"]  // 后者说的是另一件事
  //
  // `project.ok=false` 的原话是「试做区没有容器级命令隔离，**不能作为独立测试
  // 通过的证据**」——那是**给这次结果标注证据等级**，不是「有东西坏了」。
  // 而一次纯只读侦察（change_count=0）本来就没打算跑测试。
  //
  // 后端 `agent_ok` 已经把两者分开了（第 3243 行存了它），所以这里只读它、
  // **零后端改动**——不动那17 处 `status in {completed,failed}` 的状态判断。
  completed_unverified: '已完成 · 未做独立验证',
}

/**
 * 只读任务的 `failed` 不是失败。
 *
 * 判定条件取自后端已经算好的两个字段，**不在前端重新推断**：
 * `review.agent_ok`（十项检查 + 证据核验的结果）与 `review.project.ok`
 *（独立验证的结果）。
 *
 * 只有**前者真、后者假**才改写展示 —— 那正是「内容交付了，但这次结果不构成
 * 独立验证」。前者假就是真失败，一律照旧显示「执行失败」。
 */
export function statusLabelOf(state: { status?: string; review?: Record<string, unknown> } | null | undefined): string {
  const raw = String(state?.status || 'preparing')
  if (raw !== 'failed') return STATUS_LABEL[raw] || raw
  const review = (state?.review || {}) as Record<string, unknown>
  const agentOk = review.agent_ok === true
  const projectOk = (review.project as Record<string, unknown> | undefined)?.ok === true
  return agentOk && !projectOk ? STATUS_LABEL.completed_unverified : (STATUS_LABEL[raw] || raw)
}

/** 展示用的 status：`failed` 且「只是没做独立验证」时改写成 `completed_unverified`。 */
export function displayStatusOf(state: { status?: string; review?: Record<string, unknown> } | null | undefined): string {
  const raw = String(state?.status || 'preparing')
  if (raw !== 'failed') return raw
  const review = (state?.review || {}) as Record<string, unknown>
  const agentOk = review.agent_ok === true
  const projectOk = (review.project as Record<string, unknown> | undefined)?.ok === true
  return agentOk && !projectOk ? 'completed_unverified' : raw
}
export const EVALUATION_CHECK_LABELS: Record<string, string> = {
  completed: '工作流完成', review_ok: '结果复核通过',
  no_failed_tasks: '没有失败或阻塞任务',
  steps_within_limit: '执行步数在上限内',
  replans_within_limit: '重规划次数在上限内',
  no_in_doubt_tool: '没有不确定的工具副作用',
}
export const ROLE_META: Record<string, { label: string; avatar: string }> = {
  dispatcher: { label: '文件派发员', avatar: '派' },
  planner: { label: '拆解规划员', avatar: '规' },
  designer: { label: '设计师', avatar: '设' },
  researcher: { label: '检索员', avatar: '检' },
  coder: { label: '实现员', avatar: '码' },
  tester: { label: 'QA 工程师', avatar: '测' },
  reviewer: { label: '评审员', avatar: '审' },
  artist: { label: '美术员', avatar: '美' },
  audio: { label: '音频员', avatar: '音' },
}
export const STEP_GLYPH: Record<string, string> = { thought: '◌', action: '↗', observation: '✓', reflection: '↻' }
export const STEP_LABEL: Record<string, string> = { thought: '思考', action: '工具调用', observation: '观察结果', reflection: '复核' }
export const TOOL_NAMES: Record<string, string> = {
  search_code: '检索代码', search_knowledge: '检索知识库', read_file: '读取文件',
  grep: '定位代码', run_command: '运行命令', game_playtest: '运行校验',
  web_search: '网页搜索', web_fetch: '读取网页', dev_mcp_call: '调用 MCP',
  apply_edit: '修改文件', create_file: '创建文件',
  delegate: '委派子代理', orchestrate: '编排任务', game_screenshot: '画面截图',
}
export const EVENT_LABELS: Record<string, string> = {
  options_pending: '方案就绪', clarify: '停在方案选择门', options_generated: '已生成方案选项',
  choice: '已选择方案', research: '检索资料已提交', plan: '任务图已生成',
  approval_required: '等待审批', execute_start: '开始执行', execute_wave: '执行任务波次',
  subagent_start: '子代理开始', subagent_complete: '子代理完成',
  subagent_retry_start: '子代理重试', subagent_retry_complete: '重试完成',
  review: '结果复核中', review_replan: '复核未过，重规划', replan: '正在重规划',
  dag_revision_requested: '请求修改 DAG', dag_revision_approved: 'DAG 修改已批准', dag_revision_denied: 'DAG 修改被拒绝',
  complete: '工作流完成', fail: '工作流失败', interrupt: '工作流中断', resume: '工作流恢复',
  approval_granted: '审批通过', approval_denied: '审批驳回', auto_execute_start: '自动开始执行',
  acceptance_revised: '验收条件已更新', acceptance_decided: '用户已完成验收决定',
  task_complete: '任务完成', task_blocked: '任务阻塞',
  before_tool: '调用工具前', after_tool: '调用工具后',
  before_mcp: '调用 MCP 前', after_mcp: '调用 MCP 后',
  project_checkpoint_created: '创建项目快照', project_checkpoint_restored: '恢复项目快照',
  visual_snapshot_saved: '保存画面证据', subagent_step: '成员执行步骤',
  capability_lease_created: '授予临时工具权限',
}

export function roleMeta(role?: string) {
  return ROLE_META[String(role || '').toLowerCase()] || { label: role || '子代理', avatar: '代' }
}

export interface MemberRow {
  id: string; role: string; label: string; avatar: string; task: string
  status: string; elapsedMs?: number; error?: string; conclusion?: string
  steps: WorkflowStepItem[]; trace?: WorkflowChildTrace
}

/** results 字段历史上有两种包裹形态，统一拍平为 task_id -> result。 */
export function taskResults(wf: WorkflowState | null | undefined): Record<string, Record<string, unknown>> {
  const report = wf?.results || {}
  return (report.results as Record<string, Record<string, unknown>> | undefined)
    || report as Record<string, Record<string, unknown>>
}

export const STAGES = [
  { key: 'choice', label: '方案选择' },
  { key: 'plan', label: '任务规划' },
  { key: 'execute', label: '并行执行' },
  { key: 'review', label: '复核交付' },
]

export function stageIndexOf(s: string): number {
  if (['awaiting_choice', 'generating_options', 'awaiting_research'].includes(s)) return 0
  if (['planning', 'planned', 'awaiting_approval'].includes(s)) return 1
  if (['executing', 'interrupted'].includes(s)) return 2
  if (s === 'completed') return 4
  if (s === 'failed') return 2
  return 0
}

export function stepTitle(step: WorkflowStepItem): string {
  const raw = step.text.trim()
  const tool = raw.match(/^([\w.-]+)\s*\(/)?.[1]
  if (step.type === 'action' && tool) return TOOL_NAMES[tool] || tool
  return STEP_LABEL[step.type] || step.type
}

export function stepSummary(step: WorkflowStepItem): string {
  const raw = step.text.trim().replace(/\s+/g, ' ')
  if (raw.length <= 110) return raw
  return raw.slice(0, 107) + '…'
}

export function fmtMs(ms?: number): string {
  if (!ms) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

export function timelineDetail(ev: WorkflowEvent): string {
  const parts: string[] = []
  if (ev.task_id) parts.push('任务 ' + String(ev.task_id).slice(0, 80))
  if (ev.role) parts.push('角色 ' + roleMeta(String(ev.role)).label)
  if (ev.status) parts.push('状态 ' + String(ev.status).slice(0, 40))
  if (typeof ev.attempt === 'number') parts.push('第 ' + ev.attempt + ' 次尝试')
  if (typeof ev.file_count === 'number') parts.push(ev.file_count + ' 个文件')
  return parts.join(' · ')
}

export function timelineTime(ev: WorkflowEvent): string {
  const date = new Date(String(ev.ts || ''))
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN')
}

/** 验收条件引用的任务结果（拍平后的 results 表），无引用返回 null。 */
export function criterionTaskResultOf(
  evidence: string[],
  results: Record<string, Record<string, unknown>>,
): { status: string; conclusion: string } | null {
  const ref = evidence.find(value => value.startsWith('task:'))
  if (!ref) return null
  const result = results[ref.slice(5)]
  return result ? { status: String(result.status || 'unknown'),
    conclusion: String(result.conclusion || result.summary || result.error || '') } : null
}
