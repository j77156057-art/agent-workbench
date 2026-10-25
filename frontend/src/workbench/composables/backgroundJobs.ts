// 后台任务共享数据源：对话台的运行角标与后台任务面板吃同一份列表，
// 避免两处各自轮询同一接口。模块级单例 + 引用计数：有消费者才轮询，
// 页面不可见时暂停，恢复可见后立即补一次（与 composables/polling.ts 同一口径）。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { jobsApi } from '../api'
import type { BackgroundJob } from '../api'

export const RUNNING_JOB_STATES = ['starting', 'running']

export function isRunningJob(job?: BackgroundJob | null): boolean {
  return !!job && RUNNING_JOB_STATES.includes(String(job.state))
}

const jobs = ref<BackgroundJob[]>([])
const running = ref(0)
const hidden = ref(0)
const capacity = ref(8)
const root = ref('')
const loading = ref(false)
const error = ref('')

let subscribers = 0
let timer: number | null = null
let inFlight: Promise<void> | null = null

async function refresh(): Promise<void> {
  // 单飞：列表轮询、面板手动刷新、角标点击可能同时发生，只允许一个请求在途。
  if (inFlight) return inFlight
  loading.value = true
  inFlight = (async () => {
    try {
      const body = await jobsApi.list('project')
      jobs.value = body.jobs || []
      running.value = Number(body.running || 0)
      hidden.value = Number(body.hidden || 0)
      capacity.value = Number(body.capacity || 8)
      root.value = body.root || ''
      error.value = ''
    } catch (e) {
      // 保留上一次成功的数据：一次网络抖动不该把角标和列表清空。
      error.value = (e as Error).message || '后台任务读取失败'
    } finally {
      loading.value = false
      inFlight = null
    }
  })()
  return inFlight
}

function intervalMs(): number {
  // 有任务在跑就快一点看日志/状态，闲置时没必要维持高频率。
  return running.value > 0 ? 2500 : 6000
}

function clearTimer(): void {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
}

function schedule(): void {
  clearTimer()
  if (!subscribers || document.hidden) return
  timer = window.setTimeout(() => {
    timer = null
    void refresh().finally(schedule)
  }, intervalMs())
}

function onVisibility(): void {
  if (!subscribers) return
  if (document.hidden) clearTimer()
  else void refresh().finally(schedule)
}

function retain(): void {
  subscribers += 1
  if (subscribers > 1) return
  document.addEventListener('visibilitychange', onVisibility)
  void refresh().finally(schedule)
}

function release(): void {
  subscribers = Math.max(0, subscribers - 1)
  if (subscribers > 0) return
  document.removeEventListener('visibilitychange', onVisibility)
  clearTimer()
}

/** 组件内调用：随挂载/卸载自动启停共享轮询。 */
export function useJobsFeed() {
  onMounted(retain)
  onBeforeUnmount(release)
  return { jobs, running, hidden, capacity, root, loading, error, refresh }
}
