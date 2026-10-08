<script setup lang="ts">
// DocMind 开发工作台 · P0 任务 4：多标签编辑 + 保存 + 新建/改名/删除。
import { onMounted, onBeforeUnmount, computed, defineAsyncComponent, nextTick, reactive, ref, watch, Teleport } from 'vue'
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
import AgentPolicyPanel from './components/AgentPolicyPanel.vue'
import GpuPanel from './components/GpuPanel.vue'
import HarnessPanel from './components/HarnessPanel.vue'
import SemanticLocateBar from './components/SemanticLocateBar.vue'
import WorkspaceTabs from './components/WorkspaceTabs.vue'
import { useWorkbench, askConfirm } from './composables/workbench'
import { probeBackend, demoMode, demoTagMap, demoRegionCards } from './composables/demo'
import { regionColor } from './theme'
import { projectApi, getProjectId, setProjectId, type ProjectInfo } from './api'
import { appEvents } from './eventBus'

// 重型弹层/视图按需加载：仅在首次打开时拉取独立 chunk（首屏不下载、不解析）
const RelationGraph = defineAsyncComponent(() => import('./components/RelationGraph.vue'))
const UnityGraph = defineAsyncComponent(() => import('./components/UnityGraph.vue'))
const FlowCanvas = defineAsyncComponent(() => import('./components/FlowCanvas.vue'))
const RegionMapDialog = defineAsyncComponent(() => import('./components/RegionMapDialog.vue'))
const SettingsView = defineAsyncComponent(() => import('./components/SettingsView.vue'))
const AssetCenterView = defineAsyncComponent(() => import('./components/AssetCenterView.vue'))
// 自主开发舱：重型工作流面板独立分包，切到该工作区才加载
const AutonomousCockpit = defineAsyncComponent(() => import('./components/AutonomousCockpit.vue'))

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
  relationGraphOpen, unityGraphOpen, flowOpen, regionMapOpen, changesOpen,
  aiPanelOpen,
  workspace, setWorkspace, seedDemoRegionCards,
  closeAllTabs, closeRuntime,
  // 阶段 4：运行面板常驻（docked）态
  runtimeResident,
} = useWorkbench()

const dirtyCount = computed(() => tabs.value.filter((t) => t.dirty).length)
const cockpitReady = ref(false)
const cockpitRuntime = ref(false)
const cockpitFiles = ref(false)
const cockpitRef = ref<{ showOutputs: () => void } | null>(null)
/** 统一设置页（用量费用 / 网络搜索 / MCP / 智能体）显隐 */
const settingsVisible = ref(false)
const settingsSection = ref<'usage' | 'search' | 'mcp' | 'agent' | 'model'>('usage')
function openSettings(tab: 'usage' | 'search' | 'mcp' | 'agent' | 'model' = 'usage') {
  settingsSection.value = tab
  // 先同步挂载异步设置组件，再切换可见状态，避免首次点击时 watcher
  // 与异步 chunk 加载交错导致弹层没有进入 DOM。
  everMounted.settings = true
  settingsVisible.value = true
}
function onOpenSettings(ev: Event) {
  const tab = (ev as CustomEvent<{ tab?: string }>).detail?.tab
  openSettings(tab === 'mcp' ? 'mcp' : tab === 'model' ? 'model' : 'usage')
}
/**
 * 重型弹层首次打开才挂载（异步 chunk 届时才下载）；挂载后常驻、关闭不销毁，
 * 保留已加载数据与缩放状态——与原先"始终挂载 + 根 v-if 隐藏"的体验一致。
 */
const everMounted = reactive({ relation: false, unity: false, flow: false, region: false, changes: false, settings: false, cockpit: false })
watch(workspace, v => {
  if (v !== 'cockpit') return
  everMounted.cockpit = true
  // 复位开发舱内部滚动：输入框 focus 的浏览器默认 scrollIntoView 会把整页
  // .acp 标题顶出视口、目标区/预览区也会停在中段，切回开发舱时统一回到顶部
  nextTick(() => {
    document.querySelector<HTMLElement>('.acp')?.scrollTo({ top: 0 })
    document.querySelector<HTMLElement>('.acp-stage-content')?.scrollTo({ top: 0 })
    document.querySelector<HTMLElement>('.acp-controls')?.scrollTo({ top: 0 })
  })
}, { immediate: true })
watch(relationGraphOpen, (v) => { if (v) everMounted.relation = true })
watch(unityGraphOpen, (v) => { if (v) everMounted.unity = true })
watch(flowOpen, (v) => { if (v) everMounted.flow = true })
watch(changesOpen, (v) => { if (v) everMounted.changes = true })
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
const hpRef = ref<InstanceType<typeof HarnessPanel> | null>(null)
const apRef = ref<InstanceType<typeof AgentPolicyPanel> | null>(null)
function openEngineConnect() {
  appEvents.emit('docmind:open-engine')
}

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
  else   if (e.key === '4') { e.preventDefault(); setWorkspace('cockpit') }
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
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', beforeUnload)
  window.removeEventListener('keydown', onWorkspaceHotkey)
  window.removeEventListener('docmind:open-settings', onOpenSettings)
  window.removeEventListener('resize', closeTopMenus)
  window.removeEventListener('docmind:appearance-changed', onAppearanceChanged)
  if (noticeTimer !== null) { window.clearTimeout(noticeTimer); noticeTimer = null }
})
</script>

<template>
  <div class="wb-shell">
    <header class="wb-topbar">
      <div class="wb-brand">
        <span class="wb-mark" aria-hidden="true">
          <svg width="15" height="15" viewBox="0 0 15 15">
            <rect x="1.5" y="2.5" width="9" height="10" rx="1.5" fill="none" stroke="#2f6fed" stroke-width="1.3" />
            <path d="M11 5.5 H12.5 L14 7 V12.5 H11 Z" fill="rgba(47,111,237,.13)" stroke="#2f6fed" stroke-width="1.3" stroke-linejoin="round" />
          </svg>
        </span>
        <span class="wb-brand-name">DocMind <em>代码工作台</em></span>
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
            <div style="font-size:10px;line-height:1.5;color:var(--text-faint);padding:2px 8px 6px;">快捷键 Alt+P 可随时打开本菜单</div>
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
        </div>
        <!-- 用 Teleport 挂到 body：提示条原本在顶栏内，而顶栏自建层叠上下文（z-index:20），
             子元素 z-index 再高也出不去 → 设置弹窗（z:2100）会把它完全盖住，
             用户点了卡片看不到任何反馈。Teleport 到 body 后才能真正浮在最上层。 -->
        <Teleport to="body">
          <span v-if="projectNotice" class="wb-proj-notice" role="status">{{ projectNotice }}</span>
        </Teleport>
        <button
          v-if="tree"
          class="wb-region-pill"
          :class="{ off: !tree.regions_enabled }"
          :title="tree.regions_enabled ? '查看分区依赖图与各区状态' : '查看分区状态'"
          @click="openRegionMap"
        >
          <span class="wb-region-dot" />
          {{ tree.regions_enabled ? `分区治理 · ${tree.regions.length} 区` : '分区未启用' }}
        </button>
      </div>
      <SemanticLocateBar v-if="tree || demoMode" class="wb-locate-slot" />
      <div class="wb-topbar-right">
        <a class="wb-question-link" href="/" title="返回 RAG 问答页">问答页</a>
        <button class="wb-question-link" type="button" title="展开底部 AI 对话并开始提问" @click="appEvents.emit('docmind:focus-chat', {})">AI 对话</button>
        <button
          class="wb-save-btn wb-settings-btn"
          title="设置：Token 用量与费用、网络搜索、MCP 连接器、智能体预设"
          @click="openSettings('usage')"
        >
          <svg width="13" height="13" viewBox="0 0 13 13">
            <circle cx="6.5" cy="6.5" r="2.1" fill="none" stroke="currentColor" stroke-width="1.1" />
            <path d="M6.5 1.2 V2.6 M6.5 10.4 V11.8 M1.2 6.5 H2.6 M10.4 6.5 H11.8 M3 3 L4 4 M9 9 L10 10 M10 3 L9 4 M4 9 L3 10" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" />
          </svg>
          设置
        </button>
        <!-- 低频分析视图与高级工具统一收进「更多」下拉 -->
        <span class="wb-menu-wrap">
          <button
            type="button"
            class="wb-save-btn wb-menu-trigger"
            :class="{ 'wb-menu-on': moreMenuOpen }"
            title="更多：代码分析、任务工具、GPU、运行台、引擎连接"
            @click="toggleMoreMenu($event)"
          >
            更多
            <svg class="wb-menu-caret" width="9" height="9" viewBox="0 0 9 9" aria-hidden="true"><path d="M1.5 3.2 L4.5 6.2 L7.5 3.2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <div v-show="moreMenuOpen" class="wb-menu-backdrop" @click="moreMenuOpen = false" />
          <div v-show="moreMenuOpen" class="wb-menu" :style="moreMenuStyle" @click="moreMenuOpen = false">
            <div class="wb-menu-label">代码分析</div>
            <button v-if="tree" type="button" class="wb-menu-item" @click="openSymbolMap">
              <span class="wb-menu-item-name">代码地图</span>
              <small>函数/变量在哪定义、被谁调用</small>
            </button>
            <button v-if="tree" type="button" class="wb-menu-item" @click="openRelationGraph">
              <span class="wb-menu-item-name">关系图</span>
              <small>类的继承关系、场景挂载的脚本</small>
            </button>
            <button v-if="tree" type="button" class="wb-menu-item" @click="openUnityGraph">
              <span class="wb-menu-item-name">Unity 图</span>
              <small>场景/预制体/资源间的引用，断链标红</small>
            </button>
            <button type="button" class="wb-menu-item" @click="openFlow">
              <span class="wb-menu-item-name">流程图</span>
              <small>每轮问答「提问→思考→调工具→回答」</small>
            </button>
            <button v-if="tree" type="button" class="wb-menu-item" @click="openChanges">
              <span class="wb-menu-item-name">改动总览</span>
              <small>所有未提交改动（只读，各仓库分列）</small>
            </button>
            <div class="wb-menu-sep" />
            <div class="wb-menu-label">工具与运行</div>
            <button type="button" class="wb-menu-item" @click="teRef?.show()">
              <span class="wb-menu-item-name">任务与生成</span>
              <small>任务分支、Godot 控制、ComfyUI 画图、Unreal</small>
            </button>
            <button type="button" class="wb-menu-item" @click="gpRef?.show()">
              <span class="wb-menu-item-name">GPU 监控</span>
              <small>显存占用、任务排队、空闲自动释放</small>
            </button>
            <button type="button" class="wb-menu-item" @click="hpRef?.show()">
              <span class="wb-menu-item-name">AI 运行台</span>
              <small>花费、历史会话、操作记录、技能与断点</small>
            </button>
            <button type="button" class="wb-menu-item" @click="apRef?.show()">
              <span class="wb-menu-item-name">AI 运行设置</span>
              <small>本地/云端路由、项目外文件改动手动授权</small>
            </button>
            <template v-if="!runtimeResident">
              <div class="wb-menu-sep" />
              <div class="wb-menu-item wb-menu-slot-item">
                <!-- 运行游戏触发按钮由唯一 SceneRuntimePanel 实例 teleport 到此槽位；
                     docked 常驻态触发钮不渲染，整组隐藏避免空行 -->
                <span id="wb-sr-slot" />
              </div>
            </template>
            <button type="button" class="wb-menu-item" @click="openEngineConnect">
              <span class="wb-menu-item-name">游戏引擎连接</span>
              <small>Godot / Unity / Unreal 连接器与 MCP 服务</small>
            </button>
          </div>
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
      <span>你正在浏览的是离线演示版，所有数据均为示例。在本地启动 DocMind 并连接你的项目后，这里会显示真实文件和真实问答。</span>
    </div>

    <div class="wb-body">
      <aside v-if="treeError" class="wb-tree-error">
        <p class="wb-tree-error-title">文件树不可用</p>
        <p class="wb-tree-error-msg">{{ treeError.message }}</p>
        <button class="wb-retry" @click="loadTree()">重试</button>
      </aside>

      <FileTree
        v-else-if="tree"
        v-show="workspace !== 'cockpit' || cockpitFiles"
        :tree="tree"
        :selected-path="selectedPath"
        :loading="treeLoading"
        @select="openNode"
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
      <main class="wb-main" v-show="tree || demoMode">
        <WorkspaceTabs />
        <!-- 阶段 4：运行面板常驻（docked）承载槽（主区，左侧文件树/示例保留，便于边玩边改） -->
        <div id="wb-playpane-slot" class="wb-playpane-slot" v-show="runtimeResident" />
        <!-- 页面切换只隐藏编辑区，不卸载 ChatDock。否则 ChatDock 的卸载钩子会
             把正在进行的 SSE 当成用户主动停止，导致切到运行面板时回答被中断。 -->
        <!-- 自主开发舱：独立工作区，描述目标后由模型规划/执行/验证，用户审核计划与结果 -->
        <AutonomousCockpit ref="cockpitRef" v-if="everMounted.cockpit" v-show="workspace === 'cockpit' && !runtimeResident"
          @ready="cockpitReady = true" @runtime-preview="cockpitRuntime = $event" @toggle-files="cockpitFiles = !cockpitFiles" />
        <div v-if="workspace !== 'cockpit'" class="wb-editor-shell" :style="{ display: runtimeResident ? 'none' : 'contents' }">
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
        <div id="wb-chat-slot" class="wb-chat-host" v-show="workspace !== 'cockpit' && !runtimeResident" />
      </main>

      <div v-if="!treeError && !tree && !demoMode" class="wb-booting">
        <div class="cv-spinner" />
        <p>正在连接本地 DocMind 服务…</p>
      </div>
    </div>

    <!-- 唯一实例：popup（默认，固定弹层，行为不变）/ docked（常驻主区）由 runtimeResident 切换。
         docked 主体 teleport 进主区 #wb-playpane-slot；同实例仅切 mode，不重建，
         故 iframe 与引擎嵌入状态得以保留。触发按钮由该实例 teleport 回顶栏 #wb-sr-slot。 -->
    <Teleport defer to="#wb-chat-slot"><ChatDock scope="default" /></Teleport>
    <Teleport v-if="cockpitReady" defer to="#wb-cockpit-chat-slot"><ChatDock scope="cockpit" /></Teleport>
    <SceneRuntimePanel :mode="runtimeResident || (workspace === 'cockpit' && cockpitReady && cockpitRuntime) ? 'docked' : 'popup'"
      :host="workspace === 'cockpit' && cockpitReady && cockpitRuntime ? '#wb-cockpit-runtime-slot' : '#wb-playpane-slot'"
      @close="cockpitRuntime = false; cockpitRef?.showOutputs()" />

    <!-- 高级工具面板宿主：自带触发按钮隐藏（入口已收进顶栏「工具」菜单），
         弹层 teleport 到 body，经模板 ref 调 show() 唤起。 -->
    <div class="wb-tool-host" aria-hidden="true">
      <TaskEnginePanel ref="teRef" :key="currentProjectId" />
      <GpuPanel ref="gpRef" />
      <HarnessPanel ref="hpRef" />
      <AgentPolicyPanel ref="apRef" />
    </div>

    <ContextMenu />
    <AppDialog />
    <GitHistoryDialog />
    <RewriteDiffDialog />
    <RegionMapDialog v-if="everMounted.region" />
    <SymbolMap />
    <RelationGraph v-if="everMounted.relation" />
    <UnityGraph v-if="everMounted.unity" />
    <FlowCanvas v-if="everMounted.flow" />
    <GitChangesPanel v-if="everMounted.changes" />
    <SelectionToolbar />
    <SettingsView v-if="everMounted.settings" :visible="settingsVisible" :initial-tab="settingsSection" @close="settingsVisible = false" />
  </div>
</template>
