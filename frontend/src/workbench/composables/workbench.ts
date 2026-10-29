// 工作台共享状态（模块级单例）：文件树、多标签编辑、保存/冲突、新建/改名/删除流程、
// 对话框与右键菜单。组件只负责渲染与转发事件。
import { computed, ref, shallowRef } from 'vue'
// 仅类型需要；运行时调用（scrollIntoView）在 jumpToLine 内动态 import，避免拖大首屏
import type { EditorView } from '@codemirror/view'
import { fsApi, FsApiError, getProjectId, getActiveTask } from '../api'
import type {
  TreeNode, TreeResp,
} from '../api'
// 对话框域（确认/输入/警告/保存冲突）已独立成 dialogs.ts，这里仅做再导出聚合
export { askConfirm, askPrompt, askAlert, resolveDialog } from './dialogs'
export type { DialogSpec } from './dialogs'
import { dialog, askConflict, askConfirm, askPrompt, askAlert, resolveDialog } from './dialogs'
import { initSemanticDeps, useSemantic } from './workbench-semantic'
import { initAiDeps, useAiSelection } from './workbench-ai'
import { initRegionDeps, useRegionMap } from './workbench-regions'
import { initMenuDeps, useContextMenu } from './workbench-menu'
import { initGitDeps, useGitHistory } from './workbench-git'
import { initRuntimeDeps, useRuntime } from './workbench-runtime'
import { initRecentDeps, useRecentFiles } from './workbench-recent'

// ---------------------------------------------------------------- 标签页
export type { EditorTab } from './workbench-types'
import type { EditorTab } from './workbench-types'

let tabSeq = 1

/**
 * 契约文件只读名单（与后端 workbench_fs 写删保护名单保持一致）。
 * 后端 tree 仍返 writable:true，由前端在打开时兜底锁定；保存接口另有 403 兜底。
 */
const CONTRACT_READONLY = new Set([
  'regions.json',
  'DOCMIND_RULES.md',
  'DEV_INDEX.md',
  'dev_changesets.jsonl',
])

// ---------------------------------------------------------------- 右键菜单
export type { MenuItem, MenuPos } from './workbench-menu'

// ---------------------------------------------------------------- 状态
const tree = shallowRef<TreeResp | null>(null)
const treeLoading = ref(false)
const treeError = ref<FsApiError | null>(null)

const tabs = ref<EditorTab[]>([])
const activeId = ref<number | null>(null)
const selectedPath = ref<string | null>(null)

/** 点击 AI 答案引用时，文件树展开祖先目录并闪烁目标行（nonce 保证同一路径重复触发也有动画）。 */
const treeReveal = ref<{ path: string; nonce: number } | null>(null)

function nodeExists(path: string): boolean {
  return !!(tree.value && findNode(tree.value.nodes, path))
}

/** AI 答案引用：选中目标并请求文件树展开/闪烁（节点可能尚不存在，由树侧自行判空）。 */
function revealPath(path: string) {
  selectedPath.value = path
  treeReveal.value = { path, nonce: Date.now() }
}

// 语义标签 / 大白话定位 / 概览分区卡 抽到 workbench-semantic.ts（依赖在文件末尾注入）
const {
  tagMap, tagLoading, tagError, tagMeta, loadTags, refreshTags,
  locateQuery, locateLoading, locatePaths, locateResult,
  runLocate, clearLocate, openLocateFile, openLocateRegion,
  regionCards, regionCardsLoading, loadRegionCards, seedDemoRegionCards,
} = useSemantic()

// 工作区视图 + 画布/运行面板抽到 workbench-runtime.ts（tabs 在文件末尾注入）
export type { WorkspaceView, RuntimePanelTab, RightRailTab, RightRailBrowserMode } from './workbench-runtime'
const {
  workspace, setWorkspace,
  runtimeOpen, runtimeTab, runtimeResident,
  openRuntime, openRuntimeResident, closeRuntimeResident, closeRuntime,
  symbolMapOpen, openSymbolMap, closeSymbolMap,
  relationGraphOpen, openRelationGraph, closeRelationGraph,
  unityGraphOpen, openUnityGraph, closeUnityGraph,
  flowOpen, openFlow, closeFlow,
  changesOpen, openChanges, closeChanges,
  jobsOpen, openJobs, closeJobs,
  rightRailOpen, rightRailTab, rightRailBrowser, rightRailFile, rightRailDiff,
  openRightRailBrowser, openRightRailFile, openRightRailDiff,
  toggleRightRail, closeRightRail,
} = useRuntime()

// 最近打开的文件抽到 workbench-recent.ts（tree/openPath 在文件末尾注入）
const { recentFiles, pushRecent, openRecent } = useRecentFiles()

/** tab.id -> 取当前编辑器文本（由 CodeView 注册，保存时取最新内容） */
const contentGetters = new Map<number, () => string>()

// Page navigation must not discard editor drafts. Store per tab and project;
// restored drafts retain their original mtime so save conflict checks still work.
let restoredRoot = ''
function persistWorkspace() {
  if (!tree.value || restoredRoot !== tree.value.code_root) return
  try {
    sessionStorage.setItem('docmind.workspace:' + restoredRoot, JSON.stringify({
      tabs: tabs.value.filter(t => !t.loading && !t.error).map(t => ({ ...t,
        draftContent: contentGetters.get(t.id)?.() ?? t.draftContent ?? t.savedContent,
      })), activeId: activeId.value, selectedPath: selectedPath.value, workspace: workspace.value,
    }))
  } catch { /* storage unavailable: beforeunload still protects dirty files */ }
}
async function restoreWorkspace() {
  if (!tree.value || restoredRoot === tree.value.code_root) return
  restoredRoot = tree.value.code_root
  const project = getProjectId(), root = restoredRoot
  try {
    const data = JSON.parse(sessionStorage.getItem('docmind.workspace:' + restoredRoot) || 'null')
    if (!data || !Array.isArray(data.tabs) || tabs.value.length) return
    tabs.value = data.tabs.filter((t: EditorTab) => typeof t.path === 'string' && typeof t.savedContent === 'string')
      .map((t: EditorTab) => ({ ...t, saving: false, loading: false }))
    tabSeq = Math.max(0, ...tabs.value.map(t => t.id)) + 1
    activeId.value = data.activeId
    selectedPath.value = data.selectedPath
    if (['overview', 'code', 'assets'].includes(data.workspace)) workspace.value = data.workspace
    for (const tab of tabs.value) {
      if (tab.dirty) continue // Preserve the original base for conflict detection.
      try {
        const before = tab.draftContent ?? tab.savedContent
        const file = await fsApi.read(tab.path)
        if (project !== getProjectId() || root !== restoredRoot) return
        if (!tabs.value.includes(tab) || tab.dirty || (tab.draftContent ?? tab.savedContent) !== before) continue
        tab.savedContent = file.content
        tab.draftContent = file.content
        tab.mtime = file.mtime
        tab.writable = tab.writable && file.writable
        tab.previewKind = file.preview_kind ?? null
        tab.previewNote = file.preview_note ?? null
        docReplacers.get(tab.id)?.(file.content)
      } catch (e) {
        if (project === getProjectId() && root === restoredRoot && tabs.value.includes(tab) && !tab.dirty) tab.error = (e as Error).message
      }
    }
  } catch { /* damaged session data must not block opening a project */ }
}
window.addEventListener('pagehide', persistWorkspace)
window.addEventListener('beforeunload', persistWorkspace)

/**
 * tab.id -> 整文档替换器（由 CodeView 注册）。
 * P3 git 回滚/历史恢复后，磁盘内容在编辑器之外被改写：活动标签走 view.dispatch，
 * 非活动标签要直接替换其留存的 EditorState，否则切回去还是旧文本。
 */
const docReplacers = new Map<number, (content: string) => void>()

export function registerDocReplacer(id: number, fn: (content: string) => void) {
  docReplacers.set(id, fn)
}

const activeTab = computed<EditorTab | null>(
  () => tabs.value.find((t) => t.id === activeId.value) ?? null,
)

/**
 * 当前代码库是否处于 git 管理下：树中任一文件节点带非空 tracked 即为是。
 * 目录节点后端不返 tracked；本机未装 git 或项目未 init 时整树皆 null。
 * 用于删除文案：无 git 上下文时不使用「未跟踪/未纳入 git」这类指责式措辞。
 */
const gitActive = computed(() => {
  if (!tree.value) return false
  for (const n of walkNodes(tree.value.nodes)) {
    if (n.type === 'file' && n.tracked !== undefined && n.tracked !== null) return true
  }
  return false
})

// ---------------------------------------------------------------- 树辅助
function* walkNodes(nodes: TreeNode[]): Generator<TreeNode> {
  for (const n of nodes) {
    yield n
    if (n.children?.length) yield* walkNodes(n.children)
  }
}

function findNode(nodes: TreeNode[], path: string): TreeNode | null {
  for (const n of nodes) {
    if (n.path === path) return n
    if (n.children?.length) {
      const hit = findNode(n.children, path)
      if (hit) return hit
    }
  }
  return null
}

function parentPath(p: string): string | null {
  const i = p.lastIndexOf('/')
  return i > 0 ? p.slice(0, i) : null
}

async function loadTree(selectPath?: string | null) {
  treeLoading.value = true
  treeError.value = null
  try {
    tree.value = await fsApi.tree()
    await restoreWorkspace()
    syncTabGitStates()
    if (selectPath !== undefined) selectedPath.value = selectPath
    void loadTags()  // 业务标签是增强层，静默加载（失败不弹错）
    void loadRegionCards()  // 概览驾驶舱分区卡，同样静默
  } catch (e) {
    tree.value = null
    treeError.value = e as FsApiError
  } finally {
    treeLoading.value = false
  }
}

function syncTabGitStates() {
  if (!tree.value) return
  for (const tab of tabs.value) {
    const node = findNode(tree.value.nodes, tab.path)
    if (node) {
      tab.tracked = node.tracked ?? null
      tab.gitDirty = node.dirty ?? null
    }
  }
}

// ---------------------------------------------------------------- 打开 / 切换 / 关闭
async function openPath(path: string, preferWritable?: boolean) {
  pushRecent(path)
  workspace.value = 'code'  // 打开文件即进入代码工作区
  const existing = tabs.value.find((t) => t.path === path)
  if (existing) {
    activeId.value = existing.id
    selectedPath.value = path
    return
  }
  const node = tree.value ? findNode(tree.value.nodes, path) : null
  const contractLocked = CONTRACT_READONLY.has(path.split('/').pop() || path)
  const tab: EditorTab = {
    id: tabSeq++,
    path,
    name: path.split('/').pop() || path,
    lang: node?.lang ?? 'text',
    region: node?.region ?? null,
    regionName: node?.region_name ?? null,
    mtime: 0,
    writable: preferWritable ?? node?.writable ?? true,
    savedContent: '',
    dirty: false,
    loading: true,
    error: null,
    errorStatus: null,
    saving: false,
    savedAt: null,
    tracked: node?.tracked ?? null,
    gitDirty: node?.dirty ?? null,
    reindexWarn: null,
  }
  tabs.value = [...tabs.value, tab]
  // 必须取回 reactive 代理：异步回来后在 raw 对象上写属性不会触发渲染更新
  const live = tabs.value[tabs.value.length - 1]
  activeId.value = tab.id
  selectedPath.value = path
  try {
    const f = await fsApi.read(path)
    live.lang = f.lang
    live.region = f.region
    live.regionName = f.region_name
    live.mtime = f.mtime
    live.tracked = f.tracked
    live.gitDirty = f.dirty
    live.previewKind = f.preview_kind ?? null
    live.previewNote = f.preview_note ?? null
    live.previewUrl = f.preview_url ?? null
    live.writable = (node?.writable ?? true) && f.writable && !contractLocked
    live.savedContent = f.content
    live.loading = false
  } catch (e) {
    const err = e as FsApiError
    live.loading = false
    live.error = err.message
    live.errorStatus = err.status
  }
}

function openNode(node: TreeNode) {
  selectedPath.value = node.path
  if (node.type === 'file') void openPath(node.path, node.writable)
}

/** 打开文件（必要时等待异步加载与 CM 挂载）并把光标定位/滚动到指定行。 */
async function jumpToLine(path: string, line: number): Promise<void> {
  await openPath(path)
  // 此时编辑器块必已随首个标签加载；动态取值避免本模块静态引入 CodeMirror（会把
  // 650KB vendor-codemirror 拖成工作台入口的静态依赖，概览首屏白背）。
  const { EditorView: EV } = await import('@codemirror/view')
  const target = Math.max(1, line | 0)
  await new Promise<void>((resolve) => {
    const t0 = Date.now()
    const tick = () => {
      const view = (window as unknown as { __docmind_cm?: EditorView }).__docmind_cm
      const tab = activeTab.value
      if (view && tab && tab.path === path && !tab.loading && !tab.error) {
        // view 单例：确认它已切换到该标签的 doc（新标签内容加载完成后才 setState）
        const ready = tab.savedContent !== '' || view.state.doc.lines > 0
        if (ready && view.state.doc.lines >= target - 1) {
          const ln = Math.min(target, view.state.doc.lines)
          const pos = view.state.doc.line(ln).from
          view.focus()
          view.dispatch({
            selection: { anchor: pos },
            effects: EV.scrollIntoView(pos, { y: 'center' }),
          })
          resolve()
          return
        }
      }
      if (Date.now() - t0 > 5000) {
        resolve()
        return
      }
      setTimeout(tick, 50)
    }
    tick()
  })
}

function activateTab(id: number) {
  activeId.value = id
  workspace.value = 'code'  // 点编辑器标签即回到代码工作区
  const tab = tabs.value.find((t) => t.id === id)
  if (tab) selectedPath.value = tab.path
}

async function closeTab(id: number) {
  const idx = tabs.value.findIndex((t) => t.id === id)
  if (idx < 0) return
  const tab = tabs.value[idx]
  if (tab.dirty) {
    const ok = await askConfirm({
      title: '放弃未保存的改动？',
      message: `「${tab.name}」有尚未保存的修改，关闭后将丢失。`,
      confirmText: '放弃改动并关闭',
      danger: true,
    })
    if (!ok) return
  }
  contentGetters.delete(id)
  docReplacers.delete(id)
  const next = tabs.value.filter((t) => t.id !== id)
  tabs.value = next
  if (activeId.value === id) {
    const neighbor = next[Math.min(idx, next.length - 1)] ?? null
    activeId.value = neighbor ? neighbor.id : null
    selectedPath.value = neighbor ? neighbor.path : null
    if (!neighbor) workspace.value = 'overview'  // 最后一个标签关闭 → 回到概览
  }
}

export function registerContentGetter(id: number, fn: () => string) {
  contentGetters.set(id, fn)
}

/**
 * 切换项目时调用：关闭全部旧项目的编辑器标签，清空选中并回到概览。
 * 静默丢弃未保存改动——项目切换是显式动作，前端已在切换前给出「会关闭当前打开的文件」提示语义；
 * 这里不再逐文件弹确认，避免卡在一堆弹窗里无法完成切换。
 */
function closeAllTabs() {
  persistWorkspace()
  restoredRoot = ''
  for (const t of tabs.value) {
    contentGetters.delete(t.id)
    docReplacers.delete(t.id)
  }
  tabs.value = []
  activeId.value = null
  selectedPath.value = null
  workspace.value = 'overview'
}

// ---------------------------------------------------------------- 保存
async function saveTab(id: number, overwrite = false): Promise<boolean> {
  const tab = tabs.value.find((t) => t.id === id)
  if (!tab || !tab.writable || tab.saving) return false
  const content = contentGetters.get(id)?.() ?? tab.draftContent ?? tab.savedContent
  tab.saving = true
  tab.reindexWarn = null
  try {
    const taskId = getActiveTask().id
    const resp = await fsApi.save(tab.path, content, overwrite ? null : tab.mtime, true, taskId)
    tab.mtime = resp.mtime
    tab.savedContent = content
    tab.dirty = false
    tab.savedAt = Date.now()
    tab.reindexWarn = resp.reindex_warnings?.[0] ?? null
    void loadTree(tab.path)
    return true
  } catch (e) {
    const err = e as FsApiError
    if (err.status === 409 && typeof err.extra.server_mtime === 'number') {
      const doOverwrite = await askConflict(tab.path, err.extra.server_mtime as number)
      if (doOverwrite) {
        // 注意：外层 finally 尚未执行，saving 仍为 true，递归前必须先释放重入锁
        tab.saving = false
        return saveTab(id, true)
      }
      return false
    }
    await askAlert({
      title: err.status === 422 ? '语法校验未通过，写入已取消' : '保存失败',
      message: err.message,
      detail: err.status ? `HTTP ${err.status}` : undefined,
    })
    return false
  } finally {
    tab.saving = false
  }
}

function saveActive() {
  if (activeId.value != null) return saveTab(activeId.value)
  return Promise.resolve(false)
}

// ---------------------------------------------------------------- 新建
const NAME_BAD = /[\\/:*?"<>|]/

async function createAt(parent: TreeNode | null, kind: 'file' | 'folder') {
  const under = parent ? parent.path : '根目录'
  const name = await askPrompt({
    title: kind === 'file' ? '新建文件' : '新建文件夹',
    message: `位置：${under}`,
    defaultValue: kind === 'file' ? '新文件.gd' : '新文件夹',
    placeholder: '名称（可含 / 创建多级）',
  })
  if (name == null) return
  const trimmed = name.trim().replace(/^\/+|\/+$/g, '')
  if (!trimmed) return
  // 新建允许含 / 创建多级目录；单段内仍禁止 Windows 非法字符与 .. 段
  const segs = trimmed.split('/')
  const SEG_BAD = /[\\:*?"<>|]/
  if (segs.some((s) => !s || s === '..' || SEG_BAD.test(s))) {
    await askAlert({ title: '名称不合法', message: '不能包含 \\ : * ? " < > |、空路径段或 .. 段。' })
    return
  }
  const rel = parent ? `${parent.path}/${trimmed}` : trimmed
  try {
    const resp = await fsApi.create(rel, kind)
    await loadTree(resp.path)
    if (kind === 'file') await openPath(resp.path)
  } catch (e) {
    const err = e as FsApiError
    await askAlert({ title: '创建失败', message: err.message, detail: err.status ? `HTTP ${err.status}` : undefined })
  }
}

// ---------------------------------------------------------------- 改名
async function renameNode(node: TreeNode) {
  const dir = parentPath(node.path)
  const name = await askPrompt({
    title: node.type === 'dir' ? '重命名文件夹' : '重命名文件',
    message: dir ? `位置：${dir}` : '位置：根目录',
    defaultValue: node.name,
  })
  if (name == null) return
  const trimmed = name.trim()
  if (!trimmed || trimmed === node.name) return
  if (NAME_BAD.test(trimmed) || trimmed.includes('..')) {
    await askAlert({ title: '名称不合法', message: '不能包含 \\ / : * ? " < > | 或 .. 段。' })
    return
  }
  const newPath = dir ? `${dir}/${trimmed}` : trimmed
  try {
    await fsApi.rename(node.path, newPath)
    const openTab = tabs.value.find((t) => t.path === node.path)
    if (openTab) {
      openTab.path = newPath
      openTab.name = trimmed
    }
    await loadTree(newPath)
    if (activeId.value && openTab) selectedPath.value = newPath
  } catch (e) {
    const err = e as FsApiError
    await askAlert({
      title: '重命名失败',
      message: err.message,
      detail: err.status === 403 ? '分区契约保护：跨分区移动需走 dev_refactor 审批流。' : err.status ? `HTTP ${err.status}` : undefined,
    })
  }
}

// ---------------------------------------------------------------- 删除（多级确认）
async function deleteNode(node: TreeNode) {
  const isDir = node.type === 'dir'
  // 目录节点不返 tracked，用树级信号判断是否处于 git 项目
  const inGit = isDir ? gitActive.value : node.tracked !== null && node.tracked !== undefined
  let recursive = false
  let force = false
  // 第一轮预确认（让 tracked 文件/普通目录也有一次明确确认）
  let detail: string | undefined
  if (isDir) {
    detail = inGit
      ? '已纳入 git 的内容可由 git 恢复；未跟踪文件删除后不可恢复。'
      : '该项目未启用 git 版本管理（未安装 git 或未初始化仓库），删除后不可恢复。'
  } else if (node.tracked === false) {
    detail = '该文件未纳入 git，删除后不可恢复。'
  } else if (node.tracked) {
    detail = '已纳入 git，内容可由 git 恢复。'
  } else {
    detail = '该项目未启用 git 版本管理（未安装 git 或未初始化仓库），删除后不可恢复。'
  }
  const preOk = await askConfirm({
    title: isDir ? `删除文件夹「${node.name}」？` : `删除文件「${node.name}」？`,
    message: isDir ? '文件夹将被删除。' : '文件将被删除。',
    detail,
    confirmText: '删除',
    danger: true,
  })
  if (!preOk) return

  for (let guard = 0; guard < 3; guard++) {
    try {
      await fsApi.delete(node.path, recursive, force)
      // 关闭已打开的对应 tab（含目录下的子 tab）
      const prefix = node.path + '/'
      const dead = tabs.value.filter((t) => t.path === node.path || (isDir && t.path.startsWith(prefix)))
      for (const t of dead) {
        contentGetters.delete(t.id)
        if (activeId.value === t.id) {
          activeId.value = null
          selectedPath.value = null
        }
      }
      if (dead.length) tabs.value = tabs.value.filter((t) => !dead.includes(t))
      if (selectedPath.value === node.path || (isDir && selectedPath.value?.startsWith(prefix))) {
        selectedPath.value = null
      }
      await loadTree(selectedPath.value)
      return
    } catch (e) {
      const err = e as FsApiError
      if (err.status !== 409) {
        await askAlert({
          title: '删除失败',
          message: err.message,
          detail: err.status === 403 ? '契约文件/受保护目录禁止在工作台删除。' : err.status ? `HTTP ${err.status}` : undefined,
        })
        return
      }
      const extra = err.extra as Record<string, unknown>
      // 未跟踪文件/目录 → force
      const untrackedCount = Number(extra.untracked_count ?? 0)
      if (untrackedCount > 0 || (!isDir && !extra.count)) {
        const n = untrackedCount || 1
        const ok = await askConfirm(inGit
          ? {
              title: '包含未纳入 git 的内容，删除不可恢复',
              message: isDir
                ? `文件夹中有 ${n} 个未跟踪文件，删除后无法通过 git 恢复。`
                : '该文件未纳入 git，删除后不可恢复。',
              detail: typeof extra.untracked_sample === 'object'
                ? (extra.untracked_sample as string[]).slice(0, 6).join('\n')
                : undefined,
              confirmText: '仍要永久删除',
              danger: true,
            }
          : {
              // 无 git 环境/非 git 项目：中性措辞，只陈述不可逆事实
              title: '删除后不可恢复',
              message: isDir
                ? `文件夹中有 ${n} 个文件，当前项目未启用 git 版本管理，删除后无法恢复。`
                : '当前项目未启用 git 版本管理，文件删除后无法恢复。',
              detail: typeof extra.untracked_sample === 'object'
                ? (extra.untracked_sample as string[]).slice(0, 6).join('\n')
                : undefined,
              confirmText: '仍要永久删除',
              danger: true,
            })
        if (!ok) return
        force = true
        continue
      }
      // 非空目录 → recursive
      const count = Number(extra.count ?? 0)
      if (count > 0) {
        const ok = await askConfirm({
          title: '文件夹非空',
          message: `「${node.name}」内有 ${count} 个项目，将一并删除。`,
          detail: (extra.sample as string[] | undefined)?.slice(0, 6).join('\n'),
          confirmText: '递归删除全部',
          danger: true,
        })
        if (!ok) return
        recursive = true
        continue
      }
      await askAlert({ title: '删除失败', message: err.message })
      return
    }
  }
}

// 右键菜单抽到 workbench-menu.ts（文件操作回调在文件末尾注入）
const { ctxMenu, openNodeMenu, openRootMenu, closeContextMenu } = useContextMenu()

// git 回滚 + 历史版本抽到 workbench-git.ts（tabs/docReplacers/loadTree 在文件末尾注入）
export type { HistoryState } from './workbench-git'
const {
  history, revertPath, openHistory, closeHistory,
  selectHistoryVersion, restoreSelectedVersion,
} = useGitHistory()

// ================================================================ P2：选区 AI
// 选区 AI 面板 / 改写 diff 抽到 workbench-ai.ts（activeTab/contentGetters 在文件末尾注入）
export type { AiAction, AiSelection, AiOrigin, AiTraceItem, AiTurn, RewriteDiffState } from './workbench-ai'
const {
  activeSelection, setSelection,
  aiPanelOpen, openAiPanel, closeAiPanel, clearTurns,
  turns, aiStreaming, askComposing, startAskCompose,
  runAi, stopAi, applyRewrite, copyAnswer,
  rewriteDiff, openRewriteDiff, closeRewriteDiff, acceptRewriteDiff,
} = useAiSelection()


// 分区可视化面板 / 创建分区 / 补齐导出桩 抽到 workbench-regions.ts
export type { RegionMapState } from './workbench-regions'
const {
  regionMapOpen, regionMap, openRegionMap, closeRegionMap, locateRegion,
  busyRegionKey, createRegion, fillRegionExports,
} = useRegionMap()

// 语义域依赖树/跳转能力（函数声明已提升，此处注入一次）
initSemanticDeps({ tree, revealPath, jumpToLine })
// 选区 AI 域依赖当前标签与编辑器内容取值器
initAiDeps({ activeTab, contentGetters })
// 分区域依赖文件树选中态与刷新
initRegionDeps({ selectedPath, loadTree })
// 右键菜单依赖文件操作（函数声明已提升）
initMenuDeps({ createAt, renameNode, deleteNode, openNode, revertPath, openHistory })
// git 域依赖标签列表、编辑器内容替换器与文件树刷新
initGitDeps({ tabs, docReplacers, loadTree })
// 外壳域依赖标签列表；最近文件依赖树与打开文件
initRuntimeDeps({ tabs })
initRecentDeps({ tree, openPath })

export function useWorkbench() {
  return {
    // state
    tree, treeLoading, treeError, gitActive,
    tabs, activeId, activeTab, selectedPath,
    dialog, ctxMenu,
    // 工作区视图（概览驾驶舱 / 代码）+ 分区卡 + 最近文件
    workspace, setWorkspace,
    regionCards, regionCardsLoading, loadRegionCards, seedDemoRegionCards,
    recentFiles, openRecent,
    // tree
    loadTree, openNode, openPath,
    // AI 引用定位：文件树展开 + 闪烁
    treeReveal, nodeExists, revealPath,
    // P1 符号地图 / 行跳转
    jumpToLine, symbolMapOpen, openSymbolMap, closeSymbolMap,
    relationGraphOpen, openRelationGraph, closeRelationGraph,
    // P1-2 Unity GUID 引用图
    unityGraphOpen, openUnityGraph, closeUnityGraph,
    // 阶段3 AI 工作流画布（trace 回看）
    flowOpen, openFlow, closeFlow,
    // tabs
    activateTab, closeTab, saveTab, saveActive, registerContentGetter, registerDocReplacer,
    closeAllTabs,
    // P3 git 回滚 / 历史版本
    revertPath, history, openHistory, closeHistory, selectHistoryVersion, restoreSelectedVersion,
    // P4 改动总览（只读）
    changesOpen, openChanges, closeChanges,
    // 后台任务进度/日志抽屉
    jobsOpen, openJobs, closeJobs,
    // 右侧预览栏（第三栏）：文件 / 改动 / 网页
    rightRailOpen, rightRailTab, rightRailBrowser, rightRailFile, rightRailDiff,
    openRightRailBrowser, openRightRailFile, openRightRailDiff,
    toggleRightRail, closeRightRail,
    // fs ops
    createAt, renameNode, deleteNode,
    // menus / dialogs
    openNodeMenu, openRootMenu, closeContextMenu, resolveDialog,
    // P2 选区 AI
    activeSelection, setSelection,
    aiPanelOpen, openAiPanel, closeAiPanel, clearTurns,
    turns, aiStreaming, askComposing, startAskCompose,
    runAi, stopAi, applyRewrite, copyAnswer,
    // P3 改写 diff 预览
    rewriteDiff, openRewriteDiff, closeRewriteDiff, acceptRewriteDiff,
    // P3 分区可视化
    regionMapOpen, regionMap, openRegionMap, closeRegionMap, locateRegion,
    busyRegionKey, createRegion, fillRegionExports,
    // 阶段 1 语义标签 + 大白话定位
    tagMap, tagLoading, tagError, tagMeta,
    loadTags, refreshTags,
    locateQuery, locateLoading, locatePaths, locateResult,
    runLocate, clearLocate, openLocateFile, openLocateRegion,
    // 顶层「画布 / 运行」tab：联动 SceneRuntimePanel
    runtimeOpen, runtimeTab, openRuntime, closeRuntime,
    // 阶段 4：常驻态（docked）开关
    runtimeResident, openRuntimeResident, closeRuntimeResident,
  }
}
