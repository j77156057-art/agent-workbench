<script setup lang="ts">
// P3：AI 改写差异预览。替换选区前先看统一 diff（原选区 → AI 结果），
// 接受才写回编辑器（沿用 applyRewrite 的陈旧坐标/只读护栏，不自动保存）。
import { computed, onBeforeUnmount, watch } from 'vue'
import { useWorkbench, askAlert } from '../composables/workbench'
import { diffLines } from '../diff'

const {
  rewriteDiff, closeRewriteDiff, acceptRewriteDiff,
} = useWorkbench()

const diff = computed(() => {
  const d = rewriteDiff.value
  if (!d) return null
  return diffLines(d.oldCode, d.newCode)
})

/** 行号偏移：选区从文件第 startLine 行开始，替换后新行也落在同一位置。 */
function oldLine(no: number | null): string {
  if (no == null || !rewriteDiff.value) return ''
  return String(rewriteDiff.value.startLine - 1 + no)
}
function newLine(no: number | null): string {
  if (no == null || !rewriteDiff.value) return ''
  return String(rewriteDiff.value.startLine - 1 + no)
}

async function accept() {
  const reason = acceptRewriteDiff()
  if (reason) await askAlert({ title: '无法替换选区', message: reason })
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeRewriteDiff()
}

watch(rewriteDiff, (d) => {
  if (d) window.addEventListener('keydown', onKey)
  else window.removeEventListener('keydown', onKey)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div v-if="rewriteDiff && diff" class="rd-overlay wb-modal-backdrop" @mousedown.self="closeRewriteDiff">
    <section class="rd-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="改写差异预览">
      <header class="rd-top">
        <div class="rd-title">
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <path d="M2 4h6M2 7h8M2 10h5" stroke="var(--green)" stroke-width="1.2" stroke-linecap="round" />
            <path d="M9.5 8.5l2 2-2 2" fill="none" stroke="color-mix(in srgb, var(--green) 55%, var(--bg-raised))" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <h2>改写差异预览</h2>
          <span class="rd-path" :title="rewriteDiff.turn.origin.path">
            {{ rewriteDiff.name }} · 第 {{ rewriteDiff.startLine }}–{{ rewriteDiff.endLine }} 行
          </span>
        </div>
        <button class="rd-iconbtn" title="关闭（Esc）" @click="closeRewriteDiff">
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
        </button>
      </header>

      <div v-if="rewriteDiff.instruction" class="rd-instr">
        <span class="rd-instr-tag">改写要求</span>{{ rewriteDiff.instruction }}
      </div>

      <div class="rd-statbar">
        <span class="rd-stat-same">{{ diff.stats.same }} 行未变</span>
        <span class="rd-stat-add">+{{ diff.stats.adds }}</span>
        <span class="rd-stat-del">−{{ diff.stats.dels }}</span>
        <span v-if="diff.stats.unchanged" class="rd-stat-equal">AI 结果与原选区完全一致，无需替换</span>
        <span class="rd-stat-hint">仅写入编辑器，接受后仍需 Ctrl+S 保存落盘</span>
      </div>

      <div class="rd-body">
        <div class="rd-row" v-for="(r, i) in diff.rows" :key="i" :class="`rd-${r.kind}`">
          <span class="rd-gno rd-gno-old">{{ oldLine(r.oldNo) }}</span>
          <span class="rd-gno rd-gno-new">{{ newLine(r.newNo) }}</span>
          <span class="rd-sign">{{ r.kind === 'same' ? '' : r.kind === 'del' ? '−' : '+' }}</span>
          <code class="rd-code">{{ r.text || ' ' }}</code>
        </div>
      </div>

      <footer class="rd-foot">
        <button class="rd-cancel" @click="closeRewriteDiff">取消</button>
        <button
          class="rd-accept"
          :disabled="diff.stats.unchanged"
          :title="diff.stats.unchanged ? '两侧内容一致，没有可替换的差异' : '用右侧结果替换编辑器中的原选区（不会自动保存）'"
          @click="void accept()"
        >
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 6.2 L5 8.6 L9.6 3.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
          接受替换
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.rd-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  z-index: 93;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 34px;
  animation: rd-overlay-in .18s ease-out both;
}
.rd-panel {
  width: min(900px, 95vw);
  height: min(640px, 88vh);
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 24px 70px rgba(35, 52, 84, 0.16);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: rd-panel-in .22s cubic-bezier(.2,.8,.2,1) both;
}
.rd-top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.rd-title { display: flex; align-items: center; gap: 9px; flex: 1; min-width: 0; }
.rd-title h2 { margin: 0; font-size: var(--fs-body); font-weight: 600; }
.rd-path {
  font-size: var(--fs-sm);
  color: var(--text-faint);
  font-family: var(--font-code);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rd-iconbtn {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  width: 26px; height: 26px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto;
}
.rd-iconbtn:hover { background: var(--bg-hover); color: var(--text); }
.rd-iconbtn:focus-visible, .rd-cancel:focus-visible, .rd-accept:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 2px; }

.rd-instr {
  flex: 0 0 auto;
  display: flex; gap: 7px; align-items: flex-start;
  font-size: var(--fs-sm); color: var(--text-muted); background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: 8px 16px;
}
.rd-instr-tag {
  flex: 0 0 auto; font-size: var(--fs-cap); color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); border-radius: 5px; padding: 0 5px; line-height: 16px; margin-top: 1px;
}

.rd-statbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 16px;
  font-size: var(--fs-sm);
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--text) 4%, var(--bg-raised));
}
.rd-stat-same { color: var(--text-faint); }
.rd-stat-add { color: var(--green); font-weight: 600; font-family: var(--font-code); }
.rd-stat-del { color: var(--danger); font-weight: 600; font-family: var(--font-code); }
.rd-stat-equal { color: var(--amber); }
.rd-stat-hint { margin-left: auto; color: var(--text-faint); font-size: var(--fs-cap); }

.rd-body {
  flex: 1 1 auto;
  overflow: auto;
  background: #f6f8fb;
  font-family: var(--font-code);
  font-size: var(--fs-sm);
  line-height: 1.65;
  padding: 6px 0;
}
.rd-row {
  display: flex;
  align-items: stretch;
  white-space: pre;
}
.rd-gno {
  flex: 0 0 46px;
  text-align: right;
  padding: 0 8px 0 0;
  color: var(--text-muted);
  user-select: none;
  font-size: var(--fs-cap);
}
.rd-sign {
  flex: 0 0 20px;
  text-align: center;
  user-select: none;
}
.rd-code {
  flex: 1 1 auto;
  padding-right: 16px;
  color: #243044;
}
.rd-del { background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); }
.rd-del .rd-sign { color: var(--danger); }
.rd-del .rd-code { color: var(--danger); }
.rd-del .rd-gno { background: color-mix(in srgb, var(--danger) 20%, var(--bg-raised)); }
.rd-add { background: color-mix(in srgb, var(--green) 12%, var(--bg-raised)); }
.rd-add .rd-sign { color: var(--green); }
.rd-add .rd-code { color: var(--green); }
.rd-add .rd-gno { background: color-mix(in srgb, var(--green) 20%, var(--bg-raised)); }

.rd-foot {
  flex: 0 0 auto;
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 11px 16px;
  border-top: 1px solid var(--border);
  background: var(--bg);
}
.rd-cancel {
  padding: 6px 18px; font-size: var(--fs-sm); border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border-strong); background: transparent; color: var(--text-muted);
}
.rd-cancel:hover { background: var(--bg-hover); color: var(--text); }
.rd-accept {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 18px; font-size: var(--fs-sm); border-radius: 7px; cursor: pointer;
  border: 1px solid var(--green); background: linear-gradient(180deg, color-mix(in srgb, var(--green) 70%, var(--bg-raised)), var(--green)); color: var(--green-ink);
}
.rd-accept:hover:not(:disabled) { background: var(--green); }
.rd-accept:disabled { opacity: 0.45; cursor: default; }
.rd-cancel, .rd-accept { transition: background .16s ease, border-color .16s ease, color .16s ease, transform .16s ease, filter .16s ease; }
.rd-cancel:active, .rd-accept:active:not(:disabled) { transform: translateY(1px); }
@keyframes rd-overlay-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes rd-panel-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@media (max-width: 700px) {
  .rd-overlay { padding: 14px; }
  .rd-panel { width: 100%; height: min(900px, 94vh); }
  .rd-statbar { flex-wrap: wrap; gap: 6px 10px; }
  .rd-stat-hint { flex-basis: 100%; margin-left: 0; }
}
@media (max-width: 520px) {
  .rd-overlay { padding: 0; align-items: stretch; }
  .rd-panel { height: 100%; border-radius: 0; border-left: 0; border-right: 0; }
  .rd-top { padding: 10px 12px; }
  .rd-path { display: none; }
  .rd-foot { padding: 10px 12px; }
  .rd-cancel, .rd-accept { flex: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .rd-overlay, .rd-panel, .rd-iconbtn, .rd-cancel, .rd-accept { animation: none; transition: none; }
}
</style>
