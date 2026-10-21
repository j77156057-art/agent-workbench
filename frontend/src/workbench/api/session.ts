// 会话 id 与标签页存活探测（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { request } from './_base'

// ---------------------------------------------------------------- 会话 id
// Shared browser lock is acquired before either page mounts (public/session.js).
interface BrowserSession {
  ready: Promise<string>
  get(): string
  set(id: string): Promise<boolean>
  create(): Promise<string>
}
declare global { interface Window { DocMindSession: BrowserSession } }
export function getSessionId(): string { return window.DocMindSession.get() }
export function setSessionId(id: string): Promise<boolean> { return window.DocMindSession.set(id) }
export function startNewSession(): Promise<string> { return window.DocMindSession.create() }
function newSessionId(): string {
  const c = globalThis.crypto as Crypto | undefined
  const raw = c && typeof c.randomUUID === 'function'
    ? c.randomUUID().replace(/-/g, '')
    : Math.random().toString(16).slice(2) + Math.random().toString(16).slice(2)
  return 'web-' + raw.replace(/[^0-9a-fA-F]/g, '').slice(0, 24).padEnd(24, '0')
}

// ---------------------------------------------------------------- 标签页存活探测
// 目的：判断「本标签页是否为当前唯一打开的 Agent 工作台 标签」。该判断仅供「关掉重开自动续上
// 最近对话」这个仅在单窗口下才安全的兜底使用——多标签时各自独立、互不串（见 ChatDock.restoreHistory）。
// 首选 Web Locks（最可靠，见下）；不可用时退回 BroadcastChannel 心跳 → localStorage 时间戳租约；
// 三者都不可用 → 「不确定」，一律按「非唯一」处理（隔离优先）。
//
// 为什么首选 Web Locks：BroadcastChannel 心跳与 localStorage 租约都依赖**定时器**，而浏览器会把
// 后台标签的定时器节流到 ≥1 分钟——别的标签在后台时，新标签在 900ms 宽限内收不到它心跳、其租约
// 条目也会在 10s 后过期，于是**误判唯一 → 自动接到旧会话 → 两个标签共用一段对话**（正是 P0 要消除
// 的现象）。Web Locks 在文档销毁/标签关闭时才释放，**不受后台节流影响**，且 query() 是权威的、
// 立即可得；本地 http://127.0.0.1 属安全上下文，WebView2/Edge 均支持。
const TAB_CHANNEL_NAME = 'docmind-tabs'
const TAB_LEASE_KEY = 'docmind_tab_lease'
const TAB_LOCK_PREFIX = 'docmind-tab-'   // 每标签一把同名前缀的 Web Lock
const TAB_STALE_MS = 10000      // 超过此时长没收到心跳即视为该标签已离开
const TAB_HEARTBEAT_MS = 3000   // 心跳间隔
const TAB_SETTLE_MS = 900       // 心跳/租约模式的探测宽限期：给其它标签回心跳留出时间

interface TabMessage { t: 'hb' | 'hello'; id: string; ts: number }

/** Web Locks 的最小类型（不依赖 lib.dom 是否带 LockManager，兼容不同 TS 版本）。 */
interface TabLockManager {
  request(name: string, cb: (lock: unknown) => Promise<unknown>): Promise<unknown>
  query(): Promise<{ held: { name: string }[] }>
}

const docmindTabs = (() => {
  const tabId = newSessionId() + '-tab'   // 本标签标识（非会话 id，不参与落盘）
  const peers = new Map<string, number>() // 其它标签 id -> 最近一次心跳时刻
  let channel: BroadcastChannel | null = null
  let timer: number | null = null
  let mode: 'locks' | 'channel' | 'lease' | 'none' = 'none'
  let lockSole = false        // locks 模式下的同步缓存（async 探测结果），初值 false
  let started = false

  function post(kind: TabMessage['t']): void {
    if (!channel) return
    const msg: TabMessage = { t: kind, id: tabId, ts: Date.now() }
    try { channel.postMessage(msg) } catch { /* 通道已关闭：忽略 */ }
  }

  function prunePeers(): void {
    const now = Date.now()
    for (const [id, ts] of peers) {
      if (now - ts >= TAB_STALE_MS) peers.delete(id)
    }
  }

  /** 读 localStorage 租约：剔除过期条目后返回「其它标签」条数；不可读返回 -1（不确定）。 */
  function leaseOthers(): number {
    try {
      const raw = localStorage.getItem(TAB_LEASE_KEY)
      if (!raw) return 0
      const obj = JSON.parse(raw) as Record<string, unknown>
      if (!obj || typeof obj !== 'object') return 0
      const now = Date.now()
      let others = 0
      for (const k of Object.keys(obj)) {
        if (k === tabId) continue
        const v = Number(obj[k])
        if (Number.isFinite(v) && now - v < TAB_STALE_MS) others++
      }
      return others
    } catch {
      return -1
    }
  }

  /** 写租约：剔除过期条目后写入本标签时间戳（存储不可用则静默跳过）。 */
  function writeLease(): void {
    try {
      const raw = localStorage.getItem(TAB_LEASE_KEY)
      let obj: Record<string, number> = {}
      if (raw) {
        try {
          const j = JSON.parse(raw) as Record<string, unknown>
          if (j && typeof j === 'object') {
            const now = Date.now()
            for (const k of Object.keys(j)) {
              const v = Number(j[k])
              if (Number.isFinite(v) && now - v < TAB_STALE_MS) obj[k] = v
            }
          }
        } catch { obj = {} }
      }
      obj[tabId] = Date.now()
      localStorage.setItem(TAB_LEASE_KEY, JSON.stringify(obj))
    } catch { /* 存储不可用：租约机制禁用 */ }
  }

  function start(): void {
    if (started) return
    started = true
    // 首选 Web Locks：长期持有一把以本标签命名的锁（回调永不 resolve → 持有到标签关闭/文档销毁，
    // 届时浏览器自动释放）。不受后台节流影响，query() 能权威回答"是否有别的标签"。
    const locks = typeof navigator !== 'undefined'
      ? (navigator as unknown as { locks?: TabLockManager }).locks
      : undefined
    if (locks && typeof locks.request === 'function') {
      try {
        locks.request(TAB_LOCK_PREFIX + tabId, () => new Promise<never>(() => { /* 永不 resolve：长期持有 */ }))
          .catch(() => { /* 申请被拒：忽略，探测会保守返回"非唯一" */ })
        mode = 'locks'
        return
      } catch { /* 申请同步抛错：掉落到 BroadcastChannel */ }
    }
    // 次选 BroadcastChannel 心跳（真正跨标签、无残留）
    const BC = (globalThis as { BroadcastChannel?: typeof BroadcastChannel }).BroadcastChannel
    if (typeof BC === 'function') {
      try {
        channel = new BC(TAB_CHANNEL_NAME)
        channel.onmessage = (ev: MessageEvent) => {
          const m = ev.data as Partial<TabMessage> | null
          if (!m || typeof m.id !== 'string' || m.id === tabId) return
          peers.set(m.id, Date.now())
          if (m.t === 'hello') post('hb')   // 有新标签加入：立刻回心跳，让它尽快感知到我
        }
        mode = 'channel'
      } catch {
        channel = null
      }
    }
    if (mode === 'channel') {
      post('hello')   // 宣告自己加入，已存在的标签会立刻回心跳
      timer = window.setInterval(() => { post('hb'); prunePeers() }, TAB_HEARTBEAT_MS)
    } else {
      // 退回 localStorage 时间戳租约：能写才算可用
      try {
        localStorage.setItem(TAB_LEASE_KEY + ':probe', '1')
        localStorage.removeItem(TAB_LEASE_KEY + ':probe')
        mode = 'lease'
        writeLease()
        timer = window.setInterval(writeLease, TAB_HEARTBEAT_MS)
      } catch {
        mode = 'none'
      }
    }
    if (timer !== null && typeof window !== 'undefined') {
      // 标签页卸载时停掉心跳（不影响正确性，只是卫生）
      window.addEventListener('pagehide', () => { if (timer !== null) window.clearInterval(timer) })
    }
  }

  /** 权威查锁：held 里存在「名字以 TAB_LOCK_PREFIX 开头且不是自己那把」的锁 → 非唯一。 */
  async function locksSole(): Promise<boolean> {
    const locks = typeof navigator !== 'undefined'
      ? (navigator as unknown as { locks?: TabLockManager }).locks
      : undefined
    if (!locks || typeof locks.query !== 'function') return false   // 无 query：不确定 → 非唯一
    try {
      const snap = await locks.query()
      const held = (snap && Array.isArray(snap.held)) ? snap.held : []
      const mine = TAB_LOCK_PREFIX + tabId
      for (const l of held) {
        if (l && typeof l.name === 'string' && l.name.startsWith(TAB_LOCK_PREFIX) && l.name !== mine) {
          return false   // 有别的标签仍持有锁 → 非唯一
        }
      }
      return true        // 只有自己那把 → 唯一
    } catch {
      return false       // 查询异常 → 不确定 → 非唯一（隔离优先）
    }
  }

  /** 同步判断「本标签是否唯一」（locks 模式返回最近一次 async 探测的缓存，初值 false）。 */
  function soleSync(): boolean {
    start()
    if (mode === 'locks') return lockSole
    if (mode === 'channel') { prunePeers(); return peers.size === 0 }
    if (mode === 'lease') { return leaseOthers() === 0 }
    return false   // 探测不可用 → 不确定 → 按非唯一处理（隔离优先）
  }

  /** 异步判断「本标签是否唯一」：locks 模式查锁（权威、立即）；其余模式同同步逻辑。 */
  async function probe(): Promise<boolean> {
    start()
    if (mode === 'locks') {
      lockSole = await locksSole()
      return lockSole
    }
    return soleSync()
  }

  return { start, soleSync, probe, isLockMode: () => { start(); return mode === 'locks' } }
})()

/** 启动标签页存活探测（幂等）。应用启动时调用一次即可开始心跳与应答。 */
export function startTabProbe(): void {
  docmindTabs.start()
}

/** 本标签页是否为当前唯一打开的 Agent 工作台 标签（**同步**）。
 *  locks 模式：返回最近一次 probeSoleDocMindTab() 的缓存结果（尚未探测过时为 false）；
 *  channel/lease 模式：即时判定；none/不确定：false（隔离优先）。 */
export function isSoleDocMindTab(): boolean {
  return docmindTabs.soleSync()
}

let tabSettled: Promise<void> | null = null
/** **异步**判断唯一性（ChatDock 用它做自动续接的门禁）。
 *  locks 模式：直接查 Web Locks，**立即返回**（权威信号，无需宽限期）；
 *  channel/lease 模式：先等心跳「稳定」再判定（新标签刚打开时其它标签可能还没回心跳），
 *  仅首次真正等待宽限期，之后立即返回；探测不可用 → false（不自动续接）。 */
export async function probeSoleDocMindTab(settleMs = TAB_SETTLE_MS): Promise<boolean> {
  startTabProbe()
  if (docmindTabs.isLockMode()) return docmindTabs.probe()   // 权威：无需等待
  if (!tabSettled) {
    tabSettled = new Promise<void>((resolve) => { window.setTimeout(resolve, settleMs) })
  }
  await tabSettled
  return docmindTabs.probe()
}
