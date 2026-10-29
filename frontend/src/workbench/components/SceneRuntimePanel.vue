<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick, defineAsyncComponent } from 'vue'
import { runtimeApi, engineApi, playApi, sceneApi, regionsApi, bugsApi, changesetApi, aiApi, getProjectId } from '../api'
import { createPoller } from '../composables/polling'
import { appEvents } from '../eventBus'
import { blobDataUrl } from '../previewFeedback'
import { capturePreviewFrame } from '../workflowPreviewCapture'
import type { WebTemplates, WebExportResult, DesktopHost, EmbedRect, BugItem, RegionInfo } from '../api'
import { useWorkbench } from '../composables/workbench'
import { useEngineConfig } from '../composables/useEngineConfig'
import { demoMode, demoBugs, demoChangesets, demoRegionCards, demoBugFixAnswer } from '../composables/demo'
// 画布与时间线都引了重依赖（@vue-flow 约 243KB / gzip 79KB），
// 用异步组件延迟到真正切到对应 tab 再加载，工作台首屏体积不受影响。
const SceneCanvas = defineAsyncComponent(() => import('./SceneCanvas.vue'))
const RuntimeTimeline = defineAsyncComponent(() => import('./RuntimeTimeline.vue'))
// 启动哪个引擎取自项目配置（设置页「引擎与项目」），不再写死 Godot。
const engineCfg = useEngineConfig()

// 阶段 4：popup（默认，传统固定遮罩弹窗，行为逐字不变）/ docked（常驻主区，内联填充父容器）。
// 同一个组件实例通过 prop 切换形态，绝不重建（否则丢 iframe / 引擎嵌入状态）。
const props = withDefaults(defineProps<{ mode?: 'popup' | 'docked'; host?: string }>(), { mode: 'popup', host: '#wb-playpane-slot' })
defineEmits<{ close: [] }>()
const docked = computed(() => props.mode === 'docked')

const { jumpToLine, openPath, activeTab, runtimeOpen, runtimeTab, closeRuntimeResident } = useWorkbench()
const open = ref(false)
const tab = ref<'play' | 'scene' | 'timeline'>('play')

// 顶层「画布 / 运行」tab 与弹窗联动：外部（WorkspaceTabs）请求打开时同步到本地状态，
// 本地关闭/切 tab 也写回共享态，保证顶层 tab 高亮与实际面板一致。
watch(runtimeOpen, v => { if (open.value !== v) open.value = v })
watch(runtimeTab, v => { if (tab.value !== v) tab.value = v })
watch(open, v => { if (runtimeOpen.value !== v) runtimeOpen.value = v })

/* ---------------- 试玩器 ---------------- */
const iframeEl = ref<HTMLIFrameElement | null>(null)
const iframeUrl = ref('')
const iframeNonce = ref(0)
const exporting = ref(false)
const visualNote = ref('')
const visualImage = ref('')
const visualBlob = ref<Blob | null>(null)
const visualProject = ref('')
const visualBusy = ref(false)
const visualSending = ref(false)
const visualStatus = ref('')
const visualOpen = ref(false)
const visualPanel = ref<HTMLElement | null>(null)
let visualGeneration = 0

async function openVisualFeedback() {
  if (visualBusy.value || visualSending.value) return
  const frame = iframeEl.value
  if (!frame) { visualStatus.value = '请先加载 Web 试玩画面；原生桌面窗口暂需通过聊天上传截图。'; return }
  const projectId = getProjectId(), ticket = ++visualGeneration
  visualBusy.value = true
  visualStatus.value = ''
  try {
    const blob = await capturePreviewFrame(frame)
    const image = await blobDataUrl(blob)
    if (projectId !== getProjectId() || ticket !== visualGeneration) return
    visualBlob.value = blob; visualImage.value = image; visualProject.value = projectId
    visualOpen.value = true
    visualStatus.value = '已截取此刻画面。请确认截图包含你要反馈的内容。'
    await nextTick()
    visualPanel.value?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  } catch (e) { visualStatus.value = (e as Error).message || '截图失败，请通过聊天上传截图。' }
  finally { visualBusy.value = false }
}
/** 只观察当前画面并交给 AI，不要求用户先填写修改意见。 */
async function inspectCurrentView() {
  if (visualBusy.value || visualSending.value) return
  const projectId = getProjectId()
  if (!projectId) { visualStatus.value = '请先选择当前项目。'; return }
  const ticket = ++visualGeneration
  visualBusy.value = true
  visualStatus.value = '正在读取当前界面…'
  try {
    let blob: Blob | null = null
    let captureError = ''
    if (iframeEl.value) {
      try {
        blob = await capturePreviewFrame(iframeEl.value)
        visualBlob.value = blob
        visualImage.value = await blobDataUrl(blob)
        visualProject.value = projectId
      } catch (cause) {
        captureError = (cause as Error).message || '网页画面暂时无法截图'
      }
    }
    if (ticket !== visualGeneration || projectId !== getProjectId()) return
    visualSending.value = true
    visualStatus.value = '正在交给 AI 观察…'
    // v2 重构 E 阶段：AutonomousCockpit 已删，host 永远为 '#wb-playpane-slot'，固定走 'default' 通道
    const target = 'default'
    appEvents.emit('docmind:send-chat', {
      target, projectId, images: blob ? [blob] : [],
      uiContext: 'app_interface_inspect',
      prompt: [
        '请浏览并分析当前项目的运行界面。',
        blob
          ? '我附上了刚刚截取的真实画面，请描述可观察事实，并结合运行日志和项目文件复核。'
          : `当前没有可附加的网页截图${captureError ? `（${captureError}）` : ''}，请调用 dev_desktop_capture 获取当前项目嵌入窗口或前台窗口；无法捕获时要明确说明，不要猜测。`,
        '先给出界面状态、明显问题和下一步建议，不要把视觉观察当作源码或功能验收。',
      ].join('\n'),
      onStatus(status, detail) {
        if (ticket !== visualGeneration) return
        if (status === 'processing') visualStatus.value = detail || 'AI 正在查看当前界面，回复会显示在聊天区。'
        else if (status === 'awaiting_review') { visualSending.value = false; visualStatus.value = 'AI 已完成界面观察，请查看聊天区。' }
        else if (status === 'pending') { visualSending.value = false; visualStatus.value = detail || '当前对话正忙，请稍后重试。' }
        else if (status === 'failed') { visualSending.value = false; visualStatus.value = detail || '界面观察未完成，请重试。' }
      },
    })
  } catch (e) {
    visualSending.value = false
    visualStatus.value = (e as Error).message || '当前界面读取失败，请重试。'
  } finally {
    visualBusy.value = false
  }
}
function sendVisualFeedback() {
  if (!visualNote.value.trim() || !visualBlob.value || visualSending.value) return
  if (visualProject.value !== getProjectId()) { visualStatus.value = '项目已切换，请重新截取当前项目画面。'; return }
  const ticket = visualGeneration
  visualSending.value = true
  visualStatus.value = '正在交给 AI…'
  appEvents.emit('docmind:send-chat', {
    // v2 重构 E 阶段：固定走 'default'（见 :91 同款）
    target: 'default',
    projectId: visualProject.value, images: [visualBlob.value],
    prompt: visualNote.value.trim(),
    uiContext: 'web_preview_feedback',
    onStatus(status, detail) {
      if (ticket !== visualGeneration) return
      visualSending.value = status === 'processing'
      visualStatus.value = detail || (status === 'processing' ? 'AI 正在分析，回复显示在聊天区。' : '请查看聊天区的分析和方案。')
    },
  })
}
const exportMsg = ref('')
const lastExport = ref<WebExportResult | null>(null)
const tpl = ref<WebTemplates | null>(null)
const installing = ref(false)

/* ---- 原生引擎：独立窗口 / 嵌入工作台 ----
   嵌入的意义是"游戏跑在工作台窗口里"，边玩边让 AI 改代码；
   独立窗口模式下 Web 工作台和游戏窗口是两个窗口，来回切很别扭。 */
const nativeRunning = ref(false)
const reloadingNative = ref(false)
const changedEngineFiles = ref<string[]>([])
const desktop = ref<DesktopHost | null>(null)
const embedState = ref<'off' | 'embedded' | 'failed'>('off')
const embedMsg = ref('')
const engineViewport = ref<HTMLElement | null>(null)
// 默认开：桌面端启动时"游戏嵌进工作台"才是预期行为；浏览器模式下会自动禁用并提示
const autoEmbed = ref(localStorage.getItem('docmind.autoEmbed') !== '0')
let lastRect = ''

watch(autoEmbed, v => localStorage.setItem('docmind.autoEmbed', v ? '1' : '0'))

const desktopReady = computed(() => !!desktop.value?.desktop)
const hostDpi = computed(() => desktop.value?.dpi || 96)

async function refreshDesktop() {
  try { desktop.value = await engineApi.host() } catch { desktop.value = null }
}

async function pollEngineChanges() {
  if (!nativeRunning.value) {
    changedEngineFiles.value = []
    return
  }
  try {
    const r = await engineApi.changes()
    if (r.running !== false) changedEngineFiles.value = r.files || []
  } catch { /* 引擎状态轮询失败不打断试玩 */ }
}

/**
 * 把"引擎视窗"元素换算成宿主客户区的物理像素矩形。
 *
 * 比例用 宿主客户区宽 / 页面视口宽 求，而不是直接用 devicePixelRatio：
 * 窗口缩放、WebView 缩放、多屏不同 DPI 混用时 DPR 并不保证等于这个比例，
 * 而比例一错引擎窗口就整体错位（150% 缩放下尤其明显）。
 */
function viewportRect(): EmbedRect | null {
  const el = engineViewport.value
  if (!el) return null
  const box = el.getBoundingClientRect()
  if (box.width < 40 || box.height < 40) return null
  const clientW = desktop.value?.client?.width
  const scale = clientW && window.innerWidth > 0
    ? clientW / window.innerWidth
    : (window.devicePixelRatio || 1)
  return {
    x: Math.round(box.left * scale),
    y: Math.round(box.top * scale),
    width: Math.round(box.width * scale),
    height: Math.round(box.height * scale),
  }
}

/** 布局变化后同步引擎视窗；矩形没变就不重复请求。 */
async function syncEngineRect(force = false) {
  if (embedState.value !== 'embedded') return
  const rect = viewportRect()
  if (!rect) return
  const key = `${rect.x},${rect.y},${rect.width},${rect.height}`
  if (!force && key === lastRect) return
  lastRect = key
  try { await engineApi.place(rect) } catch { /* 下一次布局变化会对齐 */ }
}

let resizeTimer: number | undefined
function onWindowResize() {
  if (resizeTimer) window.clearTimeout(resizeTimer)
  resizeTimer = window.setTimeout(async () => {
    await refreshDesktop()      // 客户区变了，比例要重算
    await syncEngineRect()
  }, 120)
}

// DPI 变化（拖到不同缩放的显示器 / WebView 缩放）在窗口尺寸不变时不会触发 resize，
// 但宿主客户区物理像素会变 → 嵌入比例要重算、引擎视窗要重新 place，否则画面错位。
// 用 matchMedia(resolution) 精确捕获 DPI 跳变（每次变化需重建监听，因为查询串含当前 DPI）。
let dpiMq: MediaQueryList | null = null
function onDpiChange() {
  void onWindowResize()
  watchDpi()
}
function watchDpi() {
  dpiMq?.removeEventListener('change', onDpiChange)
  dpiMq = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`)
  dpiMq.addEventListener('change', onDpiChange)
}

async function refreshTemplates() {
  try { tpl.value = await playApi.templates() } catch { /* 忽略 */ }
}

async function doExport() {
  exporting.value = true
  exportMsg.value = '正在导出 Web 版本（首次约 30–90 秒，含脚本编译）…'
  try {
    const r = await playApi.exportWeb()
    lastExport.value = r
    if (r.ok && r.url) {
      iframeUrl.value = r.url
      iframeNonce.value += 1
      const injected = r.html_injected ? '，通信桥已注入' : ''
      exportMsg.value = `导出成功（${r.elapsed}s${injected}），试玩已加载`
    } else if (r.error === 'missing_templates') {
      exportMsg.value = '缺少 Godot Web 导出模板，请先安装（右侧/下方按钮）。'
      tpl.value = r.templates ?? tpl.value
    } else {
      exportMsg.value = '导出失败：' + (r.output?.trim().slice(-500) || r.message || r.error || '未知错误')
    }
  } catch (e) {
    exportMsg.value = (e as Error).message
  } finally {
    exporting.value = false
  }
}

async function doInstallTemplates() {
  if (!window.confirm('将从 Godot 官方 GitHub 下载导出模板包（约 600–800MB，仅提取 Web 模板约 60MB 落盘），是否继续？')) return
  installing.value = true
  exportMsg.value = '模板下载已开始…'
  try {
    await playApi.installTemplates()
    pollInstall()
  } catch (e) {
    installing.value = false
    exportMsg.value = '安装启动失败：' + (e as Error).message
  }
}

async function pollInstall() {
  await refreshTemplates()
  const st = tpl.value?.install
  if (st && ['starting', 'downloading', 'extracting'].includes(st.state)) {
    window.setTimeout(pollInstall, 1500)
    return
  }
  installing.value = false
  if (st?.state === 'done') exportMsg.value = '模板安装完成，点击「导出并试玩」。'
  else if (st?.state === 'error') exportMsg.value = '模板安装失败：' + st.error
}

function reloadFrame() { iframeNonce.value += 1 }
function openExternal() { if (iframeUrl.value) window.open(iframeUrl.value, '_blank') }
function cmdReload() {
  iframeEl.value?.contentWindow?.postMessage({ source: 'docmind-cmd', cmd: 'reload' }, '*')
}
async function refreshNativeStatus() {
  try {
    const st = await engineApi.status()
    nativeRunning.value = !!st.running
    // 刷新页面后要如实反映"现在是不是嵌着的"，不能只认本地变量
    embedState.value = st.embedded ? 'embedded' : 'off'
    if (st.embedded && st.embed_dpi) embedMsg.value = `已嵌入工作台（${st.host_dpi || st.embed_dpi} DPI 宿主）`
  } catch { /* ignore */ }
}

async function nativeStart() {
  exportMsg.value = ''
  embedMsg.value = ''
  const rect = autoEmbed.value && desktopReady.value ? viewportRect() : null
  try {
    // 启动哪个引擎由项目配置决定（设置页「引擎与项目」），不再是写死的 Godot。
    await engineCfg.load()
    const r = await engineApi.start(engineCfg.engine.value, !!rect, rect)
    nativeRunning.value = !!r.running
    if (!r.running) { exportMsg.value = r.error || '启动失败'; return }
    if (rect && r.embedded) {
      embedState.value = 'embedded'
      lastRect = `${rect.x},${rect.y},${rect.width},${rect.height}`
      const size = r.embed?.width && r.embed?.height ? `${r.embed.width}×${r.embed.height}` : '按视窗'
      embedMsg.value = `已嵌入工作台（${size}，宿主 ${r.embed?.host_dpi || hostDpi.value} DPI）`
      await engineApi.focusEngine(true).catch(() => {})
    } else if (rect) {
      embedState.value = 'failed'
      embedMsg.value = '引擎已启动但嵌入失败：' + (r.embed_error || '未知原因') + '（已退化为独立窗口）'
    } else {
      embedState.value = 'off'
      exportMsg.value = desktopReady.value
        ? '桌面游戏窗口已启动（独立窗口）'
        : '桌面游戏窗口已启动；浏览器模式下无法嵌入工作台'
    }
  } catch (e) { exportMsg.value = (e as Error).message }
}

async function nativeStop() {
  await engineApi.stop().catch(() => { /* 停不掉也要把界面状态归位 */ })
  nativeRunning.value = false
  embedState.value = 'off'
  embedMsg.value = ''
  lastRect = ''
}

async function nativeDetach() {
  try {
    const r = await engineApi.detach()
    embedState.value = 'off'
    lastRect = ''
    embedMsg.value = ''
    exportMsg.value = r.was_embedded ? '已解除嵌入，引擎回到独立窗口（进程仍在运行）' : '引擎当前未嵌入'
  } catch (e) { embedMsg.value = (e as Error).message }
}

async function nativeEmbed() {
  // 仅把已运行的引擎重新嵌进工作台（不重启进程）：切走 play tab 被 detach 后回来用。
  if (!desktopReady.value) return
  const rect = autoEmbed.value ? viewportRect() : null
  if (!rect) return
  try {
    const r = await engineApi.embed(rect)
    if (r.ok && r.embedded) {
      embedState.value = 'embedded'
      await syncEngineRect(true)
      embedMsg.value = `已嵌入工作台（${r.width || '?'}×${r.height || '?'}）`
      await engineApi.focusEngine(true).catch(() => {})
    } else {
      embedState.value = 'off'
      embedMsg.value = (r as { error?: string }).error || '重新嵌入未完成'
    }
  } catch (e) { embedMsg.value = (e as Error).message }
}

function onViewportFocus() {
  // 点引擎视窗区主动把键盘焦点交还游戏（最佳努力；引擎子窗口在顶层时系统点击也会自动聚焦）。
  if (embedState.value === 'embedded') void engineApi.focusEngine().catch(() => {})
}

async function nativeFocus() {
  try {
    const r = await engineApi.focusEngine()
    embedMsg.value = r.ok ? '已把键盘焦点交给游戏窗口' : ('聚焦失败：' + (r.error || '系统拒绝设置前台窗口'))
  } catch (e) { embedMsg.value = (e as Error).message }
}

function frameSrc() {
  return iframeUrl.value ? `${iframeUrl.value}?v=${iframeNonce.value}` : ''
}

const installPct = (st?: WebTemplates['install']) => {
  if (!st || !st.total) return 0
  return Math.min(100, Math.round((st.downloaded / st.total) * 100))
}
const fmtMB = (n: number) => `${(n / 1048576).toFixed(1)} MB`

/* ---------------- 事件时间线 ---------------- */
interface Ev { key: string; type: string; source: string; timestamp: string; data: Record<string, unknown>; eid: string }
const events = ref<Ev[]>([])
const showHistory = ref(false)
const captureSince = ref(Date.now())
const eventStatus = ref('等待事件')
const visibleEvents = computed(() => events.value.filter(e => showHistory.value || Date.parse(e.timestamp) >= captureSince.value))
const seen = new Set<string>()

function evKey(e: Record<string, unknown>) {
  return String((e.eid as string) || (e.id as string) || '')
}
function upsert(e: Record<string, unknown>) {
  const key = evKey(e)
  if (!key || seen.has(key)) return
  seen.add(key)
  events.value.push({
    key,
    type: String(e.type ?? 'event'),
    source: String(e.source ?? ''),
    timestamp: String(e.timestamp ?? ''),
    data: (e.data ?? {}) as Record<string, unknown>,
    eid: String(e.eid ?? ''),
  })
  if (events.value.length > 300) {
    const drop = events.value.splice(0, events.value.length - 300)
    drop.forEach(d => seen.delete(d.key))
  }
}

function onWindowMessage(ev: MessageEvent) {
  if (ev.source !== iframeEl.value?.contentWindow || ev.origin !== window.location.origin) return
  const d = ev.data
  if (!d || d.source !== 'docmind-runtime') return
  const eid = String(d.eid ?? '')
  eventStatus.value = '收到试玩事件'
  upsert({ eid, type: d.type, data: d.data ?? {}, timestamp: new Date().toISOString(), source: 'web' })
  runtimeApi.append([{ eid, type: d.type, data: d.data ?? {} }]).catch(() => {})
}

// 可见性感知 + 单飞的轮询器：标签页切后台时暂停，回前台立即补一次
const evPoller = createPoller(pollEvents, 3000)
const engineChangePoller = createPoller(pollEngineChanges, 2500)
// 引擎也可能由 agent 的 engine_start/stop 控制：面板开着时轮询真实状态，
// 否则用户会看到“引擎已经在跑，面板却还只有启动按钮”。
const engineStatusPoller = createPoller(refreshNativeStatus, 2500)
async function pollEvents() {
  const project = getProjectId()
  try {
    const r = await runtimeApi.events()
    if (project !== getProjectId()) return
    ;(r.events || []).forEach(upsert)
    eventStatus.value = '已同步 · 仅显示实际收到的事件'
  } catch { eventStatus.value = '事件服务未连接' }
}

async function nativeReload() {
  if (!nativeRunning.value || reloadingNative.value) return
  reloadingNative.value = true
  embedMsg.value = '正在热重载 Godot（快速重启运行实例）…'
  try {
    const r = await engineApi.reload()
    if (r.ok && r.running) {
      nativeRunning.value = true
      embedState.value = r.embedded ? 'embedded' : 'off'
      embedMsg.value = r.reload_mode === 'process_restart'
        ? 'Godot 已热重载，脚本和场景重新导入完成。'
        : 'Godot 热重载完成。'
      if (embedState.value === 'embedded') {
        await syncEngineRect(true)
        await engineApi.focusEngine(true).catch(() => {})
      }
      changedEngineFiles.value = []
    } else {
      embedMsg.value = r.error || 'Godot 热重载失败。'
    }
  } catch (e) {
    embedMsg.value = 'Godot 热重载失败：' + (e as Error).message
  } finally {
    reloadingNative.value = false
    await refreshNativeStatus()
  }
}
function startTimers() {
  captureSince.value = Date.now()
  evPoller.start()
  engineChangePoller.start()
  engineStatusPoller.start()
}
function stopTimers() {
  evPoller.stop()
  engineChangePoller.stop()
  engineStatusPoller.stop()
}

function evClass(t: string) {
  if (t.startsWith('__')) return 'sys'
  if (/damage|hit|defeat|die|death/i.test(t)) return 'bad'
  if (/ready|spawn|start|heal/i.test(t)) return 'good'
  return 'mid'
}
function evTime(e: Ev) {
  if (!e.timestamp) return ''
  const d = new Date(e.timestamp)
  return isNaN(d.getTime()) ? e.timestamp.slice(11, 19) : d.toLocaleTimeString('zh-CN', { hour12: false })
}
function evData(e: Ev) {
  const keys = Object.keys(e.data || {})
  if (!keys.length) return ''
  return keys.map(k => `${k}=${String((e.data as Record<string, unknown>)[k])}`).join('  ')
}
async function clearEvents() {
  // 只清本地数组的话，下一次轮询会把后端存量事件又拉回来（"清空后点一下又出来"）。
  // 必须同时清后端：scope='all' 连引擎日志的续读游标一起归零，否则 godot 抓取的事件照样复现。
  if (!window.confirm('清空全部运行时事件（含已抓取的引擎日志）？此操作不可撤销。')) return
  try { await runtimeApi.clear('all') } catch { eventStatus.value = '清空失败，请重试'; return }
  events.value = []
  seen.clear()
}

/* ---------------- 阶段 4：Bug 反馈闭环（边玩边改） ----------------
   玩的时候发现问题 → 「这里有问题」把运行时事件 + 引擎日志尾部归档到受控 bugs 分区 →
   让 AI 按分区约束修复 → 重导出并重载 → 不满意就回滚本次变更集。
   截图：优先从同源 iframe 的 <canvas> 取 PNG，仅用于让 AI 修时随 question 附到 /api/chat；
   dev_capture_bug 不收图（后端契约只有 7 个文本字段）。取不到 canvas / 跨源异常 → 纯文本降级。 */
const bugs = ref<BugItem[]>([])
const regionOptions = ref<RegionInfo[]>([])
const bugTitle = ref('')
const bugRegion = ref('')
const bugSeverity = ref('error')
const bugMsg = ref('')
const bugMsgErr = ref(false)
const capturingBug = ref(false)
const bugsLoading = ref(false)

function fmtBugTime(iso?: string) {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime())
    ? iso
    : d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

async function refreshRegions() {
  if (demoMode.value) {
    regionOptions.value = demoRegionCards as unknown as RegionInfo[]
    return
  }
  try {
    const r = await regionsApi.list()
    regionOptions.value = r.regions || []
  } catch { /* 分区列表拿不到就只保留「不分来源分区」 */ }
}

async function refreshBugs() {
  if (demoMode.value) {
    bugs.value = [...demoBugs]
    return
  }
  bugsLoading.value = true
  try {
    const r = await bugsApi.list()
    bugs.value = r.bugs || []
    if (!r.ok && r.error) { bugMsg.value = r.error; bugMsgErr.value = true }
  } catch (e) {
    bugMsg.value = 'Bug 列表加载失败：' + (e as Error).message
    bugMsgErr.value = true
  } finally {
    bugsLoading.value = false
  }
}

/** 最近的运行时事件（近 N 条）——作为 Bug 复现上下文。 */
function recentEventLines(limit = 8): string[] {
  return [...visibleEvents.value].slice(-limit).map(e => `${evTime(e)} ${e.type}${evData(e) ? ' ' + evData(e) : ''}`)
}

/** 引擎日志尾部——作为 Bug 的 error / traceback 素材。 */
async function engineLogTail(limit = 24): Promise<string[]> {
  try {
    const r = await engineApi.logs(limit)
    return (r.lines || []).slice(-limit)
  } catch { return [] }
}

/**
 * 截图降级 A：从同源 iframe 的 <canvas> 取 PNG Blob。
 * 工程台与 Web 导出页同源才能取到像素；跨源 / 无 canvas / 未渲染 → 返回 null（降级纯文本）。
 */
function grabCanvasPng(): Blob | null {
  try {
    const win = iframeEl.value?.contentWindow as (Window & { document?: Document }) | null
    const canvas = win?.document?.querySelector('canvas') as HTMLCanvasElement | null
    if (!canvas) return null
    const dataUrl = canvas.toDataURL('image/png')     // 跨源会抛 SecurityError → catch 降级
    const comma = dataUrl.indexOf(',')
    if (comma < 0) return null
    const bin = atob(dataUrl.slice(comma + 1))
    const arr = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
    return new Blob([arr], { type: 'image/png' })
  } catch { return null }
}

async function captureBug() {
  if (capturingBug.value) return
  capturingBug.value = true
  bugMsg.value = ''
  bugMsgErr.value = false
  // 演示模式：本地合成一张 Bug 卡，不触达后端。
  if (demoMode.value) {
    const now = new Date()
    const bid = 'BUG-' + now.toISOString().slice(0, 10).replace(/-/g, '') + '-' + now.toTimeString().slice(0, 8).replace(/:/g, '')
    bugs.value = [{
      id: bid,
      title: bugTitle.value.trim() || '试玩中发现的问题（演示）',
      severity: bugSeverity.value,
      source_region: bugRegion.value || null,
      status: 'open',
      error: '（演示数据）',
      reproduction: recentEventLines(3).join('\n'),
      created_at: now.toISOString(),
    }, ...bugs.value]
    bugMsg.value = `已归档为 ${bid}（演示模式，未写入真实分区）`
    bugTitle.value = ''
    capturingBug.value = false
    return
  }
  try {
    const logs = await engineLogTail(24)
    const errLine = logs.find(l => /error|exception|traceback|SCRIPT ERROR/i.test(l)) || logs[0] || ''
    const evLines = recentEventLines(8)
    const ctx = [
      `项目：${getProjectId() || '（未选择）'}`,
      bugRegion.value ? `来源分区：${bugRegion.value}` : '',
      '最近运行时事件：',
      evLines.length ? evLines.join('\n') : '（无）',
      '',
      '引擎日志尾部：',
      logs.length ? logs.join('\n') : '（无）',
    ].filter(Boolean).join('\n')
    const title = bugTitle.value.trim()
      || (errLine ? errLine.slice(0, 80) : `试玩问题 @ ${new Date().toLocaleString('zh-CN', { hour12: false })}`)
    const r = await bugsApi.capture({
      title,
      error: errLine || '（试玩中人工标记的问题）',
      traceback: logs.join('\n'),
      reproduction: ctx,
      source_region: bugRegion.value || undefined,
      severity: bugSeverity.value,
    })
    if (r.ok) {
      bugMsg.value = `已归档为 ${r.bug_id}${r.path ? '（' + r.path + '）' : ''}`
      bugMsgErr.value = false
      bugTitle.value = ''
      await refreshBugs()
    } else {
      bugMsg.value = r.error || '归档失败'
      bugMsgErr.value = true
    }
  } catch (e) {
    bugMsg.value = '归档失败：' + (e as Error).message
    bugMsgErr.value = true
  } finally {
    capturingBug.value = false
  }
}

async function setBug(b: BugItem, status: string) {
  try {
    const r = await bugsApi.setStatus(b.id, status)
    if (r.ok) await refreshBugs()
    else { bugMsg.value = r.error || '状态更新失败'; bugMsgErr.value = true }
  } catch (e) {
    bugMsg.value = (e as Error).message
    bugMsgErr.value = true
  }
}

/* ---- 「让 AI 修」：组装受控修复指令 → SSE 直连 agent（走受控分区写工具） ---- */
const aiFixFor = ref('')
const aiFix = ref('')
const aiFixing = ref(false)
const aiFixUsedShot = ref(false)
let aiAbort: AbortController | null = null

function buildFixQuestion(b: BugItem): string {
  return [
    '【边玩边改 · Bug 修复】请按分区约束修复下面这个在试玩中发现的问题，',
    '只改动必要文件、不要跨分区写入，改完给出简短说明与改动文件清单。',
    '',
    `Bug ID：${b.id}`,
    `标题：${b.title}`,
    `严重度：${b.severity}`,
    b.source_region ? `来源分区：${b.source_region}` : '',
    b.error ? `错误：${b.error}` : '',
    b.traceback ? `堆栈/日志：\n${b.traceback}` : '',
    b.reproduction ? `复现上下文：\n${b.reproduction}` : '',
  ].filter(Boolean).join('\n')
}

async function fixWithAi(b: BugItem) {
  if (aiFixing.value) return
  // 演示模式：离线展示一段可信的修复结论（不触达后端）。
  if (demoMode.value) {
    aiFixFor.value = b.id
    aiFixUsedShot.value = false
    aiFixing.value = true
    aiFix.value = '正在让 AI 阅读代码并修复…'
    window.setTimeout(() => { aiFix.value = demoBugFixAnswer; aiFixing.value = false }, 600)
    return
  }
  // 截图只在有 iframe 且同源可取 canvas 时才有；仅随 question 附到 /api/chat。
  const shot = grabCanvasPng()
  aiFixUsedShot.value = !!shot
  aiFixFor.value = b.id
  aiFix.value = shot ? '正在让 AI 阅读代码并修复（附带当前画面截图）…' : '正在让 AI 阅读代码并修复…'
  aiFixing.value = true
  aiAbort = new AbortController()
  const parts: string[] = []
  try {
    await aiApi.askGrounded(
      buildFixQuestion(b),
      {
        signal: aiAbort.signal,
        onEvent: (ev) => {
          if (ev.type === 'token' && typeof ev.text === 'string') {
            parts.push(ev.text)
            aiFix.value = parts.join('')
          } else if (ev.type === 'final' && typeof ev.text === 'string' && ev.text.trim()) {
            aiFix.value = ev.text
          }
        },
      },
      shot ? { images: [shot] } : {},
    )
    if (!aiFix.value) aiFix.value = '（AI 未返回内容）'
  } catch (e) {
    aiFix.value = (e as Error).name === 'AbortError' ? '已停止。' : '修复请求失败：' + (e as Error).message
  } finally {
    aiFixing.value = false
    aiAbort = null
  }
}

function stopAiFix() {
  aiAbort?.abort()
}

/** 重导出并重载：复用 doExport() + cmdReload()（不刷新页面热重载游戏）。 */
async function reexport() {
  await doExport()
  if (iframeUrl.value) cmdReload()
}

/** 回滚本次改动：取变更集列表最近一条并回滚（生成新提交撤销，不破坏历史）。 */
async function rollbackLast() {
  if (!window.confirm('回滚最近一条变更集（AI 本次改动）？会生成新提交撤销，不破坏历史。')) return
  if (demoMode.value) {
    const last = demoChangesets[demoChangesets.length - 1]
    bugMsg.value = `已回滚变更集 ${last.id}（演示模式）。`
    bugMsgErr.value = false
    return
  }
  try {
    const r = await changesetApi.list()
    const list = r.changesets || []
    if (!list.length) {
      bugMsg.value = '没有可回滚的变更集（AI 可能尚未提交改动）。'
      bugMsgErr.value = true
      return
    }
    const last = list[list.length - 1]
    const rb = await changesetApi.rollback(last.id)
    if (rb.ok) {
      bugMsg.value = `已回滚变更集 ${last.id}。`
      bugMsgErr.value = false
    } else {
      bugMsg.value = typeof rb.detail === 'string' ? rb.detail : (rb.error || '回滚失败')
      bugMsgErr.value = true
    }
  } catch (e) {
    bugMsg.value = '回滚失败：' + (e as Error).message
    bugMsgErr.value = true
  }
}

/* ---------------- 场景画布（P0-2：Vue Flow 转正） ---------------- */
// pathInput 是正在输入的路径，scenePath 是「已提交、值得去解析」的路径——
// 分开是为了避免每敲一个字符就触发一次后端解析。
const pathInput = ref('')
const scenePath = ref('')
const sceneMessage = ref('')
function loadScene() {
  const value = pathInput.value.trim()
  if (!value) { sceneMessage.value = '请先填写场景路径，例如 scenes/Main.tscn'; return }
  sceneMessage.value = ''
  scenePath.value = value
}
/** 打开「场景画布」时若还没指定场景，自动加载项目主场景（project.godot 的 run/main_scene），
 *  避免出现"一块空画布、不知道该填什么"的困惑。用户已有输入/已加载则不覆盖。 */
async function ensureSceneLoaded() {
  if (scenePath.value || pathInput.value.trim()) return
  try {
    const r = await sceneApi.main()
    if (r.ok && r.scene) {
      pathInput.value = r.scene
      sceneMessage.value = ''
      scenePath.value = r.scene
    }
  } catch { /* 拿不到主场景就保持空态提示，不打扰用户 */ }
}
/** 编辑器里打开 .tscn 时自动同步到画布——点开场景就能看结构，不用再手抄路径 */
watch(() => activeTab.value?.path, (next) => {
  if (next && next.toLowerCase().endsWith('.tscn')) {
    pathInput.value = next
    sceneMessage.value = ''
    scenePath.value = next
  }
})

/** 画布/时间线里双击文件卡 -> 在工作台编辑器打开（带行号时定位） */
function openFromScene(rel: string, line?: number) {
  if (line && line > 0) void jumpToLine(rel, line)
  else void openPath(rel)
}

/** 面板可见（弹窗打开 / 常驻）时的统一初始化：状态刷新 + 定时轮询。 */
function initPane() {
  void refreshTemplates()
  void refreshNativeStatus()
  void refreshDesktop()
  void refreshRegions()
  void refreshBugs()
  startTimers()
  if (tab.value === 'scene') void ensureSceneLoaded()
}

watch(open, v => {
  if (v) {
    initPane()
  } else {
    stopTimers()
    // 弹窗一关，那块"引擎视窗"就不存在了；继续嵌着只会让引擎画在工作台别的位置上。
    // 阶段 4：仅弹窗模式如此；常驻（docked）模式不因可见性变化 detach，只在真正 unmount 时 detach。
    if (!docked.value && embedState.value === 'embedded') void nativeDetach()
  }
})

// 切到常驻（或常驻期间组件首次挂载）时初始化；收起常驻且弹窗未开时停轮询。
// 常驻收起不 detach 引擎（仅在真正 unmount 时），与「不因可见性变化 detach」一致。
watch(docked, v => {
  if (v) initPane()
  else if (!open.value) stopTimers()
})

// 切走试玩 tab：视窗元素被 v-if 摘掉。弹窗模式必须解除；常驻模式不 detach（保持嵌入），
// 切回 play 时再按需重新定位。切回来若引擎还在跑且开着自动嵌入，自动重新嵌回。
watch(tab, v => {
  if (v !== 'play') {
    if (!docked.value && embedState.value === 'embedded') void nativeDetach()
  } else if (nativeRunning.value && autoEmbed.value && embedState.value !== 'embedded' && desktopReady.value) {
    void nativeEmbed()
  } else if (embedState.value === 'embedded') {
    // 常驻切回 play：视窗元素刚重建，下一帧按新矩形重新 place（引擎不中断）。
    void nextTick(() => syncEngineRect(true))
  }
  // 切到场景画布且还没加载场景时，自动带出项目主场景
  if (v === 'scene') void ensureSceneLoaded()
})

/** 关闭面板：docked 关常驻态，popup 关弹窗（互斥，行为与既有入口一致）。 */
function closePane() {
  // v2 重构 E 阶段：AutonomousCockpit 已删，host 永远为 '#wb-playpane-slot'，docked 走 closeRuntimeResident
  if (docked.value) closeRuntimeResident()
  else open.value = false
}

function resetProject() {
  visualGeneration++
  visualOpen.value = false; visualBlob.value = null; visualImage.value = ''
  visualNote.value = ''; visualStatus.value = ''; visualSending.value = false
  stopTimers()
  events.value = []
  seen.clear()
  showHistory.value = false
  eventStatus.value = '等待事件'
  iframeUrl.value = ''
  lastExport.value = null
  exportMsg.value = ''
  scenePath.value = ''
  pathInput.value = ''
  sceneMessage.value = ''
  nativeRunning.value = false
  changedEngineFiles.value = []
}
onMounted(() => {
  window.addEventListener('docmind:project-changed', resetProject)
  window.addEventListener('message', onWindowMessage)
  window.addEventListener('resize', onWindowResize)
  watchDpi()
  // 常驻态下组件可能一开始就是 docked（此时 watch(docked) 已触发过）；此处兜底初始化。
  if (docked.value) initPane()
})
onUnmounted(() => {
  window.removeEventListener('docmind:project-changed', resetProject)
  window.removeEventListener('message', onWindowMessage)
  window.removeEventListener('resize', onWindowResize)
  dpiMq?.removeEventListener('change', onDpiChange)
  dpiMq = null
  if (resizeTimer) window.clearTimeout(resizeTimer)
  aiAbort?.abort()
  // 常驻模式不因可见性变化 detach，只在组件真正 unmount 时解除嵌入，避免引擎窗口悬在别处。
  if (docked.value && embedState.value === 'embedded') void nativeDetach()
  stopTimers()
})
</script>

<template>
  <div class="sr-panel" :class="{ 'sr-docked': docked }">
    <!-- 触发按钮：仅弹窗模式。teleport 到顶栏槽位 #wb-sr-slot（顶栏始终渲染）。
         defer：本轮先把根节点整体插入 document，再解析 target。 -->
    <Teleport defer to="#wb-sr-slot" :disabled="docked">
      <button v-if="!docked" class="sr-trigger" title="一键导出并在工作台里运行游戏，边玩边看日志和场景" @click="open = true"><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 2.2 L9.6 6 L3 9.8 Z" fill="currentColor"/></svg><span class="sr-label">运行游戏</span></button>
    </Teleport>
    <!-- 遮罩：仅弹窗模式 -->
    <div v-if="!docked && open" class="pb-mask wb-modal-backdrop" @click.self="open = false" />
    <!--
      面板主体：弹窗模式在此就地渲染（固定弹层）；常驻模式 teleport 到主区 host 内联填充。
      同一份内容、同一个组件实例，仅切换承载方式，绝不重建。
      defer 同上：主区槽位 #wb-playpane-slot 与本实例同根，需等根节点插入后再解析 target。
    -->
    <Teleport defer :to="props.host" :disabled="!docked">
      <div v-if="docked || open" class="pb-pop" :class="[{ 'pb-pop-inline': docked }, { 'wb-modal-shell': !docked }]">
        <div class="pb-head">
          <div class="pb-tabs">
            <button :class="{ on: tab === 'play' }" @click="tab = 'play'">🎮 Web 试玩</button>
            <button :class="{ on: tab === 'scene' }" @click="tab = 'scene'">🗺 场景画布</button>
            <button :class="{ on: tab === 'timeline' }" @click="tab = 'timeline'">⏱ 运行时时间线</button>
          </div>
          <button class="pb-x" :title="docked ? '收起常驻面板' : '关闭'" @click="closePane">×</button>
        </div>

        <!-- ================= 试玩 tab ================= -->
        <div v-if="tab === 'play'" class="pb-body">
          <div class="pb-main">
            <div class="pb-toolbar">
              <button class="pb-btn primary" :disabled="exporting" @click="doExport">
                {{ exporting ? '导出中…' : (iframeUrl ? '重新导出并试玩' : '导出并试玩') }}
              </button>
              <button class="pb-btn" :disabled="!iframeUrl" @click="cmdReload" title="热重载游戏场景（不刷新页面）">↻ 重载场景</button>
              <button class="pb-btn" :disabled="!iframeUrl" @click="reloadFrame">刷新框架</button>
              <button class="pb-btn" :disabled="!iframeUrl" @click="openExternal">新标签打开</button>
              <button class="pb-btn primary" :disabled="visualBusy || visualSending" @click="inspectCurrentView">{{ visualBusy ? '读取画面…' : visualSending ? 'AI 观察中…' : '让 AI 观察当前画面' }}</button>
              <button class="pb-btn" :disabled="!iframeUrl || visualBusy || visualSending" @click="openVisualFeedback">截图并反馈给 AI</button>
              <span class="pb-sep" />
              <label
                class="pb-check"
                :class="{ off: !desktopReady }"
                :title="desktopReady
                  ? '嵌入后游戏画面直接跑在这块区域里，工作台界面照常可用'
                  : '浏览器模式下无法嵌入原生窗口，请用桌面端启动 Agent 工作台（desktop.py / 桌面快捷方式）'"
              >
                <input v-model="autoEmbed" type="checkbox" :disabled="!desktopReady" />
                嵌入工作台
              </label>
              <button v-if="!nativeRunning" class="pb-btn" @click="nativeStart">🖥 桌面窗口启动</button>
              <template v-else>
                <span class="pb-chip" :class="embedState">{{ embedState === 'embedded' ? '已嵌入' : embedState === 'failed' ? '嵌入失败' : '独立窗口' }}</span>
                <button v-if="embedState === 'embedded'" class="pb-btn" title="把键盘焦点交给游戏窗口（点过工作台之后要还回去）" @click="nativeFocus">聚焦</button>
                <button class="pb-btn" :disabled="reloadingNative" title="保存启动参数并快速重启 Godot，重新载入脚本和场景" @click="nativeReload">{{ reloadingNative ? '重载中…' : '↻ 热重载' }}</button>
                <button v-if="embedState === 'embedded'" class="pb-btn" title="引擎回到独立窗口，进程继续运行" @click="nativeDetach">解除嵌入</button>
                <button class="pb-btn warn" @click="nativeStop">停止桌面窗口</button>
              </template>
            </div>

            <div v-if="exportMsg" class="pb-msg" :class="{ err: exportMsg.includes('失败') }">{{ exportMsg }}</div>
            <div v-if="embedMsg" class="pb-msg" :class="{ err: embedState === 'failed' }">{{ embedMsg }}</div>
            <div v-if="nativeRunning && changedEngineFiles.length" class="pb-msg pb-change">
              检测到外部修改：{{ changedEngineFiles.slice(0, 4).join('、') }}<template v-if="changedEngineFiles.length > 4"> 等 {{ changedEngineFiles.length }} 个文件</template>。确认保存后再点击「热重载」。
            </div>
            <div v-if="!desktopReady" class="pb-hintline">
              当前是浏览器模式：原生引擎只能开独立窗口。用桌面端启动 Agent 工作台 后，勾上「嵌入工作台」即可让游戏跑在这块区域里。
            </div>

            <div v-if="tpl && !tpl.installed && !iframeUrl" class="pb-notpl">
              <b>未检测到 Godot Web 导出模板</b>
              <span>引擎版本：{{ tpl.version }}<br />目录：{{ tpl.template_dir }}</span>
              <div v-if="installing && tpl.install" class="pb-dl">
                <div class="pb-dl-bar"><i :style="{ width: installPct(tpl.install) + '%' }" /></div>
                <small>
                  {{ tpl.install.state === 'extracting' ? '正在解压提取 Web 模板…' : `下载中 ${fmtMB(tpl.install.downloaded)} / ${fmtMB(tpl.install.total)}（${installPct(tpl.install)}%）` }}<template v-if="tpl.install.mirror"> · 源：{{ tpl.install.mirror.replace(/^https?:\/\//, '') }}</template>
                </small>
              </div>
              <button class="pb-btn primary" :disabled="installing" @click="doInstallTemplates">
                {{ installing ? '安装中…' : '下载并安装 Web 模板' }}
              </button>
              <small>也可先用右侧「桌面窗口启动」走原生试玩（无需模板）。</small>
            </div>

            <div v-else-if="tpl?.install && ['downloading','extracting','starting'].includes(tpl.install.state)" class="pb-notpl">
              <b>模板安装中</b>
              <div class="pb-dl">
                <div class="pb-dl-bar"><i :style="{ width: installPct(tpl.install) + '%' }" /></div>
                <small>{{ tpl.install.state === 'extracting' ? '解压中…' : `${fmtMB(tpl.install.downloaded)} / ${fmtMB(tpl.install.total)}` }}<template v-if="tpl.install.mirror"> · {{ tpl.install.mirror.replace(/^https?:\/\//, '') }}</template></small>
              </div>
            </div>

            <!-- 原生引擎嵌入时，Window 子窗口会盖在这一块矩形上（ref 用于算它的坐标） -->
            <small class="pb-interaction-hint">点击游戏画面后使用项目原有按键；「让 AI 观察当前画面」会把网页截图交给视觉模型，原生窗口会调用桌面视觉工具。</small>
            <div v-if="visualStatus && !visualOpen" class="pb-visual-status" role="status">{{ visualStatus }}</div>
            <div ref="engineViewport" class="pb-framewrap" @mousedown="onViewportFocus">
              <iframe
                v-if="iframeUrl"
                ref="iframeEl"
                :src="frameSrc()"
                class="pb-frame"
                allow="autoplay; fullscreen; gamepad"
              />
              <div v-else class="pb-empty">
                点击「导出并试玩」<br />
                <small>Godot 工程将导出为 WebAssembly 并在此画布内运行，支持边玩边让 AI 修改代码</small>
              </div>
            </div>
            <section v-if="visualOpen" ref="visualPanel" class="pb-visual-feedback" aria-label="画面反馈">
              <header><b>这张画面要怎么改？</b><button class="pb-btn" @click="visualOpen = false">收起反馈</button></header>
              <img :src="visualImage" alt="发送给 AI 的当前试玩截图，请检查画面是否完整" />
              <textarea v-model="visualNote" aria-label="画面修改意见" rows="3" placeholder="例如：优化血条和地面效果；先给我方案，审核后再修改。" :disabled="visualSending" />
              <div><button class="pb-btn" :disabled="visualBusy || visualSending" @click="openVisualFeedback">重新截取</button><button class="pb-btn primary" :disabled="!visualNote.trim() || visualSending" @click="sendVisualFeedback">{{ visualSending ? 'AI 分析中…' : '发送截图与意见' }}</button></div>
              <p role="status">{{ visualStatus }}</p>
              <small>截图和意见发送到当前聊天使用的模型；分析回复不代表文件已修改。审核并完成修改后，再重新导出试玩检查效果。</small>
            </section>
          </div>

          <div class="pb-side">
            <div class="pb-side-head">
              <b>运行时事件时间线</b>
              <div><span>{{ eventStatus }}</span><label><input v-model="showHistory" type="checkbox" /> 历史事件</label><button class="pb-link" @click="clearEvents">清空</button><button class="pb-link" @click="pollEvents">刷新</button></div>
            </div>
            <div class="pb-tl">
              <div v-if="!visibleEvents.length" class="pb-tl-empty">本次尚未收到事件。游戏需接入运行时通信桥并主动上报；可勾选历史事件查看此前记录。</div>
              <div v-for="e in [...visibleEvents].reverse()" :key="e.key" class="pb-ev" :class="evClass(e.type)" :title="e.timestamp">
                <span class="pb-ev-time">{{ evTime(e) }}</span>
                <span class="pb-ev-type">{{ e.type }}</span>
                <span class="pb-ev-src">{{ e.source }}</span>
                <span v-if="evData(e)" class="pb-ev-data">{{ evData(e) }}</span>
              </div>
            </div>

            <!-- ===== 阶段 4：Bug 反馈闭环（边玩边改） ===== -->
            <div class="pb-bugwrap">
              <div class="pb-side-head">
                <b>Bug 反馈闭环</b>
                <div><span v-if="bugsLoading" class="pb-bug-loading">加载中…</span><button class="pb-link" @click="refreshBugs">刷新</button></div>
              </div>
              <div class="pb-bugtool">
                <input v-model="bugTitle" class="pb-bugtitle" placeholder="问题标题（留空自动生成）" @keyup.enter="captureBug" />
                <select v-model="bugRegion" class="pb-bugsel" title="来源分区（留空=不分来源，传错后端会拒）">
                  <option value="">（不分来源分区）</option>
                  <option v-for="r in regionOptions" :key="r.key" :value="r.key">{{ r.name }}</option>
                </select>
                <select v-model="bugSeverity" class="pb-bugsel" title="严重度">
                  <option value="error">error</option>
                  <option value="warning">warning</option>
                  <option value="info">info</option>
                </select>
                <button class="pb-btn primary" :disabled="capturingBug" @click="captureBug">{{ capturingBug ? '归档中…' : '🐞 这里有问题' }}</button>
              </div>
              <div v-if="bugMsg" class="pb-msg" :class="{ err: bugMsgErr }">{{ bugMsg }}</div>

              <div class="pb-bugs">
                <div v-if="!bugs.length" class="pb-tl-empty">暂无 Bug。玩的时候发现问题，点「🐞 这里有问题」把运行时事件 + 引擎日志归档到受控 bugs 分区。</div>
                <div v-for="b in bugs" :key="b.id" class="pb-bug" :class="'st-' + b.status">
                  <div class="pb-bug-top"><i class="pb-sev" :class="'sev-' + b.severity">{{ b.severity }}</i><span class="pb-bug-title">{{ b.title }}</span></div>
                  <div class="pb-bug-meta">{{ b.id }}<template v-if="b.source_region"> · {{ b.source_region }}</template> · {{ b.status }} · {{ fmtBugTime(b.created_at) }}</div>
                  <div class="pb-bug-acts">
                    <button class="pb-link" @click="fixWithAi(b)">让 AI 修</button>
                    <button class="pb-link" @click="setBug(b, 'investigating')">修复中</button>
                    <button class="pb-link" @click="setBug(b, 'fixed')">已修</button>
                    <button class="pb-link" @click="setBug(b, 'ignored')">忽略</button>
                  </div>
                </div>
              </div>

              <div v-if="aiFixFor" class="pb-fix">
                <div class="pb-fix-head">
                  <b>AI 修复 {{ aiFixFor }}</b>
                  <span v-if="aiFixUsedShot" class="pb-fix-shot" title="已把当前画面截图随问题一起发给 AI">🖼 附截图</span>
                  <button v-if="aiFixing" class="pb-link" @click="stopAiFix">停止</button>
                </div>
                <pre class="pb-fix-out">{{ aiFix }}</pre>
                <div class="pb-fix-acts">
                  <button class="pb-btn" :disabled="exporting" @click="reexport">重导出并重载</button>
                  <button class="pb-btn warn" @click="rollbackLast">回滚本次改动</button>
                  <button v-if="!aiFixing" class="pb-link" @click="aiFixFor = ''">收起</button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- ================= 场景画布 tab（P0-2） ================= -->
        <div v-else-if="tab === 'scene'" class="pb-body">
          <div class="pb-main">
            <div class="pb-toolbar">
              <input v-model="pathInput" class="pb-path" placeholder="scenes/Main.tscn（支持任意 .tscn 路径）"
                     @keyup.enter="loadScene" />
              <button class="pb-btn primary" @click="loadScene">加载场景</button>
              <small class="pb-hint">在左侧文件树里点开 .tscn 会自动带到这里；画布上可改节点、属性与位置，全部可撤销。</small>
            </div>
            <div v-if="sceneMessage" class="pb-msg err">{{ sceneMessage }}</div>
            <SceneCanvas v-if="scenePath" :path="scenePath" @open-file="openFromScene" />
            <div v-else class="pb-empty2">
              填入场景路径并点「加载场景」<br />
              <small>也可在左侧文件树双击任意 .tscn 直接打开画布</small>
            </div>
          </div>
        </div>

        <!-- ================= 运行时时间线 tab（P1-1） ================= -->
        <div v-else class="pb-body">
          <div class="pb-main">
            <RuntimeTimeline @open-file="openFromScene" />
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.sr-panel { position: relative; }
.sr-trigger { display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--border-strong); background: transparent; color: var(--text-muted); border-radius: 5px; padding: 5px 9px; cursor: pointer; white-space: nowrap; transition: color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease; }
.sr-trigger:hover { color: var(--text); border-color: color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); background: var(--bg-hover); transform: translateY(-1px); }

.pb-mask { position: fixed; inset: 0; background: var(--scrim); z-index: 65; animation: pb-mask-in .2s ease-out both; }
.pb-pop { position: fixed; z-index: 66; left: 50%; top: 50%; transform: translate(-50%, -50%); width: 1360px; max-width: 96vw; height: 840px; max-height: 92vh; background: var(--bg-raised); border: 1px solid var(--border-strong); border-radius: 10px; box-shadow: 0 24px 70px rgba(35, 52, 84, 0.16); display: flex; flex-direction: column; overflow: hidden; animation: pb-pop-in .24s cubic-bezier(.2,.8,.2,1) both; }
.pb-head { display: flex; align-items: center; justify-content: space-between; padding: 9px 12px; border-bottom: 1px solid var(--border); background: linear-gradient(180deg, var(--bg-raised), var(--bg-hover)); }
.pb-tabs { display: flex; gap: 6px; }
.pb-tabs button { background: transparent; border: 1px solid transparent; color: var(--text-muted); padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: var(--fs-sm); transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease; }
.pb-tabs button:hover:not(.on) { color: var(--text); background: var(--bg-selected); transform: translateY(-1px); }
.pb-tabs button.on { background: linear-gradient(180deg, var(--accent-loud), var(--accent)); border-color: var(--accent-deep); color: var(--accent-ink); }
.pb-x { background: none; border: 0; color: var(--text-muted); font-size: var(--fs-lead); cursor: pointer; border-radius: 5px; padding: 2px 6px; transition: color .16s ease, background .16s ease, transform .16s ease; }
.pb-x:hover { color: var(--text); background: var(--bg-selected); transform: rotate(90deg); }
.pb-body { flex: 1; display: flex; min-height: 0; }
.pb-main { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: auto; padding: 10px 12px; gap: 8px; background: linear-gradient(180deg, color-mix(in srgb, var(--bg-raised) 45%, transparent), transparent 35%); }
.pb-side { width: 320px; border-left: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; background: linear-gradient(180deg, var(--bg-hover), var(--bg-raised)); }
.pb-side-head { display: flex; align-items: center; justify-content: space-between; padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: var(--fs-sm); }
.pb-side-head > div { display: flex; align-items: center; gap: 8px; }
.pb-live { color: var(--danger); font-size: var(--fs-cap); letter-spacing: 1px; }
.pb-link { background: none; border: 0; color: var(--text-muted); font-size: var(--fs-cap); cursor: pointer; }
.pb-link:hover { color: var(--text); }

.pb-toolbar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pb-sep { width: 1px; height: 20px; background: var(--border); margin: 0 4px; }
.pb-btn { border: 1px solid var(--border-strong); background: transparent; color: var(--text-muted); border-radius: 5px; padding: 5px 10px; font-size: var(--fs-sm); cursor: pointer; transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease, box-shadow .16s ease; }
.pb-btn:hover:not(:disabled) { color: var(--text); border-color: color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); transform: translateY(-1px); box-shadow: 0 3px 8px rgba(35,52,84,.08); }
.pb-btn:disabled { opacity: .4; cursor: default; }
.pb-btn.primary { background: linear-gradient(180deg, var(--accent-loud), var(--accent)); border-color: var(--accent-deep); color: var(--accent-ink); }
.pb-btn.warn { border-color: color-mix(in srgb, var(--danger) 50%, transparent); color: var(--danger); }
.pb-path { flex: 1; min-width: 200px; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 6px 8px; border-radius: 5px; font-size: var(--fs-sm); }

.pb-msg { font-size: var(--fs-cap); color: var(--green); background: color-mix(in srgb, var(--green) 12%, var(--bg-raised)); border: 1px solid color-mix(in srgb, var(--green) 32%, var(--bg-raised)); border-radius: 5px; padding: 6px 9px; max-height: 90px; overflow: auto; white-space: pre-wrap; }
.pb-msg.err { color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); border-color: color-mix(in srgb, var(--danger) 38%, transparent); }

.pb-notpl { margin-top: 4px; border: 1px dashed color-mix(in srgb, var(--amber) 38%, transparent); background: color-mix(in srgb, var(--amber) 12%, var(--bg-raised)); border-radius: 7px; padding: 14px; display: flex; flex-direction: column; gap: 9px; font-size: var(--fs-sm); color: var(--text-muted); }
.pb-notpl b { color: var(--amber); font-size: var(--fs-md); }
.pb-notpl small { color: var(--text-faint); line-height: 1.6; }
.pb-dl { display: flex; flex-direction: column; gap: 4px; }
.pb-dl-bar { height: 7px; border-radius: 4px; background: rgba(35, 52, 84, 0.12); overflow: hidden; }
.pb-dl-bar i { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 70%, var(--bg-raised))); transition: width .3s; }

.pb-framewrap { flex: 0 1 auto; width: min(100%, 860px); height: 480px; min-height: 260px; border: 1px solid var(--border); border-radius: 9px; overflow: hidden; background: var(--bg-raised); position: relative; box-shadow: 0 8px 22px rgba(35,52,84,.06); transition: border-color .18s ease, box-shadow .18s ease; }
.pb-framewrap:hover { border-color: var(--border-strong); box-shadow: 0 10px 26px rgba(35,52,84,.1); }
.pb-frame { width: 100%; height: 100%; border: 0; display: block; background: var(--bg-raised); }
.pb-empty { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; color: var(--text-faint); font-size: var(--fs-body); text-align: center; padding: 30px; line-height: 1.8; }
.pb-empty small { font-size: var(--fs-cap); max-width: 380px; }

.pb-tl { flex: 1 1 45%; min-height: 150px; overflow: auto; padding: 4px 0; font: var(--fs-cap) var(--font-mono); }
.pb-tl-empty { padding: 18px 12px; color: var(--text-faint); font-family: var(--font-base, inherit); line-height: 1.7; }
.pb-ev { display: flex; align-items: baseline; gap: 7px; padding: 5px 10px; border-bottom: 1px solid rgba(35, 52, 84, 0.08); transition: background .15s ease, transform .15s ease; }
.pb-ev:hover { background: var(--bg-selected); transform: translateX(2px); }
.pb-ev-time { color: var(--text-faint); flex: 0 0 62px; }
.pb-ev-type { font-weight: 700; }
.pb-ev-src { color: var(--text-faint); font-size: var(--fs-cap); border: 1px solid var(--border); border-radius: 3px; padding: 0 4px; }
.pb-ev-data { color: var(--text-muted); }
.pb-ev.good .pb-ev-type { color: var(--green); }
.pb-ev.bad .pb-ev-type { color: var(--danger); }
.pb-ev.mid .pb-ev-type { color: var(--accent); }
.pb-ev.sys .pb-ev-type { color: var(--text-faint); }

.pb-hint { color: var(--text-faint); font-size: var(--fs-cap); }
.pb-check { display: flex; align-items: center; gap: 4px; font-size: var(--fs-cap); color: var(--text-muted); cursor: pointer; white-space: nowrap; }
.pb-check.off { opacity: .5; cursor: not-allowed; }
.pb-chip { font-size: var(--fs-cap); border: 1px solid var(--border-strong); border-radius: 4px; padding: 2px 6px; color: var(--text-muted); }
.pb-chip.embedded { color: var(--green); border-color: color-mix(in srgb, var(--green) 38%, transparent); background: color-mix(in srgb, var(--green) 12%, var(--bg-raised)); }
.pb-chip.failed { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 38%, transparent); background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); }
.pb-hintline { font-size: var(--fs-cap); color: var(--text-faint); line-height: 1.7; }
.pb-empty2 { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: var(--text-faint); border: 1px dashed var(--border); border-radius: 8px; text-align: center; line-height: 1.8; }
.pb-empty2 small { font-size: var(--fs-cap); }

/* ===== 阶段 4：常驻（docked）形态 =====
   .pb-pop 被 teleport 进主区 host（#wb-playpane-slot），去掉固定弹层的定位/尺寸/阴影，
   内联填充父容器；同时放开预览区与侧栏的固定宽度，让其自适应。 */
/* `animation: none` 是必须的，不是装饰：`.pb-pop` 上的 `animation: pb-pop-in ... both`
   的 to 帧把 `transform: translate(-50%, -50%)` 钉住了，而**动画在层叠里压过普通声明**，
   所以这里的 `transform: none` 本身拦不住它（`left/top: auto` 却能生效）。结果面板按静态
   位置落位后又被 translate 拖走自身宽高的一半：实测 1993 宽 → left = 39-996 = -957，
   1057 高 → top = 482-528 = -46，于是整个画面区被推出屏幕左侧，只剩右侧那条 Bug 列表
   飘在幕布中间（用户报的「幕布偏左」）。 */
.pb-pop-inline { position: static; left: auto; top: auto; transform: none; animation: none; width: 100%; height: 100%; max-width: none; max-height: none; flex: 1; border: 0; border-radius: 0; box-shadow: none; }
.pb-pop-inline .pb-framewrap { flex: 2 1 auto; width: 100%; height: auto; min-height: 220px; }
.pb-pop-inline .pb-side { width: clamp(260px, 26vw, 380px); }

/* ===== 阶段 4：Bug 反馈闭环 ===== */
.pb-bugwrap { flex: 1 1 52%; min-height: 0; display: flex; flex-direction: column; border-top: 1px solid var(--border); }
.pb-bug-loading { color: var(--text-faint); font-size: var(--fs-cap); }
.pb-bugtool { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px 10px; border-bottom: 1px solid rgba(35, 52, 84, 0.08); }
.pb-bugtitle { flex: 1 1 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 5px 7px; border-radius: 5px; font-size: var(--fs-sm); }
.pb-bugsel { flex: 0 1 auto; max-width: 46%; background: var(--bg); border: 1px solid var(--border); color: var(--text-muted); padding: 4px 6px; border-radius: 5px; font-size: var(--fs-cap); }
.pb-bugs { flex: 1; overflow: auto; padding: 4px 0; }
.pb-bug { padding: 8px 10px; border-bottom: 1px solid rgba(35, 52, 84, 0.08); display: flex; flex-direction: column; gap: 3px; transition: background .15s ease, border-color .15s ease, transform .15s ease; }
.pb-bug:hover { background: var(--bg-selected); transform: translateX(2px); }
.pb-bug.st-fixed { opacity: .62; }
.pb-bug.st-ignored { opacity: .45; }
.pb-bug-top { display: flex; align-items: baseline; gap: 6px; }
.pb-bug-title { font-size: var(--fs-sm); color: var(--text); line-height: 1.5; }
.pb-sev { font-style: normal; font-size: var(--fs-cap); border-radius: 3px; padding: 1px 5px; border: 1px solid var(--border); color: var(--text-muted); flex: 0 0 auto; }
.pb-sev.sev-error { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 38%, transparent); background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); }
.pb-sev.sev-warning { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 38%, transparent); background: color-mix(in srgb, var(--amber) 12%, var(--bg-raised)); }
.pb-sev.sev-info { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); background: color-mix(in srgb, var(--accent) 12%, var(--bg-raised)); }
.pb-bug-meta { font: 10px var(--font-mono); color: var(--text-faint); }
.pb-bug-acts { display: flex; flex-wrap: wrap; gap: 10px; }
.pb-fix { border-top: 1px solid var(--border); padding: 6px 10px 10px; display: flex; flex-direction: column; gap: 6px; }
.pb-fix-head { display: flex; align-items: center; gap: 8px; font-size: var(--fs-sm); }
.pb-fix-shot { font-size: var(--fs-cap); color: var(--green); }
.pb-fix-out { margin: 0; max-height: 200px; overflow: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px; font: var(--fs-cap)/1.6 var(--font-mono); color: var(--text); white-space: pre-wrap; word-break: break-word; }
.pb-fix-acts { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.pb-interaction-hint, .pb-visual-status { font-size: var(--fs-sm); color: var(--text-muted); }
.pb-visual-feedback { flex: 0 0 auto; display: flex; flex-direction: column; gap: 8px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--bg-raised); padding: 12px; box-shadow: 0 8px 20px rgba(35,52,84,.08); animation: pb-section-in .2s ease-out both; }
.pb-visual-feedback header, .pb-visual-feedback > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.pb-visual-feedback img { max-width: 100%; max-height: 180px; object-fit: contain; background: var(--bg); }
.pb-visual-feedback textarea { box-sizing: border-box; width: 100%; resize: vertical; padding: 8px; border: 1px solid var(--border); border-radius: 6px; color: var(--text); background: var(--bg); font: inherit; }
.pb-visual-feedback p { margin: 0; font-size: var(--fs-sm); }
.pb-visual-feedback small { color: var(--text-muted); }

@keyframes pb-mask-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes pb-pop-in { from { opacity: 0; transform: translate(-50%, -48%) scale(.985); } to { opacity: 1; transform: translate(-50%, -50%) scale(1); } }
@keyframes pb-section-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

@media (max-width: 900px) {
  .pb-pop { max-width: 98vw; height: 94vh; }
  .pb-body { flex-direction: column; overflow: auto; }
  .pb-main { flex: 0 0 auto; min-height: 0; }
  .pb-side { width: auto; min-height: 300px; border-left: 0; border-top: 1px solid var(--border); }
  .pb-framewrap, .pb-pop-inline .pb-framewrap { width: 100%; height: min(54vw, 480px); min-height: 220px; }
}

@media (max-width: 560px) {
  .pb-head { padding: 7px 8px; }
  .pb-tabs { gap: 3px; overflow-x: auto; }
  .pb-tabs button { padding: 5px 8px; white-space: nowrap; }
  .pb-main { padding: 8px; }
  .pb-toolbar { align-items: stretch; }
  .pb-toolbar .pb-btn { flex: 1 1 auto; }
  .pb-toolbar .pb-sep, .pb-toolbar .pb-check { display: none; }
  .pb-framewrap, .pb-pop-inline .pb-framewrap { height: 58vw; min-height: 190px; }
  .pb-side-head { align-items: flex-start; gap: 6px; flex-direction: column; }
  .pb-side-head > div { width: 100%; justify-content: flex-start; flex-wrap: wrap; }
}

@media (prefers-reduced-motion: reduce) {
  .sr-trigger, .pb-tabs button, .pb-x, .pb-btn, .pb-framewrap, .pb-ev, .pb-bug { transition: none; }
  .pb-mask, .pb-pop, .pb-visual-feedback { animation: none; }
}
</style>
