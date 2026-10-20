<script setup lang="ts">
// P2 选区 AI 结果面板：解释 / Review / 提问走 ReAct agent（markdown 答案 + 可折叠检索轨迹），
// 改写走直连快通道（纯代码，可一键替换回原选区；不自动保存，仍由 Ctrl+S 落盘）。
import { computed, nextTick, ref, watch } from 'vue'
import { useWorkbench, askAlert } from '../composables/workbench'
import type { AiTraceItem, AiTurn } from '../composables/workbench'
import { mdToHtml } from '../markdown'

const {
  turns, aiStreaming, askComposing, activeSelection,
  runAi, stopAi, closeAiPanel, clearTurns, copyAnswer, openRewriteDiff,
} = useWorkbench()

const scroller = ref<HTMLElement | null>(null)
const askText = ref('')
const copiedId = ref<number | null>(null)

const askSummary = computed(() => {
  const s = activeSelection.value
  return s ? `${s.name} · 第 ${s.startLine}–${s.endLine} 行` : ''
})

// ---------------------------------------------------------------- 极简 markdown（共用 ../markdown）
function answerHtml(turn: AiTurn): string {
  return mdToHtml(turn.answer || '')
}

const TRACE_LABEL: Record<string, string> = {
  thought: '思考',
  action: '工具调用',
  observation: '返回',
  reflection: '反思',
}
function traceLabel(it: AiTraceItem): string {
  return TRACE_LABEL[it.type] || it.type
}

// ---------------------------------------------------------------- 动作
function sendAsk() {
  const q = askText.value.trim()
  if (!q || aiStreaming.value) return
  askText.value = ''
  void runAi('ask', q)
}
function onAskKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendAsk()
  }
}

function doReplace(turn: AiTurn) {
  // 先弹行级 diff 预览，用户接受后才真正替换（弹窗内复用 applyRewrite 护栏）
  openRewriteDiff(turn)
}
async function doCopy(turn: AiTurn) {
  const ok = await copyAnswer(turn)
  if (ok) {
    copiedId.value = turn.id
    setTimeout(() => { if (copiedId.value === turn.id) copiedId.value = null }, 1400)
  } else {
    void askAlert({ title: '复制失败', message: '浏览器拒绝了剪贴板访问，请手动选择文本复制。' })
  }
}

const ACTION_DOT: Record<string, string> = {
  explain: 'var(--viz-class)',
  review: 'var(--viz-scene)',
  rewrite: 'var(--viz-const)',
  ask: 'var(--viz-script)',
}

function statusText(turn: AiTurn): string {
  if (turn.status === 'streaming') return '生成中…'
  if (turn.status === 'stopped') return '已停止'
  if (turn.status === 'error') return '失败'
  return turn.replaced ? '已替换' : '完成'
}

// 流式内容增长时自动滚到底
watch(
  () => turns.value.map((t) => `${t.status}:${t.answer.length}:${t.trace.length}`).join('|') + String(askComposing.value),
  async () => {
    await nextTick()
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
  },
)
</script>

<template>
  <aside class="ai-panel">
    <header class="ai-head">
      <div class="ai-head-title">
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          <path d="M7 1.2 L8.5 5.2 L12.6 6.7 L8.5 8.2 L7 12.3 L5.5 8.2 L1.4 6.7 L5.5 5.2 Z" fill="color-mix(in srgb, var(--viz-class) 20%, transparent)" stroke="var(--viz-class)" stroke-width="1" stroke-linejoin="round"/>
        </svg>
        选区 AI
      </div>
      <div class="ai-head-actions">
        <button v-if="aiStreaming" class="ai-stop" title="停止生成" @click="stopAi">
          <svg width="10" height="10" viewBox="0 0 10 10"><rect x="1.4" y="1.4" width="7.2" height="7.2" rx="1.2" fill="currentColor"/></svg>
          停止
        </button>
        <button class="ai-icon-btn" title="清空对话" @click="clearTurns">
          <svg width="13" height="13" viewBox="0 0 13 13"><path d="M3.2 4 H10 M5.4 4 V2.6 H7.6 V4 M4 4 L4.5 10.6 H8.5 L9 4" fill="none" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/></svg>
        </button>
        <button class="ai-icon-btn" title="关闭面板" @click="closeAiPanel">
          <svg width="13" height="13" viewBox="0 0 13 13"><path d="M3 3 L10 10 M10 3 L3 10" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg>
        </button>
      </div>
    </header>

    <!-- 就选区提问：输入态 -->
    <div v-if="askComposing" class="ai-compose">
      <div class="ai-compose-meta">
        <span class="ai-compose-pin">{{ askSummary }}</span>
        <span class="ai-compose-hint">Enter 发送 · Shift+Enter 换行</span>
      </div>
      <textarea
        v-model="askText"
        class="ai-compose-input"
        rows="4"
        placeholder="就选中的代码提问，例如：这个函数在高并发下会有问题吗？调用方有哪些？"
        autofocus
        @keydown="onAskKeydown"
      />
      <div class="ai-compose-foot">
        <button class="ai-send" :disabled="!askText.trim() || aiStreaming" @click="sendAsk">发送</button>
      </div>
    </div>

    <div ref="scroller" class="ai-scroll">
      <p v-if="!turns.length && !askComposing" class="ai-empty">
        在编辑器中选中代码，点击弹出工具条的「解释 / Review / 改写 / 提问」。
      </p>

      <article v-for="turn in turns" :key="turn.id" class="ai-card">
        <div class="ai-card-head">
          <span class="ai-dot" :style="{ background: ACTION_DOT[turn.action] || 'var(--viz-neutral)' }" />
          <span class="ai-card-title">{{ turn.title }}</span>
          <span class="ai-card-origin" :title="turn.origin.path">
            {{ turn.origin.path.split('/').pop() }}:{{ turn.origin.startLine }}-{{ turn.origin.endLine }}
          </span>
          <span class="ai-card-status" :class="`st-${turn.status}`">{{ statusText(turn) }}</span>
        </div>

        <div v-if="turn.instruction" class="ai-card-instr">
          <span class="ai-instr-tag">{{ turn.action === 'ask' ? '问' : '要求' }}</span>{{ turn.instruction }}
        </div>

        <details v-if="turn.trace.length" class="ai-trace">
          <summary>检索 / 思考过程（{{ turn.trace.length }}）</summary>
          <div v-for="(it, i) in turn.trace" :key="i" class="ai-trace-item">
            <span class="ai-trace-type" :class="`tt-${it.type}`">{{ traceLabel(it) }}</span>
            <span class="ai-trace-text">{{ it.text }}</span>
          </div>
        </details>

        <!-- 改写：等宽纯代码 + 替换动作 -->
        <template v-if="turn.action === 'rewrite'">
          <pre v-if="turn.answer" class="ai-code"><code>{{ turn.answer }}</code><span v-if="turn.status === 'streaming'" class="ai-caret" /></pre>
          <div v-else-if="turn.status === 'streaming'" class="ai-thinking">正在生成替换代码…</div>
          <div v-if="turn.status === 'done' && turn.answer.trim()" class="ai-card-actions">
            <button
              v-if="turn.origin.writable && !turn.replaced"
              class="ai-replace"
              title="先预览原选区与 AI 结果的差异，确认后再替换（不会自动保存）"
              @click="doReplace(turn)"
            >
              <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 6.2 L5 8.6 L9.6 3.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
              预览差异并替换
            </button>
            <span v-else-if="turn.replaced" class="ai-replaced">
              <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 6.2 L5 8.6 L9.6 3.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
              已替换 · Ctrl+S 保存
            </span>
            <button class="ai-copy" @click="doCopy(turn)">
              {{ copiedId === turn.id ? '已复制' : '复制代码' }}
            </button>
          </div>
        </template>

        <!-- 解释 / Review / 提问：markdown 答案 -->
        <template v-else>
          <div v-if="turn.answer" class="ai-md" v-html="answerHtml(turn)" />
          <div v-else-if="turn.status === 'streaming'" class="ai-thinking">
            正在检索代码库并组织回答<span class="ai-dots">…</span>
          </div>
        </template>

        <div v-if="turn.status === 'error'" class="ai-error-box">
          <svg width="14" height="14" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.6" fill="none" stroke="var(--danger)" stroke-width="1.1"/><path d="M7 4 V8" stroke="var(--danger)" stroke-width="1.2" stroke-linecap="round"/><circle cx="7" cy="9.9" r="0.7" fill="var(--danger)"/></svg>
          {{ turn.error }}
        </div>
        <div v-if="turn.status === 'stopped'" class="ai-stopped-hint">已停止生成{{ turn.answer ? '，以上为部分结果' : '' }}</div>
      </article>
    </div>
  </aside>
</template>

<style scoped>
.ai-panel {
  flex: 0 0 392px;
  width: 392px;
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg-raised);
  border-left: 1px solid var(--border);
  animation: ai-panel-in .2s cubic-bezier(.2,.8,.2,1) both;
}
.ai-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
}
.ai-head-title {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text);
  letter-spacing: 0.02em;
}
.ai-head-actions { display: inline-flex; gap: 4px; align-items: center; }
.ai-icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; border: none; border-radius: 6px;
  background: transparent; color: var(--text-muted); cursor: pointer;
}
.ai-icon-btn:hover { background: var(--bg-hover); color: var(--text); }
.ai-icon-btn:focus-visible, .ai-stop:focus-visible, .ai-send:focus-visible,
.ai-replace:focus-visible, .ai-copy:focus-visible, .ai-trace summary:focus-visible,
.ai-compose-input:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 2px; }
.ai-icon-btn, .ai-stop, .ai-send, .ai-replace, .ai-copy { transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease, filter .16s ease; }
.ai-stop {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 9px; font-size: var(--fs-sm); border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--danger) 38%, transparent); background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); color: var(--danger); cursor: pointer;
}
.ai-stop:hover { background: color-mix(in srgb, var(--danger) 18%, var(--bg-raised)); }

.ai-compose { padding: 10px 12px; border-bottom: 1px solid var(--border); background: var(--bg-raised); }
.ai-compose-meta { display: flex; align-items: center; justify-content: space-between; margin-bottom: 7px; }
.ai-compose-pin { font-size: var(--fs-sm); color: var(--accent); }
.ai-compose-hint { font-size: var(--fs-cap); color: var(--text-faint); }
.ai-compose-input {
  width: 100%; box-sizing: border-box; resize: vertical;
  background: color-mix(in srgb, var(--text) 4%, var(--bg-raised)); color: var(--text);
  border: 1px solid var(--border-graphic); border-radius: 8px;
  padding: 8px 10px; font-size: var(--fs-sm); line-height: 1.55;
  font-family: inherit; outline: none;
}
.ai-compose-input:focus { border-color: color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); }
.ai-compose-foot { display: flex; justify-content: flex-end; margin-top: 7px; }
.ai-send {
  padding: 5px 16px; font-size: var(--fs-sm); border-radius: 7px;
  border: 1px solid var(--accent-deep); --btn-fill: linear-gradient(180deg, var(--accent-loud), var(--accent)); background: linear-gradient(180deg, var(--accent-loud), var(--accent)); color: var(--accent-ink); cursor: pointer;
}
.ai-send:hover:not(:disabled) { --btn-fill: var(--accent); background: var(--accent); }
.ai-send:disabled { opacity: 0.45; cursor: default; }
.ai-send:active:not(:disabled), .ai-replace:active, .ai-copy:active { transform: translateY(1px); }

.ai-scroll { flex: 1; overflow-y: auto; padding: 10px 12px 24px; }
.ai-empty { font-size: var(--fs-sm); color: var(--text-faint); line-height: 1.7; margin-top: 8px; }

.ai-card {
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-raised); padding: 10px 11px; margin-bottom: 12px;
  animation: ai-card-in .18s ease-out both;
}
.ai-card-head { display: flex; align-items: center; gap: 7px; margin-bottom: 7px; }
.ai-dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; }
.ai-card-title { font-size: var(--fs-sm); font-weight: 600; color: var(--text); }
.ai-card-origin {
  font-size: var(--fs-cap); color: var(--text-muted); font-family: 'Cascadia Code', Consolas, monospace;
  max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ai-card-status { margin-left: auto; font-size: var(--fs-cap); color: var(--text-faint); }
.ai-card-status.st-streaming { color: var(--accent); }
.ai-card-status.st-error { color: var(--danger); }
.ai-card-status.st-stopped { color: var(--amber); }

.ai-card-instr {
  display: flex; gap: 7px; align-items: flex-start;
  font-size: var(--fs-sm); color: var(--text-muted); background: var(--bg-hover);
  border: 1px solid var(--border); border-radius: 8px; padding: 7px 9px; margin-bottom: 8px;
}
.ai-instr-tag {
  flex: 0 0 auto; font-size: var(--fs-cap); color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); border-radius: 5px; padding: 0 5px; line-height: 16px; margin-top: 1px;
}

.ai-trace { margin-bottom: 8px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.ai-trace summary {
  cursor: pointer; padding: 6px 9px; font-size: var(--fs-cap); color: var(--text-muted);
  list-style: none; user-select: none;
  transition: background .16s ease, color .16s ease;
}
.ai-trace summary:hover { background: var(--bg-hover); color: var(--text); }
.ai-trace summary::-webkit-details-marker { display: none; }
.ai-trace summary::before { content: '▸ '; }
.ai-trace[open] summary::before { content: '▾ '; }
.ai-trace-item {
  display: flex; gap: 7px; padding: 4px 9px; border-top: 1px solid var(--border);
  font-size: var(--fs-cap); line-height: 1.5;
}
.ai-trace-type {
  flex: 0 0 auto; height: 17px; padding: 0 6px; border-radius: 5px;
  font-size: var(--fs-cap); line-height: 17px; color: var(--text-muted); background: var(--bg-hover);
}
.ai-trace-type.tt-action { color: var(--amber); }
.ai-trace-type.tt-observation { color: var(--text-muted); }
.ai-trace-text {
  color: var(--text-muted); word-break: break-all;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}

/* markdown */
.ai-md { font-size: var(--fs-sm); line-height: 1.66; color: var(--text); word-break: break-word; }
.ai-md :deep(.md-h) { font-weight: 600; color: var(--text); margin: 9px 0 5px; font-size: var(--fs-sm); }
.ai-md :deep(.md-p) { margin: 0 0 7px; white-space: pre-wrap; }
.ai-md :deep(.md-ul) { margin: 2px 0 8px; padding-left: 18px; }
.ai-md :deep(.md-ul li) { margin: 2px 0; list-style: disc; }
.ai-md :deep(code.md-ic) {
  font-family: 'Cascadia Code', Consolas, monospace; font-size: var(--fs-cap);
  background: var(--bg-hover); color: var(--amber); border: 1px solid var(--border-strong);
  border-radius: 4px; padding: 0.5px 4px;
}
.ai-md :deep(.md-pre) {
  margin: 7px 0; padding: 9px 10px; background: #f6f8fb;
  border: 1px solid var(--border); border-radius: 8px; overflow-x: auto;
}
.ai-md :deep(.md-pre code) {
  font-family: 'Cascadia Code', Consolas, monospace; font-size: var(--fs-sm);
  line-height: 1.55; color: #243044; white-space: pre;
}

/* 改写代码块 */
.ai-code {
  position: relative; margin: 2px 0 8px; padding: 9px 10px;
  background: #f6f8fb; border: 1px solid #b7dcc7; border-radius: 8px;
  overflow-x: auto;
  font-family: 'Cascadia Code', Consolas, monospace; font-size: var(--fs-sm);
  line-height: 1.55; color: #1c6b46; white-space: pre;
}
.ai-caret {
  display: inline-block; width: 6.5px; height: 13px; margin-left: 1px;
  background: #1c9e66; vertical-align: -2px; animation: ai-blink 1s steps(1) infinite;
}
@keyframes ai-blink { 50% { opacity: 0; } }

.ai-thinking { font-size: var(--fs-sm); color: var(--text-muted); padding: 4px 0; }
.ai-dots { animation: ai-blink 1.2s steps(1) infinite; }

.ai-card-actions { display: flex; gap: 8px; align-items: center; margin-top: 2px; }
.ai-replace {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 12px; font-size: var(--fs-sm); border-radius: 7px; cursor: pointer;
  border: 1px solid var(--green); background: linear-gradient(180deg, color-mix(in srgb, var(--green) 70%, var(--bg-raised)), var(--green)); color: var(--green-ink);
}
.ai-replace:hover { background: var(--green); }
.ai-replaced { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-sm); color: var(--green); }
.ai-copy {
  padding: 5px 12px; font-size: var(--fs-sm); border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border-strong); background: transparent; color: var(--text-muted);
}
.ai-copy:hover { background: var(--bg-hover); color: var(--text); }

.ai-error-box {
  display: flex; gap: 8px; align-items: flex-start; margin-top: 8px;
  font-size: var(--fs-sm); color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised));
  border: 1px solid color-mix(in srgb, var(--danger) 38%, transparent); border-radius: 8px; padding: 8px 10px;
}
.ai-stopped-hint { margin-top: 6px; font-size: var(--fs-cap); color: var(--amber); }
@keyframes ai-panel-in { from { opacity: 0; transform: translateX(8px); } to { opacity: 1; transform: translateX(0); } }
@keyframes ai-card-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
@media (max-width: 760px) {
  .ai-panel { flex-basis: 100%; width: 100%; max-height: 48vh; border-left: 0; border-top: 1px solid var(--border); }
  .ai-scroll { padding: 8px 10px 16px; }
}
@media (prefers-reduced-motion: reduce) {
  .ai-panel, .ai-card, .ai-icon-btn, .ai-stop, .ai-send, .ai-replace, .ai-copy, .ai-trace summary, .ai-caret, .ai-dots { animation: none; transition: none; }
}
</style>
