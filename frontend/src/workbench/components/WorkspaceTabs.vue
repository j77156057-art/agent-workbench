<script setup lang="ts">
// 主区工作区标签条（Godot 式上下文切换）：概览 / 代码 为主切换；
// 素材中心不进主标签，收成右端一颗图标（Alt+3 仍可直达）—— 它不是
// 每次开工作台都要看的视图，占一个等宽文字标签只会稀释主切换。
// 最右还有一颗「收起」：整条标签栏可以从布局里彻底消失（零痕迹），
// 恢复入口在顶栏「更多」；Alt+1/2/3 始终可用，不依赖这条栏。
import { useWorkbench } from '../composables/workbench'
import type { WorkspaceView } from '../composables/workbench'

const { workspace, setWorkspace, tabs } = useWorkbench()

const real: { key: WorkspaceView; name: string; icon: string }[] = [
  { key: 'overview', name: '概览', icon: 'home' },
  { key: 'code', name: '代码', icon: 'code' },
  { key: 'assets', name: '素材', icon: 'assets' },
]

const HOTKEY: Record<string, number> = { overview: 1, code: 2, assets: 3 }

const emit = defineEmits<{ (e: 'collapse'): void }>()

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
        'ws-tab--utility': item.key === 'assets',
      }"
      :disabled="item.key === 'code' && !tabs.length"
      :title="item.key === 'code' && !tabs.length
        ? '先从左侧文件树打开一个文件（Alt+2 切回代码）'
        : item.key === 'assets' ? '素材中心（Alt+3）' : `${item.name}（Alt+${HOTKEY[item.key]}）`"
      :aria-label="item.key === 'assets' ? '素材中心' : undefined"
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
      <span class="ws-label">{{ item.name }}</span>
    </button>
    <button
      type="button"
      class="ws-collapse"
      title="收起整条标签栏（Alt+1/2/3 仍可切换视图，顶栏「更多」可恢复）"
      aria-label="收起工作区标签栏"
      @click="emit('collapse')"
    >
      <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
        <path d="M3.2 8.4 L6.5 5.1 L9.8 8.4" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M2.6 10.8 H10.4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
      </svg>
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

/* 素材中心：不占主切换的文字标签位，收到最右端做一颗静默图标。
   分隔线把它和「概览 / 代码」隔开；激活时只亮图标，不再画底部条。
   文字标签保留在 DOM 里但视觉隐藏（clip），读屏与整页守卫仍能读到「素材」。 */
.ws-tab--utility {
  margin-left: auto;
  padding: 0 9px;
  color: var(--text-faint);
  border-color: transparent;
  background: transparent;
  box-shadow: none;
}
.ws-tab--utility::before {
  content: '';
  position: absolute;
  left: -7px;
  top: 7px;
  bottom: 7px;
  width: 1px;
  background: var(--border);
}
.ws-tab--utility:hover:not(:disabled) {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  transform: none;
}
.ws-tab--utility.ws-active {
  color: var(--accent);
  background: transparent;
  border-color: transparent;
  box-shadow: none;
}
.ws-tab--utility.ws-active::after { display: none; }
.ws-tab--utility .ws-label {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  white-space: nowrap;
  border: 0;
}
/* 收起整条标签栏：与素材图标同组贴右；点后整行连按钮一起消失（恢复见顶栏「更多」）。 */
.ws-collapse {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 27px;
  height: 26px;
  margin-left: 2px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  transition: color .16s var(--ease-standard), background .16s var(--ease-standard);
}
.ws-collapse:hover {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, transparent);
}
.ws-collapse:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 1px; }

@keyframes ws-active-in {
  from { opacity: 0; transform: scaleX(.45); }
  to { opacity: 1; transform: scaleX(1); }
}
@media (prefers-reduced-motion: reduce) {
  .ws-tab, .ws-collapse, .ws-active::after { transition: none; animation: none; }
}
</style>
