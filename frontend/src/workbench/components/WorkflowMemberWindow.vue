<script setup lang="ts">
// 单个 subagent 的独立小窗（2026-10-22）。
//
// 诉求：工作流执行时，团队里每个成员（research / implement / verify …）的
// 思考、工具调用、观察、结论，各自占一个可拖动的小浮窗——「每个 subagent 一个
// 小窗口」。总览大窗（WorkflowWindow）仍在，管流程/阶段/审批/验收；这里只把
// 某一个成员的实时轨迹拆出来单独盯，也顺带把「子代理上下文本就隔离」这件事在
// 视觉上做实（一个代理一个窗，而不是全塞进一张卡）。
//
// 触发方式：**点成员才弹**（不是运行即自动弹）——由 WorkflowCard 的成员行点击
// 挂载本组件，关闭即从「已弹出」集合里移除。
//
// 数据全部来自传入的 member（useWorkflowCard 的 members computed 里的一项），
// 渲染用的 stepTitle/stepSummary/STEP_GLYPH/fmtMs 都是 workflow-format 的纯函数，
// 直接 import，无需把卡片内部函数往下钻。重试这类会改后端状态的动作 emit 上抛，
// 让卡片（持有 SSE 与 hydrate 能力）去做。
//
// Teleport 到 body + position:fixed：浮窗独立于总览窗存在，总览窗被最小化/隐藏
// （v-show，卡片仍挂载）时，已弹出的成员窗照常显示。
import { ref } from 'vue'
import { STEP_GLYPH, stepTitle, stepSummary, fmtMs, type MemberRow } from '../composables/workflow-format'

const props = defineProps<{
  member: MemberRow
  busy: boolean
  /** 级联偏移序号：多个窗同时弹出时错开左上角，避免完全重叠。 */
  index?: number
}>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'retry', id: string): void
}>()

const STATUS_TEXT: Record<string, string> = {
  pending: '待执行', running: '执行中', ok: '已完成', failed: '失败', blocked: '阻塞',
}

// ---- 拖动 + 置顶：标题栏按住移动；按下即把本窗抬到当前最高层。 ----
const popEl = ref<HTMLElement | null>(null)
const pos = ref<{ left: number; top: number } | null>(null)
const z = ref(BASE_Z + (++zSeq))
let dragging = false
let grabX = 0, grabY = 0
function onHeadDown(e: PointerEvent) {
  if ((e.target as HTMLElement).closest('.wf-mwin-btn')) return
  z.value = BASE_Z + (++zSeq) // 点击置顶
  dragging = true
  const el = popEl.value
  if (el) {
    const r = el.getBoundingClientRect()
    pos.value = { left: r.left, top: r.top }
    grabX = e.clientX - r.left
    grabY = e.clientY - r.top
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}
function onMove(e: PointerEvent) {
  if (!dragging || !pos.value) return
  const w = popEl.value?.offsetWidth || 440
  const h = popEl.value?.offsetHeight || 360
  pos.value = {
    left: Math.min(Math.max(8, e.clientX - grabX), window.innerWidth - w - 8),
    top: Math.min(Math.max(8, e.clientY - grabY), window.innerHeight - Math.min(h, 120) - 8),
  }
}
function onUp() {
  dragging = false
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
}
function popStyle() {
  const idx = props.index || 0
  const base = pos.value
    ? { left: pos.value.left + 'px', top: pos.value.top + 'px' }
    : { left: (28 + idx * 30) + 'px', top: (72 + idx * 30) + 'px' }
  return { ...base, right: 'auto', zIndex: String(z.value) }
}
</script>

<script lang="ts">
// 模块级：所有成员窗共享的层号计数与基准 z（高于总览窗 62、低于审批门弹窗 1100）。
const BASE_Z = 70
let zSeq = 0
</script>

<template>
  <Teleport to="body">
    <div
      ref="popEl"
      class="wf-mwin"
      :class="`wf-mwin-${member.status}`"
      :style="popStyle()"
      role="dialog"
      :aria-label="`${member.label} 子代理窗口`"
    >
      <div class="wf-mwin-head" @pointerdown="onHeadDown">
        <span class="wf-mwin-avatar">{{ member.avatar }}</span>
        <span class="wf-mwin-title">
          <b>{{ member.label }}</b>
          <em class="wf-mwin-state">{{ STATUS_TEXT[member.status] || member.status }}</em>
        </span>
        <small v-if="member.elapsedMs" class="wf-mwin-elapsed">{{ fmtMs(member.elapsedMs) }}</small>
        <span class="wf-mwin-actions">
          <button class="wf-mwin-btn wf-mwin-x" title="关闭（子代理继续在后台运行）" @click="emit('close')">×</button>
        </span>
      </div>
      <div class="wf-mwin-body">
        <p class="wf-mwin-task">{{ member.task }}</p>

        <div v-if="!member.steps.length && !(member.trace?.steps?.length) && member.status === 'running'" class="wf-mwin-wait">
          等待该成员的第一条输出<span class="wf-dots">…</span>
        </div>

        <div v-for="(step, i) in member.steps" :key="'l' + i" class="wf-step" :class="`wf-step-${step.type}`">
          <span class="wf-step-glyph">{{ STEP_GLYPH[step.type] || '·' }}</span>
          <div class="wf-step-body">
            <span class="wf-step-title">{{ stepTitle(step) }}</span>
            <span class="wf-step-summary">{{ stepSummary(step) }}</span>
            <pre class="wf-step-full">{{ step.text }}</pre>
          </div>
        </div>

        <div v-if="!member.steps.length && member.trace?.steps?.length" class="wf-step-hint">该成员的实时轨迹已折叠，保留收尾工具轨迹：</div>
        <div v-for="(row, i) in (member.trace?.steps || [])" :key="'t' + i" class="wf-step wf-step-action">
          <span class="wf-step-glyph">↗</span>
          <div class="wf-step-body">
            <span class="wf-step-title">{{ String(row.action || row.tool || '工具') }}</span>
            <span v-if="row.obs" class="wf-step-summary">{{ String(row.obs) }}</span>
          </div>
        </div>

        <div v-if="member.conclusion" class="wf-mwin-conclusion"><b>结论</b>{{ member.conclusion }}</div>
        <div v-if="member.error" class="wf-mwin-error"><b>错误</b>{{ member.error }}
          <button class="wf-mini-btn" :disabled="busy" @click="emit('retry', member.id)">重试该成员</button>
        </div>
        <button
          v-else-if="member.status === 'failed' || member.status === 'blocked'"
          class="wf-mini-btn"
          :disabled="busy"
          @click="emit('retry', member.id)"
        >重试该成员</button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.wf-mwin {
  position: fixed;
  width: 440px; max-width: calc(100vw - 24px);
  max-height: min(70vh, 560px);
  display: flex; flex-direction: column;
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 16px 44px rgba(35, 52, 84, 0.22);
  overflow: hidden;
  animation: wf-mwin-in .2s cubic-bezier(.2,.8,.2,1) both;
}
@keyframes wf-mwin-in { from { opacity: 0; transform: translateY(6px) scale(.985); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) { .wf-mwin { animation: none; } }
.wf-mwin-running { border-color: rgba(37,96,212,.5); }
.wf-mwin-ok { border-color: rgba(52,168,112,.45); }
.wf-mwin-failed, .wf-mwin-blocked { border-color: rgba(214,78,78,.5); }
.wf-mwin-head {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; flex: 0 0 auto;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, var(--bg-raised), var(--bg-hover));
  cursor: grab; user-select: none; touch-action: none;
}
.wf-mwin-head:active { cursor: grabbing; }
.wf-mwin-avatar {
  width: 22px; height: 22px; border-radius: 6px; flex: 0 0 auto;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: var(--fs-cap); font-weight: 700;
  background: var(--bg-selected); color: var(--accent); border: 1px solid var(--border);
}
.wf-mwin-title { display: inline-flex; align-items: baseline; gap: 7px; min-width: 0; flex: 1; }
.wf-mwin-title b { font-size: var(--fs-sm); font-weight: 600; color: var(--text); }
.wf-mwin-state { font-style: normal; font-size: var(--fs-cap); color: var(--text-faint); }
.wf-mwin-running .wf-mwin-state { color: var(--accent); }
.wf-mwin-ok .wf-mwin-state { color: var(--green); }
.wf-mwin-failed .wf-mwin-state, .wf-mwin-blocked .wf-mwin-state { color: var(--danger); }
.wf-mwin-elapsed { font-size: var(--fs-cap); color: var(--text-faint); font-variant-numeric: tabular-nums; }
.wf-mwin-actions { display: inline-flex; align-items: center; }
.wf-mwin-btn { background: none; border: 0; color: var(--text-muted); cursor: pointer; border-radius: 5px; padding: 1px 7px; font-size: var(--fs-lead); line-height: 1.2; transition: color .16s ease, background .16s ease; }
.wf-mwin-btn:hover { color: var(--text); background: var(--bg-selected); }
.wf-mwin-x:hover { color: var(--danger); }
.wf-mwin-body { flex: 1; min-height: 0; overflow-y: auto; padding: 9px 11px; display: grid; gap: 5px; align-content: start; }
.wf-mwin-task { margin: 0 0 3px; font-size: var(--fs-cap); color: var(--text-muted); line-height: 1.5; }
.wf-mwin-wait { font-size: var(--fs-sm); color: var(--text-faint); padding: 3px 0; }
.wf-mwin-conclusion, .wf-mwin-error {
  margin-top: 3px; padding: 7px 9px; border-radius: 7px;
  font-size: var(--fs-sm); line-height: 1.55; display: grid; gap: 3px;
}
.wf-mwin-conclusion b, .wf-mwin-error b { font-size: var(--fs-cap); }
.wf-mwin-conclusion { background: rgba(52,168,112,.08); color: var(--text); }
.wf-mwin-conclusion b { color: var(--green); }
.wf-mwin-error { background: rgba(214,78,78,.07); color: var(--danger); }
.wf-mwin-error .wf-mini-btn { justify-self: start; margin-top: 3px; }
/* 步骤渲染沿用卡片里的类名与视觉（此处自带一份精简样式，避免跨组件 :deep 依赖） */
.wf-step { display: flex; gap: 7px; position: relative; }
.wf-step-glyph { width: 18px; flex: 0 0 auto; text-align: center; font-size: var(--fs-cap); color: var(--text-faint); padding-top: 1px; }
.wf-step-action .wf-step-glyph { color: var(--accent); }
.wf-step-observation .wf-step-glyph { color: var(--green); }
.wf-step-thought .wf-step-glyph { color: var(--text-muted); }
.wf-step-body { min-width: 0; display: grid; gap: 1px; }
.wf-step-title { font-size: var(--fs-cap); font-weight: 600; color: var(--text); }
.wf-step-summary { font-size: var(--fs-sm); line-height: 1.5; color: var(--text-muted); white-space: pre-wrap; word-break: break-word; }
.wf-step-full { display: none; margin: 3px 0 0; padding: 7px 9px; background: var(--bg-raised); border: 1px solid var(--border); border-radius: 7px; font: inherit; font-size: var(--fs-cap); line-height: 1.55; color: var(--text-muted); white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow-y: auto; }
.wf-step:hover .wf-step-full { display: block; }
.wf-step-hint { font-size: var(--fs-cap); color: var(--text-faint); }
.wf-mini-btn { border: 1px solid var(--border); border-radius: 6px; background: var(--bg-raised); color: var(--text-muted); font-size: var(--fs-cap); padding: 2px 9px; cursor: pointer; }
.wf-mini-btn:hover:not(:disabled) { color: var(--text); border-color: var(--border-strong); }
.wf-mini-btn:disabled { opacity: .5; cursor: default; }
.wf-dots { animation: wf-mwin-blink 1.1s infinite; }
@keyframes wf-mwin-blink { 50% { opacity: .25; } }
@media (prefers-reduced-motion: reduce) { .wf-dots { animation: none; } }
</style>
