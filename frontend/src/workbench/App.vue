<script setup lang="ts">
// Agent Workbench 开发工作台 · P0 任务 4：多标签编辑 + 保存 + 新建/改名/删除。
import { onMounted, onBeforeUnmount, computed, defineAsyncComponent, nextTick, reactive, ref, watch } from 'vue'
import FileTree from './components/FileTree.vue'
import CodeView from './components/CodeView.vue'
import EditorTabs from './components/EditorTabs.vue'
import ContextMenu from './components/ContextMenu.vue'
import AppDialog from './components/AppDialog.vue'
import SymbolOutline from './components/SymbolOutline.vue'
import SymbolMap from './components/SymbolMap.vue'
import SelectionToolbar from './components/SelectionToolbar.vue'
import SelectionAiPanel from './components/SelectionAiPanel.vue'
import GitHistoryDialog from './components/GitHistoryDialog.vue'
import GitChangesPanel from './components/GitChangesPanel.vue'
import RewriteDiffDialog from './components/RewriteDiffDialog.vue'
import TaskEnginePanel from './components/TaskEnginePanel.vue'
import SceneRuntimePanel from './components/SceneRuntimePanel.vue'
import ChatDock from './components/ChatDock.vue'
import ExecApprovalDialog from './components/ExecApprovalDialog.vue'
import GpuPanel from './components/GpuPanel.vue'
import EdaVerifyPanel from './components/EdaVerifyPanel.vue'
import LiveStreamPanel from './components/LiveStreamPanel.vue'
import BrowserLivePanel from './components/BrowserLivePanel.vue'
import HarnessPanel from './components/HarnessPanel.vue'
import SemanticLocateBar from './components/SemanticLocateBar.vue'
import WorkspaceTabs from './components/WorkspaceTabs.vue'
import RightRail from './components/RightRail.vue'
import { useWorkbench, askConfirm } from './composables/workbench'
import {
  RIGHT_RAIL_DEFAULT_W, RIGHT_RAIL_MIN_W, RIGHT_RAIL_MAX_W, RIGHT_RAIL_MIN_VIEWPORT,
  clampRightRailWidth,
} from './lib/rightRail'
import { probeBackend, demoMode, demoTagMap, demoRegionCards } from './composables/demo'
import { regionColor } from './theme'
import { projectApi, getProjectId, setProjectId, type ProjectInfo } from './api'
import { appEvents } from './eventBus'
import { loadLayout, onLayoutChange, type LayoutKey } from './layoutMode'

// 重型弹层/视图按需加载：仅在首次打开时拉取独立 chunk（首屏不下载、不解析）
const RelationGraph = defineAsyncComponent(() => import('./components/RelationGraph.vue'))
const UnityGraph = defineAsyncComponent(() => import('./components/UnityGraph.vue'))
const FlowCanvas = defineAsyncComponent(() => import('./components/FlowCanvas.vue'))
const RegionMapDialog = defineAsyncComponent(() => import('./components/RegionMapDialog.vue'))
const SettingsView = defineAsyncComponent(() => import('./components/SettingsView.vue'))
const AssetCenterView = defineAsyncComponent(() => import('./components/AssetCenterView.vue'))
const BackgroundJobsPanel = defineAsyncComponent(() => import('./components/BackgroundJobsPanel.vue'))
// 自主开发舱已删除（v2 重构 §1.1 范围「只保留 ①实时对话视频流 ②窗口嵌入」；
// 原 AutonomousCockpit 组件于 commit E 阶段移除；实时流与 SceneRuntimePanel 嵌入仍由 ChatDock + SceneRuntimePanel 承担）

// J 双色 Risograph 印刷语言层 + 双色纠偏（在 main.ts 引入的 style.css / skins.css 之后加载）
import './workbench-riso.css'
// 排版令牌系统（"重做排版"版式共享基础层；仅挂载令牌、不覆写 body 13px 密度，详见 workbench-typo.css）
import './workbench-typo.css'

// 演示侧栏的文件 → 业务标签（key 取文件名，与静态示例树对齐）
const demoBadgeOf = (name: string) => {
  const hit = Object.entries(demoTagMap).find(([p]) => p.endsWith('/' + name))
  return hit ? hit[1].tags[0] : ''
}

const {
  tree, treeLoading, treeError, loadTree,
  tabs, activeTab, selectedPath, openNode, saveActive,
  openSymbolMap, openRelationGraph,
  openUnityGraph, openFlow,
  revertPath, openHistory,
  openChanges,
  openRegionMap,
  relationGraphOpen, unityGraphOpen, flowOpen, regionMapOpen, changesOpen, jobsOpen,
  aiPanelOpen,
  workspace, setWorkspace, seedDemoRegionCards,
  closeAllTabs, closeRuntime,
  // 阶段 4：运行面板常驻（docked）态
  runtimeResident,
  // 右侧预览栏（第三栏）
  rightRailOpen, toggleRightRail,
} = useWorkbench()

const dirtyCount = computed(() => tabs.value.filter((t) => t.dirty).length)

// ---------------------------------------------------------------- 资源管理器宽度（可拖拽）
// 固定布局默认左栏 288px，长路径 / 深层目录经常不够看。这里在左栏与主区之间加一条
// 拖拽分隔条：只作用于 fixed 布局（focus/dock/split/grid 各有自己的空间语义），
// 宽度记在 localStorage，双击恢复默认，聚焦后左右方向键可微调。
const RAIL_WIDTH_KEY = 'docmind.railWidth'
const RAIL_DEFAULT_W = 288
const RAIL_MIN_W = 220
const RAIL_MAX_W = 640
const RAIL_MAIN_MIN_W = 480
const layoutKey = ref<LayoutKey>(loadLayout())
let offLayoutChange: (() => void) | null = null

/** 宽度上限同时受视口约束：主区至少留 480px，避免把右侧挤到不可用。 */
function clampRailWidth(w: number) {
  const ceiling = Math.max(RAIL_MIN_W, Math.min(RAIL_MAX_W, window.innerWidth - RAIL_MAIN_MIN_W))
  return Math.round(Math.max(RAIL_MIN_W, Math.min(ceiling, w)))
}

const railWidth = ref(RAIL_DEFAULT_W)
try {
  const saved = Number(window.localStorage.getItem(RAIL_WIDTH_KEY))
  if (Number.isFinite(saved) && saved > 0) railWidth.value = clampRailWidth(saved)
} catch { /* 隐私模式等存储不可用时保持默认 */ }

// 收起态同样记在本地：收起不是「临时隐藏」，重开工作台仍按用户上次的选择。
const RAIL_COLLAPSED_KEY = 'docmind.railCollapsed'
const railCollapsed = ref(false)
try {
  railCollapsed.value = window.localStorage.getItem(RAIL_COLLAPSED_KEY) === '1'
} catch { /* 存储不可用时默认展开 */ }

const mobileViewport = window.matchMedia('(max-width: 640px)')
const mobileWorkbench = ref(mobileViewport.matches)
const mobileRailOpen = ref(false)
function syncMobileWorkbench() {
  mobileWorkbench.value = mobileViewport.matches
  mobileRailOpen.value = false
}
onMounted(() => mobileViewport.addEventListener('change', syncMobileWorkbench))
onBeforeUnmount(() => mobileViewport.removeEventListener('change', syncMobileWorkbench))
const railCollapsedActive = computed(
  () => !runtimeResident.value && (mobileWorkbench.value ? !mobileRailOpen.value : layoutKey.value === 'fixed' && railCollapsed.value),
)
const railResizable = computed(
  () => !mobileWorkbench.value && layoutKey.value === 'fixed' && !runtimeResident.value && !railCollapsedActive.value,
)
const railStyle = computed<Record<string, string> | undefined>(() =>
  railResizable.value ? { '--lay-rail': `${railWidth.value}px` } : undefined,
)
const railDragging = ref(false)
let railDragStartX = 0
let railDragStartW = 0
let railDragPointer = -1

function persistRailWidth() {
  try { window.localStorage.setItem(RAIL_WIDTH_KEY, String(railWidth.value)) } catch { /* 仅本次会话 */ }
}
function detachRailDrag() {
  railDragging.value = false
  railDragPointer = -1
  document.body.classList.remove('ft-col-resizing')
  window.removeEventListener('pointermove', onRailPointerMove)
  window.removeEventListener('pointerup', onRailPointerUp)
  window.removeEventListener('pointercancel', onRailPointerUp)
}
function onRailPointerMove(e: PointerEvent) {
  if (!railDragging.value || e.pointerId !== railDragPointer) return
  railWidth.value = clampRailWidth(railDragStartW + (e.clientX - railDragStartX))
}
function onRailPointerUp(e: PointerEvent) {
  if (!railDragging.value || e.pointerId !== railDragPointer) return
  detachRailDrag()
  persistRailWidth()
}
function onRailPointerDown(e: PointerEvent) {
  if (e.button !== 0) return
  // 不调用 preventDefault：取消 pointerdown 会按规范吞掉随后的 click/dblclick，
  // 双击复位就失效了。防选中交给 body.ft-col-resizing 的 user-select:none。
  railDragging.value = true
  // 捕获指针：拖出窗口后松手也能收到 pointerup，避免"拖动态"卡在页面上。
  ;(e.currentTarget as HTMLElement | null)?.setPointerCapture?.(e.pointerId)
  railDragStartX = e.clientX
  railDragStartW = railWidth.value
  railDragPointer = e.pointerId
  document.body.classList.add('ft-col-resizing')
  window.addEventListener('pointermove', onRailPointerMove)
  window.addEventListener('pointerup', onRailPointerUp)
  window.addEventListener('pointercancel', onRailPointerUp)
}
function resetRailWidth() {
  railWidth.value = RAIL_DEFAULT_W
  persistRailWidth()
}
function nudgeRail(dx: number) {
  railWidth.value = clampRailWidth(railWidth.value + dx)
  persistRailWidth()
}

// ---------------------------------------------------------------- 右侧预览栏（第三栏）
// 「查看改动 / 文中引用 / 文件产物 / 网页」在固定布局下进入最右侧只读栏，
// 而不是弹窗或编辑器标签。栏可拖拽改宽、双击复位、←/→ 微调；
// 关闭后**不占位也不留窄条**（零痕迹），恢复入口在顶栏「更多 → 预览侧栏」。
// 只在 fixed 布局且视口 ≥1200 时占位：dock 是竖排、split 是对半分，
// 强塞第三栏会破坏那套布局的空间语义；窄屏同理，入口回落为弹窗/编辑器。
const RIGHT_RAIL_WIDTH_KEY = 'docmind.rightRailWidth'
const rightRailWidth = ref(RIGHT_RAIL_DEFAULT_W)

/** 宽度上限同时受视口与左栏实际占宽约束：主区至少留 520px。
 *  左栏收起时仍留 36px 恢复窄条（不是 0），右栏自己还有 3px 拖拽条 —— 都要扣掉。 */
function rightRailClamp(w: number) {
  const left = railCollapsedActive.value ? 36 : railWidth.value + 3
  return clampRightRailWidth(w, {
    innerWidth: window.innerWidth,
    leftWidth: left + 3,
  })
}
try {
  const saved = Number(window.localStorage.getItem(RIGHT_RAIL_WIDTH_KEY))
  if (Number.isFinite(saved) && saved > 0) rightRailWidth.value = rightRailClamp(saved)
} catch { /* 隐私模式等存储不可用时保持默认 */ }

const rightRailViewport = window.matchMedia(`(min-width: ${RIGHT_RAIL_MIN_VIEWPORT}px)`)
const rightRailViewportWide = ref(rightRailViewport.matches)
function syncRightRailViewport() {
  rightRailViewportWide.value = rightRailViewport.matches
  rightRailWidth.value = rightRailClamp(rightRailWidth.value)
}
onMounted(() => rightRailViewport.addEventListener('change', syncRightRailViewport))
onBeforeUnmount(() => rightRailViewport.removeEventListener('change', syncRightRailViewport))

/** 当前布局/视口撑得起第三栏（不含用户开关态）。 */
const rightRailAvailable = computed(
  () => !!tree.value
    && !runtimeResident.value
    && !edaResident.value
    && layoutKey.value === 'fixed'
    && rightRailViewportWide.value
    && !mobileWorkbench.value,
)
/** 右栏真正占位：可用 + 用户已打开。 */
const rightRailActive = computed(() => rightRailAvailable.value && rightRailOpen.value)
const rightRailStyle = computed<Record<string, string> | undefined>(() =>
  rightRailActive.value ? { '--lay-rail-right': `${rightRailWidth.value}px` } : undefined,
)

const rightRailDragging = ref(false)
let rrDragStartX = 0
let rrDragStartW = 0
let rrDragPointer = -1

function persistRightRailWidth() {
  try { window.localStorage.setItem(RIGHT_RAIL_WIDTH_KEY, String(rightRailWidth.value)) } catch { /* 仅本次会话 */ }
}
function detachRightRailDrag() {
  rightRailDragging.value = false
  rrDragPointer = -1
  document.body.classList.remove('rr-col-resizing')
  window.removeEventListener('pointermove', onRightRailPointerMove)
  window.removeEventListener('pointerup', onRightRailPointerUp)
  window.removeEventListener('pointercancel', onRightRailPointerUp)
}
function onRightRailPointerMove(e: PointerEvent) {
  if (!rightRailDragging.value || e.pointerId !== rrDragPointer) return
  // 拖的是右栏左边缘：向左 = 变宽，向右 = 变窄
  rightRailWidth.value = rightRailClamp(rrDragStartW - (e.clientX - rrDragStartX))
}
function onRightRailPointerUp(e: PointerEvent) {
  if (!rightRailDragging.value || e.pointerId !== rrDragPointer) return
  detachRightRailDrag()
  persistRightRailWidth()
}
function onRightRailPointerDown(e: PointerEvent) {
  if (e.button !== 0) return
  // 同左栏：不 preventDefault，否则随后的 dblclick 会被吞掉，双击复位失效。
  rightRailDragging.value = true
  ;(e.currentTarget as HTMLElement | null)?.setPointerCapture?.(e.pointerId)
  rrDragStartX = e.clientX
  rrDragStartW = rightRailWidth.value
  rrDragPointer = e.pointerId
  document.body.classList.add('rr-col-resizing')
  window.addEventListener('pointermove', onRightRailPointerMove)
  window.addEventListener('pointerup', onRightRailPointerUp)
  window.addEventListener('pointercancel', onRightRailPointerUp)
}
function resetRightRailWidth() {
  rightRailWidth.value = rightRailClamp(RIGHT_RAIL_DEFAULT_W)
  persistRightRailWidth()
}
function nudgeRightRail(dx: number) {
  rightRailWidth.value = rightRailClamp(rightRailWidth.value + dx)
  persistRightRailWidth()
}
// 左栏拖宽/收起展开会改变右栏上限（主区至少 520），需要重新夹取。
watch([railWidth, railCollapsedActive], () => {
  rightRailWidth.value = rightRailClamp(rightRailWidth.value)
})
function onWindowResizeForRightRail() {
  rightRailWidth.value = rightRailClamp(rightRailWidth.value)
}
onMounted(() => window.addEventListener('resize', onWindowResizeForRightRail))
onBeforeUnmount(() => window.removeEventListener('resize', onWindowResizeForRightRail))

// 收起后焦点会随按钮一起消失，转到窄条上，键盘用户不会「丢焦点」。
const railRestoreBtn = ref<HTMLButtonElement | null>(null)
function toggleRailCollapsed() {
  if (mobileWorkbench.value) {
    mobileRailOpen.value = !mobileRailOpen.value
    void nextTick(() => {
      if (mobileRailOpen.value) document.querySelector<HTMLElement>('.ft-panel button')?.focus()
      else railRestoreBtn.value?.focus()
    })
    return
  }
  railCollapsed.value = !railCollapsed.value
  try {
    window.localStorage.setItem(RAIL_COLLAPSED_KEY, railCollapsed.value ? '1' : '0')
  } catch { /* 仅本次会话 */ }
  if (railCollapsed.value) nextTick(() => railRestoreBtn.value?.focus())
}

// ---------------------------------------------------------------- 工作区标签栏（整条可收起）
// 标签栏固定占 38px 垂直空间；只用「概览」或只用「代码」的人，这一行常年是纯装饰。
// 收起后整行不占位（不是缩成窄条）——「零痕迹」；恢复走顶栏「更多 → 显示工作区标签栏」，
// 切换视图仍可用 Alt+1/2/3，不必先把标签栏叫回来。
const WS_BAR_COLLAPSED_KEY = 'docmind.wsBarCollapsed'
const wsBarCollapsed = ref(false)
try {
  wsBarCollapsed.value = window.localStorage.getItem(WS_BAR_COLLAPSED_KEY) === '1'
} catch { /* 存储不可用时默认展开 */ }

const moreBtn = ref<HTMLButtonElement | null>(null)

function setWsBarCollapsed(next: boolean, focus = true) {
  if (wsBarCollapsed.value === next) return
  wsBarCollapsed.value = next
  try {
    window.localStorage.setItem(WS_BAR_COLLAPSED_KEY, next ? '1' : '0')
  } catch { /* 仅本次会话 */ }
  if (!focus) return
  // 收起后按钮随整行消失，焦点转到「更多」——恢复入口就在那里面，键盘用户不会迷路。
  nextTick(() => {
    if (next) moreBtn.value?.focus()
    else (document.querySelector('.ws-bar .ws-active') as HTMLElement | null)?.focus()
  })
}

// 自主开发舱（AutonomousCockpit.vue）于 v2 重构 E 阶段移除；
// 实时对话视频流（保留项 ①）由 ChatDock scope="cockpit" 承担、窗口嵌入（保留项 ②）由 SceneRuntimePanel 承担。
// 早期遗留的 cockpitReady / cockpitRuntime / cockpitRef 三个 ref 在 2026-10-14 末轮收口清理：
// 它们的初值与赋值都已无来源（恒假/恒 null），属于舱删除后未清干净的 dead code。
/** 统一设置页（用量费用 / 网络搜索 / MCP / 智能体 / 运行与引擎）显隐 */
const settingsVisible = ref(false)
const settingsSection = ref<'usage' | 'search' | 'mcp' | 'agent' | 'model' | 'runtime'>('usage')
function openSettings(tab: 'usage' | 'search' | 'mcp' | 'agent' | 'model' | 'runtime' = 'usage') {
  settingsSection.value = tab
  // 先同步挂载异步设置组件，再切换可见状态，避免首次点击时 watcher
  // 与异步 chunk 加载交错导致弹层没有进入 DOM。
  everMounted.settings = true
  settingsVisible.value = true
}
function onOpenSettings(ev: Event) {
  const tab = (ev as CustomEvent<{ tab?: string }>).detail?.tab
  openSettings(tab === 'mcp' ? 'mcp' : tab === 'model' ? 'model' : tab === 'runtime' ? 'runtime' : 'usage')
}
/**
 * 重型弹层首次打开才挂载（异步 chunk 届时才下载）；挂载后常驻、关闭不销毁，
 * 保留已加载数据与缩放状态——与原先"始终挂载 + 根 v-if 隐藏"的体验一致。
 */
const everMounted = reactive({ relation: false, unity: false, flow: false, region: false, changes: false, jobs: false, settings: false })
watch(relationGraphOpen, (v) => { if (v) everMounted.relation = true })
watch(unityGraphOpen, (v) => { if (v) everMounted.unity = true })
watch(flowOpen, (v) => { if (v) everMounted.flow = true })
watch(changesOpen, (v) => { if (v) everMounted.changes = true })
watch(jobsOpen, (v) => { if (v) everMounted.jobs = true })
watch(regionMapOpen, (v) => { if (v) everMounted.region = true })
/** 当前标签可回滚：已纳入 git 且磁盘或编辑器存在改动 */
const canRevertActive = computed(() => {
  const t = activeTab.value
  return !!t && t.writable && t.tracked === true && (t.gitDirty === true || t.dirty)
})
const canHistoryActive = computed(() => activeTab.value?.tracked === true)

// ---------------------------------------------------------------- 顶栏收纳菜单
// 低频入口统一收进「更多」下拉：代码分析、任务与生成、GPU、AI 运行台、
// AI 运行设置、运行游戏和游戏引擎连接。面板组件自带触发按钮已隐藏，
// 仅保留 teleport 到 body 的弹层，经 ref 唤起。
const moreMenuOpen = ref(false)
// 菜单 position:fixed（逃离 .wb-topbar overflow-y:hidden 的裁剪），
// 打开时按触发按钮实时量取坐标，左缘夹取避免溢出视口。
const moreMenuStyle = ref<Record<string, string>>({})
const MENU_WIDTH = 244
function menuStyleFor(btn: HTMLElement): Record<string, string> {
  const r = btn.getBoundingClientRect()
  let left = Math.round(r.right - MENU_WIDTH) // 与按钮右缘对齐
  left = Math.max(8, Math.min(left, window.innerWidth - MENU_WIDTH - 8))
  return { top: `${Math.round(r.bottom + 6)}px`, left: `${left}px` }
}
function toggleMoreMenu(e: MouseEvent) {
  const willOpen = !moreMenuOpen.value
  if (willOpen) moreMenuStyle.value = menuStyleFor(e.currentTarget as HTMLElement)
  moreMenuOpen.value = willOpen
}
function closeTopMenus() {
  moreMenuOpen.value = false
}
const teRef = ref<InstanceType<typeof TaskEnginePanel> | null>(null)
const gpRef = ref<InstanceType<typeof GpuPanel> | null>(null)
const evRef = ref<InstanceType<typeof EdaVerifyPanel> | null>(null)
const lsRef = ref<InstanceType<typeof LiveStreamPanel> | null>(null)
// EDA 面板嵌入态：false=弹窗（默认），true=停靠主区常驻（复用 SceneRuntimePanel 的 dock 惯用法）。
const edaResident = ref(false)
const hpRef = ref<InstanceType<typeof HarnessPanel> | null>(null)
// 2026-10-19：AI 运行设置与游戏引擎连接两个入口已移进设置页的「运行与引擎」分区，
// 这里对应的 ref 与事件派发一并删除（面板改由 SettingsView 内嵌渲染）。

// ---------------------------------------------------------------- P4 项目选择器
const projects = ref<ProjectInfo[]>([])
/** 当前项目 id（与 localStorage 同步，供请求头注入与下拉高亮）。 */
const currentProjectId = ref(getProjectId())
const projectMenuOpen = ref(false)
const projectBusy = ref(false)
const projectError = ref('')
const openFormVisible = ref(false)
const newRoot = ref('')
/** 取消切换等场景的中性轻提示（非错误，自动消散）。 */
const projectNotice = ref('')
let noticeTimer: number | null = null

const currentProject = computed<ProjectInfo | null>(
  () => projects.value.find((p) => p.project_id === currentProjectId.value) ?? null,
)
/** 顶栏展示：项目名 > 根目录 basename > '未选择项目'。 */
const currentProjectLabel = computed(() => {
  if (currentProject.value?.name) return currentProject.value.name
  const root = currentProject.value?.root || tree.value?.code_root || ''
  return baseName(root) || '未选择项目'
})

function baseName(p: string): string {
  const s = (p || '').replace(/[\\/]+$/, '')
  return s.split(/[\\/]/).pop() || ''
}

/** 中性轻提示（非错误）：短暂显示后自动消散。 */
function flashNotice(msg: string) {
  projectNotice.value = msg
  if (noticeTimer !== null) window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => { projectNotice.value = ''; noticeTimer = null }, 2600)
}

/**
 * 若无未保存标签 → 直接放行（不加摩擦）；否则弹确认。
 * 返回 true = 继续（无脏标签，或用户确认放弃）；false = 用户取消，调用方必须**完全不切换**。
 */
async function confirmDiscardDirty(action: string): Promise<boolean> {
  const n = dirtyCount.value
  if (n <= 0) return true
  const names = tabs.value.filter((t) => t.dirty).map((t) => t.name)
  const shown = names.slice(0, 8)
  const more = names.length > shown.length ? `\n…等共 ${n} 个文件` : ''
  return await askConfirm({
    title: action,
    message: `${action}将关闭当前项目的 ${n} 个未保存文件，未保存的改动会丢失。继续${action}？`,
    detail: shown.join('\n') + more,
    confirmText: '放弃改动并继续',
    danger: true,
  })
}

/** 拉取项目列表（供切换后刷新；不写 current，避免与切换流程互相覆盖）。 */
async function refreshProjects() {
  const r = await projectApi.list()
  projects.value = r.projects || []
  return r
}

/** 启动时与后端 current 对齐（本地为空或与后端不一致 → 以后端为准并回写）。 */
async function initProjects() {
  projectError.value = ''
  try {
    const r = await refreshProjects()
    const backendCurrent = r.current || ''
    if (backendCurrent !== getProjectId()) {
      setProjectId(backendCurrent)
      appEvents.emit('docmind:focus-chat', { reload: true })
    }
    currentProjectId.value = backendCurrent
  } catch (e) {
    projectError.value = (e as Error).message || '加载项目列表失败'
  }
}

/** 桌面壳原生选目录（有则用；浏览器/无桥则返回空串，仅保留文本框）。 */
async function pickDirectory(): Promise<string> {
  const w = window as unknown as {
    pywebview?: { api?: { select_directory?: () => Promise<string> | string } }
  }
  const fn = w.pywebview?.api?.select_directory
  if (typeof fn !== 'function') return ''
  try {
    const r = await fn()
    return typeof r === 'string' ? r : ''
  } catch {
    return ''
  }
}
async function browseDirectory() {
  const dir = await pickDirectory()
  if (dir) newRoot.value = dir
}

/** 切换成功后的统一清理：文件树 / 编辑器标签 / 运行面板 / 对话 / 运行台。 */
async function applyProjectSwitch() {
  closeAllTabs()   // 关闭旧项目的编辑器标签
  closeRuntime()   // 关闭运行面板 → SceneRuntimePanel 自动 detach（引擎/画布归位）
  await loadTree() // 用新项目头重新拉文件树
  appEvents.emit('docmind:focus-chat', { reload: true })
  window.dispatchEvent(new CustomEvent('docmind:project-changed'))
}

async function switchProject(pid: string) {
  projectMenuOpen.value = false
  if (projectBusy.value || !pid || pid === currentProjectId.value) return
  projectError.value = ''
  // 切项目会关闭当前项目全部编辑器标签：有未保存改动时先确认；取消则**完全不切换**
  // （不调 activate、不改 getProjectId()、不清理标签、UI 选中态保持不变），只给中性轻提示。
  if (!(await confirmDiscardDirty('切换项目'))) {
    flashNotice('已取消切换项目')
    return
  }
  const prev = currentProjectId.value
  projectBusy.value = true
  try {
    setProjectId(pid)                       // 先让后续请求带上新项目头
    const r = await projectApi.activate(pid)
    if (!r.ok) throw new Error(r.error || '切换项目失败')
    currentProjectId.value = pid
    await applyProjectSwitch()
    await refreshProjects()
  } catch (e) {
    setProjectId(prev)                      // 失败回滚，绝不留在「已切其实没切」
    currentProjectId.value = prev
    projectError.value = (e as Error).message || '切换项目失败'
  } finally {
    projectBusy.value = false
  }
}

async function openProject() {
  if (projectBusy.value) return
  const root = newRoot.value.trim()
  if (!root) { projectError.value = '请填写项目目录'; return }
  projectError.value = ''
  // 「打开项目」后端会 ensure + activate，同样会关掉当前项目标签 → 走同一道确认；取消则不切换。
  if (!(await confirmDiscardDirty('打开项目'))) {
    flashNotice('已取消打开项目')
    return
  }
  const prev = currentProjectId.value
  projectBusy.value = true
  try {
    const r = await projectApi.create(root)  // 后端 ensure + activate
    if (!r.ok) throw new Error(r.error || '打开项目失败')
    const pid = r.project_id || ''
    setProjectId(pid)
    currentProjectId.value = pid
    newRoot.value = ''
    openFormVisible.value = false
    await applyProjectSwitch()
    await refreshProjects()
  } catch (e) {
    setProjectId(prev)
    currentProjectId.value = prev
    projectError.value = (e as Error).message || '打开项目失败'
  } finally {
    projectBusy.value = false
  }
}

// 下拉菜单用 position:fixed（逃离 .wb-topbar-meta 的 overflow:hidden 裁剪），
// 位置由按钮实时量取，右边缘做夹取避免溢出视口。
const projBtn = ref<HTMLElement | null>(null)
const projMenuStyle = ref<Record<string, string>>({})

function toggleProjectMenu() {
  const willOpen = !projectMenuOpen.value
  if (willOpen) {
    const r = projBtn.value?.getBoundingClientRect()
    const width = 300
    let left = r ? Math.round(r.left) : 12
    const top = r ? Math.round(r.bottom + 6) : 54
    const maxLeft = Math.max(8, window.innerWidth - width - 8)
    if (left > maxLeft) left = maxLeft
    projMenuStyle.value = { top: `${top}px`, left: `${left}px`, width: `${width}px` }
    projectError.value = ''
  }
  projectMenuOpen.value = willOpen
}
function closeProjectMenu() {
  projectMenuOpen.value = false
}

function beforeUnload(e: BeforeUnloadEvent) {
  if (dirtyCount.value > 0) e.preventDefault()
}

// Alt+1 概览 / Alt+2 代码（代码工作区需有打开的文件；输入框内不拦截）
function onWorkspaceHotkey(e: KeyboardEvent) {
  if (!e.altKey || e.getModifierState('AltGraph')) return
  const tag = (e.target as HTMLElement | null)?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement | null)?.isContentEditable) return
  if (e.key === '1') { e.preventDefault(); setWorkspace('overview') }
  else if (e.key === '2' && tabs.value.length) { e.preventDefault(); setWorkspace('code') }
  else if (e.key === '3') { e.preventDefault(); setWorkspace('assets') }
  // 注：原 '4' → setWorkspace('cockpit') 快捷键在 v2 重构 E 阶段移除（cockpit workspace 已删）
  else if ((e.key === 'p' || e.key === 'P') && !demoMode.value) { e.preventDefault(); toggleProjectMenu() }
}

/** 外观切换后告知结果：用户点了卡片会看到界面变，但不知道变成了什么。 */
const APPEARANCE_KIND: Record<string, string> = {
  skin: '配色', style: '风格', layout: '布局', material: '材质',
}
function onAppearanceChanged(e: Event) {
  const d = (e as CustomEvent).detail
  if (!d || typeof d.label !== 'string') return
  flashNotice(`${APPEARANCE_KIND[d.kind] ?? d.kind}：${d.label}`)
}

onMounted(() => {
  // 先探测同源后端：静态预览（无 FastAPI）进演示模式，不发会失败的树请求。
  void probeBackend().then((online) => {
    if (online) {
      void initProjects().then(() => loadTree())
    } else if (demoMode.value) {
      seedDemoRegionCards(demoRegionCards)
    } else {
      // A failed health probe must expose the real connection error and retry UI.
      void initProjects().then(() => loadTree())
    }
  })
  window.addEventListener('beforeunload', beforeUnload)
  window.addEventListener('keydown', onWorkspaceHotkey)
  window.addEventListener('docmind:open-settings', onOpenSettings)
  // fixed 菜单坐标基于打开瞬间的布局：视口尺寸变化后直接收起，避免错位
  window.addEventListener('resize', closeTopMenus)
  window.addEventListener('docmind:appearance-changed', onAppearanceChanged)
  offLayoutChange = onLayoutChange((k) => { layoutKey.value = k })
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', beforeUnload)
  window.removeEventListener('keydown', onWorkspaceHotkey)
  window.removeEventListener('docmind:open-settings', onOpenSettings)
  window.removeEventListener('resize', closeTopMenus)
  window.removeEventListener('docmind:appearance-changed', onAppearanceChanged)
  offLayoutChange?.()
  offLayoutChange = null
  detachRailDrag()
  if (noticeTimer !== null) { window.clearTimeout(noticeTimer); noticeTimer = null }
})
</script>

<template>
  <div class="wb-shell" @keydown.esc="mobileRailOpen && toggleRailCollapsed()">
    <header class="wb-topbar">
      <div class="wb-brand">
        <span class="wb-mark" aria-hidden="true">
          <svg width="28" height="28" viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M16 9.5 21 6h6v17h-6l-5 3.5L11 23H5V6h6l5 3.5ZM16 9.5v17M9 11h2l2 1.5M9 15h2l2 1.5M20 12.5l2-1.5h2M20 16.5l2-1.5h2" />
          </svg>
        </span>
        <!-- 2026-10-19：顶栏右侧原本有一个「问答页」文字链接，现并入品牌 ——
             品牌即产品入口（点 Agent 工作台 回问答页），顶栏右侧只留「设置 / 更多」。
             问答页那边也有一个对称的「代码工作台」入口，双向可达。 -->
        <span class="wb-brand-name">
          <a class="wb-brand-home" href="/" title="回到 AI 问答页">DocMind</a>
          <em>代码工作台</em>
        </span>
      </div>
      <div v-if="!demoMode" class="wb-topbar-meta">
        <div class="wb-proj">
          <button
            ref="projBtn"
            class="wb-proj-btn"
            :class="{ busy: projectBusy }"
            :disabled="projectBusy"
            :title="(currentProject ? currentProject.root : '选择或打开一个项目作为工作目录') + '（Alt+P 打开项目菜单）'"
            @click="toggleProjectMenu"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" class="wb-proj-icon"><path d="M1 3 Q1 2.2 1.8 2.2 H4.6 L5.6 3.2 H10.2 Q11 3.2 11 4 V9 Q11 9.8 10.2 9.8 H1.8 Q1 9.8 1 9 Z" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>
            <span class="wb-proj-name">{{ currentProjectLabel }}</span>
            <svg class="wb-proj-caret" width="9" height="9" viewBox="0 0 9 9"><path d="M1.5 3.2 L4.5 6.2 L7.5 3.2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <!-- 项目切换菜单同样 teleport 到 body（2026-10-20，与「更多」菜单同一个根因）：
               玻璃模式顶栏的 backdrop-filter 会成为 fixed 定位的包含块，
               菜单被顶栏的 overflow 裁掉、遮罩被裁成只剩顶栏高度 →
               菜单看不见，遮罩还在吃点击。与「更多」菜单一并修。 -->
          <Teleport to="body">
            <div v-if="projectMenuOpen" class="wb-proj-backdrop" @click="closeProjectMenu" />
            <div v-if="projectMenuOpen" class="wb-proj-menu" :style="projMenuStyle" @click.stop>
            <div v-if="projectError" class="wb-proj-err">{{ projectError }}</div>
            <div v-if="projects.length" class="wb-proj-list">
              <button
                v-for="p in projects"
                :key="p.project_id"
                class="wb-proj-item"
                :class="{ active: p.project_id === currentProjectId }"
                :title="p.root"
                :disabled="projectBusy"
                @click="switchProject(p.project_id)"
              >
                <span class="wb-proj-item-name">{{ p.name }}</span>
                <span v-if="p.project_id === currentProjectId" class="wb-proj-item-check">✓</span>
              </button>
            </div>
            <div v-else class="wb-proj-empty">暂无项目，先「打开项目」</div>
            <div class="wb-proj-sep" />
            <div style="font-size: var(--fs-cap);line-height:1.5;color:var(--text-faint);padding:2px 8px 6px;">快捷键 Alt+P 可随时打开本菜单</div>
            <button class="wb-proj-open-toggle" @click="openFormVisible = !openFormVisible">＋ 打开项目…</button>
            <div v-if="openFormVisible" class="wb-proj-form">
              <input
                v-model="newRoot"
                class="wb-proj-input"
                type="text"
                placeholder="项目根目录，例如 D:\MyGame"
                @keydown.enter="openProject"
              />
              <div class="wb-proj-form-actions">
                <button class="wb-proj-browse" title="从本机选择目录" @click="browseDirectory">浏览…</button>
                <button class="wb-proj-go" :disabled="projectBusy" @click="openProject">打开</button>
              </div>
            </div>
          </div>
          </Teleport>
        </div>
        <!-- 用 Teleport 挂到 body：提示条原本在顶栏内，而顶栏自建层叠上下文（z-index:20），
             子元素 z-index 再高也出不去 → 设置弹窗（z:2100）会把它完全盖住，
             用户点了卡片看不到任何反馈。Teleport 到 body 后才能真正浮在最上层。 -->
        <Teleport to="body">
          <span v-if="projectNotice" class="wb-proj-notice" role="status">{{ projectNotice }}</span>
        </Teleport>
        <!-- 分区治理入口：2026-10-19 从「带文字的 pill」降级为「图标 + 状态点」。
             顶栏是最宝贵的一行，状态文字（分区治理 · N 区 / 分区未启用）不必常驻：
             状态由那颗点表达（绿=已启用、灰=未启用），分区入口在概览页另有可达路径，
             窄屏（≤1280）本来就整颗隐藏。完整信息在 title 与分区图里。 -->
        <button
          v-if="tree"
          class="wb-region-pill wb-region-icon"
          :class="{ off: !tree.regions_enabled }"
          :title="tree.regions_enabled ? `分区治理 · ${tree.regions.length} 区（点击查看分区依赖图）` : '分区未启用（点击查看分区状态）'"
          :aria-label="tree.regions_enabled ? `分区治理，${tree.regions.length} 个分区` : '分区未启用'"
          @click="openRegionMap"
        >
          <span class="wb-region-dot" />
          <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
            <rect x="1.4" y="1.4" width="4.4" height="4.4" fill="none" stroke="currentColor" stroke-width="1.1" />
            <rect x="7.2" y="1.4" width="4.4" height="4.4" fill="none" stroke="currentColor" stroke-width="1.1" />
            <rect x="1.4" y="7.2" width="4.4" height="4.4" fill="none" stroke="currentColor" stroke-width="1.1" />
            <rect x="7.2" y="7.2" width="4.4" height="4.4" fill="none" stroke="currentColor" stroke-width="1.1" />
          </svg>
        </button>
      </div>
      <SemanticLocateBar v-if="tree || demoMode" class="wb-locate-slot" />
      <div class="wb-topbar-right">
        <!-- 顶栏右侧（2026-10-20 重做）：问答页 / 设置 / 更多 三枚控件，图标为主、文字为辅。
             「问答页」放回顶栏 —— 它是跨页主入口，藏在 logo 里可发现性太差；
             上一版并进品牌是为了让右侧只剩两个控件，现在按产品化布局把它请回来。 -->
        <a class="wb-save-btn wb-ask-link" href="/" title="回到 AI 问答页">
          <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
            <path d="M1.6 2.4 H11.4 V8.6 H5.4 L2.6 11 V8.6 H1.6 Z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round" />
          </svg>
          问答页
        </a>
        <button
          class="wb-save-btn wb-icon-btn wb-settings-btn"
          title="设置：Token 用量与费用、网络搜索、MCP 连接器、智能体预设"
          aria-label="设置"
          @click="openSettings('usage')"
        >
          <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
            <circle cx="6.5" cy="6.5" r="2.1" fill="none" stroke="currentColor" stroke-width="1.1" />
            <path d="M6.5 1.2 V2.6 M6.5 10.4 V11.8 M1.2 6.5 H2.6 M10.4 6.5 H11.8 M3 3 L4 4 M9 9 L10 10 M10 3 L9 4 M4 9 L3 10" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" />
          </svg>
        </button>
        <!-- 低频分析视图与高级工具统一收进「更多」下拉 -->
        <span class="wb-menu-wrap">
          <button
            ref="moreBtn"
            type="button"
            class="wb-save-btn wb-icon-btn wb-menu-trigger"
            :class="{ 'wb-menu-on': moreMenuOpen }"
            title="更多：代码分析、任务工具、GPU、运行台、引擎连接"
            aria-label="更多"
            @click="toggleMoreMenu($event)"
          >
            更多
            <svg class="wb-menu-caret" width="9" height="9" viewBox="0 0 9 9" aria-hidden="true"><path d="M1.5 3.2 L4.5 6.2 L7.5 3.2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <!-- 菜单 teleport 到 body（2026-10-20 修玻璃模式"点更多没反应"）。
               根因：.wb-menu 是 position:fixed，本意是逃开顶栏的 overflow-y:hidden 裁剪；
               但 position:fixed 只有在祖先**不**建立包含块时才逃得出裁剪 ——
               而玻璃模式的顶栏带 backdrop-filter，它恰恰就是那个包含块。
               于是菜单被顶栏裁掉，只剩顶部一条边：看着像开了个空白条，
               实际菜单项全被裁没了，而那层透明遮罩还在吃点击（=.wb-menu-backdrop
               也被同一机制裁成只剩顶栏高度）→ 点「更多」像完全没反应。
               Teleport 到 body 之后，菜单既不在顶栏的裁剪范围内，也不再受
               任何祖先的 backdrop-filter 影响，两种材质下行为一致。 -->
          <Teleport to="body">
            <div v-show="moreMenuOpen" class="wb-menu-backdrop" @click="moreMenuOpen = false" />
            <div v-show="moreMenuOpen" class="wb-menu" :style="moreMenuStyle" @click="moreMenuOpen = false">
            <!-- 2026-10-20 菜单改单行：原来每项是「名称 + 一行说明」两行，
                 9 个项把菜单撑得很高。产品化做法是单行「字形 + 名称」，
                 说明文字挪到 title（悬停才出现），菜单高度减半。 -->
            <div class="wb-menu-label">分析与视图</div>
            <button type="button" class="wb-menu-item" :title="wsBarCollapsed ? '恢复顶部标签栏（Alt+1/2/3 可直接切视图，无需恢复）' : '收起顶部 38px 标签栏，主区多出一行空间'" @click="setWsBarCollapsed(!wsBarCollapsed)">
              <span class="wb-menu-glyph" aria-hidden="true">{{ wsBarCollapsed ? '⌄' : '⌃' }}</span>
              <span class="wb-menu-item-name">{{ wsBarCollapsed ? '显示工作区标签栏' : '收起工作区标签栏' }}</span>
            </button>
            <button v-if="tree" type="button" class="wb-menu-item" title="代码地图：函数/变量在哪定义、被谁调用" @click="openSymbolMap">
              <span class="wb-menu-glyph" aria-hidden="true">◈</span>
              <span class="wb-menu-item-name">代码地图</span>
            </button>
            <button v-if="tree" type="button" class="wb-menu-item" title="关系图：类的继承关系、场景挂载的脚本" @click="openRelationGraph">
              <span class="wb-menu-glyph" aria-hidden="true">◐</span>
              <span class="wb-menu-item-name">关系图</span>
            </button>
            <button v-if="tree" type="button" class="wb-menu-item" title="Unity 图：场景/预制体/资源间的引用，断链标红" @click="openUnityGraph">
              <span class="wb-menu-glyph" aria-hidden="true">◇</span>
              <span class="wb-menu-item-name">Unity 图</span>
            </button>
            <button type="button" class="wb-menu-item" title="流程图：每轮问答「提问→思考→调工具→回答」" @click="openFlow">
              <span class="wb-menu-glyph" aria-hidden="true">↻</span>
              <span class="wb-menu-item-name">流程图</span>
            </button>
            <button v-if="tree" type="button" class="wb-menu-item" title="改动总览：所有未提交改动（只读，各仓库分列）" @click="openChanges">
              <span class="wb-menu-glyph" aria-hidden="true">≡</span>
              <span class="wb-menu-item-name">改动总览</span>
            </button>
            <!-- 右栏关闭后不从界面留任何窄条/图标，恢复入口只在这里 —— 与左栏收起
                 后焦点转到「更多」是同一套「零痕迹 + 可找回」思路。 -->
            <button
              type="button"
              class="wb-menu-item"
              :disabled="!rightRailAvailable"
              :title="rightRailAvailable
                ? (rightRailActive ? '关闭右侧预览栏（文件 / 改动 / 网页只读预览）' : '打开右侧预览栏：文件 / 改动 / 网页只读预览，可拖动调整宽度')
                : '预览侧栏仅在固定布局、窗口宽度 ≥1200 且无运行面板常驻时可用'"
              @click="toggleRightRail()"
            >
              <span class="wb-menu-glyph" aria-hidden="true">▭</span>
              <span class="wb-menu-item-name">{{ rightRailActive ? '关闭预览侧栏' : '预览侧栏' }}</span>
            </button>
            <div class="wb-menu-sep" />
            <div class="wb-menu-label">开发工具与运行</div>
            <button type="button" class="wb-menu-item" title="任务与生成：任务分支、Godot 控制、ComfyUI 画图、Unreal" @click="teRef?.show()">
              <span class="wb-menu-glyph" aria-hidden="true">✦</span>
              <span class="wb-menu-item-name">任务与生成</span>
            </button>
            <button type="button" class="wb-menu-item" title="GPU 监控：显存占用、任务排队、空闲自动释放" @click="gpRef?.show()">
              <span class="wb-menu-glyph" aria-hidden="true">▤</span>
              <span class="wb-menu-item-name">GPU 监控</span>
            </button>
            <button type="button" class="wb-menu-item" title="EDA 多模态验收：结构化报告 × 视觉回归 → 三态合并门（只判不回滚）" @click="evRef?.show()">
              <span class="wb-menu-glyph" aria-hidden="true">✓</span>
              <span class="wb-menu-item-name">EDA 多模态验收</span>
            </button>
            <button type="button" class="wb-menu-item" title="实时对话：摄像头或屏幕共享画面与语音实时送入本地实时模型（WS 通道）" @click="lsRef?.show()">
              <span class="wb-menu-glyph" aria-hidden="true">◉</span>
              <span class="wb-menu-item-name">实时对话</span>
            </button>
            <button type="button" class="wb-menu-item" title="运行与引擎：探测项目引擎、选择引擎与可执行文件、连接引擎" @click="openSettings('runtime')">
              <span class="wb-menu-glyph" aria-hidden="true">⚙</span>
              <span class="wb-menu-item-name">运行与引擎</span>
            </button>
            <button type="button" class="wb-menu-item" title="AI 运行台：历史会话、操作记录、技能与断点（花费在设置里看）" @click="hpRef?.show()">
              <span class="wb-menu-glyph" aria-hidden="true">◷</span>
              <span class="wb-menu-item-name">AI 运行台</span>
            </button>
            <template v-if="!runtimeResident">
              <div class="wb-menu-sep" />
              <div class="wb-menu-item wb-menu-slot-item">
                <!-- 运行游戏触发按钮由唯一 SceneRuntimePanel 实例 teleport 到此槽位；
                     docked 常驻态触发钮不渲染，整组隐藏避免空行 -->
                <span id="wb-sr-slot" />
              </div>
            </template>
            <!-- 2026-10-19 移出「更多」菜单的两项：
                 AI 运行设置（本地/云端路由、项目外文件改动手动授权）→ 设置 → 运行与引擎
                 游戏引擎连接（Godot / Unity / Unreal 连接器与 MCP 服务）→ 设置 → 运行与引擎
                 两者都是**配置类**能力，混在「分析视图」堆里既杂乱又抢注意力。 -->
            </div>
          </Teleport>
        </span>
        <button
          v-if="activeTab && canHistoryActive"
          class="wb-save-btn wb-history-btn"
          title="历史版本：查看这个文件每次提交的记录，可一键恢复到任意旧版本"
          @click="openHistory(activeTab.path, activeTab.name)"
        >
          <svg width="12" height="12" viewBox="0 0 12 12">
            <circle cx="4" cy="3" r="1.1" fill="none" stroke="currentColor" stroke-width="1" />
            <circle cx="4" cy="9" r="1.1" fill="none" stroke="currentColor" stroke-width="1" />
            <circle cx="9" cy="6" r="1.1" fill="none" stroke="currentColor" stroke-width="1" />
            <path d="M4 4.1 V7.9 M5 3.6 C7 3.6 7.6 5 8.2 5.5 M5 8.4 C7 8.4 7.6 7 8.2 6.5" fill="none" stroke="currentColor" stroke-width="0.85" />
          </svg>
          历史
        </button>
        <button
          v-if="activeTab && canRevertActive"
          class="wb-save-btn wb-revert-btn"
          title="撤销改动：丢弃本次所有未提交修改，恢复到上次提交的版本（不可恢复）"
          @click="revertPath(activeTab.path, activeTab.name)"
        >
          <svg width="12" height="12" viewBox="0 0 12 12">
            <path d="M2.2 5.4 A3.8 3.8 0 1 1 2.2 8.4 M2.2 2.8 V5.4 H4.8" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          回滚
        </button>
        <button
          v-if="activeTab"
          class="wb-save-btn"
          :disabled="activeTab.saving || !activeTab.writable"
          :title="activeTab.writable ? '保存（Ctrl+S）' : '契约文件只读，不能在工作台修改'"
          @click="saveActive"
        >
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2 1.5 H8.7 L10.5 3.3 V10.5 H2 Z M3 1.5 V4 H8 V1.5 M3 10.5 V7.5 H9 V10.5" fill="none" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/></svg>
          保存
          <kbd>Ctrl+S</kbd>
          <i v-if="dirtyCount" class="wb-save-dirty">{{ dirtyCount }}</i>
        </button>
      </div>
    </header>

    <div v-if="demoMode" class="wb-demo-banner">
      <b>示例演示</b>
      <span>你正在浏览的是离线演示版，所有数据均为示例。在本地启动 Agent 工作台 并连接你的项目后，这里会显示真实文件和真实问答。</span>
    </div>

    <div
      class="wb-body"
      :class="{
        'wb-body--rail-resize': tree && railResizable,
        'wb-body--rail-collapsed': tree && railCollapsedActive,
      }"
      :style="[railStyle, rightRailStyle]"
    >
      <aside v-if="treeError" class="wb-tree-error">
        <p class="wb-tree-error-title">文件树不可用</p>
        <p class="wb-tree-error-msg">{{ treeError.message }}</p>
        <button class="wb-retry" @click="loadTree()">重试</button>
      </aside>

      <FileTree
        v-else-if="tree"
        v-show="!runtimeResident && !railCollapsedActive"
        :tree="tree"
        :selected-path="selectedPath"
        :loading="treeLoading"
        :collapsible="mobileWorkbench || layoutKey === 'fixed'"
        @select="openNode($event); mobileRailOpen && toggleRailCollapsed()"
        @collapse="toggleRailCollapsed"
      />

      <button v-if="mobileRailOpen" class="wb-mobile-scrim" aria-label="关闭资源管理器" tabindex="-1" @click="toggleRailCollapsed" />

      <!-- 收起态窄条（仅固定布局）：整条都是按钮，点一下展开。
           FileTree 用 v-show 保持挂载，目录展开状态不会因收起而丢。 -->
      <button
        v-if="tree && railCollapsedActive"
        ref="railRestoreBtn"
        class="wb-rail-restore"
        type="button"
        title="展开资源管理器"
        aria-label="展开资源管理器"
        aria-expanded="false"
        @click="toggleRailCollapsed"
      >
        <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
          <rect x="1.4" y="1.8" width="10.2" height="9.4" rx="1" fill="none" stroke="currentColor" stroke-width="1" />
          <path d="M5.2 1.8 V11.2" stroke="currentColor" stroke-width="1" />
          <path d="M3 5 L5.2 6.5 L3 8" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <span class="wb-rail-restore-label">资源管理器</span>
      </button>

      <!-- 资源管理器宽度拖拽条（仅固定布局）：3px 视觉分隔线 + 两侧各 4px 命中区。
           双击恢复 288px；聚焦后 ←/→ 每次 16px。其他布局的空间语义不同，不出现。 -->
      <div
        v-if="tree && railResizable"
        class="ft-resizer"
        :class="{ dragging: railDragging }"
        role="separator"
        aria-orientation="vertical"
        :aria-label="`资源管理器宽度：${railWidth} 像素，拖动或按左右方向键调整`"
        :aria-valuenow="railWidth"
        aria-valuemin="220"
        aria-valuemax="640"
        tabindex="0"
        title="拖动调整资源管理器宽度（双击恢复默认）"
        @pointerdown="onRailPointerDown"
        @dblclick="resetRailWidth"
        @keydown.left.prevent="nudgeRail(-16)"
        @keydown.right.prevent="nudgeRail(16)"
      />

      <aside v-else-if="demoMode" class="wb-sidebar wb-demo-side">
        <div class="wb-demo-side-note">项目文件（示例）</div>
        <ul>
          <li class="wb-demo-dir">📁 assets</li>
          <li class="wb-demo-dir">📁 scripts
            <ul>
              <li class="wb-demo-dir">📁 player
                <ul>
                  <li>player_stats.gd<span class="wb-demo-tag">#{{ demoBadgeOf('player_stats.gd') }}</span></li>
                  <li>player_controller.gd<span class="wb-demo-tag">#{{ demoBadgeOf('player_controller.gd') }}</span></li>
                </ul>
              </li>
              <li class="wb-demo-dir">📁 combat
                <ul>
                  <li>damage_calc.gd<span class="wb-demo-tag">#{{ demoBadgeOf('damage_calc.gd') }}</span></li>
                </ul>
              </li>
              <li class="wb-demo-dir">📁 enemy
                <ul>
                  <li>enemy_ai.gd<span class="wb-demo-tag">#{{ demoBadgeOf('enemy_ai.gd') }}</span></li>
                </ul>
              </li>
              <li>inventory_system.gd<span class="wb-demo-tag">#{{ demoBadgeOf('inventory_system.gd') }}</span></li>
            </ul>
          </li>
          <li class="wb-demo-dir">📁 scenes</li>
          <li>project.godot</li>
        </ul>
        <p>本地版会在这里列出你项目的真实文件，点击文件名即可查看和编辑。</p>
      </aside>

      <!-- 主区用 v-show 常驻（而非 v-if）：保证 #wb-playpane-slot 在组件挂载期就已存在于文档中。
           否则树/演示分支异步渲染，Teleport 目标在挂载时解析为 null → dock 迁移抛错、常驻面板落空。 -->
      <main class="wb-main" v-show="tree || demoMode" :inert="mobileRailOpen">
        <WorkspaceTabs v-show="!wsBarCollapsed" @collapse="setWsBarCollapsed(true)" />
        <!-- 阶段 4：运行面板常驻（docked）承载槽（主区，左侧文件树/示例保留，便于边玩边改） -->
        <div id="wb-playpane-slot" class="wb-playpane-slot" v-show="runtimeResident" />
        <!-- EDA 多模态验收面板常驻（docked）承载槽：EdaVerifyPanel 的 docked 主体 teleport 到此，
             在主区内联填充（左栏文件树保留）。 -->
        <div id="wb-eda-slot" class="wb-eda-slot" v-show="edaResident" />
        <!-- v2 重构（E 阶段）：#wb-cockpit-runtime-slot 与 #wb-cockpit-chat-slot 在 AutonomousCockpit 移除后
             不再有 v-show=true 的窗口（workspace === 'cockpit' 状态已删），两个 slot 整行删除。
             窗口嵌入（保留项 ②）由 #wb-playpane-slot + SceneRuntimePanel 承担，实时流（保留项 ①）
             由 #wb-chat-slot + ChatDock scope="cockpit" 承担。 -->
        <!-- 页面切换只隐藏编辑区，不卸载 ChatDock。否则 ChatDock 的卸载钩子会
             把正在进行的 SSE 当成用户主动停止，导致切到运行面板时回答被中断。 -->
        <!-- 自主开发舱（v2 重构 E 阶段）：AutonomousCockpit 组件已删除；主区不再挂载该组件 -->
        <div class="wb-editor-shell" :style="{ display: (runtimeResident || edaResident) ? 'none' : 'contents' }">
          <AssetCenterView v-if="workspace === 'assets'" />
          <template v-else-if="tree">
            <EditorTabs v-if="workspace === 'code'" />
            <div class="wb-editor-row">
              <CodeView :tab="workspace === 'code' ? activeTab : null" />
              <SelectionAiPanel v-if="aiPanelOpen && workspace === 'code'" />
              <SymbolOutline v-if="workspace === 'code'" />
            </div>
            <footer class="wb-statusbar">
              <span v-if="selectedPath" class="wb-status-path">{{ selectedPath }}</span>
              <span v-else class="wb-status-faint">未选择文件</span>
              <span v-if="dirtyCount" class="wb-status-dirty">● {{ dirtyCount}} 个文件未保存</span>
              <span class="wb-status-spacer" />
              <span v-if="tree.regions_enabled" class="wb-status-legend">
                <i v-for="r in tree.regions" :key="r.key">
                  <b :style="{ background: regionColor(r.key) }" />{{ r.name }}
                </i>
              </span>
            </footer>
          </template>
          <template v-else-if="demoMode">
            <EditorTabs />
            <div class="wb-editor-row">
              <CodeView :tab="null" />
            </div>
            <footer class="wb-statusbar">
              <span class="wb-status-faint">示例演示模式 · 未连接本地项目</span>
            </footer>
          </template>
        </div>

        <!-- AI 助手：底部抽屉（2026-10-20 改回底部，但**做矮**）。
             中途试过"右侧常驻栏"（三栏 IDE），实测下来不行：420px 的栏里，
             回答区只剩 389px 宽、每行约 26 个字（中文舒适区是 45–75 字/行），
             长回答被挤成电报体；而且 Agent 工作台 的核心动作是"问"，
             把主功能塞进角上、让代码占主位，主次是反的。
             现在回到底部，但高度从 62vh 降到 38vh：
               · 回答区横跨全宽，阅读栏 680px、每行约 48 字；
               · 主区拿到 1312×706（原来 892×948 的右栏形态下只有 892 宽）；
               · 视线自上而下：项目 → 代码 → 对话。 -->
        <div id="wb-chat-slot" class="wb-chat-host" v-show="!runtimeResident && !edaResident" />
      </main>

      <!-- 右侧预览栏（第三栏）左边缘拖拽条：3px 视觉线 + 两侧各 4px 命中区。
           拖动改宽、双击复位 460px、聚焦后 ←/→ 每次 16px（左=变宽，因为拖的是右栏左缘）。
           只在 fixed + 视口 ≥1200 且右栏打开时出现；关闭后与整栏一起消失，不占位。 -->
      <div
        v-if="rightRailActive"
        class="rr-resizer"
        :class="{ dragging: rightRailDragging }"
        role="separator"
        aria-orientation="vertical"
        :aria-label="`预览侧栏宽度：${rightRailWidth} 像素，拖动或按左右方向键调整`"
        :aria-valuenow="rightRailWidth"
        :aria-valuemin="RIGHT_RAIL_MIN_W"
        :aria-valuemax="RIGHT_RAIL_MAX_W"
        tabindex="0"
        title="拖动调整预览侧栏宽度（双击恢复默认）"
        @pointerdown="onRightRailPointerDown"
        @dblclick="resetRightRailWidth"
        @keydown.left.prevent="nudgeRightRail(16)"
        @keydown.right.prevent="nudgeRightRail(-16)"
      />
      <!-- v-if 而非 v-show：关闭后整栏从 DOM 移除（零痕迹），
           栏内 iframe 也随之中止 —— 不留一个看不见却还在加载/播放的页面。 -->
      <RightRail v-if="rightRailActive" />

      <div v-if="!treeError && !tree && !demoMode" class="wb-booting">
        <div class="cv-spinner" />
        <p>正在连接本地 Agent 工作台 服务…</p>
      </div>
    </div>

    <!-- 唯一实例：popup（默认，固定弹层，行为不变）/ docked（常驻主区）由 runtimeResident 切换。
         docked 主体 teleport 进主区 #wb-playpane-slot；同实例仅切 mode，不重建，
         故 iframe 与引擎嵌入状态得以保留。触发按钮由该实例 teleport 回顶栏 #wb-sr-slot。 -->
    <Teleport defer to="#wb-chat-slot"><ChatDock scope="default" /></Teleport>
    <SceneRuntimePanel :mode="runtimeResident ? 'docked' : 'popup'"
      host="#wb-playpane-slot" />

    <!-- 高级工具面板宿主：自带触发按钮隐藏（入口已收进顶栏「工具」菜单），
         弹层 teleport 到 body，经模板 ref 调 show() 唤起。 -->
    <div class="wb-tool-host" aria-hidden="true">
      <TaskEnginePanel ref="teRef" :key="currentProjectId" />
      <GpuPanel ref="gpRef" />
      <EdaVerifyPanel ref="evRef" :mode="edaResident ? 'docked' : 'popup'" host="#wb-eda-slot"
        @dock="edaResident = true" @undock="edaResident = false" />
      <HarnessPanel ref="hpRef" />
      <LiveStreamPanel ref="lsRef" />
      <!-- 右栏打开时隐藏右侧浮窗（两者都在右边，会重叠）；右栏「只读镜像」页签
           接管实况画面，关闭右栏后这个浮窗自动回来。 -->
      <BrowserLivePanel v-if="!rightRailActive" />
    </div>

    <ContextMenu />
    <AppDialog />
    <GitHistoryDialog />
    <RewriteDiffDialog />
    <ExecApprovalDialog />
    <RegionMapDialog v-if="everMounted.region" />
    <SymbolMap />
    <RelationGraph v-if="everMounted.relation" />
    <UnityGraph v-if="everMounted.unity" />
    <FlowCanvas v-if="everMounted.flow" />
    <GitChangesPanel v-if="everMounted.changes" />
    <BackgroundJobsPanel v-if="everMounted.jobs" />
    <SelectionToolbar />
    <SettingsView v-if="everMounted.settings" :visible="settingsVisible" :initial-tab="settingsSection" @close="settingsVisible = false" />
  </div>
</template>
