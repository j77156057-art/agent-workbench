<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { askConfirm } from '../composables/dialogs'
import { planApi } from '../api'
import type { PlanLogRow, PlanStatus, PlanTask } from '../api'

const props = defineProps<{ visible: boolean; sessionId: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const goal = ref('')
const reviewMinutes = ref(30)
const data = ref<PlanStatus | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
let poll: ReturnType<typeof setInterval> | null = null

const state = computed(() => data.value?.state || null)
const active = computed(() => !!data.value?.active)
const tasks = computed<PlanTask[]>(() => data.value?.taskbook?.items || [])
const pending = computed(() => tasks.value.filter(
  task => !['done', 'done_pending_merge'].includes(String(task.state || '')),
))
const logRows = computed<PlanLogRow[]>(() => (data.value?.log || []).slice(0, 40))

const STATE_LABEL: Record<string, string> = {
  todo: '待办', doing: '进行中', done_pending_merge: '待验收', done: '已完成',
}

const KIND_LABEL: Record<string, string> = {
  start: '启动', turn: '执行轮', review: '复盘', decision: '决策', stop: '停止', error: '错误',
}

function kindLabel(value: unknown): string {
  return KIND_LABEL[String(value || '')] || String(value || '')
}

/** 徽章讲的是「这个会话的长跑现在归谁管」，不是「这块界面在哪个产品里」——
 *  面板本来就只挂在开发台，写「仅在开发台」等于永远在说一句废话。 */
const badge = computed(() => {
  if (active.value) return state.value?.status === 'reviewing' ? '定期复盘中' : '运行中'
  const status = String(state.value?.status || '')
  if (status === 'stopped') return '已由用户停止'
  if (status === 'error') return '连续出错已停下'
  if (status === 'interrupted') return '上次中断（服务重启？）'
  return '未启动'
})

function stateLabel(value: unknown): string {
  return STATE_LABEL[String(value || '')] || String(value || '')
}

function timeLabel(value: unknown): string {
  const text = String(value || '')
  if (!text) return ''
  const stamp = new Date(text)
  return Number.isNaN(stamp.getTime()) ? text : stamp.toLocaleString()
}

function stopPoll() {
  if (poll) {
    clearInterval(poll)
    poll = null
  }
}

function schedulePoll() {
  stopPoll()
  if (!props.visible) return
  if (active.value || ['running', 'reviewing'].includes(String(state.value?.status || ''))) {
    poll = setInterval(() => { void load(true) }, 5000)
  }
}

async function load(silent = false) {
  if (!props.visible || !props.sessionId) return
  if (!silent) loading.value = true
  error.value = ''
  try {
    data.value = await planApi.status(props.sessionId)
    if (data.value?.state?.goal && !goal.value) goal.value = data.value.state.goal
    schedulePoll()
  } catch (e) {
    error.value = (e as Error)?.message || '读取 Plan 状态失败'
    stopPoll()
  } finally {
    loading.value = false
  }
}

async function start() {
  const text = goal.value.trim()
  if (!text || busy.value || active.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    data.value = await planApi.start(props.sessionId, text, Math.max(1, Number(reviewMinutes.value) || 30))
    notice.value = 'Plan 已启动：模型会连续执行，任务全部划掉后进入定期复盘'
    schedulePoll()
  } catch (e) {
    error.value = (e as Error)?.message || '启动失败'
  } finally {
    busy.value = false
  }
}

async function stop() {
  if (!active.value || busy.value) return
  const confirmed = await askConfirm({
    title: '停止 Plan 长跑',
    message: '停止后模型不再执行任务或自动复盘。已产生的任务书、决策账本与代码都会保留。',
    confirmText: '停止 Plan',
    danger: true,
  })
  if (!confirmed) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    data.value = await planApi.stop(props.sessionId)
    notice.value = 'Plan 已停止'
    stopPoll()
  } catch (e) {
    error.value = (e as Error)?.message || '停止失败'
  } finally {
    busy.value = false
  }
}

watch(() => props.visible, visible => {
  if (visible) void load()
  else stopPoll()
})
watch(() => props.sessionId, () => { if (props.visible) void load() })
onBeforeUnmount(stopPoll)
</script>

<template>
  <aside v-if="visible" class="pp" role="complementary" aria-label="Plan 长跑">
    <header class="pp-head">
      <strong>Plan 长跑</strong>
      <span class="pp-badge" :class="active ? 'on' : ''">{{ badge }}</span>
      <button class="pp-close" type="button" aria-label="关闭 Plan 面板" @click="emit('close')">×</button>
    </header>

    <p class="pp-hint">
      模型连续执行任务书，全部划掉后进入定期复盘（审查 / 测试 / 完善 / 改进）。
      只有用户可以停止；关闭面板不会停止。
    </p>

    <p v-if="error" class="pp-error">{{ error }}</p>
    <p v-else-if="notice" class="pp-ok">{{ notice }}</p>

    <div class="pp-body">
      <template v-if="!active">
        <label class="pp-label">本次长跑目标</label>
        <textarea v-model="goal" class="pp-textarea" rows="4" placeholder="例如：把登录模块补完并跑通全部验收；我会离开一段时间。" />
        <div class="pp-row">
          <label class="pp-label pp-inline">复盘间隔（分钟）</label>
          <input v-model.number="reviewMinutes" type="number" min="1" max="1440" class="pp-input pp-num" />
          <button class="pp-btn pp-primary" type="button" :disabled="busy || !goal.trim()" @click="start">
            {{ busy ? '启动中…' : '启动 Plan' }}
          </button>
        </div>
      </template>

      <div v-else class="pp-active">
        <div class="pp-status">
          <span class="pp-state">{{ state?.status === 'reviewing' ? '定期复盘' : '执行中' }}</span>
          <span class="pp-meta">第 {{ state?.iteration || 0 }} 轮</span>
          <button class="pp-btn pp-danger" type="button" :disabled="busy" @click="stop">停止 Plan</button>
        </div>
        <p class="pp-goal">{{ state?.goal }}</p>
        <p v-if="state?.next_review_at" class="pp-meta">下次复盘：{{ timeLabel(state.next_review_at) }}</p>
        <p v-if="state?.last_final" class="pp-last">{{ state.last_final }}</p>
      </div>

      <div class="pp-section">
        <div class="pp-section-head">
          <strong>任务书</strong>
          <span>{{ data?.taskbook?.done || 0 }}/{{ data?.taskbook?.total || 0 }} 已完成</span>
        </div>
        <p v-if="!tasks.length" class="pp-meta">还没有任务书；启动后模型会先建计划。</p>
        <div v-for="task in tasks" :key="String(task.key || task.text)" class="pp-task">
          <span class="pp-task-state" :class="String(task.state || '')">{{ stateLabel(task.state) }}</span>
          <span>{{ task.text }}</span>
        </div>
        <p v-if="pending.length && active" class="pp-meta">剩余 {{ pending.length }} 项；模型会继续执行。</p>
      </div>

      <div class="pp-section">
        <div class="pp-section-head">
          <strong>决策账本</strong>
          <span>{{ logRows.length }} 条</span>
        </div>
        <p v-if="!logRows.length" class="pp-meta">还没有记录。</p>
        <div v-for="(row, index) in logRows" :key="`${row.ts}-${index}`" class="pp-log">
          <div class="pp-log-head">
            <span class="pp-log-kind">{{ kindLabel(row.kind) }}</span>
            <span class="pp-meta">{{ timeLabel(row.ts) }}</span>
          </div>
          <p class="pp-log-text">{{ row.text }}</p>
          <details v-if="row.actions?.length">
            <summary>{{ row.actions.length }} 个动作</summary>
            <pre class="pp-pre">{{ row.actions.join('\n') }}</pre>
          </details>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.pp {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 401;
  width: min(520px, 94vw);
  display: flex;
  flex-direction: column;
  background: var(--bg-raised);
  border-left: 1px solid var(--border-strong);
  box-shadow: -12px 0 34px rgba(35, 52, 84, .16);
  color: var(--text);
  font-size: var(--fs-sm);
}
.pp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.pp-head strong { font-size: var(--fs-md); }
.pp-badge {
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 7px;
  color: var(--text-muted);
  font-size: var(--fs-cap);
}
.pp-badge.on { color: var(--green); border-color: color-mix(in srgb, var(--green) 45%, var(--border)); }
.pp-close { margin-left: auto; border: 0; background: transparent; color: var(--text-muted); font-size: 20px; cursor: pointer; }
.pp-hint, .pp-meta { color: var(--text-muted); font-size: var(--fs-cap); margin: 6px 14px; }
.pp-ok { margin: 6px 14px; color: var(--green); font-size: var(--fs-cap); }
.pp-error { margin: 6px 14px; color: var(--danger); font-size: var(--fs-cap); }
.pp-body { flex: 1 1 auto; overflow-y: auto; padding: 4px 14px 14px; }
.pp-label { display: block; color: var(--text-muted); font-size: var(--fs-cap); margin: 8px 0 4px; }
.pp-inline { display: inline; margin: 0; }
.pp-textarea, .pp-input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font: inherit;
  padding: 7px 8px;
}
.pp-textarea { resize: vertical; margin-bottom: 6px; }
.pp-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pp-num { width: 74px; margin: 0; }
.pp-btn {
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg-hover);
  color: var(--text);
  padding: 5px 11px;
  font-size: var(--fs-cap);
  cursor: pointer;
}
.pp-btn:disabled { opacity: .45; cursor: default; }
.pp-primary { margin-left: auto; border-color: var(--accent); color: var(--accent); }
.pp-danger { color: var(--danger); }
.pp-active { border: 1px solid var(--border); border-radius: 8px; padding: 8px; margin: 8px 0; background: var(--bg); }
.pp-status { display: flex; align-items: center; gap: 8px; }
.pp-status .pp-btn { margin-left: auto; }
.pp-state { color: var(--green); font-weight: 600; }
.pp-goal { margin: 6px 0 0; white-space: pre-wrap; line-height: 1.6; }
.pp-last { color: var(--text-muted); font-size: var(--fs-cap); white-space: pre-wrap; max-height: 120px; overflow: auto; }
.pp-section { margin-top: 12px; padding-top: 8px; border-top: 1px dashed var(--border); }
.pp-section-head { display: flex; justify-content: space-between; gap: 8px; color: var(--text-muted); font-size: var(--fs-cap); margin-bottom: 6px; }
.pp-task { display: flex; gap: 7px; align-items: flex-start; padding: 3px 0; font-size: var(--fs-cap); line-height: 1.5; }
.pp-task-state { flex: 0 0 auto; color: var(--text-muted); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; }
.pp-task-state.done { color: var(--green); }
.pp-task-state.doing { color: var(--amber); }
.pp-log { border-bottom: 1px dashed var(--border); padding: 6px 0; }
.pp-log-head { display: flex; justify-content: space-between; gap: 8px; }
.pp-log-kind { color: var(--accent); font-size: var(--fs-cap); }
.pp-log-text { margin: 3px 0; color: var(--text-dim); font-size: var(--fs-cap); line-height: 1.55; white-space: pre-wrap; }
.pp-log summary { color: var(--text-muted); cursor: pointer; font-size: var(--fs-cap); }
.pp-pre { margin: 4px 0 0; padding: 7px; border: 1px dashed var(--border); border-radius: 6px; background: var(--bg); color: var(--text-muted); font: 11px/1.5 var(--font-mono); max-height: 180px; overflow: auto; white-space: pre-wrap; }
.pp :deep(button:focus-visible), .pp :deep(textarea:focus-visible), .pp :deep(input:focus-visible) {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
</style>
