<script setup lang="ts">
// 问答侧栏只承载会话与资料轻操作；项目管理和索引统一进入代码工作台。
import { computed, ref } from 'vue'
import type { ModelConfigInfo, ProjectInfo, SessionInfo } from '../../workbench/api'

const props = defineProps<{
  projects: ProjectInfo[]
  currentPid: string
  config: ModelConfigInfo | null
  demo: boolean
  ingestBusy: boolean
  ingestMsg: string
  sessions?: SessionInfo[]
  currentSessionId?: string
  sessionBusy?: boolean
  /** 空对话首屏：项目/资料移到首页右栏，桌面侧栏只保留导航与历史。 */
  home?: boolean
}>()
const emit = defineEmits<{
  (e: 'ingest', file: File): void
  (e: 'openSettings'): void
  (e: 'newSession'): void
  (e: 'switchSession', id: string): void
  (e: 'deleteSession', id: string): void
  (e: 'close'): void
}>()

const fileInput = ref<HTMLInputElement | null>(null)

const current = computed(() =>
  props.projects.find((p) => p.project_id === props.currentPid) || null)
const files = computed(() => props.config?.ingested_files || [])
const visibleFiles = computed(() => props.demo
  ? ['数值设计案.pdf', '战斗规则.md', '常见问题.txt']
  : files.value)

function pickFile() {
  fileInput.value?.click()
}
function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  if (f) emit('ingest', f)
  input.value = ''
}

function sessTitle(s: SessionInfo): string {
  return (s.title || s.preview || '').trim() || '未命名对话'
}
function sessTime(v?: string): string {
  if (!v) return ''
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return ''
  const delta = Math.max(0, Date.now() - d.getTime())
  if (delta < 60_000) return '刚刚'
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`
  return d.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })
}
</script>

<template>
  <aside class="ask-side" :class="{ 'is-home': home }" aria-label="工作空间与会话">
    <header class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" width="32" height="32" fill="none">
          <path d="M5 6.5h9l3 3v17l-4-3H5z" fill="currentColor" opacity=".16" />
          <path d="M16 9.5 21 6h6v17h-6l-5 3.5L11 23H5V6h6l5 3.5Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
          <path d="M16 9.5v17M9 11h2l2 1.5M9 15h2l2 1.5M20 12.5l2-1.5h2M20 16.5l2-1.5h2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </div>
      <div class="brand-text">
        <div class="brand-name">DocMind</div>
        <div class="brand-sub">工作空间</div>
      </div>
      <button
        type="button"
        class="icon-button sidebar-close"
        title="关闭侧栏"
        aria-label="关闭侧栏"
        @click="emit('close')"
      >
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true">
          <path d="m6 6 12 12M18 6 6 18" />
        </svg>
      </button>
    </header>

    <button type="button" class="new-chat" @click="emit('newSession')">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true">
        <path d="M12 5v14M5 12h14" />
      </svg>
      新对话
    </button>

    <nav class="side-nav" aria-label="主导航">
      <a class="nav-link" href="/" aria-current="page" @click.prevent="emit('close')">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2 1.8-5A8 8 0 1 1 20 11.5Z" />
          <path d="M8 10h8M8 14h5" />
        </svg>
        AI 问答
      </a>
      <a class="nav-link" href="/workbench">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <path d="m7 9 3 3-3 3M13 15h4" />
        </svg>
        代码工作台
      </a>
    </nav>

    <section class="history-section" aria-labelledby="sidebar-history-title">
      <h2 id="sidebar-history-title" class="section-title">最近对话</h2>
      <div class="sess-list" :aria-busy="sessionBusy">
        <div
          v-for="s in (sessions || [])"
          :key="s.session_id"
          class="sess-item"
          :class="{ active: s.session_id === currentSessionId }"
        >
          <button
            type="button"
            class="sess-main"
            :title="sessTitle(s)"
            :aria-current="s.session_id === currentSessionId ? 'true' : undefined"
            :disabled="sessionBusy"
            @click="emit('switchSession', s.session_id)"
          >
            <span class="sess-copy">
              <strong>{{ sessTitle(s) }}</strong>
              <small v-if="sessTime(s.updated_at)">{{ sessTime(s.updated_at) }}</small>
            </span>
          </button>
          <button
            type="button"
            class="icon-button sess-del"
            :title="`删除对话：${sessTitle(s)}`"
            :aria-label="`删除对话：${sessTitle(s)}`"
            :disabled="sessionBusy"
            @click.stop="emit('deleteSession', s.session_id)"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5M14 11v5" />
            </svg>
          </button>
        </div>
        <p v-if="!(sessions || []).length" class="empty-note">新建对话，开始探索你的资料</p>
      </div>
    </section>

    <div class="workspace-resources">
      <section class="project-section" aria-labelledby="sidebar-project-title">
        <div class="section-heading">
          <h2 id="sidebar-project-title" class="section-title">当前项目</h2>
          <a class="section-action" href="/workbench" title="在代码工作台管理项目与索引">管理</a>
        </div>
        <div class="project-name" :class="{ muted: !demo && !current }" :title="demo ? '示例游戏项目' : current?.name">
          {{ demo ? '示例游戏项目' : current?.name || '未选择项目' }}
        </div>
        <div v-if="current?.root" class="project-root" :title="current.root">{{ current.root }}</div>
        <p v-else-if="!demo && !current" class="empty-note">也可以直接提问</p>
      </section>

      <section class="documents-section" aria-labelledby="sidebar-documents-title" :aria-busy="ingestBusy">
        <div class="section-heading">
          <h2 id="sidebar-documents-title" class="section-title">
            资料文档 <span class="section-count">{{ visibleFiles.length }}</span>
          </h2>
          <button
            type="button"
            class="section-action add-doc"
            :disabled="demo || ingestBusy"
            :title="demo ? '示例模式暂不支持上传' : ingestBusy ? '正在处理资料文档' : '上传资料文档（PDF / Markdown / 文本）'"
            aria-label="上传资料文档"
            @click="pickFile"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 16V4m-4 4 4-4 4 4M4 15v5h16v-5" />
            </svg>
            {{ ingestBusy ? '处理中' : '上传' }}
          </button>
        </div>
        <input ref="fileInput" type="file" accept=".pdf,.md,.txt" hidden @change="onFileChange">
        <div class="ingest-status" role="status" aria-live="polite" aria-atomic="true">
          <p v-if="ingestMsg" class="ingest-msg">{{ ingestMsg }}</p>
        </div>
        <div class="file-list">
          <div v-for="n in visibleFiles" :key="n" class="file-item">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8M8 16h6" />
            </svg>
            <span class="file-nm" :title="n">{{ n }}</span>
          </div>
          <p v-if="!visibleFiles.length" class="empty-note">上传 PDF、Markdown 或文本</p>
        </div>
      </section>
    </div>

    <footer class="sidebar-footer">
      <button type="button" class="model-settings" title="模型参数设置" aria-label="打开模型参数设置" @click="emit('openSettings')">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M4 7h16M4 17h16" />
          <circle cx="9" cy="7" r="2.5" fill="var(--bg-raised)" />
          <circle cx="15" cy="17" r="2.5" fill="var(--bg-raised)" />
        </svg>
        <span class="model-copy">
          <span class="model-label">模型设置</span>
          <span class="model-name" :title="config?.llm_model">{{ config?.llm_model || '未配置模型' }}</span>
        </span>
        <svg class="settings-chevron" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="m9 6 6 6-6 6" />
        </svg>
      </button>
    </footer>
  </aside>
</template>

<style scoped>
.ask-side {
  box-sizing: border-box;
  display: flex;
  flex: 0 0 256px;
  flex-direction: column;
  gap: 16px;
  width: 256px;
  height: 100dvh;
  min-height: 0;
  padding: 20px 12px 12px;
  overflow-x: hidden;
  overflow-y: auto;
  border-right: 1px solid var(--border);
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font-ui);
  font-size: 14px;
  line-height: 1.5;
}

.ask-side button,
.ask-side a {
  font-family: inherit;
  font-size: inherit;
  line-height: inherit;
  -webkit-tap-highlight-color: var(--bg-hover);
}

.ask-side button {
  cursor: pointer;
}

.ask-side button:disabled {
  color: var(--text-muted);
  cursor: not-allowed;
}

.ask-side :is(button, a):focus-visible {
  outline: 2px solid var(--accent-text);
  outline-offset: -2px;
}

.ask-side svg {
  flex-shrink: 0;
}

.brand {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 10px;
  min-height: 40px;
  padding: 0 8px;
}

.brand-mark {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  color: var(--accent-text);
}

.brand-text {
  flex: 1;
  min-width: 0;
}

.brand-name {
  font-size: 18px;
  font-weight: 650;
  letter-spacing: -.03em;
}

.brand-sub {
  color: var(--text-muted);
  font-size: 12px;
}

.icon-button {
  display: inline-flex;
  flex: 0 0 40px;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--text-muted);
}

.sidebar-close {
  display: none;
  margin-right: -8px;
}

.new-chat {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  min-height: 40px;
  padding: 8px 12px;
  border: 1px solid var(--accent-deep);
  border-radius: 8px;
  background: var(--accent);
  color: var(--accent-ink);
  font-weight: 600;
}

.new-chat:hover {
  background: color-mix(in srgb, var(--accent) 88%, var(--ink));
  color: var(--accent-ink);
}
.icon-button:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--ink);
}

.side-nav {
  display: flex;
  flex-shrink: 0;
  flex-direction: column;
  gap: 4px;
}

.nav-link {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 40px;
  padding: 8px 12px;
  border-radius: 8px;
  color: var(--text-muted);
  text-decoration: none;
}

.nav-link:hover {
  background: var(--bg-hover);
  color: var(--ink);
}

.nav-link[aria-current='page'] {
  background: var(--accent-soft);
  color: var(--accent-text);
  box-shadow: inset 2px 0 0 var(--accent);
  font-weight: 600;
}

.history-section {
  display: flex;
  flex: 1 1 0;
  flex-direction: column;
  gap: 8px;
  min-height: 96px;
}

.section-title {
  margin: 0;
  padding: 0 10px;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 500;
  line-height: 20px;
}

.sess-list {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 4px;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-color: var(--border) var(--bg-raised);
}

.sess-item {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  min-width: 0;
  border-radius: 8px;
}

.sess-item:hover,
.sess-item:focus-within {
  background: var(--bg-hover);
}

.sess-item.active {
  background: var(--bg-selected);
}

.sess-main {
  display: flex;
  flex: 1;
  align-items: center;
  min-width: 0;
  min-height: 40px;
  padding: 8px 10px;
  border: 0;
  border-radius: 8px;
  background: none;
  color: var(--ink);
  text-align: left;
}

.sess-copy {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.sess-copy strong {
  overflow: hidden;
  font-size: 14px;
  font-weight: 400;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sess-main[aria-current='true'] strong {
  font-weight: 600;
}

.sess-copy small {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 16px;
}

.sess-del {
  opacity: 0;
  pointer-events: none;
}

.sess-item:hover .sess-del,
.sess-item:focus-within .sess-del {
  opacity: 1;
  pointer-events: auto;
}

.workspace-resources {
  display: flex;
  flex-shrink: 0;
  flex-direction: column;
  gap: 12px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  min-height: 40px;
}

.section-heading .section-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-count {
  font-variant-numeric: tabular-nums;
}

.ask-side .section-action {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  gap: 5px;
  min-width: 40px;
  min-height: 40px;
  padding: 8px;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--text-muted);
  font-size: 12px;
  text-decoration: none;
}

.section-action:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--ink);
}

.project-name,
.project-root {
  margin: 0 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.project-name {
  font-weight: 500;
}

.project-name.muted {
  color: var(--text-muted);
}

.project-root {
  margin-top: 4px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
}

.empty-note {
  margin: 0;
  padding: 4px 10px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.ingest-msg {
  max-height: 64px;
  margin: 0 10px 6px;
  overflow-y: auto;
  color: var(--text-muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.file-list {
  max-height: 96px;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-color: var(--border) var(--bg-raised);
}

.file-item {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 4px 10px;
}

.file-item svg {
  color: var(--text-muted);
}

.file-nm {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-footer {
  flex-shrink: 0;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}

.model-settings {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 56px;
  padding: 8px 10px;
  border: 0;
  border-radius: 8px;
  background: none;
  color: var(--ink);
  text-align: left;
}

.model-settings:hover {
  background: var(--bg-hover);
}

.model-copy {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.model-label {
  font-weight: 500;
}

.model-name {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings-chevron {
  color: var(--text-muted);
}

@media (min-width: 921px) {
  .ask-side.is-home .workspace-resources { display: none; }
  .ask-side.is-home .history-section { flex: 1 1 auto; }
}

@media (hover: none), (any-pointer: coarse) {
  .sess-del {
    opacity: 1;
    pointer-events: auto;
  }
}

@media (max-height: 700px) {
  .ask-side {
    gap: 12px;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .history-section {
    flex: 1 0 136px;
  }

  .file-list {
    max-height: 64px;
  }
}

@media (max-width: 920px) {
  .ask-side {
    display: none;
  }

  .ask-side.is-open {
    position: fixed;
    top: 0;
    left: 0;
    z-index: 60;
    display: flex;
    width: min(288px, 85vw);
    height: 100dvh;
    padding-top: max(16px, env(safe-area-inset-top));
    padding-bottom: max(12px, env(safe-area-inset-bottom));
  }

  .sidebar-close {
    display: inline-flex;
  }
}
</style>
