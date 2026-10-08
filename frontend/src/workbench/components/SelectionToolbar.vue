<script setup lang="ts">
// P2 选区 AI 浮条：编辑器中非空选区时，在选区末端弹出的动作条（fixed 定位，Teleport 到 body）。
// 解释 / Review / 提问 → ReAct agent（可检索项目）；改写 → 直连 LLM 快通道（可一键替换）。
import { computed } from 'vue'
import { useWorkbench } from '../composables/workbench'

const {
  activeSelection, aiStreaming, askComposing,
  runAi, startAskCompose,
} = useWorkbench()

const visible = computed(() =>
  !!activeSelection.value && !aiStreaming.value && !askComposing.value,
)

// 选区下方优先；贴近视口底沿时翻到选区上方；水平方向夹到视口内。
const style = computed(() => {
  const s = activeSelection.value
  if (!s) return {}
  const W = 268 // 浮条近似宽度，用于右边界夹取
  const flipUp = s.y > window.innerHeight - 96
  const left = Math.max(8, Math.min(s.x + 10, window.innerWidth - W - 8))
  const top = flipUp ? s.y - 42 : s.y + 8
  return { left: `${left}px`, top: `${top}px` }
})

const meta = computed(() => {
  const s = activeSelection.value
  if (!s) return ''
  const lines = s.endLine - s.startLine + 1
  return `第 ${s.startLine}–${s.endLine} 行 · ${lines} 行 · ${s.text.length} 字符`
})

// mousedown.prevent：避免点按钮时编辑器失焦、选区视觉被清
function press(_e: MouseEvent) {}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible && activeSelection"
      class="sel-ai-bar"
      :style="style"
      @mousedown.prevent="press"
    >
      <div class="sel-ai-meta">{{ meta }}</div>
      <div class="sel-ai-actions">
        <button class="sel-ai-btn" title="让 AI 解释选中代码（结合项目检索）" @click="runAi('explain')">
          <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
            <circle cx="5.4" cy="5.4" r="3.6" fill="none" stroke="currentColor" stroke-width="1.1" />
            <path d="M8.1 8.1 L11.2 11.2" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" />
          </svg>
          解释
        </button>
        <button class="sel-ai-btn" title="审查潜在 bug、边界条件与可读性问题" @click="runAi('review')">
          <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
            <path d="M6.5 1.6 L11.4 3.4 V6.6 C11.4 9.2 9.4 10.9 6.5 11.8 C3.6 10.9 1.6 9.2 1.6 6.6 V3.4 Z" fill="none" stroke="currentColor" stroke-width="1.05" stroke-linejoin="round"/>
            <path d="M4.4 6.4 L5.8 7.8 L8.7 4.9" fill="none" stroke="currentColor" stroke-width="1.05" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          Review
        </button>
        <button
          v-if="activeSelection.writable"
          class="sel-ai-btn sel-ai-btn-accent"
          title="让 AI 改写这段代码，结果可一键替换选区（不会自动保存）"
          @click="runAi('rewrite')"
        >
          <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
            <path d="M8.4 1.8 L11.2 4.6 L4.2 11.6 H1.8 V9.2 Z" fill="none" stroke="currentColor" stroke-width="1.05" stroke-linejoin="round"/>
            <path d="M7.2 3 L10 5.8" stroke="currentColor" stroke-width="1.05"/>
          </svg>
          改写
        </button>
        <button class="sel-ai-btn sel-ai-btn-ask" title="就选中代码自由提问" @click="startAskCompose">
          <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
            <path d="M1.6 2.2 H11.4 V8.8 H5.2 L2.6 10.9 V8.8 H1.6 Z" fill="none" stroke="currentColor" stroke-width="1.05" stroke-linejoin="round"/>
          </svg>
          提问
        </button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.sel-ai-bar {
  position: fixed;
  z-index: 1200;
  min-width: 196px;
  padding: 6px 6px 7px;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid var(--border-strong);
  border-radius: 9px;
  box-shadow: 0 10px 30px rgba(35, 52, 84, 0.18), 0 0 0 1px rgba(35, 52, 84, 0.08);
  backdrop-filter: blur(6px);
  user-select: none;
  animation: sel-ai-pop 0.12s ease-out;
}
@keyframes sel-ai-pop {
  from { opacity: 0; transform: translateY(3px); }
  to { opacity: 1; transform: translateY(0); }
}
.sel-ai-meta {
  font-size: 10px;
  color: var(--text-muted);
  padding: 0 4px 5px;
  white-space: nowrap;
  letter-spacing: 0.02em;
}
.sel-ai-actions {
  display: flex;
  gap: 3px;
}
.sel-ai-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--text);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
  transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease;
}
.sel-ai-btn:hover {
  background: var(--bg-hover);
  color: var(--text);
}
.sel-ai-btn-accent {
  color: #1c9e66;
}
.sel-ai-btn-accent:hover {
  background: rgba(28, 158, 102, 0.14);
  color: #15734c;
}
.sel-ai-btn-ask {
  color: #2f6fed;
}
.sel-ai-btn-ask:hover {
  background: rgba(47, 111, 237, 0.12);
  color: #2560d4;
}
.sel-ai-btn:focus-visible { outline: 2px solid #2f6fed88; outline-offset: 2px; }
.sel-ai-btn:active { transform: translateY(1px); }
@media (max-width: 520px) {
  .sel-ai-bar { min-width: 0; max-width: calc(100vw - 16px); }
  .sel-ai-actions { flex-wrap: wrap; }
  .sel-ai-btn { flex: 1 1 auto; justify-content: center; }
  .sel-ai-meta { white-space: normal; line-height: 1.4; }
}
@media (prefers-reduced-motion: reduce) {
  .sel-ai-bar, .sel-ai-btn { animation: none; transition: none; }
}
</style>
