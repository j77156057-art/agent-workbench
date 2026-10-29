<script setup lang="ts">
// 「查看改动」差异弹窗：把某一轮工具写文件的前后完整内容渲染成统一 diff。
// 渲染本体已抽到 DiffBody（右侧预览栏也用它），这里只保留弹窗壳。
// 只做展示，不写回编辑器、不触发保存——落盘由工具本身已完成。
import { computed } from 'vue'
import type { FileDiffEntry } from '../composables/chat-types'
import DiffBody from './DiffBody.vue'

const props = defineProps<{
  open: boolean
  entries: FileDiffEntry[]
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

// 全部一致时头部给个提示（多数是重复写同内容）。
// 口径与 diffLines 一致：先按行切分并归一 CRLF，再比较是否逐行相同。
const allUnchanged = computed(
  () => props.entries.length > 0 && props.entries.every((e) =>
    typeof e.old === 'string' && typeof e.new === 'string' && !e.truncated
    && e.old.replace(/\r\n/g, '\n') === e.new.replace(/\r\n/g, '\n')),
)

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open) emit('close')
}
</script>

<template>
  <!-- Teleport 到 body：ChatDock 挂在 .wb-chat-host 里，该祖先带 transform 的入场动画
       会成为 position:fixed 后代的包含块，把遮罩裁在 dock 框内、无法居中。挂到 body 才脱离。 -->
  <Teleport to="body">
  <div v-if="open" class="fd-overlay wb-modal-backdrop" tabindex="-1" @keydown="onKey" @mousedown.self="emit('close')">
    <section class="fd-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="文件改动预览">
      <header class="fd-top">
        <div class="fd-title">
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <path d="M2 4h6M2 7h8M2 10h5" stroke="var(--green)" stroke-width="1.2" stroke-linecap="round" />
            <path d="M9.5 8.5l2 2-2 2" fill="none" stroke="color-mix(in srgb, var(--green) 55%, var(--bg-raised))" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <h2>文件改动预览</h2>
          <span class="fd-count">{{ entries.length }} 个文件</span>
          <span v-if="allUnchanged" class="fd-equal">内容与原文件一致</span>
        </div>
        <button class="fd-iconbtn" title="关闭（Esc）" @click="emit('close')">
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
        </button>
      </header>

      <DiffBody :entries="entries" />

      <footer class="fd-foot">
        <span class="fd-foot-hint">改动已写入磁盘，此处仅供查看</span>
        <button class="fd-close" @click="emit('close')">关闭</button>
      </footer>
    </section>
  </div>
  </Teleport>
</template>

<style scoped>
.fd-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  z-index: 94;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 34px;
  animation: fd-overlay-in .18s ease-out both;
}
.fd-panel {
  width: min(960px, 95vw);
  height: min(680px, 88vh);
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 24px 70px rgba(35, 52, 84, 0.16);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: fd-panel-in .22s cubic-bezier(.2,.8,.2,1) both;
}
.fd-top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.fd-title { display: flex; align-items: center; gap: 9px; flex: 1; min-width: 0; }
.fd-title h2 { margin: 0; font-size: var(--fs-body); font-weight: 600; }
.fd-count { font-size: var(--fs-sm); color: var(--text-faint); }
.fd-equal { font-size: var(--fs-sm); color: var(--amber); }
.fd-iconbtn {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  width: 26px; height: 26px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto;
}
.fd-iconbtn:hover { background: var(--bg-hover); color: var(--text); }
.fd-iconbtn:focus-visible, .fd-close:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 2px; }

.fd-foot {
  flex: 0 0 auto;
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 12px;
  padding: 11px 16px;
  border-top: 1px solid var(--border);
  background: var(--bg);
}
.fd-foot-hint { margin-right: auto; font-size: var(--fs-cap); color: var(--text-faint); }
.fd-close {
  padding: 6px 18px; font-size: var(--fs-sm); border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border-strong); background: transparent; color: var(--text-muted);
}
.fd-close:hover { background: var(--bg-hover); color: var(--text); }

@keyframes fd-overlay-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes fd-panel-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@media (max-width: 700px) {
  .fd-overlay { padding: 14px; }
  .fd-panel { width: 100%; height: min(900px, 94vh); }
}
@media (max-width: 520px) {
  .fd-overlay { padding: 0; align-items: stretch; }
  .fd-panel { height: 100%; border-radius: 0; border-left: 0; border-right: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .fd-overlay, .fd-panel { animation: none; }
}
</style>
