<script setup lang="ts">
// P3：文件历史版本。左侧提交列表（gitlog），右侧选中版本内容预览（git show），
// 可一键把文件恢复为该版本（只写工作区，不动提交历史）。
import { computed, onBeforeUnmount, watch } from 'vue'
import { useWorkbench } from '../composables/workbench'
import type { GitCommit } from '../api'

const {
  history, closeHistory, selectHistoryVersion, restoreSelectedVersion,
} = useWorkbench()

function fmtTime(c: GitCommit): string {
  if (!c.time) return c.time_raw.slice(0, 10)
  return new Date(c.time * 1000).toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).replace(/\//g, '-')
}

const previewLines = computed(() => history.value?.preview.split('\n').length ?? 0)

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeHistory()
}

watch(history, (h) => {
  if (h) window.addEventListener('keydown', onKey)
  else window.removeEventListener('keydown', onKey)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div v-if="history" class="gh-overlay wb-modal-backdrop" @mousedown.self="closeHistory">
    <section class="gh-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="文件历史版本">
      <header class="gh-top wb-modal-head">
        <div class="gh-title">
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <circle cx="3.5" cy="7" r="1.5" fill="none" stroke="var(--viz-class)" stroke-width="1.1" />
            <circle cx="10.5" cy="3" r="1.5" fill="none" stroke="var(--viz-const)" stroke-width="1.1" />
            <circle cx="10.5" cy="11" r="1.5" fill="none" stroke="var(--viz-fn)" stroke-width="1.1" />
            <path d="M4.7 6.2 L9.3 3.7 M4.7 7.8 L9.3 10.3" stroke="var(--viz-link)" stroke-width="1" />
          </svg>
          <h2>历史版本</h2>
          <span class="gh-path" :title="history.path">{{ history.name }}</span>
        </div>
        <button class="gh-iconbtn" title="关闭（Esc）" @click="closeHistory">
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
        </button>
      </header>

      <div class="gh-body">
        <!-- 左：提交列表 -->
        <aside class="gh-commits">
          <div v-if="history.loading" class="gh-state"><div class="cv-spinner" /><p>正在读取提交记录…</p></div>
          <div v-else-if="history.error" class="gh-state">
            <p class="gh-error">{{ history.error }}</p>
          </div>
          <div v-else-if="history.commits.length === 0" class="gh-state">
            <p class="gh-faint">该文件还没有提交记录。</p>
          </div>
          <ul v-else class="gh-list">
            <li v-for="c in history.commits" :key="c.full_hash">
              <button
                :class="['gh-commit', { active: history.selected?.full_hash === c.full_hash }]"
                @click="void selectHistoryVersion(c)"
              >
                <span class="gh-commit-msg">{{ c.message }}</span>
                <span class="gh-commit-meta">
                  <code>{{ c.hash }}</code>
                  <i>{{ c.author }} · {{ fmtTime(c) }}</i>
                </span>
              </button>
            </li>
          </ul>
        </aside>

        <!-- 右：版本预览 -->
        <div class="gh-preview-wrap">
          <div v-if="!history.selected" class="gh-state">
            <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
              <path d="M10 6 H24 L31 13 V34 H10 Z M24 6 V13 H31" stroke="var(--text-faint)" stroke-width="1.4" stroke-linejoin="round" />
              <path d="M14 20 H26 M14 25 H26 M14 30 H21" stroke="var(--text-faint)" stroke-width="1.3" stroke-linecap="round" />
            </svg>
            <p class="gh-faint">在左侧选择一次提交，预览该版本的文件内容</p>
          </div>
          <template v-else>
            <div class="gh-preview-bar">
              <div class="gh-preview-info">
                <code class="gh-preview-hash">{{ history.selected.hash }}</code>
                <span class="gh-preview-msg">{{ history.selected.message }}</span>
                <i v-if="!history.previewLoading && !history.previewError" class="gh-preview-size">{{ previewLines }} 行</i>
              </div>
              <button
                class="gh-restore-btn"
                :disabled="history.previewLoading || !!history.previewError || history.restoring"
                @click="void restoreSelectedVersion()"
              >
                <svg width="12" height="12" viewBox="0 0 12 12">
                  <path d="M2.5 5.5 A3.5 3.5 0 1 1 2.5 8 M2.5 3.2 V5.5 H4.8" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                {{ history.restoring ? '恢复中…' : '恢复此版本' }}
              </button>
            </div>
            <div v-if="history.previewLoading" class="gh-state gh-preview-state">
              <div class="cv-spinner" /><p>正在读取历史内容…</p>
            </div>
            <div v-else-if="history.previewError" class="gh-state gh-preview-state">
              <p class="gh-error">{{ history.previewError }}</p>
            </div>
            <pre v-else class="gh-preview">{{ history.preview }}</pre>
          </template>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.gh-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  z-index: 92;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 34px;
  animation: gh-overlay-in .18s ease-out both;
}
.gh-panel {
  width: min(980px, 95vw);
  height: min(680px, 88vh);
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 24px 70px rgba(35, 52, 84, 0.16);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: gh-panel-in .22s cubic-bezier(.2,.8,.2,1) both;
}
.gh-top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.gh-title { display: flex; align-items: center; gap: 9px; flex: 1; min-width: 0; }
.gh-title h2 { margin: 0; font-size: var(--fs-body); font-weight: 600; }
.gh-path {
  font-size: var(--fs-sm);
  color: var(--text-faint);
  font-family: var(--font-code);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gh-iconbtn {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  width: 26px; height: 26px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto;
}
.gh-iconbtn:hover { background: var(--bg-hover); color: var(--text); }
.gh-iconbtn:focus-visible,
.gh-commit:focus-visible,
.gh-restore-btn:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 2px; }

.gh-body { flex: 1 1 auto; display: flex; min-height: 0; }

.gh-commits {
  flex: 0 0 300px;
  border-right: 1px solid var(--border);
  overflow-y: auto;
  background: var(--bg);
}
.gh-list { list-style: none; margin: 0; padding: 6px; }
.gh-commit {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  text-align: left;
  border: 1px solid transparent;
  background: transparent;
  border-radius: 6px;
  padding: 7px 9px;
  cursor: pointer;
  transition: background .16s ease, border-color .16s ease, transform .16s ease;
}
.gh-commit:hover { background: var(--bg-hover); transform: translateX(2px); }
.gh-commit.active { background: color-mix(in srgb, var(--accent) 8%, transparent); border-color: color-mix(in srgb, var(--accent) 27%, transparent); }
.gh-commit-msg {
  font-size: var(--fs-sm);
  color: var(--text);
  line-height: 1.4;
  word-break: break-all;
}
.gh-commit-meta {
  display: flex;
  align-items: center;
  gap: 7px;
  font-style: normal;
}
.gh-commit-meta code {
  font-family: var(--font-code);
  font-size: var(--fs-cap);
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  border-radius: 3px;
  padding: 0 5px;
}
.gh-commit-meta i {
  font-style: normal;
  font-size: var(--fs-cap);
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gh-preview-wrap { flex: 1 1 auto; display: flex; flex-direction: column; min-width: 0; }
.gh-preview-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.gh-preview-info {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.gh-preview-hash {
  font-family: var(--font-code);
  font-size: var(--fs-cap);
  color: var(--green);
  background: color-mix(in srgb, var(--green) 7%, transparent);
  border-radius: 3px;
  padding: 1px 6px;
  flex: 0 0 auto;
}
.gh-preview-msg {
  font-size: var(--fs-sm);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gh-preview-size {
  font-style: normal;
  font-size: var(--fs-cap);
  color: var(--text-faint);
  flex: 0 0 auto;
  margin-left: auto;
}
.gh-restore-btn {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid color-mix(in srgb, var(--amber) 38%, transparent);
  background: color-mix(in srgb, var(--amber) 12%, var(--bg-raised));
  color: var(--amber);
  font-size: var(--fs-sm);
  padding: 4px 12px;
  border-radius: 6px;
  cursor: pointer;
}
.gh-restore-btn:hover:not(:disabled) { background: color-mix(in srgb, var(--amber) 15%, var(--bg-raised)); border-color: color-mix(in srgb, var(--amber) 38%, transparent); }
.gh-restore-btn:disabled { opacity: 0.5; cursor: default; }
.gh-restore-btn { transition: background .16s ease, border-color .16s ease, transform .16s ease, opacity .16s ease; }
.gh-restore-btn:active:not(:disabled) { transform: translateY(1px); }

.gh-preview {
  flex: 1 1 auto;
  margin: 0;
  overflow: auto;
  padding: 12px 16px;
  font-family: var(--font-code);
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text);
  white-space: pre;
  tab-size: 4;
}

.gh-state {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 24px;
  text-align: center;
}
.gh-preview-state { min-height: 120px; }
.gh-state p { margin: 0; font-size: var(--fs-sm); }
.gh-faint { color: var(--text-faint); }
.gh-error { color: var(--danger); }
@keyframes gh-overlay-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes gh-panel-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@media (max-width: 760px) {
  .gh-overlay { padding: 14px; }
  .gh-panel { width: 100%; height: min(900px, 94vh); }
  .gh-body { flex-direction: column; }
  .gh-commits { flex: 0 0 35%; max-height: 260px; border-right: 0; border-bottom: 1px solid var(--border); }
  .gh-preview-bar { flex-wrap: wrap; }
  .gh-preview-info { flex-basis: 100%; }
}
@media (max-width: 520px) {
  .gh-overlay { padding: 0; align-items: stretch; }
  .gh-panel { height: 100%; border-radius: 0; border-left: 0; border-right: 0; }
  .gh-top { padding: 10px 12px; }
  .gh-path { display: none; }
  .gh-commits { flex-basis: 30%; }
  .gh-preview { font-size: var(--fs-cap); padding: 10px 12px; }
}
@media (prefers-reduced-motion: reduce) {
  .gh-overlay, .gh-panel, .gh-iconbtn, .gh-commit, .gh-restore-btn { animation: none; transition: none; }
}
</style>
