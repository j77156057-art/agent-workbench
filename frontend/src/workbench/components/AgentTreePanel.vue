<script setup lang="ts">
import AgentTree from './AgentTree.vue'
import { closeAgentTree, openAgentContext } from '../uiPanels'

defineProps<{ visible: boolean; sessionId: string }>()

// 点击任意节点（含根）直接切右栏到那个 Agent 的上下文；不新开窗口、不换项目。
function openNode(id: string, label: string) {
  openAgentContext(id, label)
}
</script>

<template>
  <aside
    v-if="visible"
    class="atp"
    data-agent-tree-panel
    role="complementary"
    aria-label="Agent 树与任务编排"
  >
    <header class="atp-head">
      <div class="atp-identity">
        <span class="atp-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <div>
          <strong>Agent 树</strong>
          <span>{{ sessionId || '未选择会话' }}</span>
        </div>
      </div>
      <small class="atp-kind">运行时编排 · 非长期记忆</small>
      <button
        class="atp-close"
        type="button"
        aria-label="关闭 Agent 树"
        data-agent-tree-close
        @click="closeAgentTree"
      >
        ×
      </button>
    </header>
    <div class="atp-body">
      <AgentTree :session-id="sessionId" selected-id="" @select="openNode" />
    </div>
  </aside>
</template>

<style scoped>
.atp {
  position: fixed;
  inset: 0 0 0 auto;
  z-index: var(--mi-z-panel);
  display: flex;
  flex-direction: column;
  /* 720px 会把节点卡片挤成单行省略；宽屏撑到 920，窄屏按视口比例回落。 */
  width: min(920px, 68vw);
  overflow: hidden;
  border-left: var(--mi-border-width) solid var(--mi-border);
  background: var(--mi-bg);
  box-shadow: var(--mi-shadow-panel);
  color: var(--mi-text);
  color-scheme: dark;
  font-family: var(--mi-font-ui);
  font-size: var(--mi-fs-body);
  line-height: var(--mi-lh-body);
  isolation: isolate;
}
.atp-head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--mi-space-3);
  padding: var(--mi-space-4) var(--mi-space-5);
  border-bottom: var(--mi-border-width) solid var(--mi-border-subtle);
}
.atp-identity { display: flex; min-width: 0; align-items: center; gap: var(--mi-space-3); }
.atp-identity > div { min-width: 0; }
.atp-identity strong {
  display: block;
  font-size: var(--mi-fs-title);
  line-height: var(--mi-lh-title);
  font-weight: var(--mi-weight-semibold);
}
.atp-identity span {
  display: block;
  overflow: hidden;
  color: var(--mi-text-muted);
  font-size: var(--mi-fs-caption);
  line-height: var(--mi-lh-caption);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.atp-mark { position: relative; flex: 0 0 var(--mi-space-6); width: var(--mi-space-6); height: var(--mi-space-6); }
.atp-mark i { position: absolute; width: var(--mi-state-mark-size); height: var(--mi-state-mark-size); border: var(--mi-border-width) solid var(--mi-border-strong); border-radius: var(--mi-radius-pill); }
.atp-mark i:nth-child(1) { top: 0; left: 50%; margin-left: calc(var(--mi-state-mark-size) / -2); }
.atp-mark i:nth-child(2) { bottom: 0; left: 0; }
.atp-mark i:nth-child(3) { right: 0; bottom: 0; }
.atp-mark::before,
.atp-mark::after {
  content: '';
  position: absolute;
  top: 38%;
  width: var(--mi-border-width);
  height: 34%;
  background: var(--mi-border-strong);
}
.atp-mark::before { left: 30%; transform: rotate(24deg); }
.atp-mark::after { right: 30%; transform: rotate(-24deg); }
.atp-kind { flex: 0 0 auto; margin-left: auto; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); white-space: nowrap; }
.atp-close {
  flex: 0 0 var(--mi-icon-button);
  width: var(--mi-icon-button);
  height: var(--mi-icon-button);
  padding: 0;
  border: var(--mi-border-width) solid transparent;
  border-radius: var(--mi-radius-control);
  background: transparent;
  color: var(--mi-text-secondary);
  font-family: inherit;
  font-size: var(--mi-fs-title);
  line-height: 1;
  cursor: pointer;
}
.atp-close:hover { border-color: var(--mi-border); background: var(--mi-hover); color: var(--mi-text); }
.atp-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding: var(--mi-space-5);
  scrollbar-color: var(--mi-border-strong) transparent;
}
.atp :deep(button:focus-visible), .atp :deep(textarea:focus-visible), .atp :deep(summary:focus-visible) {
  outline: var(--mi-focus-width) solid var(--mi-accent);
  outline-offset: var(--mi-space-1);
}
@media (max-width: 1439px) { .atp { width: min(840px, 74vw); } }
@media (max-width: 1199px) { .atp { width: min(720px, 100vw); } }
@media (max-width: 860px) {
  .atp { width: 100vw; }
  .atp-head, .atp-body { padding-inline: var(--mi-space-4); }
  .atp-kind { display: none; }
}
</style>
