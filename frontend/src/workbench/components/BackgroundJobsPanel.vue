<script setup lang="ts">
// 后台任务面板：AI 用 run_command(background: true) 起的长命令（构建 / 测试 / 服务）
// 在这里统一看进度、实时日志，并可在不打断对话的情况下终止。
//
// 数据源与对话台角标共用 composables/backgroundJobs.ts（模块级单例 + 引用计数），
// 所以面板打开/关闭不会另起一套轮询；这里只额外轮询"选中任务"的日志窗口。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { jobsApi } from '../api'
import type { BackgroundJob } from '../api'
import { askConfirm, useWorkbench } from '../composables/workbench'
import { isRunningJob, useJobsFeed } from '../composables/backgroundJobs'
import { usePolling } from '../composables/polling'

const { jobsOpen, closeJobs } = useWorkbench()
const { jobs, running, hidden, capacity, root, loading, error: listError, refresh } = useJobsFeed()

const selectedId = ref('')
const detail = ref<BackgroundJob | null>(null)
const detailError = ref('')
const actionError = ref('')
const cancelling = ref('')
const autoScroll = ref(true)
const logEl = ref<HTMLElement | null>(null)

const selected = computed(() =>
  jobs.value.find((job) => job.job_id === selectedId.value) || null)

// 详情是日志的唯一来源；列表只负责"有哪些任务/状态有没有变"。
const detailJob = computed<BackgroundJob | null>(() =>
  detail.value && detail.value.job_id === selectedId.value ? detail.value : selected.value)

const detailActive = computed(() => jobsOpen.value && isRunningJob(selected.value))

interface StateInfo { label: string; tone: 'busy' | 'ok' | 'bad' | 'warn' | 'muted' }

function stateInfo(job?: BackgroundJob | null): StateInfo {
  const state = String(job?.state || 'running')
  if (state === 'starting') return { label: '启动中', tone: 'busy' }
  if (state === 'running') return { label: '运行中', tone: 'busy' }
  if (state === 'cancelled') return { label: '已取消', tone: 'muted' }
  if (state === 'timeout') return { label: '超时终止', tone: 'warn' }
  if (state === 'failed') return { label: '异常结束', tone: 'bad' }
  if (state === 'done') {
    return job?.exit_code === 0
      ? { label: '已完成', tone: 'ok' }
      : { label: `退出码 ${job?.exit_code ?? '?'}`, tone: 'bad' }
  }
  return { label: state, tone: 'muted' }
}

function fmtDuration(seconds: number): string {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  if (minutes < 60) return `${minutes}m${String(rest).padStart(2, '0')}s`
  return `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, '0')}m`
}

function fmtChars(value: number): string {
  const n = Math.max(0, Number(value) || 0)
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

function progressPct(job?: BackgroundJob | null): number {
  if (!job?.timeout) return 0
  return Math.max(0, Math.min(100, (Number(job.elapsed) || 0) / job.timeout * 100))
}

/** 项目内目录显示相对路径，跨盘/项目外仍显示绝对路径（不隐藏事实）。 */
function shortCwd(job?: BackgroundJob | null): string {
  const cwd = String(job?.cwd || '')
  if (!cwd) return '—'
  const base = String(root.value || '').replace(/[\\/]+$/, '')
  if (!base) return cwd
  const a = cwd.replace(/[\\/]+$/, '').toLowerCase()
  const b = base.toLowerCase()
  if (a === b) return '.'
  if (a.startsWith(b + '\\') || a.startsWith(b + '/')) return cwd.slice(base.length + 1)
  return cwd
}

function scrollToBottom(): void {
  nextTick(() => {
    const node = logEl.value
    if (node) node.scrollTop = node.scrollHeight
  })
}

async function loadDetail(): Promise<void> {
  const id = selectedId.value
  if (!id || !jobsOpen.value) return
  // 用户主动往上翻时不要抢滚动条；只有原本贴着底部才继续跟随。
  const node = logEl.value
  const pinned = !node || node.scrollHeight - node.scrollTop - node.clientHeight < 40
  try {
    const body = await jobsApi.detail(id)
    if (id !== selectedId.value) return
    detail.value = body
    detailError.value = ''
    if (autoScroll.value && pinned) scrollToBottom()
  } catch (e) {
    if (id !== selectedId.value) return
    detailError.value = (e as Error).message
  }
}

async function refreshAll(): Promise<void> {
  await refresh()
  await loadDetail()
}

async function cancelJob(job: BackgroundJob): Promise<void> {
  const ok = await askConfirm({
    title: '终止这个后台任务？',
    message: job.command,
    detail: '进程会被停止；已采集的输出会保留，仍可在这里查看。',
    confirmText: '终止',
    danger: true,
  })
  if (!ok) return
  cancelling.value = job.job_id
  actionError.value = ''
  try {
    const body = await jobsApi.cancel(job.job_id)
    if (detail.value?.job_id === job.job_id) detail.value = body
    await refresh()
    if (selectedId.value === job.job_id) await loadDetail()
  } catch (e) {
    actionError.value = (e as Error).message
  } finally {
    cancelling.value = ''
  }
}

function selectJob(id: string): void {
  if (selectedId.value === id) return
  selectedId.value = id
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape' && jobsOpen.value) closeJobs()
}

watch(selectedId, (id) => {
  detail.value = null
  detailError.value = ''
  actionError.value = ''
  autoScroll.value = true
  if (id) void loadDetail()
})

watch([jobsOpen, jobs], () => {
  if (!jobsOpen.value) return
  if (!jobs.value.length) {
    selectedId.value = ''
    detail.value = null
    return
  }
  if (jobs.value.some((job) => job.job_id === selectedId.value)) return
  // 默认选"正在跑的"：打开面板最想看的通常是活着的那个。
  const candidate = jobs.value.find(isRunningJob) || jobs.value[0]
  selectedId.value = candidate.job_id
})

// 列表先把状态翻成终态时，详情可能还停在上一帧（比如最后一段日志还没取到）：
// 补一次最终读取，然后详情轮询会自行停止。
watch(() => selected.value?.state, (next, previous) => {
  if (next && next !== previous && !isRunningJob(selected.value)) void loadDetail()
})

watch(jobsOpen, (open) => {
  if (!open) return
  void refresh()
  if (selectedId.value) void loadDetail()
})

watch(autoScroll, (on) => { if (on) scrollToBottom() })

usePolling(loadDetail, 1500, { active: detailActive })

onMounted(() => {
  window.addEventListener('keydown', onKey)
  if (jobsOpen.value) {
    void refresh()
    if (selectedId.value) void loadDetail()
  }
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div v-if="jobsOpen" class="bj-backdrop" @click="closeJobs" />
    <aside
      v-if="jobsOpen"
      class="bj-drawer"
      role="dialog"
      aria-modal="true"
      aria-label="后台任务"
    >
      <header class="bj-head">
        <div class="bj-head-title">
          <b>后台任务</b>
          <span v-if="running" class="bj-live"><i aria-hidden="true" />{{ running }} 个运行中</span>
          <span v-else class="bj-idle">当前没有运行中的任务</span>
        </div>
        <div class="bj-head-actions">
          <span class="bj-cap" :title="`最多同时运行 ${capacity} 个后台任务；已结束的任务保留约 15 分钟`">
            {{ jobs.length }} / {{ capacity }}
          </span>
          <button class="bj-icon" type="button" title="立即刷新" :disabled="loading" @click="refreshAll">⟳</button>
          <button class="bj-icon" type="button" title="关闭（Esc）" aria-label="关闭后台任务面板" @click="closeJobs">×</button>
        </div>
      </header>

      <p class="bj-sub">
        AI 用 <code>run_command(background: true)</code> 起的构建、测试或本地服务会在这里持续显示进度与日志，
        不需要反复让它读日志。
      </p>

      <p v-if="listError" class="bj-err">{{ listError }} <button class="bj-link" @click="refreshAll">重试</button></p>
      <p v-if="hidden > 0" class="bj-hint">另有 {{ hidden }} 个任务属于其它项目；切换顶栏项目后可查看。</p>

      <div v-if="!jobs.length" class="bj-empty">
        <b>{{ loading ? '正在读取后台任务…' : '当前项目还没有后台任务' }}</b>
        <p v-if="!loading">
          长命令跑起来后会出现在这里。也可以直接让 AI 跑测试/构建，并在命令后加一行
          <code>background: true</code>。
        </p>
      </div>

      <div v-else class="bj-body">
        <ul class="bj-list" aria-label="后台任务列表">
          <li v-for="job in jobs" :key="job.job_id">
            <button
              class="bj-item"
              :class="{ on: job.job_id === selectedId }"
              type="button"
              :aria-pressed="job.job_id === selectedId"
              :title="job.command"
              @click="selectJob(job.job_id)"
            >
              <span class="bj-dot" :class="stateInfo(job).tone" aria-hidden="true" />
              <span class="bj-item-main">
                <span class="bj-cmd">{{ job.command }}</span>
                <span class="bj-meta">
                  <span class="bj-state" :class="stateInfo(job).tone">{{ stateInfo(job).label }}</span>
                  <span>{{ fmtDuration(job.elapsed) }}</span>
                  <span v-if="job.total_chars">{{ fmtChars(job.total_chars) }} 字符</span>
                  <span v-if="isRunningJob(job) && job.timeout">剩余 {{ fmtDuration(job.timeout - job.elapsed) }}</span>
                </span>
              </span>
              <span class="bj-chev" aria-hidden="true">›</span>
            </button>
          </li>
        </ul>

        <section v-if="selectedId" class="bj-detail" aria-label="任务详情">
          <div class="bj-detail-head">
            <div class="bj-detail-title">
              <span class="bj-jobid">{{ selectedId }}</span>
              <span class="bj-state" :class="stateInfo(detailJob).tone">{{ stateInfo(detailJob).label }}</span>
              <span class="bj-detail-spacer" />
              <button
                v-if="selected && isRunningJob(selected)"
                class="bj-stop"
                type="button"
                :disabled="cancelling === selectedId"
                title="停止这个后台进程；已采集的输出保留"
                @click="cancelJob(selected)"
              >{{ cancelling === selectedId ? '终止中…' : '终止' }}</button>
            </div>

            <code class="bj-command">{{ detailJob?.command }}</code>

            <div class="bj-facts">
              <span title="任务启动时的工作目录">工作目录：{{ shortCwd(detailJob) }}</span>
              <span>已用 {{ fmtDuration(detailJob?.elapsed || 0) }} / 预算 {{ fmtDuration(detailJob?.timeout || 0) }}</span>
              <span
                v-if="detailJob?.exit_code !== null && detailJob?.exit_code !== undefined
                  && detailJob?.state !== 'cancelled' && detailJob?.state !== 'timeout'"
              >退出码 {{ detailJob.exit_code }}</span>
              <span>输出 {{ fmtChars(detailJob?.total_chars || 0) }} 字符</span>
            </div>

            <div
              v-if="isRunningJob(detailJob)"
              class="bj-progress"
              :title="`已用 ${fmtDuration(detailJob?.elapsed || 0)}，超时预算 ${fmtDuration(detailJob?.timeout || 0)}`"
            >
              <i :style="{ width: progressPct(detailJob) + '%' }" />
            </div>

            <div v-if="detailJob?.truncated" class="bj-trunc">
              输出超过缓冲上限，中段已省略（保留头尾各约 4000 字符）；总字符数仍然准确。
            </div>
            <p v-if="detailJob?.error" class="bj-job-err">{{ detailJob.error }}</p>
            <p v-if="detailError" class="bj-err">{{ detailError }} <button class="bj-link" @click="loadDetail">重试</button></p>
            <p v-if="actionError" class="bj-err">{{ actionError }}</p>
          </div>

          <div class="bj-logbar">
            <span>实时日志</span>
            <label class="bj-auto">
              <input v-model="autoScroll" type="checkbox" />
              自动滚到底
            </label>
          </div>
          <pre ref="logEl" class="bj-logs">{{ (detailJob?.logs || '').trim() || (isRunningJob(detailJob) ? '（等待输出…）' : '（这个任务没有输出）') }}</pre>
        </section>
      </div>
    </aside>
  </Teleport>
</template>

<style scoped>
.bj-backdrop {
  position: fixed;
  inset: 0;
  z-index: 299;
  background: color-mix(in srgb, var(--bg) 32%, transparent);
  animation: bj-fade .16s ease-out;
}
.bj-drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 300;
  width: min(680px, 100vw);
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 18px 0;
  background: var(--bg-raised);
  border-left: 1px solid var(--border-2);
  box-shadow: -18px 0 50px rgba(35, 52, 84, .18);
  animation: bj-in .18s ease-out;
}
.bj-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.bj-head-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}
.bj-head-title b { font-size: var(--fs-body); color: var(--text); }
.bj-live {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-cap);
  color: var(--accent);
  white-space: nowrap;
}
.bj-live i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: bj-pulse 1.4s ease-in-out infinite;
}
.bj-idle { font-size: var(--fs-cap); color: var(--text-faint); }
.bj-head-actions { display: inline-flex; align-items: center; gap: 4px; }
.bj-cap {
  font-size: var(--fs-cap);
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
  margin-right: 2px;
}
.bj-icon {
  width: 24px;
  height: 24px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  font-size: var(--fs-body);
  line-height: 1;
  cursor: pointer;
}
.bj-icon:hover { color: var(--text); border-color: var(--border-strong); background: var(--bg-hover); }
.bj-icon:disabled { opacity: .5; cursor: default; }
.bj-sub {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: 1.65;
  color: var(--text-muted);
}
.bj-sub code, .bj-empty code {
  font-family: var(--font-code);
  font-size: .95em;
  color: var(--accent-text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
}
.bj-err { margin: 0; font-size: var(--fs-sm); color: var(--danger); }
.bj-hint { margin: 0; font-size: var(--fs-cap); color: var(--text-faint); }
.bj-link {
  border: none;
  background: transparent;
  color: var(--accent-text);
  font-size: inherit;
  cursor: pointer;
  text-decoration: underline;
  padding: 0;
}
.bj-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-align: center;
  padding: 0 24px 48px;
}
.bj-empty b { font-size: var(--fs-body); color: var(--text); }
.bj-empty p { margin: 0; max-width: 420px; font-size: var(--fs-sm); line-height: 1.7; color: var(--text-muted); }
.bj-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bj-list {
  flex: 0 1 auto;
  max-height: 38%;
  min-height: 88px;
  overflow: auto;
  list-style: none;
  margin: 0;
  padding: 5px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg);
}
.bj-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 8px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}
.bj-item:hover { background: var(--bg-hover); }
.bj-item.on {
  background: var(--bg-selected);
  border-color: color-mix(in srgb, var(--accent) 42%, transparent);
}
.bj-dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-faint);
}
.bj-dot.busy { background: var(--accent); animation: bj-pulse 1.4s ease-in-out infinite; }
.bj-dot.ok { background: var(--green); }
.bj-dot.bad { background: var(--danger); }
.bj-dot.warn { background: var(--amber); }
.bj-item-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.bj-cmd {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-code);
  font-size: var(--fs-cap);
  color: var(--text);
}
.bj-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: var(--fs-cap);
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
.bj-state { font-weight: 600; }
.bj-state.busy { color: var(--accent-text); }
.bj-state.ok { color: var(--green); }
.bj-state.bad { color: var(--danger); }
.bj-state.warn { color: var(--amber); }
.bj-state.muted { color: var(--text-faint); }
.bj-chev { flex: 0 0 auto; color: var(--text-faint); font-size: var(--fs-body); }
.bj-detail {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding-bottom: 14px;
}
.bj-detail-head { display: flex; flex-direction: column; gap: 6px; }
.bj-detail-title { display: flex; align-items: center; gap: 8px; }
.bj-detail-spacer { flex: 1; }
.bj-jobid {
  font-family: var(--font-code);
  font-size: var(--fs-cap);
  color: var(--text-muted);
}
.bj-stop {
  height: 23px;
  padding: 0 9px;
  border: 1px solid color-mix(in srgb, var(--danger) 55%, transparent);
  border-radius: 5px;
  background: transparent;
  color: var(--danger);
  font-size: var(--fs-cap);
  font-weight: 600;
  cursor: pointer;
}
.bj-stop:hover:not(:disabled) { background: color-mix(in srgb, var(--danger) 10%, transparent); }
.bj-stop:disabled { opacity: .55; cursor: default; }
.bj-command {
  display: block;
  max-height: 64px;
  overflow: auto;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-code);
  font-size: var(--fs-cap);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
.bj-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: var(--fs-cap);
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}
.bj-progress {
  height: 5px;
  border-radius: 3px;
  overflow: hidden;
  background: var(--bg);
  border: 1px solid var(--border);
}
.bj-progress i {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width .3s ease;
}
.bj-trunc {
  font-size: var(--fs-cap);
  line-height: 1.55;
  color: var(--amber);
  background: color-mix(in srgb, var(--amber) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--amber) 40%, transparent);
  border-radius: 6px;
  padding: 4px 7px;
}
.bj-job-err { margin: 0; font-size: var(--fs-cap); color: var(--danger); }
.bj-logbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: var(--fs-cap);
  color: var(--text-muted);
}
.bj-auto { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; user-select: none; }
.bj-auto input { margin: 0; accent-color: var(--accent); }
.bj-logs {
  flex: 1;
  min-height: 120px;
  margin: 0;
  padding: 9px 11px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-code);
  font-size: var(--fs-cap);
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}
@keyframes bj-in {
  from { transform: translateX(24px); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
@keyframes bj-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes bj-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: .35; transform: scale(.7); }
}
@media (prefers-reduced-motion: reduce) {
  .bj-drawer, .bj-backdrop { animation: none; }
  .bj-dot.busy, .bj-live i { animation: none; }
  .bj-progress i { transition: none; }
}
@media (max-width: 720px) {
  .bj-drawer { width: 100vw; padding: 12px 12px 0; }
  .bj-list { max-height: 34%; }
  .bj-sub { font-size: var(--fs-cap); }
}
</style>
