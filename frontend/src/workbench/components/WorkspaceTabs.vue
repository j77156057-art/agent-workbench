<script setup lang="ts">
import { useWorkbench } from '../composables/workbench'
import type { WorkspaceView } from '../composables/workbench'

const { workspace, setWorkspace, tabs } = useWorkbench()

const views: { key: WorkspaceView; name: string }[] = [
  { key: 'overview', name: '概览' },
  { key: 'code', name: '代码' },
  { key: 'assets', name: '素材' },
]
const HOTKEY: Record<WorkspaceView, number> = { overview: 1, code: 2, assets: 3 }
const emit = defineEmits<{ (e: 'collapse'): void }>()

function pick(key: WorkspaceView) {
  if (key === 'code' && !tabs.value.length) return
  setWorkspace(key)
}
</script>

<template>
  <nav class="ws-bar" aria-label="工作区切换">
    <button
      v-for="item in views"
      :key="item.key"
      type="button"
      class="ws-tab"
      :class="{ 'ws-active': workspace === item.key }"
      :disabled="item.key === 'code' && !tabs.length"
      :aria-current="workspace === item.key ? 'page' : undefined"
      :aria-keyshortcuts="`Alt+${HOTKEY[item.key]}`"
      :title="item.key === 'code' && !tabs.length
        ? '先从左侧文件树打开一个文件（Alt+2 切回代码）'
        : `${item.name}（Alt+${HOTKEY[item.key]}）`"
      @click="pick(item.key)"
    >
      <svg v-if="item.key === 'overview'" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="m2.5 7 5.5-4.5L13.5 7v6.5h-11V7Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>
        <path d="M6.5 13.5V9h3v4.5" stroke="currentColor" stroke-width="1.2"/>
      </svg>
      <svg v-else-if="item.key === 'code'" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="m5 4.5-3.5 3.5L5 11.5m6-7 3.5 3.5-3.5 3.5M9 3 7 13" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <svg v-else width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <rect x="2" y="3" width="12" height="10" rx="1.5" stroke="currentColor" stroke-width="1.2"/>
        <circle cx="5.5" cy="6.5" r="1" stroke="currentColor" stroke-width="1.2"/>
        <path d="m3 12 3.5-3.5L9 11l2-2 2 2" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>
      </svg>
      <span class="ws-label">{{ item.name }}</span>
    </button>
    <button
      type="button"
      class="ws-collapse"
      title="收起工作区标签栏（Alt+1/2/3 仍可切换，顶栏「更多」可恢复）"
      aria-label="收起工作区标签栏"
      @click="emit('collapse')"
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="m4.5 10 3.5-3.5 3.5 3.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
  </nav>
</template>

<style scoped>
.ws-bar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 46px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
  overflow-x: auto;
  user-select: none;
}
.ws-tab {
  position: relative;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 46px;
  padding: 0 14px;
  border: 0;
  background: none;
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-size: 14px;
  font-weight: 500;
  line-height: 1.5;
  white-space: nowrap;
  cursor: pointer;
  transition: color .15s ease, background .15s ease;
}
.ws-tab:hover:not(:disabled) { color: var(--text); background: var(--bg-hover); }
.ws-active { color: var(--accent-text); font-weight: 600; }
.ws-active::after {
  content: '';
  position: absolute;
  right: 12px;
  bottom: 0;
  left: 12px;
  height: 2px;
  background: var(--accent-text);
}
.ws-tab:disabled { opacity: .5; cursor: not-allowed; }
.ws-label { display: inline; }
.ws-collapse {
  flex: none;
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  margin-left: auto;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  transition: color .15s ease, background .15s ease;
}
.ws-collapse:hover { color: var(--text); background: var(--bg-hover); }
.ws-tab:focus-visible, .ws-collapse:focus-visible { outline: 2px solid var(--accent-text); outline-offset: -4px; }
@media (max-width: 620px) {
  .ws-bar { gap: 2px; padding: 0 8px; }
  .ws-tab { gap: 6px; padding: 0 10px; }
}
@media (prefers-reduced-motion: reduce) {
  .ws-tab, .ws-collapse { transition: none; }
}
</style>
