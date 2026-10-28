<script setup lang="ts">
// 「查看改动」差异弹窗：把某一轮工具写文件的前后完整内容渲染成统一 diff。
// 数据来自 observation 事件的 file_diffs（结构化字段，非正文二次解析）：
//   - 正常条目带 old/new 全文 → 用 diffLines（与「AI 改写」同一套 LCS 引擎）逐行渲染；
//   - 超大文件后端只回摘要 diff、不下发全文（truncated=true）→ 回退纯文本摘要，
//     诚实告诉用户「内容过大只给摘要」，不假装能逐行对照。
// 只做展示，不写回编辑器、不触发保存——落盘由工具本身已完成。
import { computed } from 'vue'
import type { FileDiffEntry } from '../composables/chat-types'
import { diffLines } from '../diff'

const props = defineProps<{
  open: boolean
  entries: FileDiffEntry[]
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

interface RenderedFile {
  path: string
  kind: FileDiffEntry['kind']
  truncated: boolean
  rows: ReturnType<typeof diffLines>['rows']
  stats: ReturnType<typeof diffLines>['stats']
  summary: string
}

const files = computed<RenderedFile[]>(() =>
  (props.entries || []).map((e) => {
    const hasFull = typeof e.old === 'string' && typeof e.new === 'string'
    if (hasFull && !e.truncated) {
      const d = diffLines(e.old as string, e.new as string)
      return { path: e.path, kind: e.kind, truncated: false, rows: d.rows, stats: d.stats, summary: e.diff }
    }
    // 超大 / 无全文：只呈现后端给的摘要 diff 文本
    return { path: e.path, kind: e.kind, truncated: true, rows: [], stats: { adds: 0, dels: 0, same: 0, unchanged: false }, summary: e.diff }
  }),
)

// 全部一致时头部给个提示（多数是重复写同内容）
const allUnchanged = computed(
  () => files.value.length > 0 && files.value.every((f) => f.truncated ? false : f.stats.unchanged),
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
          <span class="fd-count">{{ files.length }} 个文件</span>
          <span v-if="allUnchanged" class="fd-equal">内容与原文件一致</span>
        </div>
        <button class="fd-iconbtn" title="关闭（Esc）" @click="emit('close')">
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
        </button>
      </header>

      <div class="fd-body">
        <section v-for="(f, fi) in files" :key="fi" class="fd-file">
          <div class="fd-file-head">
            <span class="fd-file-badge" :data-kind="f.kind">{{ f.kind === 'create' ? '新建' : '修改' }}</span>
            <span class="fd-file-path" :title="f.path">{{ f.path }}</span>
            <span v-if="!f.truncated" class="fd-file-stat">
              <span class="fd-add">+{{ f.stats.adds }}</span>
              <span class="fd-del">−{{ f.stats.dels }}</span>
            </span>
          </div>

          <div v-if="f.truncated" class="fd-fallback">
            <div class="fd-fallback-note">文件过大，仅提供改动摘要（未下发完整前后内容）</div>
            <pre class="fd-fallback-diff">{{ f.summary || '（无差异摘要）' }}</pre>
          </div>
          <div v-else class="fd-diff">
            <div v-for="(r, ri) in f.rows" :key="ri" class="fd-row" :class="`fd-${r.kind}`">
              <span class="fd-gno">{{ r.oldNo ?? '' }}</span>
              <span class="fd-gno">{{ r.newNo ?? '' }}</span>
              <span class="fd-sign">{{ r.kind === 'same' ? '' : r.kind === 'del' ? '−' : '+' }}</span>
              <code class="fd-code">{{ r.text || ' ' }}</code>
            </div>
          </div>
        </section>
      </div>

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

.fd-body {
  flex: 1 1 auto;
  overflow: auto;
  background: #f6f8fb;
  padding: 4px 0 12px;
}
.fd-file { border-bottom: 1px solid var(--border); }
.fd-file:last-child { border-bottom: 0; }
.fd-file-head {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 16px;
  background: color-mix(in srgb, var(--text) 5%, var(--bg-raised));
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-sm);
}
.fd-file-badge {
  flex: 0 0 auto;
  font-size: var(--fs-cap);
  padding: 0 6px;
  line-height: 17px;
  border-radius: 5px;
  border: 1px solid var(--border-strong);
  color: var(--text-muted);
}
.fd-file-badge[data-kind="create"] { color: var(--green); border-color: color-mix(in srgb, var(--green) 40%, var(--bg-raised)); }
.fd-file-path {
  flex: 1 1 auto;
  min-width: 0;
  font-family: 'Cascadia Code', Consolas, monospace;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fd-file-stat { flex: 0 0 auto; display: inline-flex; gap: 8px; font-family: 'Cascadia Code', Consolas, monospace; font-weight: 600; }
.fd-add { color: var(--green); }
.fd-del { color: var(--danger); }

.fd-diff {
  font-family: 'Cascadia Code', 'JetBrains Mono', Consolas, monospace;
  font-size: var(--fs-sm);
  line-height: 1.65;
  padding: 6px 0;
}
.fd-row { display: flex; align-items: stretch; white-space: pre; }
.fd-gno {
  flex: 0 0 42px;
  text-align: right;
  padding: 0 8px;
  color: var(--text-muted);
  user-select: none;
  font-size: var(--fs-cap);
}
.fd-sign { flex: 0 0 18px; text-align: center; user-select: none; }
.fd-code { flex: 1 1 auto; padding-right: 16px; color: #243044; }
.fd-del { background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); }
.fd-del .fd-sign, .fd-del .fd-code { color: var(--danger); }
.fd-add { background: color-mix(in srgb, var(--green) 12%, var(--bg-raised)); }
.fd-add .fd-sign, .fd-add .fd-code { color: var(--green); }

.fd-fallback { padding: 8px 16px; }
.fd-fallback-note { font-size: var(--fs-sm); color: var(--amber); margin-bottom: 6px; }
.fd-fallback-diff {
  margin: 0;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: var(--fs-sm);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-muted);
}

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
