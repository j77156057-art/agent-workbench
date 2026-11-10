<script setup lang="ts">
// 右侧记忆侧栏：一个外壳 + 顶部横向小标签（卡片墙 / 上下文记忆）。
// 两个面板常驻挂载，切标签只换显示，各自滚动位置与内部状态都保留。
import { computed, onBeforeUnmount, ref } from 'vue'
import AgentWallPanel from './AgentWallPanel.vue'
import MemoryContextPanel from './MemoryContextPanel.vue'
import { closeRightSidebar, rightSidebarOpen, rightSidebarTab } from '../uiPanels'
import { usePanelWidth } from '../../shared/usePanelWidth'

const props = withDefaults(defineProps<{
  wallSessionId: string
  memorySessionId: string
  agentId?: string
  agentLabel?: string
  askHost?: boolean
}>(), { agentId: 'main', agentLabel: '平台助手', askHost: false })

const TABS = [
  { key: 'wall', label: '卡片墙', title: 'Agent 卡片墙：对话记录 / 记忆 / 使用记录' },
  { key: 'memory', label: '上下文记忆', title: '当前 Agent 的上下文注入与记忆库存' },
] as const

const shell = ref<HTMLElement | null>(null)
// ≤720px 面板整屏铺开，拖拽无意义，也不套用记忆的像素宽度。
const narrowQuery = window.matchMedia('(max-width: 720px)')
const narrow = ref(narrowQuery.matches)
function onNarrowChange() { narrow.value = narrowQuery.matches }
narrowQuery.addEventListener('change', onNarrowChange)
onBeforeUnmount(() => narrowQuery.removeEventListener('change', onNarrowChange))

const {
  width, dragging, onPointerDown, nudge, reset,
} = usePanelWidth(shell, {
  storageKey: 'docmind.rightSidebarWidth', min: 420, max: 960, mainMin: 280, edge: 'left',
})
const shellStyle = computed(() => (
  !narrow.value && width.value != null ? { width: `${width.value}px` } : undefined))
</script>

<template>
  <aside
    v-if="rightSidebarOpen"
    ref="shell"
    class="rs"
    :style="shellStyle"
    role="dialog"
    aria-label="记忆侧栏"
  >
    <div
      class="rs-resizer"
      :class="{ dragging }"
      role="separator"
      aria-orientation="vertical"
      aria-label="调整记忆侧栏宽度"
      :aria-valuenow="width ?? 720"
      aria-valuemin="420"
      aria-valuemax="960"
      tabindex="0"
      title="拖动调整宽度，双击恢复默认"
      @pointerdown="onPointerDown"
      @dblclick="reset"
      @keydown.left.prevent="nudge(-16)"
      @keydown.right.prevent="nudge(16)"
    />

    <header class="rs-head">
      <div class="rs-tabs" role="tablist" aria-label="记忆侧栏面板">
        <button
          v-for="item in TABS"
          :key="item.key"
          type="button"
          role="tab"
          class="rs-tab"
          :class="{ on: rightSidebarTab === item.key }"
          :aria-selected="rightSidebarTab === item.key"
          :title="item.title"
          @click="rightSidebarTab = item.key"
        >{{ item.label }}</button>
      </div>
      <button type="button" class="rs-close" aria-label="关闭记忆侧栏" @click="closeRightSidebar">×</button>
    </header>

    <div class="rs-pane" v-show="rightSidebarTab === 'wall'">
      <AgentWallPanel
        :visible="true"
        :session-id="props.wallSessionId"
        :ask-host="props.askHost"
        embedded
      />
    </div>
    <div class="rs-pane" v-show="rightSidebarTab === 'memory'">
      <MemoryContextPanel
        :visible="true"
        :session-id="props.memorySessionId"
        :agent-id="props.agentId"
        :agent-label="props.agentLabel"
        embedded
      />
    </div>
  </aside>
</template>

<style scoped>
/* 外壳沿用两个面板原有的 --mi-* 令牌，不引入新颜色/字号 */
.rs { position: fixed; inset: 0 0 0 auto; z-index: var(--mi-z-panel); width: min(720px, 100vw); display: flex; flex-direction: column; overflow: hidden; border-left: var(--mi-border-width) solid var(--mi-border); background: var(--mi-bg); box-shadow: var(--mi-shadow-panel); color: var(--mi-text); color-scheme: dark; font-family: var(--mi-font-ui); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); isolation: isolate; }
.rs *, .rs *::before, .rs *::after { box-sizing: border-box; }
.rs-resizer { position: absolute; inset: 0 auto 0 0; width: 8px; z-index: 3; cursor: col-resize; touch-action: none; outline: none; }
.rs-resizer::before { content: ''; position: absolute; inset: 0; }
.rs-resizer:hover::before, .rs-resizer.dragging::before, .rs-resizer:focus-visible::before { background: color-mix(in srgb, var(--mi-accent) 42%, transparent); }
.rs-head { display: flex; align-items: center; gap: var(--mi-space-2); flex: 0 0 auto; min-width: 0; padding: var(--mi-space-1) var(--mi-space-2); border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.rs-tabs { display: flex; align-items: center; gap: var(--mi-space-1); flex: 1 1 auto; min-width: 0; overflow-x: auto; overflow-y: hidden; white-space: nowrap; scrollbar-width: thin; }
.rs-tab { flex: 0 0 auto; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 3px var(--mi-space-2); border: var(--mi-border-width) solid transparent; border-radius: var(--mi-radius-control); background: transparent; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); cursor: pointer; }
.rs-tab:hover { background: var(--mi-hover); color: var(--mi-text); }
.rs-tab.on { background: var(--mi-surface); border-color: var(--mi-border); color: var(--mi-text); }
.rs-tab:focus-visible { outline: 2px solid var(--mi-accent); outline-offset: 1px; }
.rs-close { flex: 0 0 auto; width: var(--mi-icon-button); height: var(--mi-icon-button); padding: 0; border: var(--mi-border-width) solid transparent; border-radius: var(--mi-radius-control); background: transparent; color: var(--mi-text-secondary); font-size: var(--mi-fs-title); line-height: 1; cursor: pointer; }
.rs-close:hover { border-color: var(--mi-border); background: var(--mi-hover); color: var(--mi-text); }
.rs-pane { flex: 1 1 auto; min-height: 0; display: flex; }
@media (max-width: 720px) {
  .rs { width: 100vw; }
  .rs-resizer { display: none; }
}
</style>
