// 内置浏览器实况：轮询哪个浏览器在跑、它当前长什么样、刚才做了什么。
//
// 为什么是轮询而不是 WS：后端的帧本来就在 CDP 线程里持续更新，这里每 600ms
// 取一次"最新帧 + 版本号"，版本没变就不换图。链路比新开一路 WS 简单得多，
// 而 600ms 的延迟对人"看着它操作"足够（模型一步动作通常是秒级）。
import { computed, ref } from 'vue'
import { browserLiveApi, type BrowserLivePage, type BrowserLiveSnapshot } from '../api/browser'
import type { BrowserEvidence } from './chat-types'

const pages = ref<BrowserLivePage[]>([])
const activePage = ref('')
const snapshot = ref<BrowserLiveSnapshot | null>(null)
const frameVersion = ref(0)
const lastError = ref('')
/** 用户手动收起后不再拉帧（页面列表仍轮询，下一次浏览器动作会重新展开）。 */
const collapsed = ref(false)

let watchers = 0
let pagesTimer: ReturnType<typeof setTimeout> | null = null
let liveTimer: ReturnType<typeof setTimeout> | null = null

export const PAGE_POLL_MS = 2000
export const LIVE_POLL_MS = 600

const frameSrc = computed(() => {
  const frame = snapshot.value?.frame
  return frame ? 'data:image/jpeg;base64,' + frame : ''
})

const activeMeta = computed<BrowserLivePage | null>(
  () => pages.value.find((row) => row.page === activePage.value) || null,
)

const visible = computed(() => !collapsed.value && pages.value.length > 0)

const title = computed(
  () => snapshot.value?.title || snapshot.value?.url || activeMeta.value?.url || '正在打开网页',
)

const lastAction = computed(
  () => snapshot.value?.last_action || activeMeta.value?.last_action || '',
)

const liveState = computed(
  () => snapshot.value?.state || activeMeta.value?.live_state || 'starting',
)

/** 最近动作倒序（后端给的是正序，面板最新一条在上）。 */
const recentSteps = computed(() => (snapshot.value?.steps || []).slice().reverse())

function clearTimers() {
  if (pagesTimer) { clearTimeout(pagesTimer); pagesTimer = null }
  if (liveTimer) { clearTimeout(liveTimer); liveTimer = null }
}

function schedulePages() {
  if (!watchers || pagesTimer) return
  pagesTimer = setTimeout(() => { pagesTimer = null; void refreshPages() }, PAGE_POLL_MS)
}

function scheduleLive() {
  if (!watchers || liveTimer) return
  liveTimer = setTimeout(() => { liveTimer = null; void refreshLive() }, LIVE_POLL_MS)
}

async function refreshPages() {
  try {
    const res = await browserLiveApi.pages()
    pages.value = res.pages || []
    lastError.value = ''
    if (!pages.value.length) {
      activePage.value = ''
      snapshot.value = null
      frameVersion.value = 0
      if (liveTimer) { clearTimeout(liveTimer); liveTimer = null }
    } else if (!pages.value.some((row) => row.page === activePage.value)) {
      // 没有钉住的页面（或它已结束）：跟最近有动作的那一个。
      const ranked = pages.value.slice().sort((a, b) => (b.last_step || 0) - (a.last_step || 0))
      activePage.value = ranked[0].page
    }
  } catch (e) {
    lastError.value = (e as Error).message || '浏览器实况不可用'
  }
  schedulePages()
  scheduleLive()
}

async function refreshLive() {
  const page = activePage.value
  if (!page || collapsed.value) return
  try {
    const snap = await browserLiveApi.live(page)
    if (activePage.value !== page) { scheduleLive(); return }
    if (!snap.ok && snap.state === 'closed') {
      // 会话被关闭/回收：把这一行从列表里摘掉，面板随之收起或切到下一个。
      pages.value = pages.value.filter((row) => row.page !== page)
      snapshot.value = null
      frameVersion.value = 0
      if (!pages.value.length) activePage.value = ''
    } else {
      snapshot.value = snap
      const version = Number(snap.frame_version || 0)
      if (version !== frameVersion.value) frameVersion.value = version
      lastError.value = ''
    }
  } catch (e) {
    lastError.value = (e as Error).message || '浏览器实况中断'
  }
  scheduleLive()
}

/** 页面上挂一个订阅者：首个订阅者启动轮询，最后一个离开时停止。 */
export function startBrowserWatch() {
  watchers += 1
  if (watchers === 1) {
    void refreshPages()
    void refreshLive()
  }
}

export function stopBrowserWatch() {
  watchers = Math.max(0, watchers - 1)
  if (!watchers) clearTimers()
}

/**
 * 对话里出现一条浏览器证据时调用：立刻把实况窗钉到那个会话并展开。
 *
 * 不能只靠 pages 轮询——第一条动作发生时用户正盯着对话，面板不该等到
 * 下一次页面轮询（最多 2s）才亮起来。
 */
export function noteBrowserEvidence(ev?: BrowserEvidence | null) {
  if (!ev?.live || !ev.page) return
  const changed = activePage.value !== ev.page
  activePage.value = ev.page
  collapsed.value = false
  if (changed) {
    snapshot.value = null
    frameVersion.value = 0
  }
  if (!watchers) {
    startBrowserWatch()
    return
  }
  // 已有轮询在跑：清掉待触发的定时器，立刻拉一次，避免与直接调用并发。
  if (!pages.value.length) void refreshPages()
  if (liveTimer) { clearTimeout(liveTimer); liveTimer = null }
  void refreshLive()
}

export function collapseBrowserWatch() {
  collapsed.value = true
}

export function expandBrowserWatch() {
  collapsed.value = false
  if (watchers) {
    if (liveTimer) { clearTimeout(liveTimer); liveTimer = null }
    void refreshLive()
  }
}

/** 面板关闭：只收起，不关会话（关浏览器是模型/用户的决定）。 */
export function dismissBrowserWatch() {
  collapsed.value = true
}

export function useBrowserLive() {
  return {
    pages, activePage, snapshot, frameSrc, frameVersion, lastError, collapsed, visible,
    activeMeta, title, lastAction, liveState, recentSteps,
    startBrowserWatch, stopBrowserWatch, collapseBrowserWatch, expandBrowserWatch,
    dismissBrowserWatch,
  }
}
