<script setup lang="ts">
// 主区工作区标签条（Godot 式上下文切换）：概览 / 代码 / 素材。
import { useWorkbench } from '../composables/workbench'
import type { WorkspaceView } from '../composables/workbench'

const { workspace, setWorkspace, tabs } = useWorkbench()

const real: { key: WorkspaceView; name: string; icon: string }[] = [
  { key: 'overview', name: '概览', icon: 'home' },
  { key: 'code', name: '代码', icon: 'code' },
  { key: 'assets', name: '素材', icon: 'assets' },
]

const HOTKEY: Record<string, number> = { overview: 1, code: 2, assets: 3 }

function pick(key: WorkspaceView) {
  if (key === 'code' && !tabs.value.length) return
  setWorkspace(key)
}
</script>

<template>
  <nav class="ws-bar" aria-label="工作区切换">
    <button
      v-for="item in real"
      :key="item.key"
      type="button"
      class="ws-tab"
      :class="{
        'ws-active': workspace === item.key,
        'ws-disabled': item.key === 'code' && !tabs.length,
      }"
      :disabled="item.key === 'code' && !tabs.length"
      :title="item.key === 'code' && !tabs.length ? '先从左侧文件树打开一个文件（Alt+2 切回代码）' : `${item.name}（Alt+${HOTKEY[item.key]}）`"
      @click="pick(item.key)"
    >
      <svg v-if="item.icon === 'home'" width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
        <path d="M2 6 L6.5 2.2 L11 6 V10.6 Q11 11 10.6 11 H2.4 Q2 11 2 10.6 Z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>
        <path d="M5.2 11 V7.4 H7.8 V11" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>
      </svg>
      <svg v-else-if="item.icon === 'assets'" width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
        <rect x="1.6" y="2.4" width="9.8" height="8.2" rx="1" fill="none" stroke="currentColor" stroke-width="1.05"/>
        <circle cx="4.3" cy="4.9" r="0.95" fill="none" stroke="currentColor" stroke-width="1.05"/>
        <path d="M2.6 9.6 L5.2 7 L7.2 8.8 L8.8 7.4 L10.4 9" fill="none" stroke="currentColor" stroke-width="1.05" stroke-linejoin="round"/>
      </svg>
      <svg v-else width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
        <path d="M4.6 4.4 L2.2 6.5 L4.6 8.6 M8.4 4.4 L10.8 6.5 L8.4 8.6" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span>{{ item.name }}</span>
    </button>
  </nav>
</template>

<style scoped>
.ws-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 2px;
  padding: 0 12px;
  height: 38px;
  background: linear-gradient(180deg, var(--bg-raised) 0%, color-mix(in srgb, var(--bg-raised) 90%, var(--bg)) 100%);
  border-bottom: 1px solid var(--border);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.75);
  user-select: none;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: none;
}
.ws-bar::-webkit-scrollbar { display: none; height: 0; }
.ws-tab { flex: 0 0 auto; white-space: nowrap; }
.ws-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-size: var(--fs-sm);
  font-weight: 600;
  cursor: pointer;
  position: relative;
  transition: color .16s var(--ease-standard), background .16s var(--ease-standard), transform .16s var(--ease-spring), border-color .16s var(--ease-standard);
}
.ws-tab:hover:not(:disabled) { color: var(--text); background: color-mix(in srgb, var(--accent) 8%, transparent); transform: translateY(-1px); }
.ws-tab:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 1px; }
.ws-tab:active:not(:disabled) { transform: translateY(0); }
.ws-active {
  color: var(--accent-text);
  background: var(--bg-raised);
  border-color: var(--border);
  box-shadow: 0 3px 9px rgba(35,52,84,.08);
}
.ws-active::after {
  content: '';
  position: absolute;
  left: 13px;
  right: 13px;
  bottom: -1px;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  animation: ws-active-in .2s var(--ease-spring) both;
}
.ws-disabled { opacity: .62; cursor: default; }
.ws-disabled:hover { background: transparent; }
@keyframes ws-active-in {
  from { opacity: 0; transform: scaleX(.45); }
  to { opacity: 1; transform: scaleX(1); }
}
@media (prefers-reduced-motion: reduce) {
  .ws-tab, .ws-active::after { transition: none; animation: none; }
}
</style>
