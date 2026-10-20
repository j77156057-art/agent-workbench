<script setup lang="ts">
// 工作流历史弹层（薄封装）：状态标签/圆点样式/时间格式化内聚于此，
// 挂回卡片、中断、删除等副作用全部留在父组件，通过 emit 上抛。
import type { WorkflowSummary } from '../api'

defineProps<{
  open: boolean
  items: WorkflowSummary[]
  busy: boolean
  error: string
  /** 正在执行中断/删除的工作流 id（按钮 loading 与互斥禁用） */
  actingId: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'refresh'): void
  (e: 'reattach', item: WorkflowSummary): void
  (e: 'interrupt', item: WorkflowSummary): void
  (e: 'delete', item: WorkflowSummary): void
}>()

const WF_STATUS_LABEL: Record<string, string> = {
  generating_options: '正在生成方案', awaiting_choice: '等待选择方案',
  awaiting_research: '等待联网检索', researching: '联网调研中',
  planning: '任务规划中', awaiting_plan_approval: '等待计划审批',
  awaiting_approval: '等待审核', planned: '待执行',
  executing: '子代理执行中', reviewing: '汇总检查中',
  completed: '已完成', failed: '已失败', interrupted: '已中断',
}
const WF_TERMINAL = new Set(['completed', 'failed', 'interrupted'])

function wfStatusLabel(status: string): string {
  return WF_STATUS_LABEL[status] || status
}
function wfTerminal(item: WorkflowSummary): boolean {
  return WF_TERMINAL.has(item.status)
}
function wfDotKind(item: WorkflowSummary): 'run' | 'ok' | 'err' | 'stop' {
  if (item.status === 'completed') return 'ok'
  if (item.status === 'failed') return 'err'
  if (item.status === 'interrupted') return 'stop'
  return 'run'
}
function sessionTime(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const delta = Math.max(0, Date.now() - date.getTime())
  if (delta < 60_000) return '刚刚'
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`
  return date.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="cd-pop-mask wb-modal-backdrop" @click="emit('close')" />
    <div v-if="open" class="cd-session-pop cd-wf-pop wb-modal-shell" role="dialog" aria-modal="true" aria-label="工作流历史" @click.stop>
      <div class="cd-session-head">
        <div>
          <div class="cd-pop-title">工作流历史</div>
          <div class="cd-session-subtitle">当前项目发起过的工作流。点击条目可在对话中打开卡片；运行中可中断，已结束可删除记录与磁盘文件。</div>
        </div>
        <button class="cd-mini" :disabled="busy" title="刷新工作流列表" @click="emit('refresh')">刷新</button>
      </div>
      <div v-if="error" class="cd-session-error">{{ error }}</div>
      <div v-if="busy && !items.length" class="cd-session-empty">加载中…</div>
      <div v-else-if="!items.length && !error" class="cd-session-empty">还没有发起过工作流</div>
      <div v-for="item in items" :key="item.workflow_id" class="cd-session-item cd-wf-item">
        <button class="cd-session-main cd-wf-main" :disabled="!!actingId" @click="emit('reattach', item)">
          <span class="cd-wf-dot" :class="'cd-wf-dot-' + wfDotKind(item)" aria-hidden="true" />
          <span class="cd-session-copy">
            <strong>{{ item.request || '未命名工作流' }}</strong>
            <small>
              <em class="cd-wf-state" :class="'cd-wf-state-' + wfDotKind(item)">{{ wfStatusLabel(item.status) }}</em>
              <template v-if="item.task_count"> · {{ item.task_done ?? 0 }}/{{ item.task_count }} 个任务</template>
              <span v-if="sessionTime(item.updated_at || '')"> · {{ sessionTime(item.updated_at || '') }}</span>
            </small>
            <small v-if="wfTerminal(item) && (item.error || item.interrupt_reason)" class="cd-wf-sub">
              {{ item.error || item.interrupt_reason }}
            </small>
          </span>
        </button>
        <button
          v-if="!wfTerminal(item)"
          class="cd-session-delete cd-wf-act"
          title="中断这个工作流"
          :disabled="!!actingId"
          @click.stop="emit('interrupt', item)"
        >{{ actingId === item.workflow_id ? '…' : '中断' }}</button>
        <button
          v-else
          class="cd-session-delete cd-wf-act cd-wf-act-danger"
          title="删除记录与磁盘文件"
          :disabled="!!actingId"
          @click.stop="emit('delete', item)"
        >{{ actingId === item.workflow_id ? '…' : '删' }}</button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cd-pop-mask {
  position: fixed;
  inset: 0;
  z-index: 59;
}
.cd-pop-title { font-size: var(--fs-sm); font-weight: 600; color: var(--text); margin-bottom: 8px; }
.cd-mini {
  height: 21px; padding: 0 9px; font-size: var(--fs-cap);
  border: 1px solid var(--border-strong); border-radius: 5px;
  background: transparent; color: var(--text-muted); cursor: pointer;
}
.cd-mini:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
.cd-mini:disabled { opacity: .45; cursor: default; }
.cd-session-pop {
  position: fixed;
  right: 10px; bottom: 64px;
  width: 360px; max-width: calc(100vw - 24px);
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-radius: 8px; box-shadow: 0 14px 38px rgba(35,52,84,.2);
  padding: 10px; z-index: 60; animation: cd-history-in .18s cubic-bezier(.2,.8,.2,1) both;
}
.cd-session-head { display: flex; align-items: flex-start; gap: 8px; justify-content: space-between; }
.cd-session-subtitle { color: var(--text-faint); font-size: var(--fs-cap); line-height: 1.45; margin-top: -4px; }
.cd-session-error { padding: 6px 8px; color: var(--danger); background: #fff4f4; border: 1px solid #f2caca; border-radius: 5px; font-size: var(--fs-cap); }
.cd-session-empty { padding: 12px 5px; color: var(--text-faint); font-size: var(--fs-cap); text-align: center; }
.cd-session-item { display: flex; align-items: stretch; border-top: 1px solid var(--border); }
.cd-session-main { flex: 1; min-width: 0; display: flex; gap: 7px; align-items: center; padding: 8px 5px; border: 0; background: transparent; color: var(--text); text-align: left; cursor: pointer; }
.cd-session-main { border-radius: 6px; transition: background .15s ease, transform .15s ease; }.cd-session-main:hover { background: var(--bg-hover); transform: translateX(2px); }.cd-session-main:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.cd-session-main:disabled { opacity: .6; cursor: default; }
.cd-session-copy { min-width: 0; display: block; }
.cd-session-copy strong, .cd-session-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-session-copy strong { font-size: var(--fs-sm); font-weight: 600; }
.cd-session-copy small { margin-top: 2px; color: var(--text-faint); font-size: var(--fs-cap); }
.cd-session-delete { align-self: center; margin-right: 5px; width: 22px; height: 22px; border: 0; background: transparent; color: var(--text-faint); cursor: pointer; font-size: var(--fs-h2); border-radius: 5px; transition: color .15s ease, background .15s ease, transform .15s ease; }
.cd-session-delete:hover { color: var(--danger); background: rgba(214,78,78,.07); transform: translateY(-1px); }.cd-session-delete:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.cd-session-delete:disabled { cursor: default; opacity: .5; }
/* 工作流历史弹层 */
.cd-wf-pop { max-height: min(70vh, 520px); }
.cd-wf-dot { flex: 0 0 auto; align-self: center; width: 8px; height: 8px; border-radius: 50%; }
.cd-wf-dot-run { background: var(--amber); box-shadow: 0 0 6px rgba(214,158,46,.6); animation: cd-wf-pulse 1.4s ease-in-out infinite; }
.cd-wf-dot-ok { background: var(--green); }
.cd-wf-dot-err { background: var(--danger); }
.cd-wf-dot-stop { background: var(--text-faint); }
@keyframes cd-wf-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
.cd-wf-state { font-style: normal; }
.cd-wf-state-run { color: var(--amber); }
.cd-wf-state-ok { color: var(--green); }
.cd-wf-state-err { color: var(--danger); }
.cd-wf-state-stop { color: var(--text-faint); }
.cd-wf-sub { display: block; color: var(--danger); margin-top: 2px; }
.cd-wf-act {
  width: auto; min-width: 34px; height: auto; align-self: center;
  margin-right: 5px; padding: 3px 8px; border: 1px solid var(--border);
  border-radius: 5px; font-size: var(--fs-cap); color: var(--text-muted);
}
.cd-wf-act:hover:not(:disabled) { color: var(--amber); border-color: var(--amber); }
.cd-wf-act-danger:hover:not(:disabled) { color: var(--danger); border-color: var(--danger); }
@keyframes cd-history-in { from { opacity: 0; transform: translateY(5px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@media (max-width: 560px) { .cd-session-pop { right: 6px; bottom: 52px; width: calc(100vw - 12px); } .cd-session-subtitle { max-width: 70vw; } }
@media (prefers-reduced-motion: reduce) { .cd-session-pop, .cd-session-main, .cd-session-delete { animation: none; transition: none; } .cd-wf-dot-run { animation: none; } }
</style>
