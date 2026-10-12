<script setup lang="ts">
// P0-2 场景画布：把 Godot .tscn 画成可操作的节点图。
//
// 设计取舍（与原 spike 的「容器嵌套」方案不同，这里说明理由）：
//   * spike 验证的是 Vue Flow 的 group 容器能不能承载"分区+文件"。那个模型到了场景树上
//     会退化成"父节点=容器"，三层以上嵌套时尺寸传播 / extent 钳制 / fitView 都变得脆弱；
//   * 场景开发真正要看的是**空间关系**：position / transform 才是高频操作对象，容器嵌套
//     表达不了这个。所以这里改成 **扁平节点 + 三类边**，并提供两种布局：
//       「层级布局」= DFS 前序的水平树；「空间布局」= 直接按场景坐标落点（可拖拽回写位置）。
//   * 实例（instance）与脚本引用（ExtResource）另外画成"文件卡"，边用不同颜色区分，
//     双击文件卡即在编辑器里打开该文件——场景图和代码因此连成一条线。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { cat } from '../theme'
import {
  VueFlow, applyNodeChanges, applyEdgeChanges, MarkerType,
} from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import type { Node, Edge, NodeChange, EdgeChange, VueFlowStore } from '@vue-flow/core'
// Vue Flow 的样式不带在包里，必须显式引（漏了不会报错，只会让 .vue-flow__node
// 退化成 position:static —— 画布看起来"有东西"但节点其实堆成一列）。
// 不引 node-resizer（本项目没用到它的缩放功能）。
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'
import { sceneApi } from '../api'
import type { SceneFile, SceneGraph, SceneNode, SceneUndo } from '../api'
import SceneNodeCard from './SceneNodeCard.vue'
import SceneFileCard from './SceneFileCard.vue'

const props = defineProps<{ path: string }>()
const emit = defineEmits<{ (e: 'open-file', rel: string): void }>()

const COLORS = { '2d': '#58a6ff', '3d': '#bc8cff', root: '#e3a83a', instance: '#2ec4b6' }
const EDGE = {
  hierarchy: '#5b6675',
  script: '#d2a8ff',
  reference: '#5b6675',
  instance: '#2ec4b6',
}

const graph = ref<SceneGraph | null>(null)
const loading = ref(false)
const message = ref('')
const messageKind = ref<'info' | 'ok' | 'err'>('info')
const busy = ref(false)

const nodes = shallowRef<(Node & { selected?: boolean })[]>([])
const edges = shallowRef<Edge[]>([])
const selectedId = ref<string | null>(null)
const selectedIds = ref<string[]>([])
const layoutMode = ref<'hierarchy' | 'space'>('hierarchy')
const showScripts = ref(true)
const showInstances = ref(true)
// 资源引用（贴图 / 材质 / 字体…）默认不画：真实工程里动辄上百个 ext_resource，
// 全铺到画布上会把场景层级淹没。需要排查"这张图被谁引用"时再打开。
const showResources = ref(false)

const undoStack = ref<SceneUndo[]>([])
const redoStack = ref<SceneUndo[]>([])
const spaceScale = ref(1)
/** 已为哪个场景路径固定过空间缩放；同路径内编辑不再重算，避免一拖全图塌缩 */
const scaleFixedFor = ref<string>('')
/** 刚被本次操作改动过的节点 id -> 高亮态，用于给用户"改到哪了"的即时反馈 */
const flash = ref<Record<string, string>>({})

// —— 交互增强状态 ——
const searchQuery = ref('')                       // 节点/类型搜索关键字
const collapsedIds = ref<Set<string>>(new Set())  // 已折叠（隐藏子树）的节点 id
const hoverId = ref<string | null>(null)          // 悬停高亮源（选中优先）
// 关系高亮用的邻接表（reload 时构建）
const parentOf = ref<Map<string, string>>(new Map())
const childMap = ref<Map<string, Set<string>>>(new Map())
const fileIdSet = ref<Set<string>>(new Set())
// Vue Flow 实例（聚焦选中 / 导出用）
const vf = shallowRef<VueFlowStore | null>(null)
const canvasEl = ref<HTMLElement | null>(null)
let canvasResizeObserver: ResizeObserver | null = null
let fitFrame = 0

const scene = computed(() => graph.value)
const nodeById = computed(() => new Map((graph.value?.nodes ?? []).map(n => [n.id, n])))
const selected = computed(() => (selectedId.value ? nodeById.value.get(selectedId.value) ?? null : null))
const canEdit = computed(() => !!graph.value?.guard.writable)

function say(text: string, kind: 'info' | 'ok' | 'err' = 'info') {
  message.value = text
  messageKind.value = kind
}

const ROW = 84
const COL = 296
const FILE_COL = 330
// 卡片近似尺寸（比 CSS 实测略大一圈），空间布局用它做矩形防重叠检测
const CARD_W = 256
const CARD_H = 72
const FILE_W = 270
const FILE_H = 62
const CARD_GAP = 14

interface Box { x: number; y: number; w: number; h: number }
function boxesOverlap(a: Box, b: Box): boolean {
  return a.x < b.x + b.w + CARD_GAP && a.x + a.w + CARD_GAP > b.x
    && a.y < b.y + b.h + CARD_GAP && a.y + a.h + CARD_GAP > b.y
}
/** 在期望落点附近螺旋搜索最近的不重叠位置（优先向下找，符合树图阅读习惯） */
function nearestFree(x: number, y: number, w: number, h: number, blockers: Box[]): { x: number; y: number } {
  const snap = (v: number) => Math.round(v / 8) * 8
  const sx = snap(x), sy = snap(y)
  const hit = (cx: number, cy: number) => blockers.some(b => boxesOverlap({ x: cx, y: cy, w, h }, b))
  if (!hit(sx, sy)) return { x: sx, y: sy }
  const step = 24
  for (let ring = 1; ring < 120; ring++) {
    const d = ring * step
    const tries: Array<[number, number]> = []
    for (let k = -ring; k <= ring; k++) {
      tries.push([sx + k * step, sy + d], [sx + k * step, sy - d], [sx - d, sy + k * step], [sx + d, sy + k * step])
    }
    tries.sort((p, q) => Math.hypot(p[0] - sx, p[1] - sy) - Math.hypot(q[0] - sx, q[1] - sy))
    for (const [cx, cy] of tries) {
      const fx = snap(cx), fy = snap(cy)
      if (!hit(fx, fy)) return { x: fx, y: fy }
    }
  }
  return { x: sx, y: sy }
}

/* ------------------------------------------------------------------ 布局 */
function buildOrder(list: SceneNode[]): { node: SceneNode; depth: number }[] {
  const byParent = new Map<string, SceneNode[]>()
  const ids = new Set(list.map(n => n.id))
  for (const node of list) {
    const key = node.parent && ids.has(node.parent) ? node.parent : '__orphan__'
    if (!byParent.has(key)) byParent.set(key, [])
    byParent.get(key)!.push(node)
  }
  const out: { node: SceneNode; depth: number }[] = []
  const seen = new Set<string>()
  const walk = (id: string, depth: number) => {
    const node = id === '__orphan__' ? null : list.find(n => n.id === id)
    if (node) {
      if (seen.has(node.id)) return
      seen.add(node.id)
      out.push({ node, depth })
    }
    // 折叠的节点：自身仍画出，但不再向下递归其子树（仅层级布局下生效；空间布局保留坐标不隐藏）
    if (node && collapsedIds.value.has(node.id) && layoutMode.value === 'hierarchy') return
    for (const child of byParent.get(id) ?? []) walk(child.id, node ? depth + 1 : depth)
  }
  walk(graph.value?.root_id ?? '.', 0)
  walk('__orphan__', 1)
  for (const node of list) if (!seen.has(node.id)) out.push({ node, depth: 1 })
  return out
}

// 空间缩放只在场景首次加载时算一次（见 reload 中按 path 固定），编辑后保持稳定，
// 避免「拖一个节点→reload 重算全局缩放→所有节点朝原点塌缩聚拢」的 bug。
function computeSpaceScale(nodes: Array<{ position?: number[] | null }> | undefined) {
  const pts = (nodes || []).filter(n => n.position)
  const span = pts.reduce((acc, n) => Math.max(acc, Math.abs(n.position![0]), Math.abs(n.position![1])), 0)
  spaceScale.value = span > 2400 ? 2400 / span : 1
}

function layout(): { nodes: Node[]; edges: Edge[]; maxX: number; minY: number } {
  const g = graph.value
  if (!g) return { nodes: [], edges: [], maxX: 0, minY: 0 }
  const ordered = buildOrder(g.nodes)

  // 哪些节点有子节点（决定折叠按钮显隐）
  const kids = new Set<string>()
  for (const n of g.nodes) if (n.parent) kids.add(n.parent)

  // 使用已固定的空间缩放（不在每次 reload 重算），保证拖拽后节点坐标稳定不塌缩
  const scale = spaceScale.value

  const placed = new Map<string, { x: number; y: number }>()
  const overflow = new Map<string, number>()
  const out: Node[] = []
  let row = 0
  for (const { node, depth } of ordered) {
    let pos: { x: number; y: number }
    if (layoutMode.value === 'hierarchy') {
      pos = { x: depth * COL, y: row * ROW }
      row += 1
    } else if (node.position) {
      pos = { x: node.position[0] * scale, y: node.position[1] * scale }
    } else {
      const parent = node.parent ? placed.get(node.parent) : undefined
      const count = (overflow.get(node.parent ?? '') ?? 0) + 1
      overflow.set(node.parent ?? '', count)
      pos = parent ? { x: parent.x, y: parent.y + count * 66 } : { x: 0, y: (row + count) * ROW }
    }
    placed.set(node.id, pos)
    const isRoot = node.id === g.root_id
    const rawColor = isRoot ? COLORS.root : node.instance_id ? COLORS.instance : COLORS[node.space]
    // 空间分色（2D 蓝 / 3D 紫 / 根 金 / 实例 青）也是照深色底调的，出口派生一次
    const color = cat(rawColor)
    out.push({
      id: node.id,
      type: 'sceneNode',
      position: pos,
      draggable: layoutMode.value === 'space' && !isRoot,
      data: {
        node, color,
        flash: flash.value[node.id] ?? '',
        hasChildren: kids.has(node.id),
        collapsed: collapsedIds.value.has(node.id),
        collapsible: layoutMode.value === 'hierarchy',
        onToggle: toggleCollapse,
        onHover: setHover,
      },
    })
  }

  // 文件卡单独排一列，避免和场景节点抢空间
  const maxX = ordered.reduce((acc, { depth }) => Math.max(acc, depth * COL), 0)
  const visibleFiles = g.files.filter(f => !f.orphan && (
    (f.kind === 'script' && showScripts.value)
    || (f.kind === 'scene' && showInstances.value)
    || (f.kind === 'resource' && showResources.value)))
  visibleFiles.forEach((file, index) => {
    out.push({
      id: file.id,
      type: 'sceneFile',
      position: layoutMode.value === 'space'
        ? { x: (placed.size ? Math.max(...[...placed.values()].map(p => p.x)) : 0) + FILE_COL, y: index * 88 }
        : { x: maxX + FILE_COL, y: index * 88 },
      draggable: true,
      data: {
        file,
        color: file.kind === 'scene' ? EDGE.instance : file.kind === 'script' ? EDGE.script : EDGE.hierarchy,
        id: file.id,
        onHover: setHover,
      },
    })
  })

  // 空间布局：真实坐标里多个节点常常重合（都没填 position 就是 0,0），统一做矩形避让。
  // 只改画布展示坐标，不回写场景文件。
  if (layoutMode.value === 'space') {
    const placed: Box[] = []
    for (const vn of out) {
      const isFile = vn.type === 'sceneFile'
      const w = isFile ? FILE_W : CARD_W
      const h = isFile ? FILE_H : CARD_H
      const free = nearestFree(vn.position.x, vn.position.y, w, h, placed)
      vn.position = free
      placed.push({ x: free.x, y: free.y, w, h })
    }
  }

  const fileIds = new Set(visibleFiles.map(f => f.id))
  const edgeList: Edge[] = []
  for (const e of g.edges) {
    if (e.kind !== 'hierarchy' && !fileIds.has(e.target)) continue
    const color = EDGE[e.kind]
    edgeList.push({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.kind === 'hierarchy' ? 'children' : 'ref',
      targetHandle: e.kind === 'hierarchy' ? 'parent' : 'in',
      type: 'smoothstep',
      animated: e.kind !== 'hierarchy',
      style: {
        stroke: color,
        strokeWidth: e.kind === 'hierarchy' ? 1.4 : 1.2,
        strokeDasharray: e.kind === 'hierarchy' ? undefined : e.kind === 'script' ? '5 4' : '2 3',
      },
      markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12, color },
    })
  }
  return { nodes: out, edges: edgeList, maxX, minY: 0 }
}

function rebuild() {
  const { nodes: list, edges: list2 } = layout()
  nodes.value = list
  edges.value = list2
  if (selectedId.value && !nodeById.value.has(selectedId.value)) selectedId.value = null
  decorate()
}

/**
 * Vue Flow 的 fit-view-on-init 只在组件首次挂载时执行一次，而场景图是异步加载的：
 * 组件挂载时 nodes 还是空数组，数据回来后视口不会自动重新适配，真实项目就会出现
 * "接口已加载但画布一片空白"。等 Vue 完成两轮 DOM 更新并确认容器有尺寸后再适配，
 * 同时覆盖弹窗/常驻面板切换时父容器从 0 尺寸恢复的情况。
 */
function scheduleFitView() {
  if (fitFrame) window.cancelAnimationFrame(fitFrame)
  void nextTick(async () => {
    await nextTick()
    fitFrame = window.requestAnimationFrame(() => {
      fitFrame = 0
      const store = vf.value
      const el = canvasEl.value
      if (!store || !el || !nodes.value.length || el.clientWidth < 2 || el.clientHeight < 2) return
      try { store.fitView({ padding: 0.18, duration: 180 }) } catch { /* 节点尚未完成测量时由 ResizeObserver 重试 */ }
    })
  })
}

function onNodesChange(changes: NodeChange[]) {
  if (!vf.value) return
  nodes.value = applyNodeChanges(changes, vf.value.nodes.value)
  if (changes.some(c => c.type === 'select' || c.type === 'remove')) {
    selectedIds.value = nodes.value.filter(n => n.selected).map(n => n.id)
    const last = selectedIds.value[selectedIds.value.length - 1]
    if (last) selectedId.value = last
  }
}
function onEdgesChange(changes: EdgeChange[]) {
  if (!vf.value) return
  edges.value = applyEdgeChanges(changes, vf.value.edges.value)
}
/**
 * Vue Flow v1 的事件载荷是**单个对象** `{ event, node, nodes, ... }`，不是 `(event, node)`。
 * 这一点不看源码很容易写错：写成 `(_e, n) => n.id` 会在点节点时抛
 * "Cannot read properties of undefined"，而且只在点击时才炸（浏览器冒烟抓到的）。
 * 这里两种形态都兜住，顺带对未来 minor 变更免疫。
 */
function pickNode(args: unknown[]): Node | undefined {
  const looksLikeNode = (value: unknown): value is Node =>
    !!value && typeof value === 'object' && 'id' in value && 'position' in value
  if (looksLikeNode(args[1])) return args[1]
  const first = args[0] as { node?: unknown } | null | undefined
  return looksLikeNode(first?.node) ? (first!.node as Node) : undefined
}

function onNodeClick(...args: unknown[]) {
  const node = pickNode(args)
  if (!node || node.type === 'sceneFile') return
  selectedId.value = node.id
}

/* ------------------------------------------------------------------ 加载 */
async function reload() {
  if (!props.path.trim()) return
  loading.value = true
  try {
    const g = await sceneApi.graph(props.path.trim())
    if (!g.ok) {
      graph.value = null
      nodes.value = []
      edges.value = []
      say(g.error || '加载场景失败', 'err')
      return
    }
    graph.value = g
    // 关系高亮邻接表：parentOf / childMap / fileIdSet
    const pMap = new Map<string, string>()
    const cMap = new Map<string, Set<string>>()
    const fSet = new Set<string>()
    for (const n of g.nodes) {
      if (n.parent) {
        pMap.set(n.id, n.parent)
        if (!cMap.has(n.parent)) cMap.set(n.parent, new Set())
        cMap.get(n.parent)!.add(n.id)
      }
    }
    for (const f of g.files) fSet.add(f.id)
    parentOf.value = pMap
    childMap.value = cMap
    fileIdSet.value = fSet
    if (scaleFixedFor.value !== props.path.trim()) {
      computeSpaceScale(g.nodes)
      scaleFixedFor.value = props.path.trim()
    }
    rebuild()
    scheduleFitView()
    const warn = g.structure_errors?.length
      ? `（结构异常 ${g.structure_errors.length} 项，编辑已锁定）`
      : ''
    say(`已加载 ${g.stats.nodes} 节点 / ${g.stats.files} 外部引用 / ${g.stats.edges} 边${warn}`,
      g.structure_errors?.length ? 'err' : 'ok')
  } catch (e) {
    graph.value = null
    nodes.value = []
    edges.value = []
    say((e as Error).message, 'err')
  } finally {
    loading.value = false
  }
}

/* ------------------------------------------------------------------ 编辑 */
// undo 与 /api/scene/op 请求体同形，直接展开即可——不要在这里做字段映射，
// 一旦两边字段名漂移，撤销会静默变成空 payload。
async function runOp(payload: Record<string, unknown>, options: { remember?: boolean } = {}): Promise<boolean> {
  if (busy.value) return false
  const g = graph.value
  if (!g) return false
  if (!canEdit.value) {
    say(`该场景不可写入：${g.guard.reason || '只读'}`, 'err')
    return false
  }
  busy.value = true
  try {
    const r = await sceneApi.op({ path: g.path, if_mtime: g.stats.mtime, ...payload })
    if (!r.ok) {
      if (r.stale) {
        say('场景已被外部修改（Godot 编辑器保存过？），请点「重新加载」后再操作。', 'err')
      } else if (r.rolled_back) {
        say('编辑会破坏场景结构，已自动回滚：' + (r.error ?? ''), 'err')
      } else {
        say(r.error || '操作失败', 'err')
      }
      return false
    }
    if (options.remember !== false && r.undo) {
      undoStack.value.push(r.undo)
      redoStack.value = []
      if (undoStack.value.length > 100) undoStack.value.shift()
    }
    if (r.node) {
      flash.value = { ...flash.value, [r.node]: 'ok' }
      window.setTimeout(() => { const next = { ...flash.value }; delete next[r.node!]; flash.value = next; rebuild() }, 900)
    }
    for (const w of r.warnings ?? []) say(w, 'ok')
    await reload()
    if (r.node && nodeById.value.has(r.node)) selectedId.value = r.node
    return true
  } catch (e) {
    say((e as Error).message, 'err')
    return false
  } finally {
    busy.value = false
  }
}

async function undo() {
  const inv = undoStack.value.pop()
  if (!inv) return say('没有可撤销的操作', 'info')
  const g = graph.value
  if (!g) return
  const r = await sceneApi.op({ path: g.path, if_mtime: g.stats.mtime, ...inv })
  if (!r.ok) {
    undoStack.value.push(inv)
    say('撤销失败：' + (r.error ?? ''), 'err')
    return
  }
  if (r.undo) redoStack.value.push(r.undo)
  await reload()
  say('已撤销', 'ok')
}

async function redo() {
  const inv = redoStack.value.pop()
  if (!inv) return say('没有可重做的操作', 'info')
  const g = graph.value
  if (!g) return
  const r = await sceneApi.op({ path: g.path, if_mtime: g.stats.mtime, ...inv })
  if (!r.ok) {
    redoStack.value.push(inv)
    say('重做失败：' + (r.error ?? ''), 'err')
    return
  }
  if (r.undo) undoStack.value.push(r.undo)
  await reload()
  say('已重做', 'ok')
}

/* 拖拽落点 -> 写回 position（只在空间布局下生效，且排除根节点） */
async function onNodeDragStop(...args: unknown[]) {
  const node = pickNode(args)
  if (layoutMode.value !== 'space' || !node) return
  const isScene = node.type === 'sceneNode'
  const w = isScene ? CARD_W : FILE_W
  const h = isScene ? CARD_H : FILE_H
  // 落点若压在别的卡片上，把被拖卡片挪到最近空位，避免「一拖就叠在一起」
  const blockers: Box[] = (vf.value?.nodes.value ?? [])
    .filter(n => n.id !== node.id)
    .map(n => ({
      x: n.position.x, y: n.position.y,
      w: n.type === 'sceneFile' ? FILE_W : CARD_W,
      h: n.type === 'sceneFile' ? FILE_H : CARD_H,
    }))
  const free = nearestFree(node.position.x, node.position.y, w, h, blockers)
  node.position = free
  nodes.value = nodes.value.map(n => (n.id === node.id ? { ...n, position: free } : n))

  if (!isScene) return // 文件卡只做展示避让，不回写
  const target = nodeById.value.get(node.id)
  if (!target || target.id === graph.value?.root_id) {
    rebuild()
    return
  }
  if (target.position_from === 'transform') {
    say(`${target.name} 用 transform 定位，画布拖拽不改写；请在右侧检查器编辑。`, 'info')
    rebuild()
    return
  }
  const scale = spaceScale.value || 1
  const moved = [Math.round(free.x / scale), Math.round(free.y / scale)]
  const current = target.position ?? [0, 0]
  if (Math.abs(moved[0] - current[0]) < 1 && Math.abs(moved[1] - current[1]) < 1) return
  if (!canEdit.value) {
    say('该场景只读，拖拽不会写回。', 'info')
    rebuild()
    return
  }
  await runOp({ op: 'move', node: node.id, position: moved })
  say(`已写回 ${target.name} position = Vector2(${moved[0]}, ${moved[1]})`, 'ok')
}

/* ------------------------------------------------------------------ 检查器动作 */
const draft = ref({ name: '', type: 'Node2D', prop: '', value: '' })
const reparentTo = ref('')

watch(selectedId, () => {
  draft.value.name = selected.value?.name ?? ''
  draft.value.type = selected.value?.space === '3d' ? 'Node3D' : 'Node2D'
  reparentTo.value = ''
})

async function openFile(file: SceneFile) {
  if (file.resolved && file.rel) emit('open-file', file.rel)
}

const addDraft = ref({ name: '', type: 'Node2D' })
async function addChildNew() {
  const node = selected.value
  if (!node) return
  const type = addDraft.value.type.trim()
  const name = addDraft.value.name.trim() || type
  if (!type) return say('请填写节点类型', 'err')
  if (await runOp({ op: 'add', parent: node.id, type, name })) say(`已新增 ${name}`, 'ok')
  addDraft.value.name = ''
}
async function dupNode() {
  const node = selected.value
  if (node) await runOp({ op: 'duplicate', node: node.id })
}
async function delNode() {
  const node = selected.value
  if (!node) return
  if (node.id === graph.value?.root_id) return say('根节点不能删除', 'err')
  if (!window.confirm(`删除节点 ${node.id}（含其全部子孙）？可用「撤销」恢复。`)) return
  await runOp({ op: 'delete', node: node.id })
}
async function renameNode() {
  const node = selected.value
  const name = draft.value.name.trim()
  if (!node || !name || name === node.name) return
  await runOp({ op: 'rename', node: node.id, name })
  draft.value.name = name
}
async function reparentNode() {
  const node = selected.value
  if (!node || !reparentTo.value) return
  await runOp({ op: 'reparent', node: node.id, parent: reparentTo.value })
  reparentTo.value = ''
}
async function setProperty(property: string, value: string) {
  const node = selected.value
  if (!node) return
  await runOp({ op: 'set_props', node: node.id, properties: { [property]: value } })
}
async function removeProperty(property: string) {
  const node = selected.value
  if (!node) return
  await runOp({ op: 'set_props', node: node.id, remove: [property] })
}
async function addProperty() {
  const key = draft.value.prop.trim()
  if (!key) return
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return say('属性名必须是合法标识符', 'err')
  await runOp({ op: 'set_props', node: selected.value!.id, properties: { [key]: draft.value.value || 'null' } })
  draft.value.prop = ''
  draft.value.value = ''
}
async function deleteSelected() {
  const ids = selectedIds.value.filter(id => id !== graph.value?.root_id && nodeById.value.has(id))
  if (!ids.length) return say('先用 Shift 框选/点选要删除的节点', 'info')
  if (!window.confirm(`删除选中的 ${ids.length} 个节点（含子孙）？可用「撤销」恢复。`)) return
  for (const id of ids) await runOp({ op: 'delete', node: id })
}

/* 换父候选：排除自身与子孙，避免造成循环 */
const parentCandidates = computed(() => {
  const node = selected.value
  if (!node) return []
  return (graph.value?.nodes ?? [])
    .filter(n => n.id !== node.id && !n.id.startsWith(node.id === '.' ? '\u0000' : node.id + '/'))
    .map(n => ({ id: n.id, label: n.id === '.' ? `${n.name}（根）` : n.id }))
})

function onKeydown(ev: KeyboardEvent) {
  const tag = (ev.target as HTMLElement)?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
  const mod = ev.ctrlKey || ev.metaKey
  if (mod && ev.key.toLowerCase() === 'z' && !ev.shiftKey) { ev.preventDefault(); void undo() }
  else if (mod && (ev.key.toLowerCase() === 'y' || (ev.shiftKey && ev.key.toLowerCase() === 'z'))) { ev.preventDefault(); void redo() }
  else if (ev.key === 'Delete') { ev.preventDefault(); void deleteSelected() }
}

function miniColor(n: Node) {
  const file = (n.data as { file?: SceneFile })?.file
  if (file) return EDGE.script
  return (n.data as { color?: string })?.color ?? '#3a424d'
}

watch(layoutMode, () => { rebuild(); scheduleFitView() })
watch([showScripts, showInstances, showResources], () => { rebuild(); scheduleFitView() })
watch(() => props.path, () => { undoStack.value = []; redoStack.value = []; void reload() })
onMounted(() => {
  canvasResizeObserver = new ResizeObserver(() => scheduleFitView())
  if (canvasEl.value) canvasResizeObserver.observe(canvasEl.value)
  void reload()
  scheduleFitView()
})
onBeforeUnmount(() => {
  canvasResizeObserver?.disconnect()
  canvasResizeObserver = null
  if (fitFrame) window.cancelAnimationFrame(fitFrame)
})

/* ------------------------------------------------------------------ 交互增强 */
function setHover(id: string | null) { hoverId.value = id }
function clearHover() { hoverId.value = null }

// 折叠/展开子树：切换集合后重排（layout 会据此剪枝）
function toggleCollapse(id: string) {
  const s = new Set(collapsedIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  collapsedIds.value = s
  rebuild()
}

// 选中或悬停某节点时，算出「关联集合」：祖先链 + 子孙 + 脚本/实例化引用文件（反之亦然）
function computeRelated(id: string | null): Set<string> | null {
  if (!id) return null
  const g = graph.value
  if (!g) return null
  const set = new Set<string>([id])
  let cur = parentOf.value.get(id)
  while (cur) { set.add(cur); cur = parentOf.value.get(cur) }
  const stack = [...(childMap.value.get(id) ?? [])]
  while (stack.length) {
    const c = stack.pop()!
    if (!set.has(c)) {
      set.add(c)
      for (const k of childMap.value.get(c) ?? []) stack.push(k)
    }
  }
  for (const e of g.edges) {
    if (e.kind !== 'script' && e.kind !== 'instance') continue
    if (e.source === id) set.add(e.target)
    else if (e.target === id) set.add(e.source)
  }
  return set
}

// 在不重排的前提下，给节点/边打上 match/dim/rel 装饰态（搜索 + 关系高亮共用）
function decorate() {
  const q = searchQuery.value.trim().toLowerCase()
  const active = hoverId.value || selectedId.value
  const related = computeRelated(active)
  nodes.value = nodes.value.map(n => {
    const d = n.data as Record<string, unknown>
    const label = d.node
      ? ((d.node as SceneNode).name + ' ' + (d.node as SceneNode).type).toLowerCase()
      : d.file ? ((d.file as SceneFile).rel || (d.file as SceneFile).raw || '').toLowerCase() : ''
    const hit = q ? label.includes(q) : false
    const inRel = related ? related.has(n.id) : false
    const dim = !!(related ? !inRel : (q && !hit))
    return { ...n, data: { ...d, match: hit, dim, rel: !!related && inRel, searchActive: !!q, highlightActive: !!related } }
  })
  edges.value = edges.value.map(e => {
    const inRel = related ? (related.has(e.source) && related.has(e.target)) : false
    return { ...e, class: related ? (inRel ? 'sc-edge-rel' : 'sc-edge-dim') : '' }
  })
}

// 聚焦选中节点：用 Vue Flow 实例把视口居中到该节点
function focusSelected() {
  const id = selectedId.value
  if (!id || !vf.value) return
  const n = vf.value.findNode(id)
  if (!n) return
  const p = n.position as { x: number; y: number }
  const zoom = Math.max(1, (vf.value.getViewport()?.zoom) || 1)
  vf.value.setCenter(p.x + 122, p.y + 30, { zoom, duration: 400 })
}

// 选中节点的祖先路径面包屑（可点击跳转）
const breadcrumb = computed(() => {
  const node = selected.value
  if (!node) return [] as { id: string; name: string }[]
  const chain: { id: string; name: string }[] = []
  let cur: SceneNode | undefined = node
  while (cur) {
    chain.unshift({ id: cur.id, name: cur.name })
    cur = cur.parent ? nodeById.value.get(cur.parent) : undefined
  }
  return chain
})

function basename(p: string) { return (p.split(/[\\/]/).pop() || 'scene').replace(/\.tscn$/i, '') }
function downloadBlob(blob: Blob, name: string) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(a.href), 1000)
}

// 由当前 nodes/edges 生成一张矢量示意图（不依赖第三方库），用于导出 SVG/PNG
function buildSceneSvg(): string {
  const ns = nodes.value
  if (!ns.length) return ''
  const pos = new Map(ns.map(n => [n.id, n.position]))
  const W = 244
  const H = 54
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const n of ns) {
    const p = n.position
    minX = Math.min(minX, p.x); minY = Math.min(minY, p.y)
    maxX = Math.max(maxX, p.x + W); maxY = Math.max(maxY, p.y + H)
  }
  const pad = 24
  const vbX = minX - pad, vbY = minY - pad
  const vbW = (maxX - minX) + pad * 2, vbH = (maxY - minY) + pad * 2
  const esc = (s: string) => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!))
  let body = ''
  for (const e of edges.value) {
    const a = pos.get(e.source), b = pos.get(e.target)
    if (!a || !b) continue
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2
    const col = ((e.style as { stroke?: string } | undefined)?.stroke) || '#5b6675'
    const dash = (e.style as { strokeDasharray?: string } | undefined)?.strokeDasharray
    const da = dash ? ` stroke-dasharray="${dash}"` : ''
    body += `<path d="M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}" fill="none" stroke="${col}" stroke-width="1.4"${da} marker-end="url(#arrow)"/>`
  }
  for (const n of ns) {
    const d = n.data as Record<string, unknown>
    const node = d.node as SceneNode | undefined
    const file = d.file as SceneFile | undefined
    const col = (d.color as string) || (node ? '#5b6675' : '#888')
    const name = esc(node ? node.name : (file ? (file.rel || file.raw) : n.id))
    const sub = esc(node ? node.type : (file ? (file.kind === 'script' ? '脚本' : file.kind === 'scene' ? '被实例化场景' : '资源') : ''))
    body += `<g transform="translate(${n.position.x},${n.position.y})">`
      + `<rect width="${W}" height="${H}" rx="8" style="fill: var(--bg-raised); stroke: var(--border-2)"/>`
      + `<rect width="4" height="${H}" rx="2" fill="${col}"/>`
      + `<text x="14" y="22" font-family="Consolas,monospace" font-size="12.5" style="fill: var(--text)">${name}</text>`
      + `<text x="14" y="40" font-family="Consolas,monospace" font-size="10" style="fill: var(--text-muted)">${sub}</text>`
      + `</g>`
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${vbW}" height="${vbH}" viewBox="${vbX} ${vbY} ${vbW} ${vbH}">`
    + `<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" style="fill: color-mix(in srgb, #5b6675 55%, var(--ink))"/></marker></defs>`
    + `<rect x="${vbX}" y="${vbY}" width="${vbW}" height="${vbH}" style="fill: var(--bg)"/>`
    + body + `</svg>`
}

function exportScene(fmt: 'svg' | 'png') {
  const g = graph.value
  if (!g) return
  const svg = buildSceneSvg()
  if (!svg) { say('没有可导出的内容', 'err'); return }
  const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' })
  if (fmt === 'svg') {
    downloadBlob(blob, `${basename(g.path)}.svg`)
    say('已导出 SVG', 'ok')
    return
  }
  const url = URL.createObjectURL(blob)
  const img = new Image()
  img.onload = () => {
    const scale = 2
    const canvas = document.createElement('canvas')
    canvas.width = img.width * scale
    canvas.height = img.height * scale
    const ctx = canvas.getContext('2d')
    if (!ctx) { URL.revokeObjectURL(url); say('导出 PNG 失败', 'err'); return }
    ctx.scale(scale, scale)
    ctx.drawImage(img, 0, 0)
    URL.revokeObjectURL(url)
    canvas.toBlob(b => { if (b) { downloadBlob(b, `${basename(g.path)}.png`); say('已导出 PNG', 'ok') } }, 'image/png')
  }
  img.onerror = () => { URL.revokeObjectURL(url); say('导出 PNG 失败', 'err') }
  img.src = url
}

// 搜索 / 悬停 / 选中变化只重打装饰态（不重排）；布局类变化由 rebuild 顺带 decorate
watch(searchQuery, decorate)
watch(hoverId, decorate)
watch(selectedId, decorate)

// 暴露给自动化探针（与 spike 一样留一个窄门面，避免探针依赖 Vue Flow store 形状）
function onPaneReady(instance: VueFlowStore) {
  vf.value = instance
  scheduleFitView()
  ;(window as unknown as { __sceneCanvas: unknown }).__sceneCanvas = {
    findNode: (id: string) => instance.findNode(id),
    nodeList: () => instance.nodes.value,
    graph: () => graph.value,
    select: (id: string) => { selectedId.value = id },
  }
}

defineExpose({ reload, undo, redo, addChildNew })
</script>

<template>
  <div class="sc-wrap" tabindex="0" @keydown="onKeydown">
    <div class="sc-bar">
      <input
        class="sc-path"
        :value="path"
        readonly
        :title="path"
      />
      <button class="sc-btn" :disabled="loading" @click="reload">{{ loading ? '加载中…' : '重新加载' }}</button>
      <span class="sc-sep" />
      <div class="sc-seg">
        <button :class="{ on: layoutMode === 'hierarchy' }" @click="layoutMode = 'hierarchy'">层级布局</button>
        <button :class="{ on: layoutMode === 'space' }" @click="layoutMode = 'space'">空间布局</button>
      </div>
      <label class="sc-check"><input v-model="showScripts" type="checkbox" />脚本边</label>
      <label class="sc-check"><input v-model="showInstances" type="checkbox" />实例化边</label>
      <label class="sc-check"><input v-model="showResources" type="checkbox" />资源引用</label>
      <span class="sc-sep" />
      <div class="sc-search">
        <input v-model="searchQuery" class="sc-search-input" placeholder="搜索节点 / 类型…" spellcheck="false" />
        <button v-if="searchQuery" class="sc-search-x" title="清除搜索" @click="searchQuery = ''">×</button>
      </div>
      <button class="sc-btn" :disabled="!selectedId" title="把视口居中到选中节点" @click="focusSelected">聚焦选中</button>
      <span class="sc-sep" />
      <button class="sc-btn" :disabled="!undoStack.length || busy" title="Ctrl+Z" @click="undo">撤销 {{ undoStack.length || '' }}</button>
      <button class="sc-btn" :disabled="!redoStack.length || busy" title="Ctrl+Y" @click="redo">重做 {{ redoStack.length || '' }}</button>
      <button class="sc-btn" :disabled="busy" @click="deleteSelected">删除所选</button>
      <span class="sc-sep" />
      <button class="sc-btn" :disabled="!scene" title="导出当前画布为 SVG" @click="exportScene('svg')">导出SVG</button>
      <button class="sc-btn" :disabled="!scene" title="导出当前画布为 PNG" @click="exportScene('png')">导出PNG</button>
      <span class="sc-spacer" />
      <span v-if="scene" class="sc-guard" :class="{ ro: !canEdit }">
        <template v-if="!canEdit">🔒 {{ scene.guard.reason || '只读' }}</template>
        <template v-else>
          {{ scene.guard.region_name || '未分区' }}
          <i v-if="scene.guard.tracked" :class="{ dirty: scene.guard.dirty }" :title="scene.guard.dirty ? '有未提交改动' : '已纳入 git'">{{ scene.guard.dirty ? '● 已改动' : '● 已跟踪' }}</i>
          <i v-else class="untracked" title="未纳入 git，误删不可恢复">未跟踪</i>
        </template>
      </span>
    </div>

    <div v-if="message" class="sc-msg" :class="messageKind">{{ message }}</div>

    <div v-if="!scene" class="sc-empty">
      <p>没有可显示的场景。</p>
      <small>在左侧文件树里点开一个 .tscn（例如 scenes/Main.tscn），或在上方填入场景路径后重新加载。</small>
    </div>

    <div v-else class="sc-body">
      <div ref="canvasEl" class="sc-canvas">
        <VueFlow
          :nodes="nodes"
          :edges="edges"
          :nodes-draggable="layoutMode === 'space'"
          :min-zoom="0.2"
          :max-zoom="2"
          :snap-to-grid="true"
          :snap-grid="[8, 8]"
          :default-viewport="{ zoom: 0.9 }"
          fit-view-on-init
          @nodes-change="onNodesChange"
          @edges-change="onEdgesChange"
          @node-click="onNodeClick"
          @node-drag-stop="onNodeDragStop"
          @pane-ready="onPaneReady"
        >
          <template #node-sceneNode="nodeProps"><SceneNodeCard :data="nodeProps.data" :selected="nodeProps.selected" /></template>
          <template #node-sceneFile="nodeProps"><SceneFileCard :data="nodeProps.data" :selected="nodeProps.selected" @open="openFile" /></template>
          <Background :gap="22" :size="1.4" color="#c6d0de" />
          <Controls />
          <MiniMap pannable zoomable :node-color="miniColor" />
        </VueFlow>
        <div v-if="!nodes.length" class="sc-canvas-empty">
          <b>场景解析成功，但没有可绘制节点</b>
          <small>请确认当前文件是有效的 Godot .tscn，或点击“重新加载”重试。</small>
        </div>
        <div v-if="layoutMode === 'space'" class="sc-hint">
          空间布局：拖动节点即写回 <code>position</code>（transform 定位的节点不会被拖动改写）
        </div>
        <div v-else class="sc-hint">
          层级布局：上→下为父子关系；虚线是脚本引用，点线是实例化引用；双击文件卡打开文件
        </div>
        <div class="sc-legend">
          <span class="sc-leg-title">图例</span>
          <span class="sc-leg"><i class="sc-leg-dot" style="background:#58a6ff"></i>2D</span>
          <span class="sc-leg"><i class="sc-leg-dot" style="background:#bc8cff"></i>3D</span>
          <span class="sc-leg"><i class="sc-leg-dot" style="background:#e3a83a"></i>根</span>
          <span class="sc-leg"><i class="sc-leg-dot" style="background:#2ec4b6"></i>实例</span>
          <span class="sc-leg-line"><i class="sc-leg-bar" style="background:#5b6675"></i>父子</span>
          <span class="sc-leg-line"><i class="sc-leg-bar dash" style="background:#d2a8ff"></i>脚本</span>
          <span class="sc-leg-line"><i class="sc-leg-bar dot" style="background:#2ec4b6"></i>实例化</span>
        </div>
      </div>

      <aside class="sc-side">
        <template v-if="selected">
          <nav v-if="breadcrumb.length" class="sc-crumbs">
            <button v-for="(c, i) in breadcrumb" :key="c.id" type="button"
              class="sc-crumb" :class="{ cur: i === breadcrumb.length - 1 }"
              :title="c.id" @click="selectedId = c.id">{{ c.name }}</button>
          </nav>
          <div class="sc-side-head">
            <b>{{ selected.name }}</b>
            <span class="sc-side-path">{{ selected.id }}</span>
          </div>
          <div class="sc-side-meta">
            <span>{{ selected.type }}</span>
            <span v-if="selected.overridden" class="sc-tag warn">实例子树覆写</span>
            <span v-if="!canEdit" class="sc-tag">只读</span>
          </div>

          <section class="sc-sec">
            <h4>重命名</h4>
            <div class="sc-row">
              <input v-model="draft.name" :disabled="selected.id === scene.root_id" @keyup.enter="renameNode" />
              <button class="sc-btn" :disabled="!canEdit || selected.id === scene.root_id" @click="renameNode">应用</button>
            </div>
          </section>

          <section class="sc-sec">
            <h4>新增子节点</h4>
            <div class="sc-row">
              <input v-model="addDraft.type" class="sc-w2" placeholder="类型 Node2D" />
              <input v-model="addDraft.name" class="sc-w2" placeholder="名称（留空用类型）" @keyup.enter="addChildNew" />
              <button class="sc-btn" :disabled="!canEdit" @click="addChildNew">＋</button>
            </div>
          </section>

          <section class="sc-sec">
            <h4>换父节点</h4>
            <div class="sc-row">
              <select v-model="reparentTo" :disabled="selected.id === scene.root_id">
                <option value="">选择新父节点…</option>
                <option v-for="c in parentCandidates" :key="c.id" :value="c.id">{{ c.label }}</option>
              </select>
              <button class="sc-btn" :disabled="!canEdit || !reparentTo" @click="reparentNode">移动</button>
            </div>
          </section>

          <section class="sc-sec">
            <h4>操作</h4>
            <div class="sc-row">
              <button class="sc-btn" :disabled="!canEdit || selected.id === scene.root_id" @click="dupNode">复制子树</button>
              <button class="sc-btn danger" :disabled="!canEdit || selected.id === scene.root_id" @click="delNode">删除</button>
            </div>
          </section>

          <section class="sc-sec grow">
            <h4>属性 <small>{{ selected.properties.length }}</small></h4>
            <div class="sc-props">
              <div v-for="p in selected.properties" :key="p.name" class="sc-prop">
                <span class="sc-prop-name" :title="`L${p.line}`">{{ p.name }}</span>
                <input
                  :value="p.value"
                  :class="`k-${p.kind}`"
                  :disabled="!canEdit"
                  @change="setProperty(p.name, ($event.target as HTMLInputElement).value)"
                />
                <button class="sc-x" :disabled="!canEdit" title="删除该属性" @click="removeProperty(p.name)">×</button>
              </div>
            </div>
            <div class="sc-row">
              <input v-model="draft.prop" class="sc-w2" placeholder="新属性名" />
              <input v-model="draft.value" class="sc-w2" placeholder="值" @keyup.enter="addProperty" />
              <button class="sc-btn" :disabled="!canEdit" @click="addProperty">＋</button>
            </div>
          </section>
        </template>

        <template v-else>
          <div class="sc-side-head"><b>场景概览</b></div>
          <div class="sc-stats">
            <div><span>节点</span><b>{{ scene.stats.nodes }}</b></div>
            <div><span>层级深度</span><b>{{ scene.stats.max_depth }}</b></div>
            <div><span>外部引用</span><b>{{ scene.stats.files }}<template v-if="scene.stats.orphans">（{{ scene.stats.orphans }} 个未引用）</template></b></div>
            <div><span>脚本 / 实例化</span><b>{{ scene.stats.scripts }} / {{ scene.stats.instances }}</b></div>
            <div><span>行数</span><b>{{ scene.stats.lines }}</b></div>
            <div><span>引擎空间</span><b>{{ scene.space === '3d' ? '3D' : '2D' }}</b></div>
            <div><span>目录</span><b>{{ scene.path }}</b></div>
          </div>
          <section v-if="scene.files.length" class="sc-sec">
            <h4>外部引用</h4>
            <div class="sc-props">
              <div v-for="f in scene.files" :key="f.id" class="sc-ref" :class="{ ghost: !f.resolved }" @dblclick="openFile(f)">
                <span class="sc-file-chip" :style="{ '--fc': f.kind === 'script' ? EDGE.script : f.kind === 'scene' ? EDGE.instance : EDGE.hierarchy }">{{ f.chip }}</span>
                <span class="sc-ref-name">{{ f.rel || f.raw }}</span>
                <small>{{ f.kind }} · {{ f.used }} 处{{ f.orphan ? ' · 未引用' : '' }}</small>
              </div>
            </div>
          </section>
          <section v-if="scene.structure_errors.length" class="sc-sec">
            <h4 class="err">结构异常（编辑已锁定）</h4>
            <ul class="sc-warn"><li v-for="(w, i) in scene.structure_errors" :key="i">{{ w }}</li></ul>
          </section>
          <section v-else-if="scene.warnings.length" class="sc-sec">
            <h4>解析提示</h4>
            <ul class="sc-warn"><li v-for="(w, i) in scene.warnings" :key="i">{{ w }}</li></ul>
          </section>
        </template>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.sc-wrap { display: flex; flex-direction: column; min-height: 0; flex: 1; outline: none; }
.sc-bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding-bottom: 8px; }
.sc-path { flex: 1; min-width: 160px; max-width: 320px; background: var(--bg); border: 1px solid var(--border); color: var(--text-muted); padding: 5px 8px; border-radius: 5px; font-size: 11px; font-family: var(--font-mono); transition: border-color .15s ease, box-shadow .15s ease; }.sc-path:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(47,111,237,.1); }
.sc-sep { width: 1px; height: 18px; background: var(--border); margin: 0 3px; }
.sc-spacer { flex: 1; }
.sc-btn { border: 1px solid var(--border-strong); background: transparent; color: var(--text-muted); border-radius: 5px; padding: 5px 9px; font-size: 11px; cursor: pointer; white-space: nowrap; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease, box-shadow .15s ease; }
.sc-btn:hover:not(:disabled) { color: var(--text); border-color: #b9d0f5; background: var(--bg-selected); transform: translateY(-1px); box-shadow: 0 3px 8px rgba(35,52,84,.08); }.sc-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.sc-btn:disabled { opacity: .38; cursor: default; }
.sc-btn.danger { border-color: rgba(224, 72, 79, 0.5); color: #c23a40; }
.sc-seg { display: flex; border: 1px solid var(--border-strong); border-radius: 5px; overflow: hidden; }
.sc-seg button { background: transparent; border: 0; color: var(--text-muted); padding: 5px 10px; font-size: 11px; cursor: pointer; transition: color .15s ease, background .15s ease; }
.sc-seg button.on { background: linear-gradient(180deg, #3b7ef2, #2f6fed); color: #fff; }
.sc-check { display: flex; align-items: center; gap: 4px; font-size: 11px; color: var(--text-muted); cursor: pointer; }
.sc-guard { font-size: 11px; color: var(--text-faint); }
.sc-guard i { font-style: normal; margin-left: 4px; color: var(--green); }
.sc-guard i.dirty { color: var(--amber); }
.sc-guard i.untracked { color: var(--text-faint); }
.sc-guard.ro { color: var(--amber); }
.sc-msg { font-size: 11px; border-radius: 5px; padding: 5px 9px; margin-bottom: 7px; border: 1px solid var(--border); color: var(--text-muted); }
.sc-msg.ok { color: var(--green); border-color: #bfe2d0; background: #e9f7f0; }
.sc-msg.err { color: #c23a40; border-color: #eeb7ba; background: #fdecec; }
.sc-empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: var(--text-faint); border: 1px dashed var(--border); border-radius: 8px; }
.sc-empty small { max-width: 380px; text-align: center; line-height: 1.7; }
.sc-body { flex: 1; display: flex; min-height: 0; gap: 10px; }
.sc-canvas { flex: 1; min-width: 0; position: relative; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: linear-gradient(180deg, #f7f9fd, #eef2f8); box-shadow: 0 7px 20px rgba(35,52,84,.06); transition: border-color .18s ease, box-shadow .18s ease; }.sc-canvas:focus-within { border-color: #b9d0f5; box-shadow: 0 0 0 3px rgba(47,111,237,.08), 0 7px 20px rgba(35,52,84,.08); }
.sc-canvas-empty { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 7px; color: var(--text-muted); background: rgba(247, 249, 253, .92); pointer-events: none; text-align: center; }
.sc-canvas-empty b { font-size: 12px; font-weight: 600; }
.sc-canvas-empty small { max-width: 330px; color: var(--text-faint); font-size: 11px; line-height: 1.6; }
.sc-hint { position: absolute; left: 12px; bottom: 12px; background: rgba(255, 255, 255, 0.92); border: 1px solid var(--border); border-radius: 7px; padding: 6px 10px; font-size: 11px; color: var(--text-muted); pointer-events: none; }
.sc-hint code { color: var(--accent); font-family: var(--font-mono); }
.sc-legend {
  position: absolute; right: 12px; top: 12px; display: flex; flex-wrap: wrap; gap: 4px 10px;
  max-width: 226px; background: rgba(255, 255, 255, .92); border: 1px solid var(--border);
  border-radius: 8px; padding: 7px 10px; font-size: 10px; color: var(--text-muted);
  pointer-events: none; box-shadow: 0 2px 8px rgba(22, 33, 54, .08);
}
.sc-leg-title { width: 100%; font-weight: 600; color: var(--text); margin-bottom: 1px; }
.sc-leg { display: inline-flex; align-items: center; gap: 4px; }
.sc-leg-dot { width: 9px; height: 9px; border-radius: 3px; flex: none; }
.sc-leg-line { display: inline-flex; align-items: center; gap: 4px; }
.sc-leg-bar { width: 16px; height: 0; border-top: 2px solid; flex: none; }
.sc-leg-bar.dash { border-top-style: dashed; }
.sc-leg-bar.dot { border-top-style: dotted; }
.sc-side { width: 306px; flex: 0 0 306px; border: 1px solid var(--border); border-radius: 8px; display: flex; flex-direction: column; min-height: 0; overflow: auto; padding: 9px 10px; gap: 8px; background: linear-gradient(180deg, var(--bg-hover), var(--bg-raised)); box-shadow: 0 5px 16px rgba(35,52,84,.04); }
.sc-side-head { display: flex; align-items: baseline; gap: 7px; }
.sc-side-head b { font-size: 13px; }
.sc-side-path { font-size: 10px; color: var(--text-faint); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sc-side-meta { display: flex; gap: 6px; flex-wrap: wrap; font-size: 10px; color: var(--text-muted); }
.sc-tag { border: 1px solid var(--border); border-radius: 3px; padding: 0 5px; }
.sc-tag.warn { color: var(--amber); border-color: #dfb067; }
.sc-sec { display: flex; flex-direction: column; gap: 5px; }
.sc-sec.grow { flex: 1; min-height: 0; }
.sc-sec h4 { margin: 0; font-size: 11px; font-weight: 600; color: var(--text-muted); letter-spacing: .3px; }
.sc-sec h4 small { color: var(--text-faint); font-weight: 400; }
.sc-sec h4.err { color: #c23a40; }
.sc-row { display: flex; gap: 5px; align-items: center; }
.sc-row input, .sc-row select { flex: 1; min-width: 0; background: var(--bg); border: 1px solid var(--border); color: var(--text); font-size: 11px; padding: 4px 6px; border-radius: 4px; transition: border-color .15s ease, box-shadow .15s ease; }.sc-row input:focus, .sc-row select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(47,111,237,.1); }
.sc-row input.sc-w2 { flex: 1 1 40%; }
.sc-props { display: flex; flex-direction: column; gap: 3px; max-height: 320px; overflow: auto; }
.sc-prop { display: flex; gap: 5px; align-items: center; }
.sc-prop-name { flex: 0 0 104px; font-size: 10px; color: var(--text-muted); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sc-prop input { flex: 1; min-width: 0; background: var(--bg); border: 1px solid var(--border); color: var(--text); font-size: 10px; padding: 3px 5px; border-radius: 4px; font-family: var(--font-mono); }
.sc-prop input.k-vector, .sc-prop input.k-transform { color: #2f6fed; }
.sc-prop input.k-ref { color: #7a5af8; }
.sc-prop input.k-number { color: #1c9e66; }
.sc-x { flex: none; background: none; border: 0; color: var(--text-faint); cursor: pointer; font-size: 13px; padding: 0 3px; }
.sc-x:hover { color: var(--danger); }
.sc-stats { display: flex; flex-direction: column; gap: 3px; font-size: 11px; }
.sc-stats div { display: flex; justify-content: space-between; gap: 8px; color: var(--text-muted); }
.sc-stats b { color: var(--text); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sc-ref { display: flex; align-items: center; gap: 6px; font-size: 10px; padding: 3px 4px; border-radius: 4px; cursor: pointer; }
.sc-ref:hover { background: var(--bg-hover); transform: translateX(2px); }.sc-ref { transition: background .15s ease, transform .15s ease; }
.sc-ref:focus-visible, .sc-crumb:focus-visible, .sc-search-x:focus-visible, .sc-x:focus-visible { outline: 2px solid #2f6fed88; outline-offset: 2px; }
.sc-ref.ghost { opacity: .55; }
.sc-ref-name { flex: 1; font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sc-ref small { color: var(--text-faint); }
.sc-file-chip { flex: none; font-family: var(--font-mono); font-size: 9px; font-weight: 700; color: var(--fc); background: color-mix(in srgb, var(--fc) 14%, transparent); border-radius: 3px; padding: 1px 4px; }
.sc-warn { margin: 0; padding-left: 16px; font-size: 10px; color: var(--text-muted); line-height: 1.7; }
/* 搜索框 / 面包屑 */
.sc-search { display: flex; align-items: center; position: relative; }
.sc-search-input { width: 150px; box-sizing: border-box; background: var(--bg); border: 1px solid var(--border-strong); color: var(--text); font-size: 11px; padding: 5px 22px 5px 8px; border-radius: 5px; }
.sc-search-input:focus { border-color: var(--accent); outline: none; }
.sc-search-x { position: absolute; right: 4px; border: 0; background: none; color: var(--text-faint); cursor: pointer; font-size: 14px; line-height: 1; padding: 0; }
.sc-search-x:hover { color: var(--danger); }
.sc-crumbs { display: flex; flex-wrap: wrap; gap: 2px 3px; align-items: center; margin-bottom: 2px; }
.sc-crumb { border: 0; background: none; color: var(--accent); font-size: 11px; cursor: pointer; padding: 1px 2px; border-radius: 4px; max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sc-crumb:hover { background: var(--bg-hover); }
.sc-crumb.cur { color: var(--text); font-weight: 600; cursor: default; }
.sc-crumb:not(:last-child)::after { content: '›'; color: var(--text-faint); margin-left: 3px; }
@media (max-width: 900px) { .sc-body { flex-direction: column; overflow: auto; }.sc-canvas { flex: 1 1 440px; min-height: 360px; }.sc-side { width: auto; flex: 0 0 auto; max-height: 46vh; }.sc-search-input { width: 120px; } }
@media (max-width: 560px) { .sc-bar { align-items: stretch; }.sc-path { max-width: none; flex-basis: 100%; }.sc-check, .sc-sep { display: none; }.sc-btn { flex: 1 1 auto; }.sc-seg { flex: 1 1 auto; }.sc-seg button { flex: 1 1 50%; }.sc-search { flex: 1 1 100%; }.sc-search-input { width: 100%; }.sc-canvas { min-height: 280px; }.sc-side { max-height: none; } }
@media (prefers-reduced-motion: reduce) { .sc-path, .sc-btn, .sc-seg button, .sc-canvas, .sc-row input, .sc-row select, .sc-ref, .sc-node, .sc-file, .sc-collapse { transition: none; animation: none; } }
</style>

<style>
/* Vue Flow 主题覆盖（非 scoped：这些类由库渲染在组件外层） */
.sc-canvas .vue-flow__node-sceneNode,
.sc-canvas .vue-flow__node-sceneFile { padding: 0 !important; border: none; background: transparent; }
.sc-canvas .vue-flow__attribution { background: transparent !important; color: var(--text-muted) !important; }
.sc-canvas .vue-flow__controls { box-shadow: none !important; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.sc-canvas .vue-flow__controls-button { background: var(--bg-raised); border-bottom: 1px solid var(--border); fill: var(--text); width: 24px; height: 24px; }
.sc-canvas .vue-flow__controls-button:hover { background: var(--bg-hover); fill: var(--text); }
.sc-canvas .vue-flow__minimap { background: var(--bg-raised) !important; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.sc-canvas .vue-flow__minimap-mask { fill: color-mix(in srgb, var(--bg) 55%, transparent); }
.sc-canvas .vue-flow__edge.selected .vue-flow__edge-path { stroke: var(--accent); }
.sc-canvas .vue-flow__selection { background: var(--accent-soft); border: 1px solid var(--border-2); }

.sc-node {
  width: 244px;
  background: #ffffff;
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--nc);
  border-radius: 8px;
  padding: 6px 9px;
  cursor: grab;
  box-shadow: 0 1px 2px rgba(22, 33, 54, .07), 0 6px 14px rgba(22, 33, 54, .05);
  transition: border-color .12s ease, box-shadow .12s ease, transform .12s ease;
}
.sc-node:active { cursor: grabbing; }
.sc-node:hover { border-color: color-mix(in srgb, var(--nc) 55%, #c4cedd); box-shadow: 0 2px 4px rgba(22, 33, 54, .1), 0 12px 24px rgba(22, 33, 54, .1); transform: translateY(-1px); }
.sc-node.sel { box-shadow: 0 0 0 2px color-mix(in srgb, var(--nc) 32%, transparent), 0 8px 18px rgba(22, 33, 54, .12); border-color: var(--nc); transform: none; }
.sc-node.dim { opacity: .78; border-left-style: dashed; }
.sc-node.flash { box-shadow: 0 0 0 2px rgba(28, 158, 102, 0.55); }
.sc-node.flash.bad { box-shadow: 0 0 0 2px rgba(224, 72, 79, 0.55); }
.sc-node-head { display: flex; align-items: center; gap: 6px; }
.sc-node-dot { width: 6px; height: 6px; border-radius: 2px; background: var(--nc); flex: none; }
.sc-node-name { font-size: 12px; color: var(--text); font-family: Consolas, monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sc-node-pos { margin-left: auto; font-size: 10px; color: var(--text-faint); font-family: Consolas, monospace; flex: none; }
.sc-node-meta { display: flex; align-items: center; gap: 5px; margin-top: 3px; }
.sc-node-type { font-size: 10px; color: var(--text-muted); font-family: Consolas, monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sc-chip { font-size: 9px; border-radius: 3px; padding: 0 4px; border: 1px solid; flex: none; }
.sc-chip.inst { color: #0c857c; border-color: #a7ded7; }
.sc-chip.script { color: #7a5af8; border-color: #cfc2fb; }
.sc-chip.group { color: #5a6778; border-color: var(--border-strong); }
.sc-chip.over { color: #b5791f; border-color: #dfb067; }
.sc-handle { width: 7px !important; height: 7px !important; background: #ffffff !important; border: 1.5px solid #93a0b2 !important; border-radius: 2px; }
.sc-handle.ref { top: 70% !important; }
.sc-handle:hover { background: #2f6fed !important; }

.sc-file {
  display: flex; align-items: center; gap: 8px;
  width: 258px; padding: 6px 9px;
  background: #ffffff; border: 1px solid var(--border); border-left: 3px solid var(--fc);
  border-radius: 8px; cursor: pointer;
  box-shadow: 0 1px 2px rgba(22, 33, 54, .07), 0 6px 14px rgba(22, 33, 54, .05);
  transition: border-color .12s ease, box-shadow .12s ease, transform .12s ease;
}
.sc-file:hover { border-color: color-mix(in srgb, var(--fc) 50%, #c4cedd); box-shadow: 0 2px 4px rgba(22, 33, 54, .1), 0 12px 24px rgba(22, 33, 54, .1); transform: translateY(-1px); }
.sc-file.sel { box-shadow: 0 0 0 2px color-mix(in srgb, var(--fc) 25%, transparent), 0 8px 18px rgba(22, 33, 54, .12); }
.sc-file.ghost { border-style: dashed; opacity: .7; }
.sc-file-chip { flex: none; font-family: Consolas, monospace; font-size: 9px; font-weight: 700; color: var(--fc); background: color-mix(in srgb, var(--fc) 14%, transparent); border-radius: 3px; padding: 2px 5px; }
.sc-file-meta { min-width: 0; flex: 1; }
.sc-file-name { font-family: Consolas, monospace; font-size: 12px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sc-file-sub { font-size: 10px; color: var(--text-faint); }
.sc-file-warn { color: #b5791f; }
/* 折叠按钮（节点卡内，仅层级布局显示） */
.sc-collapse { flex: none; width: 16px; height: 16px; margin-right: 2px; border: 1px solid var(--border-strong); background: var(--bg); border-radius: 4px; font-size: 9px; line-height: 1; color: var(--text-muted); cursor: pointer; padding: 0; transition: color .14s ease, border-color .14s ease, background .14s ease, transform .14s ease; }
.sc-collapse:hover { border-color: var(--accent); color: var(--accent); background: var(--bg-hover); }
.sc-collapse:active { transform: translateY(1px); }
/* 搜索命中 / 关系高亮装饰态 */
.sc-node.hit { box-shadow: 0 0 0 2px var(--accent), 0 8px 18px rgba(22, 33, 54, .12); border-color: var(--accent); }
.sc-node.faded { opacity: .2; filter: saturate(.55); }
.sc-node.rel { box-shadow: 0 0 0 2px color-mix(in srgb, var(--nc) 45%, transparent), 0 8px 18px rgba(22, 33, 54, .12); }
.sc-file.hit { box-shadow: 0 0 0 2px var(--accent), 0 8px 18px rgba(22, 33, 54, .12); border-color: var(--accent); }
.sc-file.faded { opacity: .2; }
.sc-file.rel { box-shadow: 0 0 0 2px color-mix(in srgb, var(--fc) 42%, transparent), 0 8px 18px rgba(22, 33, 54, .12); }
.sc-canvas .vue-flow__controls-button:focus-visible { outline: 2px solid #2f6fed88; outline-offset: -2px; }
.sc-canvas .vue-flow__edge.sc-edge-rel .vue-flow__edge-path { stroke: #2f6fed !important; stroke-width: 2 !important; }
.sc-canvas .vue-flow__edge.sc-edge-dim { opacity: .12; }
</style>
