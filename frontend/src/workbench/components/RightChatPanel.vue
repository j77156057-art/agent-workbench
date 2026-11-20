<script setup lang="ts">
// 右侧浮层三页签（Qoder 对话台形态）：对话 / 原始输出 / 时间线。
//
// 为什么是**浮层**而不是常驻第三栏：仓库里有一条实测结论（App.vue 记着）
// 「中途试过右侧常驻栏（三栏 IDE），420px 的栏里长回答被挤成电报体」。底部抽屉
// 仍然是回答长文的地方；这个浮层按需弹出、可拖宽（默认 640px），弹在主区之上，
// **不改全局三栏布局**。
//
// 三个页签各司其职：
//   · 对话     —— 复用真正的 ChatDock（scope="panel"），独立会话，底部抽屉不受影响
//   · 原始输出 —— SSE 事件原样流（内存态，不落盘；见 composables/rawOutput.ts）
//   · 时间线   —— 本会话的检查点（可分叉）+ 运行时事件时间轴（RuntimeTimeline）
import { computed, onMounted, ref, watch } from 'vue'
import ChatDock from './ChatDock.vue'
import RuntimeTimeline from './RuntimeTimeline.vue'
import { clearRawOutput, rawOutputEvents } from '../composables/rawOutput'
import { harnessApi } from '../api'
import type { SessionTurn } from '../api/harness'

type TabId = 'chat' | 'output' | 'timeline'

const TABS: { id: TabId; label: string }[] = [
  { id: 'chat', label: '对话' },
  { id: 'output', label: '原始输出' },
  { id: 'timeline', label: '时间线' },
]

const OPEN_KEY = 'docmind.rightChatPanel.open'
const WIDTH_KEY = 'docmind.rightChatPanel.width'

const open = ref(false)
const tab = ref<TabId>('chat')
const width = ref(640)
const panelSession = ref('')
const turns = ref<SessionTurn[]>([])
const turnsError = ref('')
const forkNote = ref('')

const events = computed(() => rawOutputEvents())
const eventCount = computed(() => events.value.length)

function loadStored() {
  try {
    open.value = localStorage.getItem(OPEN_KEY) === '1'
    const w = Number(localStorage.getItem(WIDTH_KEY))
    if (Number.isFinite(w) && w >= 420 && w <= 1100) width.value = w
  } catch { /* 隐私模式/无存储：用默认值 */ }
}
function persist() {
  try {
    localStorage.setItem(OPEN_KEY, open.value ? '1' : '0')
    localStorage.setItem(WIDTH_KEY, String(width.value))
  } catch { /* 存不了就不存 */ }
}

function toggle() {
  open.value = !open.value
  persist()
}
function close() {
  open.value = false
  persist()
}

/* ---- 宽度拖拽：左边缘拖动改宽度 ---- */
const MIN_W = 420
const MAX_W = 1100
let dragging = false
function onDragStart(e: PointerEvent) {
  dragging = true
  ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
  window.addEventListener('pointermove', onDragMove)
  window.addEventListener('pointerup', onDragEnd)
}
function onDragMove(e: PointerEvent) {
  if (!dragging) return
  const next = Math.round(window.innerWidth - e.clientX)
  width.value = Math.max(MIN_W, Math.min(MAX_W, next))
}
function onDragEnd() {
  if (!dragging) return
  dragging = false
  window.removeEventListener('pointermove', onDragMove)
  window.removeEventListener('pointerup', onDragEnd)
  persist()
}

/* ---- 检查点（会话回合） ---- */
async function loadTurns() {
  const sid = panelSession.value
  if (!sid) { turns.value = []; return }
  try {
    const detail = await harnessApi.sessionDetail(sid)
    if (!detail?.ok) {
      turns.value = []
      turnsError.value = '取不到这一会话的历史'
      return
    }
    turnsError.value = ''
    turns.value = detail.turns || []
  } catch (e) {
    turns.value = []
    turnsError.value = '取会话历史失败：' + (e as { message?: string })?.message
  }
}

async function forkAt(index: number) {
  const sid = panelSession.value
  if (!sid) return
  try {
    const res = await harnessApi.forkSession(sid, index)
    forkNote.value = res?.ok
      ? `已从第 ${index + 1} 轮分叉：${res.session_id || ''}`
      : (res?.error || res?.message || '分叉失败')
  } catch (e) {
    forkNote.value = '分叉失败：' + (e as { message?: string })?.message
  }
}

watch(panelSession, () => { void loadTurns() })
watch(tab, (t) => { if (t === 'timeline') void loadTurns() })
onMounted(() => {
  loadStored()
  void loadTurns()
})

function clock(ts: number): string {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}
function pretty(data: unknown): string {
  try { return JSON.stringify(data, null, 2) } catch { return String(data) }
}
function turnTitle(t: SessionTurn): string {
  const raw = (t.user || '').replace(/\s+/g, ' ').trim()
  return raw.length > 42 ? raw.slice(0, 42) + '…' : (raw || '（无提问）')
}
</script>

<template>
  <div class="wb-rcp">
    <!-- 入口：右边缘的竖标签（浮层关着时才露出来） -->
    <button
      v-if="!open"
      type="button"
      class="wb-rcp-handle"
      title="打开对话面板（对话 / 原始输出 / 时间线）"
      @click="toggle"
    >
      对话面板
    </button>

    <section v-else class="wb-rcp-panel" :style="{ width: width + 'px' }" aria-label="对话面板">
      <header class="wb-rcp-head">
        <nav class="wb-rcp-tabs" role="tablist">
          <button
            v-for="t in TABS"
            :key="t.id"
            type="button"
            role="tab"
            class="wb-rcp-tab"
            :class="{ on: tab === t.id }"
            :aria-selected="tab === t.id"
            @click="tab = t.id"
          >
            {{ t.label }}
            <span v-if="t.id === 'output' && eventCount" class="wb-rcp-badge">{{ eventCount }}</span>
          </button>
        </nav>
        <button type="button" class="wb-rcp-close" title="收起面板" @click="close">×</button>
      </header>

      <div class="wb-rcp-body">
        <!-- 对话：真正的 ChatDock，独立会话（scope=panel） -->
        <ChatDock v-if="tab === 'chat'" scope="panel" @session="panelSession = $event" />

        <!-- 原始输出：SSE 事件原样流（内存态） -->
        <div v-else-if="tab === 'output'" class="wb-rcp-output">
          <div class="wb-rcp-output-bar">
            <span>本机内存里的 SSE 事件流，刷新即清空；不记提问与代码原文。</span>
            <button type="button" class="wb-rcp-mini" :disabled="!eventCount" @click="clearRawOutput()">清空</button>
          </div>
          <p v-if="!eventCount" class="wb-rcp-empty">还没有事件——发一轮对话就有了。</p>
          <article v-for="e in events" :key="e.id" class="wb-rcp-ev" :data-type="e.type">
            <header>
              <span class="wb-rcp-ev-type">{{ e.type }}</span>
              <span class="wb-rcp-ev-time">{{ clock(e.at) }}</span>
            </header>
            <p class="wb-rcp-ev-sum">{{ e.summary }}</p>
            <details>
              <summary>原文</summary>
              <pre>{{ pretty(e.data) }}</pre>
            </details>
          </article>
        </div>

        <!-- 时间线：本会话检查点 + 运行时事件轴 -->
        <div v-else class="wb-rcp-timeline">
          <section class="wb-rcp-block">
            <h4>本会话检查点</h4>
            <p v-if="turnsError" class="wb-rcp-empty">{{ turnsError }}</p>
            <p v-else-if="!turns.length" class="wb-rcp-empty">这一会话还没有可分叉的回合。</p>
            <ol v-else class="wb-rcp-turns">
              <li v-for="(t, i) in turns" :key="t.turn_id || i">
                <div class="wb-rcp-turn-head">
                  <span class="wb-rcp-turn-idx">第 {{ i + 1 }} 轮</span>
                  <span v-if="t.checkpoint_id" class="wb-rcp-cp" title="带检查点，可从这里分叉">检查点</span>
                </div>
                <p class="wb-rcp-turn-q">{{ turnTitle(t) }}</p>
                <button type="button" class="wb-rcp-mini" @click="forkAt(i)">分叉到这里</button>
              </li>
            </ol>
            <p v-if="forkNote" class="wb-rcp-note">{{ forkNote }}</p>
          </section>
          <section class="wb-rcp-block">
            <h4>运行时事件轴</h4>
            <RuntimeTimeline />
          </section>
        </div>
      </div>

      <!-- 左边缘拖拽条：改宽度 -->
      <div class="wb-rcp-resize" title="拖动改宽度" @pointerdown="onDragStart" />
    </section>
  </div>
</template>

<style scoped>
.wb-rcp { position: relative; }

.wb-rcp-handle {
  position: fixed;
  right: 0;
  top: 50%;
  transform: translateY(-50%);
  z-index: 40;
  writing-mode: vertical-rl;
  padding: 14px 5px;
  border: 1px solid var(--border);
  border-right: 0;
  border-radius: var(--r-sm) 0 0 var(--r-sm);
  background: var(--bg-raised);
  color: var(--text-muted);
  font-family: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
  box-shadow: -2px 0 12px color-mix(in srgb, var(--ink) 10%, transparent);
}
.wb-rcp-handle:hover { color: var(--text); background: var(--bg-hover); }

.wb-rcp-panel {
  position: fixed;
  right: 0;
  top: 0;
  bottom: 0;
  z-index: 45;
  display: flex;
  flex-direction: column;
  background: var(--bg);
  border-left: 1px solid var(--border);
  box-shadow: -8px 0 28px color-mix(in srgb, var(--ink) 14%, transparent);
}

.wb-rcp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px 6px 10px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
}
.wb-rcp-tabs { display: flex; gap: 4px; flex: 1 1 auto; min-width: 0; }
.wb-rcp-tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px;
  border: 1px solid transparent;
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-muted);
  font-family: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
}
.wb-rcp-tab:hover { background: var(--bg-hover); color: var(--text); }
.wb-rcp-tab.on {
  border-color: var(--border);
  background: var(--bg);
  color: var(--text);
  font-weight: 600;
}
.wb-rcp-badge {
  padding: 0 5px;
  border-radius: var(--r-full, 999px);
  background: var(--accent-soft);
  color: var(--accent-text);
  font-size: var(--fs-cap);
  font-variant-numeric: tabular-nums;
}
.wb-rcp-close {
  flex: none;
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
}
.wb-rcp-close:hover { background: var(--bg-hover); color: var(--text); }

.wb-rcp-body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }

.wb-rcp-output, .wb-rcp-timeline {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 12px 24px;
}
.wb-rcp-output-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-faint);
  font-size: var(--fs-cap);
  margin-bottom: 8px;
}
.wb-rcp-output-bar span { flex: 1 1 auto; }
.wb-rcp-mini {
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-muted);
  font-family: inherit;
  font-size: var(--fs-cap);
  cursor: pointer;
}
.wb-rcp-mini:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.wb-rcp-mini:disabled { opacity: .5; cursor: default; }

.wb-rcp-empty { color: var(--text-faint); font-size: var(--fs-sm); margin: 8px 0; }

.wb-rcp-ev {
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  padding: 6px 9px;
  margin-bottom: 6px;
  background: color-mix(in srgb, var(--bg-raised) 55%, transparent);
}
.wb-rcp-ev header { display: flex; align-items: center; gap: 8px; }
.wb-rcp-ev-type {
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  color: var(--accent-text);
}
.wb-rcp-ev[data-type="error"] .wb-rcp-ev-type { color: var(--danger); }
.wb-rcp-ev-time { color: var(--text-faint); font-size: var(--fs-cap); font-variant-numeric: tabular-nums; }
.wb-rcp-ev-sum {
  margin: 3px 0 0;
  color: var(--text-muted);
  font-size: var(--fs-sm);
  word-break: break-word;
}
.wb-rcp-ev details { margin-top: 4px; }
.wb-rcp-ev summary { color: var(--text-faint); font-size: var(--fs-cap); cursor: pointer; }
.wb-rcp-ev pre {
  margin: 4px 0 0;
  max-height: 220px;
  overflow: auto;
  padding: 6px 8px;
  border-radius: var(--r-xs);
  background: color-mix(in srgb, var(--bg) 70%, transparent);
  color: var(--text-maint);
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  white-space: pre-wrap;
  word-break: break-word;
}

.wb-rcp-block { margin-bottom: 18px; }
.wb-rcp-block h4 {
  margin: 0 0 6px;
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
}
.wb-rcp-turns { margin: 0; padding-left: 18px; }
.wb-rcp-turns li { margin-bottom: 8px; }
.wb-rcp-turn-head { display: flex; align-items: center; gap: 6px; }
.wb-rcp-turn-idx { color: var(--text-muted); font-size: var(--fs-cap); }
.wb-rcp-cp {
  padding: 0 5px;
  border-radius: var(--r-xs);
  background: color-mix(in srgb, var(--green) 12%, transparent);
  color: var(--green);
  font-size: var(--fs-cap);
}
.wb-rcp-turn-q {
  margin: 2px 0 4px;
  color: var(--text);
  font-size: var(--fs-sm);
  word-break: break-word;
}
.wb-rcp-note { margin: 6px 0 0; color: var(--text-muted); font-size: var(--fs-cap); }

.wb-rcp-resize {
  position: absolute;
  left: -3px;
  top: 0;
  bottom: 0;
  width: 6px;
  cursor: col-resize;
  z-index: 2;
}
.wb-rcp-resize:hover { background: color-mix(in srgb, var(--accent) 18%, transparent); }
</style>
