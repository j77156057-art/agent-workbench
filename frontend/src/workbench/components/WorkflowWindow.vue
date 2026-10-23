<script setup lang="ts">
// 工作流独立小窗（2026-10-22）：把整张 WorkflowCard 从对话流里挪出来，做成
// 一个**非模态、可拖动**的浮窗，解决「多智能体团队卡片挤在聊天消息中间、看着像
// 和主 Agent 共用了上下文」的视觉耦合（后端子代理上下文本就是隔离的，只是前端
// 摆在一起造成错觉）。
//
// 复用现成的 popup/Teleport 惯用法（见 SceneRuntimePanel 的 .pb-pop）：Teleport 到
// body、fixed 浮层。但刻意**不做遮罩、不居中模态**——用户要能同时看到对话和团队
// 实况。卡片内部的人工审批门（WorkflowGateDialog）仍自带 Teleport 到 body、盖在
// 最上层，不受本窗可见性影响。
//
// 卡片本身（WorkflowCard）内容不改，只是换了个宿主。窗关闭 = 隐藏（v-show），
// 卡片仍挂载 → 它的 SSE 通道、gate 事件都不会断，避免「窗一关、后端停在审批门
// 里没人批」的孤儿问题。
import { ref } from 'vue'
import WorkflowCard from './WorkflowCard.vue'
import type { WorkflowState } from '../api'

const props = defineProps<{
  workflowId: string
  seed?: Partial<WorkflowState>
}>()
const emit = defineEmits<{
  (e: 'activity'): void
  (e: 'team', payload: { workflowId: string; active: boolean; members: Array<{ id: string; label: string; role: string; status: string; task: string }> }): void
  /** 人工审批门开合：透传给 ChatDock，据以锁定对话台发送/切换 */
  (e: 'gate', open: boolean): void
  (e: 'close'): void
}>()

const visible = ref(false)
const minimized = ref(false)
function show() { visible.value = true; minimized.value = false }
function hide() { visible.value = false }
function toggleMin() { minimized.value = !minimized.value }
function onClose() { hide(); emit('close') }
defineExpose({ show, hide })

// ---- 轻量拖动：标题栏按住移动。默认右上角锚定，一旦拖动切换为 left/top 显式定位。
const pos = ref<{ left: number; top: number } | null>(null)
let dragging = false
let grabX = 0, grabY = 0
function onHeadDown(e: PointerEvent) {
  if ((e.target as HTMLElement).closest('.wf-win-btn')) return // 按钮不触发拖动
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
  const w = popEl.value?.offsetWidth || 520
  const h = popEl.value?.offsetHeight || 480
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
const popEl = ref<HTMLElement | null>(null)
const popStyle = () => pos.value
  ? { left: pos.value.left + 'px', top: pos.value.top + 'px', right: 'auto' }
  : {}
</script>

<template>
  <Teleport to="body">
    <div
      v-show="visible"
      ref="popEl"
      class="wf-win"
      :class="{ 'wf-win-min': minimized }"
      :style="popStyle()"
      role="dialog"
      aria-label="多智能体工作流"
    >
      <div class="wf-win-head" @pointerdown="onHeadDown">
        <span class="wf-win-title">
          <span class="wf-win-glyph" aria-hidden="true">⧉</span>
          多智能体工作流
        </span>
        <span class="wf-win-actions">
          <button class="wf-win-btn" :title="minimized ? '展开' : '最小化'" @click="toggleMin">{{ minimized ? '▢' : '—' }}</button>
          <button class="wf-win-btn wf-win-x" title="关闭（工作流继续在后台运行）" @click="onClose">×</button>
        </span>
      </div>
      <div v-show="!minimized" class="wf-win-body">
        <WorkflowCard
          v-if="workflowId"
          :key="workflowId"
          :workflow-id="workflowId"
          :seed="seed"
          @activity="emit('activity')"
          @team="(p) => emit('team', p)"
          @gate="(o: boolean) => emit('gate', o)"
        />
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.wf-win {
  position: fixed; z-index: 62;
  right: 16px; top: 56px;
  width: 520px; max-width: calc(100vw - 32px);
  max-height: calc(100vh - 80px);
  display: flex; flex-direction: column;
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 18px 50px rgba(35, 52, 84, 0.22);
  overflow: hidden;
  animation: wf-win-in .22s cubic-bezier(.2,.8,.2,1) both;
}
@keyframes wf-win-in { from { opacity: 0; transform: translateY(8px) scale(.98); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) { .wf-win { animation: none; } }
.wf-win-min { max-height: 40px; }
.wf-win-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px; flex: 0 0 auto;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, var(--bg-raised), var(--bg-hover));
  cursor: grab; user-select: none; touch-action: none;
}
.wf-win-head:active { cursor: grabbing; }
.wf-win-title { display: inline-flex; align-items: center; gap: 7px; font-size: var(--fs-sm); font-weight: 600; color: var(--text); }
.wf-win-glyph { color: var(--accent); font-size: var(--fs-lead); line-height: 1; }
.wf-win-actions { display: inline-flex; align-items: center; gap: 2px; }
.wf-win-btn { background: none; border: 0; color: var(--text-muted); cursor: pointer; border-radius: 5px; padding: 2px 8px; font-size: var(--fs-lead); line-height: 1; transition: color .16s ease, background .16s ease; }
.wf-win-btn:hover { color: var(--text); background: var(--bg-selected); }
.wf-win-x:hover { color: var(--danger); }
.wf-win-body { flex: 1; min-height: 0; overflow-y: auto; padding: 10px; }
/* 卡片在窗内去掉外边距，撑满浮窗宽度 */
.wf-win-body :deep(.wf-card) { margin: 0; width: 100%; }
</style>
