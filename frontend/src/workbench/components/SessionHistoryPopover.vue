<script setup lang="ts">
// 会话历史弹层（薄封装）：列表渲染与展示格式化内聚于此，
// 数据加载、会话切换、新建/删除/清空等副作用全部留在父组件，通过 emit 上抛。
import { computed, ref, watch } from 'vue'
import { harnessApi, type SessionInfo, type SessionSnapshot, type SessionTurn } from '../api'

const props = defineProps<{
  open: boolean
  items: SessionInfo[]
  busy: boolean
  error: string
  currentId: string
  generating: boolean
  /** 是否允许「清空当前对话内容」（当前有消息，或 demo 模式） */
  canClear: boolean
}>()

const query = ref('')
const filteredItems = computed(() => {
  const needle = query.value.trim().toLowerCase()
  if (!needle) return props.items
  return props.items.filter((item) => [
    sessionTitle(item), item.session_id, item.preview || '', item.updated_at || '', String(item.turns ?? ''),
  ].join(' ').toLowerCase().includes(needle))
})

const snapshotFor = ref('')
const snapshots = ref<SessionSnapshot[]>([])
const snapshotLoading = ref(false)
const snapshotError = ref('')
let snapshotRequest = 0
const snapshotTitle = computed(() => {
  const item = props.items.find((x) => x.session_id === snapshotFor.value)
  return item ? sessionTitle(item) : snapshotFor.value
})

const rewindTarget = ref<SessionSnapshot | null>(null)
const rewindStage = ref<'preview' | 'confirm'>('preview')
const rewindLoading = ref(false)
const rewindBusy = ref(false)
const rewindError = ref('')
const rewindNotice = ref('')
const rewindCurrentCount = ref(0)
const discardedTurns = ref<SessionTurn[]>([])
const rewindAfter = computed(() => rewindTarget.value?.total_turn_count ?? rewindTarget.value?.turn_count ?? 0)
const rewindGenerating = computed(() => props.generating && snapshotFor.value === props.currentId)
const rewindCanContinue = computed(() => !rewindLoading.value && !rewindError.value
  && !rewindGenerating.value && discardedTurns.value.length > 0)

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'refresh'): void
  (e: 'create'): void
  (e: 'select', sessionId: string): void
  (e: 'delete', sessionId: string): void
  (e: 'fork', sessionId: string, atSeq?: number): void
  (e: 'rewound', sessionId: string): void
  (e: 'clear'): void
}>()

function sessionTitle(item: SessionInfo): string {
  return (item.title || item.preview || '').trim() || '未命名对话'
}
function sessionTime(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const delta = Math.max(0, Date.now() - date.getTime())
  if (delta < 60_000) return '刚刚'
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`
  return date.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })
}
function snapshotTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString()
}
function resetRewind() {
  rewindTarget.value = null
  rewindStage.value = 'preview'
  rewindLoading.value = false
  rewindError.value = ''
  rewindCurrentCount.value = 0
  discardedTurns.value = []
}
function turnSummary(value: string): string {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  return text ? (text.length > 96 ? `${text.slice(0, 96)}…` : text) : '（无内容摘要）'
}
function turnUnfinished(turn: SessionTurn): boolean {
  const status = String(turn.status || '').toLowerCase()
  return ['streaming', 'running', 'pending', 'in_progress'].includes(status)
    || turn.completed === false || turn.done === false
}
async function openRewindPreview(snapshot: SessionSnapshot) {
  const sessionId = snapshotFor.value
  rewindTarget.value = snapshot
  rewindStage.value = 'preview'
  rewindNotice.value = ''
  rewindError.value = ''
  rewindLoading.value = true
  discardedTurns.value = []
  try {
    const detail = await harnessApi.sessionDetail(sessionId)
    if (rewindTarget.value?.seq !== snapshot.seq || snapshotFor.value !== sessionId) return
    rewindCurrentCount.value = detail.turns.length
    const after = snapshot.total_turn_count ?? snapshot.turn_count
    if (after >= detail.turns.length) {
      rewindError.value = '当前会话没有可丢弃的后续轮次，或快照已不适用。'
      return
    }
    if (detail.turns.some(turnUnfinished)) {
      rewindError.value = '会话里有未完成的轮次，请完成或停止后再回退。'
      return
    }
    discardedTurns.value = detail.turns.slice(after)
  } catch (e) {
    if (rewindTarget.value?.seq === snapshot.seq) {
      rewindError.value = (e as Error).message || '影响预览读取失败'
    }
  } finally {
    if (rewindTarget.value?.seq === snapshot.seq) rewindLoading.value = false
  }
}
async function executeRewind() {
  const target = rewindTarget.value
  const sessionId = snapshotFor.value
  if (!target || rewindBusy.value || rewindGenerating.value || !discardedTurns.value.length) return
  const after = rewindAfter.value
  rewindBusy.value = true
  rewindError.value = ''
  try {
    const result = await harnessApi.rewindSession(sessionId, target.seq)
    const backupSeq = Number(result.backup_seq || 0)
    rewindNotice.value = backupSeq
      ? `已回退到 ${after} 轮；回退前状态已保存为快照 #${backupSeq}，可从时间轴分叉。`
      : `已回退到 ${after} 轮；后端未返回备份编号，请检查时间轴。`
    resetRewind()
    emit('rewound', sessionId)
    await loadSnapshots(sessionId)
  } catch (e) {
    rewindError.value = (e as Error).message || '回退失败，会话未更新'
  } finally {
    rewindBusy.value = false
  }
}
function retryRewind() {
  if (rewindStage.value === 'confirm') void executeRewind()
  else if (rewindTarget.value) void openRewindPreview(rewindTarget.value)
}
async function loadSnapshots(id: string) {
  const ticket = ++snapshotRequest
  snapshotFor.value = id
  snapshots.value = []
  snapshotError.value = ''
  snapshotLoading.value = true
  try {
    const result = await harnessApi.sessionSnapshots(id)
    if (ticket !== snapshotRequest) return
    snapshots.value = (result.items || []).slice().reverse()
  } catch (e) {
    if (ticket !== snapshotRequest) return
    snapshotError.value = (e as Error).message || '快照读取失败'
  } finally {
    if (ticket === snapshotRequest) snapshotLoading.value = false
  }
}
function toggleSnapshots(id: string) {
  resetRewind()
  rewindNotice.value = ''
  if (snapshotFor.value === id) {
    snapshotRequest += 1
    snapshotFor.value = ''
    snapshotLoading.value = false
    return
  }
  void loadSnapshots(id)
}
watch(() => props.open, (value) => {
  if (!value) {
    snapshotRequest += 1
    resetRewind()
    rewindNotice.value = ''
    snapshotFor.value = ''
    snapshotLoading.value = false
  }
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="cd-pop-mask wb-modal-backdrop" @click="emit('close')" />
    <div v-if="open" class="cd-session-pop wb-modal-shell" :class="{ 'cd-session-pop-rewind': rewindTarget }" role="dialog" aria-modal="true" aria-label="会话历史" @click.stop>
      <div class="cd-session-head">
        <div>
          <div class="cd-pop-title">会话历史</div>
          <div class="cd-session-subtitle">当前项目的对话会单独保存，切换后会恢复完整问答。</div>
        </div>
        <button class="cd-mini" :disabled="busy" title="刷新会话列表" @click="emit('refresh')">刷新</button>
      </div>
      <button class="cd-session-new" :disabled="busy" @click="emit('create')">
        <span class="cd-session-new-icon">＋</span>
        <span><b>新建对话</b><small>开启一段空白会话，不影响历史记录</small></span>
      </button>
      <label class="cd-session-search">
        <span>检索会话</span>
        <input v-model="query" type="search" placeholder="标题、首条提问或会话 ID" aria-label="搜索会话标题或会话 ID" />
        <span v-if="query" class="cd-session-search-count">{{ filteredItems.length }} / {{ items.length }}</span>
      </label>
      <div v-if="error" class="cd-session-error">{{ error }}</div>
      <div v-if="!items.length && !error" class="cd-session-empty">还没有已保存的对话</div>
      <div v-else-if="query && !filteredItems.length" class="cd-session-empty">没有匹配的会话，试试其它关键词。</div>
      <div v-for="item in filteredItems" :key="item.session_id" class="cd-session-item" :class="{ active: item.session_id === currentId }">
        <button class="cd-session-main" :disabled="busy" @click="emit('select', item.session_id)">
          <span class="cd-session-current" aria-hidden="true">{{ item.session_id === currentId ? '●' : '○' }}</span>
          <span class="cd-session-copy">
            <strong>{{ sessionTitle(item) }}</strong>
            <small>{{ item.turns }} 轮<span v-if="sessionTime(item.updated_at)"> · {{ sessionTime(item.updated_at) }}</span></small>
          </span>
        </button>
        <button
          class="cd-session-fork"
          :class="{ 'cd-session-action-on': snapshotFor === item.session_id }"
          :aria-expanded="snapshotFor === item.session_id"
          :disabled="busy"
          @click.stop="toggleSnapshots(item.session_id)"
        >快照</button>
        <button
          class="cd-session-fork"
          :title="`分叉会话：${sessionTitle(item)}（保留原会话）`"
          :disabled="busy"
          @click.stop="emit('fork', item.session_id)"
        >分叉</button>
        <button
          class="cd-session-delete"
          :title="item.session_id === currentId ? '清空当前会话' : '删除这条对话历史'"
          :disabled="busy"
          @click.stop="emit('delete', item.session_id)"
        >×</button>
      </div>
      <section v-if="snapshotFor && !rewindTarget" class="cd-snapshots" aria-label="会话快照时间轴">
        <div class="cd-snapshot-head">
          <div><b>快照时间轴</b><small>{{ snapshotTitle }} · 分叉不修改原会话</small></div>
          <button class="cd-mini" @click="toggleSnapshots(snapshotFor)">收起</button>
        </div>
        <div v-if="rewindNotice" class="cd-rewind-notice" role="status">{{ rewindNotice }}</div>
        <div v-if="snapshotLoading" class="cd-session-empty">正在读取快照…</div>
        <div v-else-if="snapshotError" class="cd-snapshot-error" role="alert">
          <span>{{ snapshotError }}</span>
          <button class="cd-mini" @click="loadSnapshots(snapshotFor)">重试</button>
        </div>
        <div v-else-if="!snapshots.length" class="cd-session-empty">该会话还没有快照。</div>
        <ol v-else class="cd-snapshot-list">
          <li v-for="snapshot in snapshots" :key="snapshot.seq">
            <span class="cd-snapshot-dot" aria-hidden="true"></span>
            <div class="cd-snapshot-copy">
              <b>第 {{ snapshot.total_turn_count ?? snapshot.turn_count }} 轮后<span>· #{{ snapshot.seq }}</span></b>
              <small>{{ snapshotTime(snapshot.at) }}</small>
              <p>{{ snapshot.label || snapshot.question || '未命名快照' }}</p>
            </div>
            <div class="cd-snapshot-actions">
              <button class="cd-session-fork" :disabled="busy || rewindBusy" @click="emit('fork', snapshotFor, snapshot.seq)">从此处分叉</button>
              <button
                class="cd-session-fork cd-rewind-link"
                :disabled="busy || rewindBusy || rewindGenerating"
                :title="rewindGenerating ? '会话正在生成中，请停止或等待完成后再回退' : '预览回退影响'"
                @click="openRewindPreview(snapshot)"
              >回退到此</button>
            </div>
          </li>
        </ol>
      </section>
      <section v-if="rewindTarget" class="cd-rewind-panel" :aria-label="rewindStage === 'preview' ? '回退影响预览' : '确认不可恢复回退'">
        <div class="cd-rewind-head">
          <div>
            <b>{{ rewindStage === 'preview' ? '回退影响预览' : '确认不可恢复回退' }}</b>
            <small>目标快照 #{{ rewindTarget.seq }} · {{ snapshotTitle }}</small>
          </div>
          <button class="cd-mini" :disabled="rewindBusy" @click="resetRewind">取消</button>
        </div>
        <div v-if="rewindLoading" class="cd-session-empty">正在计算会影响哪些轮次…</div>
        <template v-else>
          <div v-if="rewindError" class="cd-rewind-error" role="alert">
            <span>{{ rewindError }}</span>
            <button class="cd-mini" :disabled="rewindBusy" @click="retryRewind">重试</button>
          </div>
          <template v-else>
            <div class="cd-rewind-metrics" aria-label="回退影响汇总">
              <span><small>当前</small><b>{{ rewindCurrentCount }} 轮</b></span>
              <span><small>回退后</small><b>{{ rewindAfter }} 轮</b></span>
              <span class="danger"><small>永久丢弃</small><b>{{ discardedTurns.length }} 轮</b></span>
            </div>
            <template v-if="rewindStage === 'preview'">
              <ol class="cd-rewind-turns">
                <li v-for="(turn, index) in discardedTurns" :key="index">
                  <b>第 {{ rewindAfter + index + 1 }} 轮</b>
                  <p><span>问题</span>{{ turnSummary(turn.user) }}</p>
                  <p><span>回答</span>{{ turnSummary(turn.assistant) }}</p>
                </li>
              </ol>
              <p class="cd-rewind-backup">执行前会自动保存当前 {{ rewindCurrentCount }} 轮为备份快照；成功后可从时间轴分叉。备份不会阻止原会话丢弃上述轮次。</p>
              <p v-if="rewindGenerating" class="cd-rewind-error">会话正在生成中，请停止或等待完成后再回退。</p>
              <div class="cd-rewind-actions">
                <button class="cd-mini" @click="resetRewind">取消</button>
                <button class="cd-rewind-next" :disabled="!rewindCanContinue" @click="rewindStage = 'confirm'">继续确认</button>
              </div>
            </template>
            <template v-else>
              <p class="cd-rewind-warning"><b>此操作不可恢复。</b>原会话将永久丢弃第 {{ rewindAfter + 1 }} 至 {{ rewindCurrentCount }} 轮，只保留前 {{ rewindAfter }} 轮。</p>
              <p class="cd-rewind-backup">回退前快照仅用于之后新建分叉，不会让原会话自动恢复。</p>
              <div class="cd-rewind-actions">
                <button class="cd-mini" :disabled="rewindBusy" @click="rewindStage = 'preview'">返回预览</button>
                <button class="cd-rewind-confirm" :disabled="rewindBusy || rewindGenerating" @click="executeRewind">
                  {{ rewindBusy ? '正在回退…' : '确认不可恢复回退' }}
                </button>
              </div>
            </template>
          </template>
        </template>
      </section>
      <button
        class="cd-session-clear"
        :disabled="busy || !canClear"
        title="清空当前对话的全部消息（会话记录本身保留）"
        @click="emit('clear')"
      >清空当前对话内容</button>
    </div>
  </Teleport>
</template>

<style scoped>
.cd-pop-mask {
  position: fixed;
  inset: 0;
  z-index: 59;
  background: var(--scrim-soft);
  backdrop-filter: blur(1px);
  animation: sh-fade-in .16s ease-out both;
}
.cd-pop-title { font-size: var(--fs-sm); font-weight: 600; color: var(--text); margin-bottom: 8px; }
.cd-mini {
  height: 21px; padding: 0 9px; font-size: var(--fs-cap);
  border: 1px solid var(--border-strong); border-radius: 5px;
  background: transparent; color: var(--text-muted); cursor: pointer;
}
.cd-mini:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
.cd-mini:disabled { opacity: .45; cursor: default; }
.cd-mini:focus-visible,
.cd-session-new:focus-visible,
.cd-session-main:focus-visible,
.cd-session-delete:focus-visible,
.cd-session-clear:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 2px; }
.cd-mini, .cd-session-new, .cd-session-main, .cd-session-delete, .cd-session-clear { transition: color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease, opacity .16s ease; }
.cd-session-pop {
  position: fixed;
  right: 10px; bottom: 64px;
  width: 360px; max-width: calc(100vw - 24px);
  max-height: min(62vh, 430px); overflow-y: auto;
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-radius: var(--mi-radius-md); box-shadow: var(--mi-shadow-pop);
  padding: 10px; z-index: 60;
  animation: sh-pop-in .2s cubic-bezier(.2,.8,.2,1) both;
}
.cd-session-head { display: flex; align-items: flex-start; gap: 8px; justify-content: space-between; }
.cd-session-subtitle { color: var(--text-faint); font-size: var(--fs-cap); line-height: 1.45; margin-top: -4px; }
.cd-session-new {
  width: 100%; display: flex; align-items: center; gap: 9px; margin: 8px 0;
  padding: 8px 9px; border: 1px solid var(--mi-border); border-radius: var(--mi-radius-sm);
  background: var(--mi-surface); color: var(--accent); text-align: left; cursor: pointer;
}
.cd-session-new:hover { background: var(--bg-selected); border-color: var(--accent); }
.cd-session-new:disabled { opacity: .55; cursor: default; }
.cd-session-new-icon { width: 20px; height: 20px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid currentColor; border-radius: 50%; font-size: var(--fs-h2); line-height: 1; }
.cd-session-new b, .cd-session-new small { display: block; }
.cd-session-new b { font-size: var(--fs-sm); }
.cd-session-new small { margin-top: 2px; color: var(--text-muted); font-size: var(--fs-cap); }
.cd-session-clear {
  width: 100%;
  margin-top: 8px;
  padding: 8px 9px 4px;
  border: 0;
  border-top: 1px solid var(--border);
  background: transparent;
  color: var(--danger);
  font-size: var(--fs-cap);
  text-align: center;
  cursor: pointer;
}
.cd-session-clear:hover:not(:disabled) { background: var(--mi-surface-raised); }
.cd-session-clear:disabled { opacity: .45; cursor: default; }
.cd-session-error { padding: 6px 8px; color: var(--danger); background: var(--danger-soft); border: 1px solid var(--danger); border-radius: var(--mi-radius-sm); font-size: var(--fs-cap); }
.cd-session-empty { padding: 12px 5px; color: var(--text-faint); font-size: var(--fs-cap); text-align: center; }
.cd-session-search { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px; margin: 4px 0 8px; color: var(--text-muted); font-size: var(--fs-cap); }
.cd-session-search input { min-width: 0; height: 30px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 5px; background: var(--surface); color: var(--text); font: inherit; outline: none; }
.cd-session-search input:focus { border-color: var(--accent-text); box-shadow: 0 0 0 2px var(--accent-soft); }
.cd-session-search-count { font-family: var(--ui2-font-mono, var(--font-mono)); font-variant-numeric: tabular-nums; }
.cd-session-item { display: flex; align-items: stretch; border-top: 1px solid var(--border); }
.cd-session-item.active { background: var(--bg-selected); }
.cd-session-main { flex: 1; min-width: 0; display: flex; gap: 7px; align-items: center; padding: 8px 5px; border: 0; background: transparent; color: var(--text); text-align: left; cursor: pointer; }
.cd-session-main:hover { background: var(--bg-hover); }
.cd-session-main:disabled { opacity: .6; cursor: default; }
.cd-session-current { flex: 0 0 14px; color: var(--text-faint); font-size: var(--fs-cap); }
.cd-session-item.active .cd-session-current { color: var(--accent); }
.cd-session-copy { min-width: 0; display: block; }
.cd-session-copy strong, .cd-session-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-session-copy strong { font-size: var(--fs-sm); font-weight: 600; }
.cd-session-copy small { margin-top: 2px; color: var(--text-faint); font-size: var(--fs-cap); }
.cd-session-delete { align-self: center; margin-right: 5px; width: 22px; height: 22px; border: 0; background: transparent; color: var(--text-faint); cursor: pointer; font-size: var(--fs-h2); }
.cd-session-delete:hover { color: var(--danger); }
.cd-session-delete:disabled { cursor: default; opacity: .5; }
.cd-session-fork { align-self: center; margin-right: 2px; padding: 3px 6px; border: 0; border-radius: 4px; background: transparent; color: var(--text-muted); font-size: var(--fs-cap); cursor: pointer; white-space: nowrap; }
.cd-session-fork:hover { color: var(--accent-text); background: var(--bg-hover); }
.cd-session-fork:disabled { cursor: default; opacity: .5; }
.cd-session-action-on { color: var(--accent-text); background: var(--bg-selected); }
.cd-snapshots { margin-top: 6px; padding: 10px 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); }
.cd-snapshot-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.cd-snapshot-head b, .cd-snapshot-head small { display: block; }
.cd-snapshot-head b { font-size: var(--fs-sm); }
.cd-snapshot-head small { margin-top: 3px; color: var(--text-faint); font-size: var(--fs-cap); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 245px; }
.cd-snapshot-error { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 0 2px; color: var(--danger); font-size: var(--fs-cap); }
.cd-snapshot-list { list-style: none; margin: 10px 0 0 7px; padding: 0 0 0 10px; border-left: 1px solid var(--border-strong); }
.cd-snapshot-list li { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 0 0 12px 4px; }
.cd-snapshot-list li:last-child { padding-bottom: 0; }
.cd-snapshot-dot { position: absolute; left: -15px; top: 5px; width: 7px; height: 7px; border: 2px solid var(--bg); border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.cd-snapshot-copy { min-width: 0; }
.cd-snapshot-copy b, .cd-snapshot-copy small { display: block; font-size: var(--fs-cap); }
.cd-snapshot-copy b span { margin-left: 5px; color: var(--text-faint); font-family: var(--font-mono); font-weight: 400; }
.cd-snapshot-copy small { margin-top: 2px; color: var(--text-faint); }
.cd-snapshot-copy p { margin: 4px 0 0; color: var(--text-muted); font-size: var(--fs-cap); line-height: 1.45; overflow-wrap: anywhere; }
.cd-snapshot-actions { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
.cd-rewind-link { color: var(--danger); }
.cd-session-pop-rewind { width: 480px; max-height: min(78vh, 680px); }
.cd-rewind-notice { margin-top: 8px; padding: 7px 8px; border-left: 2px solid var(--green); background: color-mix(in srgb, var(--green) 7%, var(--bg)); color: var(--green); font-size: var(--fs-cap); line-height: 1.55; }
.cd-rewind-panel { margin-top: 8px; padding: 11px; border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border)); border-radius: 6px; background: var(--bg-raised); }
.cd-rewind-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.cd-rewind-head b, .cd-rewind-head small { display: block; }
.cd-rewind-head b { font-size: var(--fs-sm); }
.cd-rewind-head small { margin-top: 3px; color: var(--text-faint); font-size: var(--fs-cap); }
.cd-rewind-metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; margin: 10px 0; border: 1px solid var(--border); background: var(--border); }
.cd-rewind-metrics > span { padding: 7px 9px; background: var(--bg); }
.cd-rewind-metrics small, .cd-rewind-metrics b { display: block; }
.cd-rewind-metrics small { color: var(--text-faint); font-size: var(--fs-cap); }
.cd-rewind-metrics b { margin-top: 2px; font-family: var(--font-mono); font-size: var(--fs-md); }
.cd-rewind-metrics .danger b { color: var(--danger); }
.cd-rewind-turns { list-style: none; margin: 0; padding: 0; max-height: 190px; overflow-y: auto; }
.cd-rewind-turns li { padding: 8px 0; border-top: 1px solid var(--border); }
.cd-rewind-turns li > b { font-size: var(--fs-cap); }
.cd-rewind-turns p { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 6px; margin: 4px 0 0; color: var(--text-muted); font-size: var(--fs-cap); line-height: 1.5; }
.cd-rewind-turns p span { color: var(--text-faint); }
.cd-rewind-backup, .cd-rewind-warning { margin: 9px 0 0; color: var(--text-muted); font-size: var(--fs-cap); line-height: 1.6; }
.cd-rewind-warning { padding: 8px; border-left: 2px solid var(--danger); background: color-mix(in srgb, var(--danger) 7%, var(--bg)); color: var(--danger); }
.cd-rewind-error { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 8px; color: var(--danger); font-size: var(--fs-cap); line-height: 1.5; }
.cd-rewind-actions { display: flex; justify-content: flex-end; gap: 7px; margin-top: 10px; }
.cd-rewind-next, .cd-rewind-confirm { min-height: 28px; padding: 4px 10px; border: 1px solid var(--accent); border-radius: 5px; background: var(--accent); color: var(--accent-ink); cursor: pointer; font-size: var(--fs-cap); }
.cd-rewind-confirm { border-color: var(--danger); background: var(--danger); }
.cd-rewind-next:disabled, .cd-rewind-confirm:disabled { opacity: .45; cursor: default; }
@keyframes sh-fade-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes sh-pop-in { from { opacity: 0; transform: translateY(8px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@media (max-width: 560px) {
  .cd-session-pop { left: 8px; right: 8px; bottom: 8px; width: auto; max-width: none; max-height: calc(100vh - 16px); border-radius: 10px; }
}
@media (prefers-reduced-motion: reduce) {
  .cd-pop-mask, .cd-session-pop, .cd-mini, .cd-session-new, .cd-session-main, .cd-session-delete, .cd-session-clear { animation: none; transition: none; }
}
</style>
