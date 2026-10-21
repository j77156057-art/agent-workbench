<script setup lang="ts">
// P1：关系图。全项目类/脚本/场景节点 + 三类有向边：
//   inherits（子 → 父）、mounts（场景 → 挂载脚本）、calls（项目内调用）。
//
// 布局：**左→右分层继承树**（不是力导向）。
// 实测 DocMind 自己的项目：1485 节点 / 1245 继承边 —— 继承边占 99.4%，
// 而且它们本来是树不是网。力导向画一棵树 = 1245 条弹簧把节点扯成一团毛球。
// 分层树让"谁继承谁"一眼可读，代价是大扇出要折叠（见 relationTreeLayout.ts）。
//
// 零第三方依赖：SVG 渲染，滚轮缩放/拖拽平移/节点拖动。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { fsApi } from '../api'
import type { RelationGraphResp, RelationNode, RelationEdge } from '../api'
import { useWorkbench } from '../composables/workbench'
import { graphNodeStyle, regionColor } from '../theme'
import {
  buildForest,
  layoutTree,
  ORPHAN_GROUP_ID,
  type TreePlacement,
  type TreeEdgeDraw,
} from '../lib/relationTreeLayout'

const { relationGraphOpen, closeRelationGraph, jumpToLine } = useWorkbench()

const data = ref<RelationGraphResp | null>(null)
const loading = ref(false)
const error = ref('')
const ready = ref(false)
const query = ref('')
const showExternal = ref(true)
const showInherits = ref(true)
const showMounts = ref(true)
const showCalls = ref(true)
/** 展开了哪些分组卡（分组卡 id） */
const expandedGroups = ref<string[]>([])
const svgEl = ref<SVGSVGElement | null>(null)

interface Geo { w: number; h: number; label: string; sub: string; doc: string }

const geometry = ref<Record<string, Geo>>({})

// 视口变换（唯一一个每帧响应式更新的绑定，节点/边坐标全部命令式写 DOM）
const zoom = ref(1)
const panX = ref(0)
const panY = ref(0)

let dragSim: Placement | null = null
let dragStart = { x: 0, y: 0 }
let panning = false
let panStart = { x: 0, y: 0, px: 0, py: 0 }
let down: { id: string; x: number; y: number; moved: boolean } | null = null
let bgDown: { x: number; y: number; moved: boolean } | null = null
const selectedId = ref<string | null>(null)

/** 画布上的一个可绘制实体：普通节点卡 或 分组卡。 */
interface Placement extends TreePlacement {
  /** 普通节点对应的原始节点（分组卡为 null） */
  node: RelationNode | null
  /** 拖拽偏移（用户手动挪过） */
  ox: number
  oy: number
}

const placements = ref<Placement[]>([])
/** placement id → 实体 */
const byId = new Map<string, Placement>()
/** 要画的边 */
const treeEdges = ref<TreeEdgeDraw[]>([])
const edgeEls = new Map<string, { path: SVGPathElement; label: SVGGElement | null }>()

// ------------------------------------------------------------ 数据加载

async function reload() {
  loading.value = true
  error.value = ''
  ready.value = false
  try {
    data.value = await fsApi.relationGraph()
    // 让 loading 态先绘制一帧，避免布局计算卡住 spinner
    await nextTick()
    await new Promise((r) => setTimeout(r, 30))
    buildLayout()
    ready.value = true
    await nextTick()
    fitView() // buildLayout 时 svg 还没显示，正式渲染后按真实尺寸再适配一次
    syncDom()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

watch(relationGraphOpen, (open) => {
  if (open) {
    void reload()
    window.addEventListener('keydown', onKey)
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
  } else {
    window.removeEventListener('keydown', onKey)
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
  }
  // immediate：组件在首次打开时才由 App 异步挂载，挂载即 open=true，需立即加载与绑键
}, { immediate: true })

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
})

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeRelationGraph()
}

// 切换过滤项：重建分层树
watch([showExternal, showInherits, showMounts, showCalls], () => {
  if (!data.value || !ready.value) return
  buildLayout()
  nextTick(() => syncDom())
})

// 展开/收起分组卡：重新布局（树形变了，坐标全变）
watch(expandedGroups, () => {
  if (!data.value || !ready.value) return
  buildLayout()
  nextTick(() => syncDom())
})

/**
 * 搜索词变化：重建布局。
 *
 * 这条 watch 以前是缺的（旧实现里搜索只改 opacity，不改布局，所以没暴露）。
 * 现在搜索会**只看相关子树**（命中的节点 + 它们的祖先链），
 * 树的形状变了 —— 不重建的话，屏幕上还是那棵大树，只是被压暗了，
 * 等于搜索没生效。
 */
watch(query, () => {
  if (!data.value || !ready.value) return
  buildLayout()
  nextTick(() => syncDom())
})

// ------------------------------------------------------------ 过滤 / 搜索

const viewNodes = computed<RelationNode[]>(() => {
  const d = data.value
  if (!d) return []
  return showExternal.value ? d.nodes : d.nodes.filter((n) => !n.external)
})

const viewEdges = computed<RelationEdge[]>(() => {
  const d = data.value
  if (!d) return []
  const ids = new Set(viewNodes.value.map((n) => n.id))
  return d.edges.filter(
    (e) =>
      ids.has(e.source) &&
      ids.has(e.target) &&
      (e.kind !== 'inherits' || showInherits.value) &&
      (e.kind !== 'mounts' || showMounts.value) &&
      (e.kind !== 'calls' || showCalls.value),
  )
})

/**
 * 搜索命中的节点。
 *
 * 旧实现（力导向时代）命中后还会把**直接邻居**也点亮，为的是在毛球里
 * 还能看出点关系。但在分层树里邻居是"父子"或"无关"，混着点亮反而误导 ——
 * 树的形状本身已经表达了关系。所以这里只留真正命中的节点。
 */
const matched = computed<Set<string>>(() => {
  const out = new Set<string>()
  const q = query.value.trim().toLowerCase()
  if (!q || !data.value) return out
  const words = q.split(/\s+/).filter(Boolean)
  for (const n of data.value.nodes) {
    const hay = `${n.label} ${n.sub} ${n.rel} ${n.doc} ${n.region_name}`.toLowerCase()
    if (words.every((w) => hay.includes(w))) out.add(n.id)
  }
  return out
})

/** 完整关系森林（未按搜索裁剪）：祖先链要按**树的父关系**回溯。 */
const fullForest = computed(() => buildForest(viewNodes.value, viewEdges.value))

/**
 * 搜索时**只看相关子树**：命中的节点 + 它们的祖先链。
 *
 * 这是分层树相对力导向最大的好处：树有明确的父子关系，
 * "从命中节点往根上回溯"能得到一条有意义的路径，
 * 而力导向的图里根本没有"祖先"这回事，只能把邻居点亮。
 *
 * 祖先链必须按**森林的父关系**走（继承 > 挂载 > 调用，和树主干同一套），
 * 否则调用边上的节点会被漏掉 —— 树上它是子节点，搜索却找不到它的来路。
 *
 * 没有搜索词时返回 null = 不启用聚焦（画整棵树）。
 */
const focusIds = computed<Set<string> | null>(() => {
  const q = query.value.trim()
  if (!q || !data.value) return null
  const hits = matched.value
  if (!hits.size) return new Set<string>()
  const keep = new Set<string>()
  const parentOf = fullForest.value.parentOf
  for (const id of hits) {
    let cur: string | undefined = id
    let guard = 0
    while (cur !== undefined && guard++ < 64) {
      if (keep.has(cur)) break
      keep.add(cur)
      cur = parentOf.get(cur)
    }
  }
  return keep
})

function nodeDim(id: string): boolean {
  const q = query.value.trim()
  if (!q) return false
  if (focusIds.value) return !focusIds.value.has(id)
  return !matched.value.has(id)
}
function edgeDim(e: TreeEdgeDraw): boolean {
  const q = query.value.trim()
  if (!q) return false
  if (focusIds.value) return !focusIds.value.has(e.from) || !focusIds.value.has(e.to)
  return !matched.value.has(e.from) || !matched.value.has(e.to)
}

// ------------------------------------------------------------ 布局

let measureCtx: CanvasRenderingContext2D | null = null

/**
 * 量文字宽度。
 *
 * **字体栈必须和渲染时一模一样**，否则算出来的卡片宽度会偏小，
 * 文字就溢出卡片边框（实测：`.rg-sub` 用等宽的 Space Mono 11.8px，
 * 早先这里按 Segoe UI 9.5px 算 —— 等宽字宽得多，
 * 于是 tests/test_realtime_gateway_bridge.py 这种长路径直接冲出卡片）。
 *
 * 字号也必须是真实令牌值：`--fs-sm` 12.9px / `--fs-cap` 11.8px，
 * 不是当初估的 12 / 9.5。
 */
const FONT_UI = '"Space Grotesk", "Noto Sans SC", system-ui, sans-serif'
const FONT_MONO = '"Space Mono", ui-monospace, "JetBrains Mono", Consolas, monospace'

const FS_LABEL = 12.9
const FS_CAP = 11.8

function measure(text: string, size: number, bold: boolean, mono = false): number {
  if (!measureCtx) {
    measureCtx = document.createElement('canvas').getContext('2d')
  }
  if (!measureCtx) return text.length * size * 0.62
  measureCtx.font = `${bold ? '600 ' : ''}${size}px ${mono ? FONT_MONO : FONT_UI}`
  return measureCtx.measureText(text).width
}

function fitText(
  text: string,
  maxW: number,
  size: number,
  bold: boolean,
  mono = false,
): string {
  if (measure(text, size, bold, mono) <= maxW) return text
  let t = text
  while (t.length > 1 && measure(t + '…', size, bold, mono) > maxW) t = t.slice(0, -1)
  return t + '…'
}

function edgeKey(e: TreeEdgeDraw): string {
  return e.key
}

/**
 * 重建整张图：算布局 → 填 placements / treeEdges。
 *
 * 节点卡片的尺寸要先量好（fitText 依赖 canvas 测量），再交给布局引擎排布 ——
 * 引擎按"每列最宽的卡"算列距，所以卡片宽高必须先进去。
 */
function buildLayout() {
  const nodes = viewNodes.value
  const edges = viewEdges.value
  const geo: Record<string, Geo> = {}

  // 展开中的基类要留出"收起"角标的位置，否则角标会压住卡片标题
  const expandedBases = new Set(
    expandedGroups.value
      .filter((g) => g.startsWith('group:') && g !== ORPHAN_GROUP_ID)
      .map((g) => g.slice('group:'.length)),
  )

  for (const n of nodes) {
    const badgeW = expandedBases.has(n.id) ? 66 : 0
    const label = fitText(n.label, 220 - badgeW, FS_LABEL, true)
    const subRaw = n.external ? (n.sub ? `(${n.sub})` : '') : n.sub
    // .rg-sub 渲染成等宽字体（--font-mono），量宽也要用等宽，否则算窄了会溢出卡片
    const sub = subRaw ? fitText(subRaw, 220, FS_CAP, false, true) : ''
    // 卡片第三行：文档/注释首行，帮用户不打开文件也能认出这个类/场景是干什么的
    const docRaw = (n.doc || '').split(/\r?\n/)[0].trim()
    const doc = !n.external && docRaw && docRaw !== n.label ? fitText(docRaw, 220, FS_CAP, false) : ''
    const labelW = measure(label, FS_LABEL, true)
    const subW = sub ? measure(sub, FS_CAP, false, true) : 0
    const docW = doc ? measure(doc, FS_CAP, false) : 0
    const w = Math.max(112, Math.min(264, Math.max(labelW, subW, docW) + 42))
    const h = doc ? 58 : sub ? 46 : 32
    geo[n.id] = { w, h, label, sub, doc }
  }

  // 搜索聚焦时只画相关子树（命中的节点 + 它们的祖先链）
  const focus = focusIds.value
  let useNodes = nodes
  let useEdges = edges
  if (focus) {
    useNodes = nodes.filter((n) => focus.has(n.id))
    const ids = new Set(useNodes.map((n) => n.id))
    useEdges = edges.filter((e) => ids.has(e.source) && ids.has(e.target))
  }

  const forest = buildForest(useNodes, useEdges)

  // 搜索时自动展开"装着命中节点"的分组卡：
  // 否则命中节点藏在折叠卡里，用户搜索到了却看不见，等于没搜到。
  // 要先算一遍才知道哪些组藏着命中节点，所以这里可能排两次。
  const expanded = new Set(expandedGroups.value)
  const sizeOf = {
    widthOf: (id: string) => geo[id]?.w ?? 150,
    heightOf: (id: string) => geo[id]?.h ?? 34,
  }
  let res = layoutTree(useNodes, forest, { expandedGroups: [...expanded] }, sizeOf)
  if (focus) {
    let changed = false
    for (const p of res.placements) {
      if (p.kind !== 'group') continue
      if (expanded.has(p.id)) continue
      if (p.members.some((m) => focus.has(m))) {
        expanded.add(p.id)
        changed = true
      }
    }
    if (changed) res = layoutTree(useNodes, forest, { expandedGroups: [...expanded] }, sizeOf)
  }

  const nodeById = new Map(useNodes.map((n) => [n.id, n]))
  const list: Placement[] = []
  byId.clear()
  for (const p of res.placements) {
    const item: Placement = { ...p, node: p.kind === 'node' ? nodeById.get(p.id) ?? null : null, ox: 0, oy: 0 }
    list.push(item)
    byId.set(p.id, item)
  }
  placements.value = list
  treeEdges.value = res.edges
  geometry.value = geo
  fitView()
}

/**
 * 分组卡的连线：从基类卡右边缘 → 折线 → 子卡左边缘。
 *
 * 画折线而不是直线：树有 4 列、几十行，直线会横穿整张图，
 * 折线只在相邻两列之间画一小段竖直段，一眼能看出"这一挂属于哪个基类"。
 */
function edgePath(e: TreeEdgeDraw): { d: string; mx: number; my: number } {
  const a = byId.get(e.from)
  const b = byId.get(e.to)
  if (!a || !b) return { d: '', mx: 0, my: 0 }
  const ax = a.x + a.ox
  const ay = a.y + a.oy + a.height / 2
  const bx = b.x + b.ox
  const by = b.y + b.oy + b.height / 2
  if (e.cross) {
    // 交叉连线（多继承 / 又被调用）：直线 + 虚线，标出"这条不是主干"
    return { d: `M ${ax} ${ay} L ${bx} ${by}`, mx: (ax + bx) / 2, my: (ay + by) / 2 }
  }
  // 主干：基类右边缘 → 中点竖直 → 子类左边缘
  const x1 = ax + a.width / 2
  const x2 = bx - b.width / 2
  const mid = (x1 + x2) / 2
  return {
    d: `M ${x1} ${ay} H ${mid} V ${by} H ${x2}`,
    mx: mid,
    my: (ay + by) / 2,
  }
}

function syncDom() {
  for (const e of treeEdges.value) {
    const els = edgeEls.get(edgeKey(e))
    if (!els) continue
    const p = edgePath(e)
    els.path.setAttribute('d', p.d)
    if (els.label) {
      els.label.setAttribute('transform', `translate(${p.mx}, ${p.my})`)
      els.label.style.display = zoom.value < 0.55 ? 'none' : ''
    }
  }
  for (const s of placements.value) {
    const el = nodeEl(s.id)
    if (el) el.setAttribute('transform', `translate(${s.x + s.ox - s.width / 2}, ${s.y + s.oy - s.height / 2})`)
  }
}

// 卡片三行文字的 y 坐标：有 doc 时 16/32/48，只有 sub 时 17/37，单行垂直居中
function geoY(id: string, line: 'label' | 'sub' | 'doc'): number {
  const g = geometry.value[id]
  if (!g) return line === 'label' ? 16 : 0
  if (g.doc) return line === 'label' ? 16 : line === 'sub' ? 32 : 48
  if (g.sub) return line === 'label' ? 17 : 37
  return (g.h || 32) / 2 + 4
}

// 初始模板绑定读取（首帧 Vue 渲染时用，之后命令式覆盖）
function initialPos(id: string, axis: 'x' | 'y'): number {
  const s = byId.get(id)
  if (!s) return 0
  return axis === 'x' ? s.x + s.ox - s.width / 2 : s.y + s.oy - s.height / 2
}
function edgePathOf(e: TreeEdgeDraw): string {
  return edgePath(e).d
}
function edgeLabelPos(e: TreeEdgeDraw): { x: number; y: number } {
  const p = edgePath(e)
  return { x: p.mx, y: p.my }
}

// ------------------------------------------------------------ 视口

function fitView() {
  const svg = svgEl.value
  if (!svg || placements.value.length === 0) {
    zoom.value = 1
    panX.value = 0
    panY.value = 0
    return
  }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const s of placements.value) {
    minX = Math.min(minX, s.x + s.ox - s.width / 2)
    maxX = Math.max(maxX, s.x + s.ox + s.width / 2)
    minY = Math.min(minY, s.y + s.oy - s.height / 2)
    maxY = Math.max(maxY, s.y + s.oy + s.height / 2)
  }
  const cw = svg.clientWidth || 900
  const ch = svg.clientHeight || 600
  const bw = Math.max(1, maxX - minX + 80)
  const bh = Math.max(1, maxY - minY + 80)
  const z = Math.min(1.25, Math.max(0.05, Math.min(cw / bw, ch / bh)))
  zoom.value = z
  panX.value = cw / 2 - ((minX + maxX) / 2) * z
  panY.value = ch / 2 - ((minY + maxY) / 2) * z
}

function zoomBy(f: number) {
  const svg = svgEl.value
  if (!svg) return
  const cx = (svg.clientWidth || 900) / 2
  const cy = (svg.clientHeight || 600) / 2
  applyZoom(f, cx, cy)
}

function applyZoom(f: number, cx: number, cy: number) {
  // 下限放到 0.05：分层树高瘦（实测真实项目 1168×3986），0.18 缩不下去就看不到全貌
  const nz = Math.min(2.5, Math.max(0.05, zoom.value * f))
  const k = nz / zoom.value
  panX.value = cx - (cx - panX.value) * k
  panY.value = cy - (cy - panY.value) * k
  zoom.value = nz
}

function onWheel(e: WheelEvent) {
  if (!ready.value) return
  e.preventDefault()
  const r = svgEl.value!.getBoundingClientRect()
  applyZoom(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - r.left, e.clientY - r.top)
}

function toWorld(clientX: number, clientY: number) {
  const r = svgEl.value!.getBoundingClientRect()
  return {
    x: (clientX - r.left - panX.value) / zoom.value,
    y: (clientY - r.top - panY.value) / zoom.value,
  }
}

// ------------------------------------------------------------ 交互

function nodeEl(id: string): SVGGElement | undefined {
  return nodeEls.value.get(id)
}
const nodeEls = ref(new Map<string, SVGGElement>())

function setNodeRef(el: Element | unknown, id: string) {
  if (el instanceof SVGGElement) nodeEls.value.set(id, el)
  else nodeEls.value.delete(id)
}

function setEdgeRef(el: Element | unknown, key: string) {
  if (el instanceof SVGGElement) {
    edgeEls.set(key, {
      path: el.querySelector('.rg-line') as SVGPathElement,
      // 继承边不渲染文字标签（见模板注释），所以这里可能是 null —— syncDom 要能扛住
      label: el.querySelector('.rg-elabel') as SVGGElement | null,
    })
  } else {
    edgeEls.delete(key)
  }
}

function onNodeDown(ev: PointerEvent, p: Placement) {
  ev.stopPropagation()
  down = { id: p.id, x: ev.clientX, y: ev.clientY, moved: false }
  dragSim = p
  // 记住拖拽起点对应的布局坐标，拖动量作为偏移量叠加（分层树的位置是算出来的，
  // 直接改 x/y 会在下次 buildLayout 时被冲掉）
  dragStart = { x: p.x + p.ox, y: p.y + p.oy }
}

function onBgDown(ev: PointerEvent) {
  if (!ready.value) return
  panning = true
  bgDown = { x: ev.clientX, y: ev.clientY, moved: false }
  panStart = { x: ev.clientX, y: ev.clientY, px: panX.value, py: panY.value }
  ;(ev.currentTarget as Element).setPointerCapture?.(ev.pointerId)
}

function onPointerMove(ev: PointerEvent) {
  if (dragSim && down) {
    if (Math.hypot(ev.clientX - down.x, ev.clientY - down.y) > 4) down.moved = true
    const w = toWorld(ev.clientX, ev.clientY)
    dragSim.ox = w.x - dragStart.x
    dragSim.oy = w.y - dragStart.y
    syncDom()
  } else if (panning) {
    if (bgDown && Math.hypot(ev.clientX - bgDown.x, ev.clientY - bgDown.y) > 4) bgDown.moved = true
    panX.value = panStart.px + (ev.clientX - panStart.x)
    panY.value = panStart.py + (ev.clientY - panStart.y)
  }
}

let suppressBgClick = false

function onNodeClick(ev: MouseEvent, p: Placement) {
  // 选中走 click：浏览器对"按下后轻微抖动"有自带容差，比手写 pointerup 判定更稳
  ev.stopPropagation()
  // 点分组卡 = 展开/收起这一组（大扇出折叠的入口）
  if (p.kind === 'group') {
    toggleGroup(p.id)
    return
  }
  selectedId.value = p.id
}

/** 展开/收起一个分组卡。展开后整棵树重排（watch expandedGroups 会触发 buildLayout）。 */
function toggleGroup(groupId: string) {
  const set = new Set(expandedGroups.value)
  if (set.has(groupId)) set.delete(groupId)
  else set.add(groupId)
  expandedGroups.value = [...set]
}

function onBgClick() {
  // 平移画布松手也会触发 click，平移过就不当作"取消选中"
  if (suppressBgClick) {
    suppressBgClick = false
    return
  }
  selectedId.value = null
}

function onPointerUp() {
  const clicked = down
  dragSim = null
  down = null
  if (clicked && clicked.moved) syncDom()
  suppressBgClick = !!(bgDown && bgDown.moved)
  bgDown = null
  panning = false
}

async function onNodeDbl(p: Placement) {
  const n = p.node
  if (n?.rel) {
    closeRelationGraph()
    await jumpToLine(n.rel, n.line || 1)
  }
}

async function openSelected() {
  const n = selectedNode.value
  if (n?.rel) {
    closeRelationGraph()
    await jumpToLine(n.rel, n.line || 1)
  }
}

// ------------------------------------------------------------ 选中详情

interface Incident { edge: RelationEdge; other: RelationNode | null }
const selectedNode = computed<RelationNode | null>(
  () => data.value?.nodes.find((n) => n.id === selectedId.value) || null)
const selectedIncident = computed<{ out: Incident[]; incoming: Incident[] }>(() => {
  const id = selectedId.value
  const d = data.value
  if (!id || !d) return { out: [], incoming: [] }
  const nodeById = new Map(d.nodes.map((n) => [n.id, n]))
  const out: Incident[] = []
  const incoming: Incident[] = []
  for (const edge of d.edges) {
    if (edge.source === id) out.push({ edge, other: nodeById.get(edge.target) || null })
    else if (edge.target === id) incoming.push({ edge, other: nodeById.get(edge.source) || null })
  }
  const byLabel = (x: Incident, y: Incident) => (x.other?.label || '').localeCompare(y.other?.label || '')
  return { out: out.sort(byLabel), incoming: incoming.sort(byLabel) }
})
function kindText(k: string): string {
  return graphNodeStyle(k).label
}

/**
 * 分组卡的描边色。
 *
 * 走 theme.ts 的 'group' 节点样式（cat() 派生），不用边框令牌 ——
 * SVG 的 fill 是绘制属性、对比度门禁不算它，描边是跟**画布点阵底**比对的，
 * zinc 下边框令牌只有 2.82:1（差 0.18），cat() 派生那档三套皮肤都过。
 * 选中态换成强调色（行内样式盖得住 class 里的描边）。
 */
/** 当前处于展开状态的分组卡对应的基类 id（"group:<base>" 去掉前缀）。 */
const expandedBaseIds = computed<Set<string>>(() => {
  const s = new Set<string>()
  for (const g of expandedGroups.value) {
    if (!g.startsWith('group:') || g === ORPHAN_GROUP_ID) continue
    s.add(g.slice('group:'.length))
  }
  return s
})

/** 展开状态的基类：它当年折掉的那批子类数（收起按钮上要写"收起 N 个"）。 */
function foldedCountOf(baseId: string): number {
  return fullForest.value.childrenOf.get(baseId)?.length ?? 0
}

function groupStroke(p: Placement): string {
  if (selectedId.value === p.id) {
    return 'color-mix(in srgb, var(--accent) 55%, var(--ink))'
  }
  return graphNodeStyle('group').stroke
}
function edgeKindText(k: string): string {
  return k === 'inherits' ? '继承' : k === 'mounts' ? '场景挂载' : k === 'calls' ? '调用' : k
}
function incidentMethods(it: Incident): string {
  const ms = it.edge.methods || []
  const head = ms.slice(0, 4).join('、')
  return ms.length > 4 ? `${head} 等 ${ms.length} 个方法` : head
}

/** 选中的分组卡（点分组卡是展开/收起，不会选中，所以这里只在详情面板用）。 */
const selectedGroup = computed<Placement | null>(
  () => placements.value.find((p) => p.id === selectedId.value && p.kind === 'group') || null)

/** 分组卡里的成员节点（详情面板列出来，让人能点进去）。 */
const selectedGroupMembers = computed<RelationNode[]>(() => {
  const g = selectedGroup.value
  if (!g) return []
  const d = data.value
  if (!d) return []
  const byId2 = new Map(d.nodes.map((n) => [n.id, n]))
  return g.members.map((id) => byId2.get(id)).filter((n): n is RelationNode => !!n)
})

const viewport = computed(() => `translate(${panX.value}, ${panY.value}) scale(${zoom.value})`)
const stats = computed(() => data.value?.stats)
const styleOf = graphNodeStyle
const colorOf = regionColor
</script>

<template>
  <div v-if="relationGraphOpen" class="rg-overlay wb-modal-backdrop" @mousedown.self="closeRelationGraph">
    <section class="rg-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="关系图">
      <header class="rg-top">
        <div class="rg-title">
          <svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">
            <circle cx="3.6" cy="4" r="1.7" fill="none" style="stroke: var(--viz-class)" stroke-width="1.1" />
            <circle cx="11.4" cy="4" r="1.7" fill="none" style="stroke: var(--viz-script)" stroke-width="1.1" />
            <circle cx="7.5" cy="11.2" r="1.7" fill="none" style="stroke: var(--viz-scene)" stroke-width="1.1" />
            <path d="M5 4.8 L9.6 4.8 M4.6 5.4 L6.6 9.7 M10.4 5.4 L8.4 9.7" style="stroke: var(--viz-neutral)" stroke-width="0.9" />
          </svg>
          <h2>关系图</h2>
          <span v-if="stats" class="rg-stats">
            {{ stats.user_nodes }} 个类/脚本/场景 · {{ stats.external_nodes }} 个外部基类 ·
            {{ stats.edges_by_kind.inherits || 0 }} 继承 · {{ stats.edges_by_kind.mounts || 0 }} 挂载 ·
            {{ stats.edges_by_kind.calls || 0 }} 调用
          </span>
        </div>
        <button class="rg-iconbtn" title="重新布局" @click="reload">
          <svg width="13" height="13" viewBox="0 0 13 13"><path d="M11 6.5 A4.5 4.5 0 1 1 6.5 2 M11 1.4 V4.2 H8.2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <button class="rg-iconbtn" title="关闭（Esc）" @click="closeRelationGraph">
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
        </button>
      </header>

      <div class="rg-toolbar">
        <div class="rg-search">
          <svg width="12" height="12" viewBox="0 0 12 12"><circle cx="5.2" cy="5.2" r="3.4" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M7.8 7.8 L10.5 10.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
          <input v-model="query" type="text" placeholder="搜索类 / 脚本 / 场景 / 分区（高亮节点与直接关系）" />
          <kbd v-if="query" class="rg-clear" @click="query = ''">清空</kbd>
        </div>
        <div class="rg-toggles">
          <button :class="['rg-toggle', { off: !showInherits }]" @click="showInherits = !showInherits">
            <i class="rg-tg-line inh" />继承
          </button>
          <button :class="['rg-toggle', { off: !showMounts }]" @click="showMounts = !showMounts">
            <i class="rg-tg-line mnt" />场景挂载
          </button>
          <button :class="['rg-toggle', { off: !showCalls }]" @click="showCalls = !showCalls">
            <i class="rg-tg-line call" />调用
          </button>
          <button :class="['rg-toggle', { off: !showExternal }]" @click="showExternal = !showExternal">
            <i class="rg-tg-ext" />引擎/外部基类
          </button>
        </div>
        <div class="rg-zoom">
          <button title="放大" @click="zoomBy(1.2)">+</button>
          <button title="缩小" @click="zoomBy(1 / 1.2)">−</button>
          <button title="适应窗口" @click="fitView">适应</button>
        </div>
      </div>

      <div class="rg-canvas-wrap">
        <div v-if="loading" class="rg-state"><div class="cv-spinner" /><p>正在分析继承与挂载关系…</p></div>
        <div v-else-if="error" class="rg-state">
          <p class="rg-error">{{ error }}</p>
          <button class="rg-retry" @click="reload">重试</button>
        </div>
        <div v-else-if="ready && viewNodes.length === 0" class="rg-state">
          <p class="rg-faint">当前过滤条件下没有可显示的节点。</p>
        </div>
        <div v-else-if="ready && viewEdges.length === 0 && (!showInherits && !showMounts && !showCalls)" class="rg-state">
          <p class="rg-faint">关系边都已隐藏，打开上方开关查看关系。</p>
        </div>

        <svg
          v-show="ready"
          ref="svgEl"
          class="rg-svg"
          :class="{ grabbing: panning }"
          @pointerdown="onBgDown"
          @click="onBgClick"
          @wheel="onWheel"
        >
          <!-- 箭头颜色跟各自那条线同源（以前是三个更浅的写死值）。
               用 style 而不是 fill 属性：属性里写 var()/color-mix() 不解析，
               会静默回落到黑色 —— 深色皮肤下就是"看不见"，浅色皮肤下是凭空变色。 -->
          <defs>
            <marker id="rg-arrow-inh" viewBox="0 0 10 10" refX="8.5" refY="5"
                    markerWidth="9" markerHeight="9" orient="auto-start-reverse">
              <path d="M0 0 L9 5 L0 10 Z" style="fill: var(--viz-inherit)" />
            </marker>
            <marker id="rg-arrow-mnt" viewBox="0 0 10 10" refX="8.5" refY="5"
                    markerWidth="9" markerHeight="9" orient="auto-start-reverse">
              <path d="M0 0 L9 5 L0 10 Z" style="fill: var(--viz-mount)" />
            </marker>
            <marker id="rg-arrow-call" viewBox="0 0 10 10" refX="8.5" refY="5"
                    markerWidth="9" markerHeight="9" orient="auto-start-reverse">
              <path d="M0 0 L9 5 L0 10 Z" style="fill: var(--viz-call)" />
            </marker>
          </defs>

          <g :transform="viewport">
            <g class="rg-edges">
              <g v-for="e in treeEdges" :key="edgeKey(e)"
                 :ref="(el) => setEdgeRef(el, edgeKey(e))"
                 :class="['rg-edge-g', { dim: edgeDim(e), cross: e.cross }]">
                <title v-if="e.kind === 'calls'">调用：{{ (e.methods || []).map((m) => m + '()').join('、') }}（首个调用点第 {{ e.line }} 行）</title>
                <title v-else>{{ e.label }}{{ e.line ? `（第 ${e.line} 行）` : '' }}</title>
                <path
                  class="rg-line"
                  :class="e.kind"
                  :d="edgePathOf(e)"
                  :marker-end="e.kind === 'mounts' ? 'url(#rg-arrow-mnt)' : e.kind === 'calls' ? 'url(#rg-arrow-call)' : 'url(#rg-arrow-inh)'"
                />
                <!-- 边文字标签：只给挂载/调用边留。
                     继承边在树里有上百条，每条都写 extends/bases 纯属噪声，
                     而且边图层在节点图层之下（标签被卡片盖住），
                     几十个标签叠在一起反而更看不清 —— 继承关系由颜色+图例+tooltip 表达就够。 -->
                <g v-if="e.kind !== 'inherits'" class="rg-elabel" :transform="`translate(${edgeLabelPos(e).x}, ${edgeLabelPos(e).y})`">
                  <rect x="-26" y="-8" width="52" height="13" rx="3" class="rg-elabel-bg-inh" v-if="e.kind === 'inherits'" />
                  <rect x="-20" y="-8" width="40" height="13" rx="3" class="rg-elabel-bg-mnt" v-else-if="e.kind === 'mounts'" />
                  <rect x="-20" y="-8" width="40" height="13" rx="3" class="rg-elabel-bg-call" v-else />
                  <text text-anchor="middle" y="2" :class="['rg-elabel-tx', e.kind]">{{ e.label }}</text>
                </g>
              </g>
            </g>

            <g class="rg-nodes">
              <g
                v-for="p in placements"
                :key="p.id"
                :ref="(el) => setNodeRef(el, p.id)"
                class="rg-node"
                :class="{
                  ext: p.node?.external,
                  dim: nodeDim(p.id),
                  selected: selectedId === p.id,
                  group: p.kind === 'group',
                }"
                :transform="`translate(${initialPos(p.id, 'x')}, ${initialPos(p.id, 'y')})`"
                @pointerdown="onNodeDown($event, p)"
                @click="onNodeClick($event, p)"
                @dblclick="onNodeDbl(p)"
              >
                <!-- 分组卡：大扇出折叠后的替代物（点它展开/收起） -->
                <template v-if="p.kind === 'group'">
                  <rect
                    :width="p.width"
                    :height="p.height"
                    rx="7"
                    class="rg-group-rect"
                    :style="{ stroke: groupStroke(p) }"
                  />
                  <text class="rg-group-mark" x="11" :y="p.height / 2 + 3.6">⋯</text>
                  <text class="rg-group-label" x="26" :y="p.height / 2 + 4">
                    {{ p.groupBase ? p.label : p.label }} · {{ p.groupSize }}
                  </text>
                  <title>{{ p.groupBase ? `${p.label} 的 ${p.groupSize} 个子类（点击展开）` : `${p.groupSize} 个没有任何关系的节点（点击展开）` }}</title>
                </template>
                <template v-else-if="p.node">
                  <rect
                    :width="p.width"
                    :height="p.height"
                    rx="7"
                    :style="{ fill: styleOf(p.node.kind).fill, stroke: styleOf(p.node.kind).stroke }"
                    :stroke-dasharray="styleOf(p.node.kind).dashed ? '4 3' : undefined"
                  />
                  <circle
                    v-if="p.node.region"
                    class="rg-region-dot"
                    :cx="9" :cy="p.height / 2" r="3"
                    :style="{ fill: colorOf(p.node.region) }"
                  />
                  <text class="rg-mark"
                        :x="p.node.region ? 17 : 12"
                        :y="p.height / 2 + 3.6">
                    {{ styleOf(p.node.kind).mark }}
                  </text>
                  <text class="rg-label" x="30"
                        :y="geoY(p.id, 'label')">
                    {{ geometry[p.id]?.label }}
                  </text>
                  <text v-if="geometry[p.id]?.sub" class="rg-sub" x="30"
                        :y="geoY(p.id, 'sub')">
                    {{ geometry[p.id]?.sub }}
                  </text>
                  <text v-if="geometry[p.id]?.doc" class="rg-doc" x="30"
                        :y="geoY(p.id, 'doc')">
                    {{ geometry[p.id]?.doc }}
                  </text>
                  <!-- 展开中的基类：角标是"收起"入口。
                       分组卡展开后自己就没了，不给这个角标的话用户收起不回去。 -->
                  <g v-if="expandedBaseIds.has(p.id)" class="rg-fold" @click.stop="toggleGroup('group:' + p.id)">
                    <rect :x="p.width - 62" y="3" width="59" height="14" rx="4" />
                    <text x="p.width - 52" y="13">⊟ 收起 {{ foldedCountOf(p.id) }}</text>
                  </g>
                  <title>{{ p.node.rel ? `${p.node.rel}${p.node.doc ? '\n' + p.node.doc : ''}` : `${p.node.label}（${styleOf(p.node.kind).label}）` }}</title>
                </template>
              </g>
            </g>
          </g>
        </svg>

        <!-- 分组卡详情：点分组卡是展开/收起，这里是选中后的成员清单 -->
        <aside v-if="selectedGroup" class="rg-detail" @pointerdown.stop>
          <header class="rg-d-head">
            <span class="rg-d-mark rg-d-mark-group">⋯</span>
            <b class="rg-d-title">{{ selectedGroup.label }} 的 {{ selectedGroup.groupSize }} 个子类</b>
            <button class="rg-d-x" title="关闭详情" @click="selectedId = null">×</button>
          </header>
          <div class="rg-d-tags">
            <span class="rg-d-kind">分组（已折叠）</span>
          </div>
          <div class="rg-d-sec">成员 · {{ selectedGroupMembers.length }}</div>
          <ul class="rg-d-list">
            <li v-for="m in selectedGroupMembers.slice(0, 200)" :key="m.id">
              <a class="rg-d-link" @click="selectedId = m.id">{{ m.label }}</a>
              <em>{{ m.rel }}</em>
            </li>
          </ul>
          <p v-if="selectedGroupMembers.length > 200" class="rg-d-empty">
            仅列出前 200 个，其余 {{ selectedGroupMembers.length - 200 }} 个请用搜索定位。
          </p>
        </aside>

        <!-- 节点详情卡：单击节点出现 -->
        <aside v-if="selectedNode" class="rg-detail" @pointerdown.stop>
          <header class="rg-d-head">
            <span class="rg-d-mark" :style="{ color: styleOf(selectedNode.kind).stroke }">{{ styleOf(selectedNode.kind).mark }}</span>
            <b class="rg-d-title">{{ selectedNode.label }}</b>
            <button class="rg-d-x" title="关闭详情" @click="selectedId = null">×</button>
          </header>
          <div class="rg-d-tags">
            <span class="rg-d-kind">{{ kindText(selectedNode.kind) }}</span>
            <span v-if="selectedNode.region_name" class="rg-d-region">
              <i :style="{ background: colorOf(selectedNode.region) }" />{{ selectedNode.region_name }}
            </span>
            <span v-if="selectedNode.external" class="rg-d-ext">引擎 / 外部基类</span>
          </div>
          <p v-if="selectedNode.doc" class="rg-d-doc">{{ selectedNode.doc }}</p>
          <p v-if="selectedNode.rel" class="rg-d-rel">
            <span class="rg-d-path">{{ selectedNode.rel }}</span><span
              v-if="selectedNode.line" class="rg-d-line">:{{ selectedNode.line }}</span>
          </p>
          <p v-else class="rg-d-rel rg-d-faint">引擎内置类型，不在项目文件中</p>

          <div class="rg-d-sec">出边 · {{ selectedIncident.out.length }}</div>
          <ul v-if="selectedIncident.out.length" class="rg-d-list">
            <li v-for="(it, idx) in selectedIncident.out" :key="'o' + idx">
              <i class="rg-d-arrow" :class="it.edge.kind">→</i>
              <a v-if="it.other" class="rg-d-link" @click="selectedId = it.other!.id">{{ it.other.label }}</a>
              <span v-else class="rg-d-faint">?</span>
              <em>{{ edgeKindText(it.edge.kind) }}<template v-if="incidentMethods(it)"> · {{ incidentMethods(it) }}</template></em>
            </li>
          </ul>
          <p v-else class="rg-d-empty">无</p>

          <div class="rg-d-sec">入边 · {{ selectedIncident.incoming.length }}</div>
          <ul v-if="selectedIncident.incoming.length" class="rg-d-list">
            <li v-for="(it, idx) in selectedIncident.incoming" :key="'i' + idx">
              <i class="rg-d-arrow" :class="it.edge.kind">←</i>
              <a v-if="it.other" class="rg-d-link" @click="selectedId = it.other!.id">{{ it.other.label }}</a>
              <span v-else class="rg-d-faint">?</span>
              <em>{{ edgeKindText(it.edge.kind) }}<template v-if="incidentMethods(it)"> · {{ incidentMethods(it) }}</template></em>
            </li>
          </ul>
          <p v-else class="rg-d-empty">无</p>

          <button class="rg-d-open" :disabled="!selectedNode.rel" @click="openSelected">
            {{ selectedNode.rel ? '打开文件（或双击节点）' : '该节点无项目文件' }}
          </button>
          <button
            v-if="selectedNode && expandedBaseIds.has(selectedNode.id)"
            class="rg-d-fold"
            @click="toggleGroup('group:' + selectedNode.id)"
          >
            收起这 {{ foldedCountOf(selectedNode.id) }} 个子类
          </button>
        </aside>

        <div v-if="ready && viewNodes.length" class="rg-legend">
          <span><i class="rg-lg rg-lg-class" />命名类</span>
          <span><i class="rg-lg rg-lg-script" />脚本</span>
          <span><i class="rg-lg rg-lg-scene" />场景</span>
          <span><i class="rg-lg rg-lg-ext" />外部基类</span>
          <span class="rg-legend-tip">滚轮缩放 · 拖动平移 · 单击看详情 · 双击打开文件</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.rg-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 26px;
  animation: rg-overlay-in .18s ease-out both;
}
.rg-panel {
  width: min(1280px, 97vw);
  height: min(820px, 92vh);
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 24px 70px rgba(35, 52, 84, 0.16);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: rg-panel-in .22s cubic-bezier(.2,.8,.2,1) both;
}
.rg-top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.rg-title { display: flex; align-items: center; gap: 9px; flex: 1; }
.rg-title h2 { margin: 0; font-size: var(--fs-body); font-weight: 600; }
.rg-stats { font-size: var(--fs-sm); color: var(--text-faint); }
.rg-iconbtn {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  width: 26px; height: 26px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.rg-iconbtn:hover { background: var(--bg-hover); color: var(--text); }
.rg-iconbtn:focus-visible,
.rg-toggle:focus-visible,
.rg-zoom button:focus-visible,
.rg-clear:focus-visible,
.rg-d-x:focus-visible,
.rg-d-open:focus-visible,
.rg-retry:focus-visible,
.rg-d-link:focus-visible {
  outline: 2px solid var(--accent-deep);
  outline-offset: 2px;
}

.rg-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 16px;
  border-bottom: 1px solid var(--border);
}
.rg-search {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  padding: 0 10px;
  height: 30px;
  color: var(--text-faint);
  max-width: 460px;
}
.rg-search:focus-within { border-color: color-mix(in srgb, var(--accent) 40%, transparent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 20%, transparent); }
.rg-search input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text);
  font-size: var(--fs-sm);
  font-family: inherit;
  min-width: 0;
}
.rg-clear {
  font-style: normal;
  font-size: var(--fs-cap);
  color: var(--text-muted);
  cursor: pointer;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
}
.rg-clear:hover { color: var(--text); }
.rg-toggles { display: flex; gap: 6px; }
.rg-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  font-size: var(--fs-sm);
  padding: 3px 10px;
  border-radius: 11px;
  cursor: pointer;
}
.rg-toggle:hover { border-color: var(--border-strong); color: var(--text); }
.rg-toggle { transition: color .16s ease, border-color .16s ease, background .16s ease, opacity .16s ease; }
.rg-toggle:not(.off) { background: color-mix(in srgb, var(--accent) 5%, transparent); border-color: color-mix(in srgb, var(--accent) 33%, transparent); }
.rg-toggle.off { opacity: 0.42; }
.rg-tg-line { width: 14px; height: 0; border-top: 2px solid var(--viz-inherit); display: inline-block; }
.rg-tg-line.mnt { border-top: 2px dashed var(--viz-mount); }
.rg-tg-line.call { border-top: 2px solid var(--viz-call); }
.rg-tg-ext {
  width: 11px; height: 9px;
  border: 1.5px dashed var(--viz-external);
  border-radius: 2px;
  display: inline-block;
}
.rg-zoom { margin-left: auto; display: flex; gap: 4px; }
.rg-zoom button {
  min-width: 26px; height: 24px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  border-radius: 4px;
  font-size: var(--fs-sm);
  cursor: pointer;
  padding: 0 7px;
}
.rg-zoom button:hover { color: var(--text); border-color: var(--border-strong); }
.rg-zoom button { transition: color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease; }
.rg-zoom button:active { transform: translateY(1px); }

.rg-canvas-wrap { flex: 1; position: relative; overflow: hidden; background: var(--bg); }
.rg-svg {
  width: 100%;
  height: 100%;
  display: block;
  touch-action: none;
  cursor: grab;
  background-image:
    radial-gradient(circle, rgba(125, 133, 144, 0.10) 1px, transparent 1px);
  background-size: 26px 26px;
}
.rg-svg.grabbing { cursor: grabbing; }

/* 这批线色是照深色底调的，纸底实测 2.2–2.4:1。统一走 theme.ts cat() 同一档：
   色相留 55%、其余混向本皮肤 --ink，浅色皮肤压暗、深色皮肤提亮，一个比例两边都好。
   边标签是 8px 小字（要 4.5:1），所以它走 regionTextColor 那档 36%，别混用。 */
.rg-line { fill: none; stroke-width: 1.3; }
.rg-line.inherits { stroke: var(--viz-inherit); }
.rg-line.mounts { stroke: var(--viz-mount); stroke-dasharray: 6 4; }
.rg-line.calls { stroke: var(--viz-call); }
/* 交叉连线（多继承/又被调用）：虚线 + 弱一点，一眼看出"这条不是主干" */
.rg-edge-g.cross .rg-line { stroke-dasharray: 3 3; opacity: .6; }
.rg-edge-g.dim { opacity: 0.10; }
.rg-elabel-bg-inh { fill: var(--bg); stroke: var(--viz-inherit); stroke-width: 0.8; }
.rg-elabel-bg-mnt { fill: var(--bg); stroke: var(--viz-mount); stroke-width: 0.8; }
.rg-elabel-bg-call { fill: var(--bg); stroke: var(--viz-call); stroke-width: 0.8; }
.rg-elabel-tx { font-size: var(--fs-cap); }
.rg-elabel-tx.inherits { fill: var(--viz-inherit-t); }
.rg-elabel-tx.mounts { fill: var(--viz-mount-t); }
.rg-elabel-tx.calls { fill: var(--viz-call-t); }

.rg-node { cursor: pointer; }
.rg-node rect { stroke-width: 1.3; transition: stroke-width .14s ease, filter .14s ease, opacity .14s ease; }
.rg-node:hover rect { stroke-width: 2; filter: brightness(1.08); }
.rg-node.selected rect { stroke-width: 2.4; filter: drop-shadow(0 2px 6px color-mix(in srgb, var(--accent) 35%, transparent)); }
.rg-node.dim { opacity: 0.13; }

/* 分组卡：大扇出折叠后的替代物。
   描边用中性色 + 虚线 = "这不是一个类，是一堆被折叠起来的类"。
   字色走 --text-muted（次级信息），不能跟普通节点卡一样用主文字色 —— 它不是主信息。

   填充用 --bg-raised（不是 color-mix 调深）：SVG 的 fill 是**绘制属性**，
   画布对比度门禁不算它 —— 描边是跟**画布点阵底**比对的，
   门禁会把纯底板色（等于表面令牌）排除，把调深过的中间色当"看不见的图形"报出来。
   描边本身走 theme.ts 的 'group' 节点样式（cat() 派生），三套皮肤都过 3:1。 */
.rg-group-rect {
  fill: var(--bg-raised);
  stroke-width: 1.3;
  stroke-dasharray: 5 3;
}
.rg-node.group:hover .rg-group-rect { stroke-width: 2; }
.rg-node.group.selected .rg-group-rect {
  stroke-width: 2.4;
  stroke: color-mix(in srgb, var(--accent) 55%, var(--border-graphic));
}
.rg-group-mark {
  font-family: var(--font-mono);
  font-size: var(--fs-md);
  font-weight: 700;
  text-anchor: middle;
  fill: var(--text-muted);
}
.rg-group-label {
  font-size: var(--fs-cap);
  font-family: var(--font-mono);
  fill: var(--text-muted);
}
.rg-d-mark-group { color: var(--text-muted); }

/* 展开中的基类上的"收起"角标。
   角标是控件边界：底用 --bg-raised（纯底板，不参与 1.4.11），
   字用 --text-muted（在纸底上 ≥4.5:1），描边用主题层的 group 描边档。 */
.rg-fold { cursor: pointer; }
.rg-fold rect {
  fill: var(--bg-raised);
  stroke: var(--border-strong);
  stroke-width: 0.8;
}
.rg-fold text {
  font-family: var(--font-mono);
  font-size: var(--fs-micro);
  fill: var(--text-muted);
}
.rg-fold:hover rect { stroke: var(--border-strong); filter: brightness(1.04); }
/* 角标与标签以前用 :fill 画成分类色（#d6e7ff 那批浅色在纸底上等于隐形）。
   现在色相由卡片描边+底色说，字回归中性。 */
.rg-mark { font-family: var(--font-mono); font-size: var(--fs-cap); font-weight: 700; text-anchor: middle; fill: var(--text-muted); }
.rg-label { font-size: var(--fs-sm); font-weight: 600; fill: var(--text); }
/* 节点卡带着分区色相的淡底，副标压在上面。用 --text-faint（最弱一档）在种子的
   浅色卡上只有 4.27:1，棘轮当场报出来。--text-muted 才是"次级信息"该用的那一档
   （paper/graphite/zinc 的 --text-faint 本来就等于 --text-muted，所以那三套零变化）。 */
.rg-sub { font-size: var(--fs-cap); fill: var(--text-muted); font-family: var(--font-mono); }
.rg-doc { font-size: var(--fs-cap); fill: var(--text-muted); }

/* 节点详情卡 */
.rg-detail {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 300px;
  max-height: calc(100% - 24px);
  overflow-y: auto;
  background: color-mix(in srgb, var(--bg-raised) 92%, transparent);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  box-shadow: 0 10px 30px rgba(35, 52, 84, 0.16);
  padding: 11px 13px;
  font-size: var(--fs-sm);
  z-index: 5;
}
.rg-d-head { display: flex; align-items: center; gap: 8px; }
.rg-d-mark { font-family: var(--font-mono); font-weight: 700; font-size: var(--fs-md); }
.rg-d-title { flex: 1 1 auto; font-size: var(--fs-md); font-weight: 600; word-break: break-all; }
.rg-d-x {
  border: none; background: none; font-size: var(--fs-lead); line-height: 1;
  color: var(--text-faint); cursor: pointer; padding: 0 2px;
}
.rg-d-x:hover { color: var(--text); }
.rg-d-tags { display: flex; flex-wrap: wrap; gap: 5px; margin: 7px 0; }
.rg-d-tags span {
  font-size: var(--fs-cap); border-radius: 4px; padding: 1px 7px;
  border: 1px solid var(--border); color: var(--text-muted); background: var(--bg);
  display: inline-flex; align-items: center; gap: 4px;
}
.rg-d-kind { color: var(--viz-inherit-t) !important; border-color: var(--viz-inherit-a) !important; }
.rg-d-region i { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.rg-d-ext { color: var(--viz-mount-t) !important; border-color: var(--viz-mount-a) !important; }
.rg-d-doc { margin: 4px 0 6px; color: var(--text); line-height: 1.5; }
.rg-d-rel { margin: 0 0 6px; word-break: break-all; }
.rg-d-path { font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-muted); }
.rg-d-line { font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--accent); }
.rg-d-faint { color: var(--text-faint); }
.rg-d-sec { font-size: var(--fs-cap); font-weight: 600; color: var(--text-faint); margin: 8px 0 3px; }
.rg-d-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.rg-d-list li { display: flex; align-items: baseline; gap: 6px; font-size: var(--fs-cap); }
.rg-d-arrow { font-style: normal; font-family: var(--font-mono); width: 14px; text-align: center; flex: 0 0 auto; }
.rg-d-arrow.inherits { color: var(--viz-inherit); }
.rg-d-arrow.mounts { color: var(--viz-mount); }
.rg-d-arrow.calls { color: var(--viz-call); }
.rg-d-link { color: var(--accent); cursor: pointer; word-break: break-all; }
.rg-d-link:hover { text-decoration: underline; }
.rg-d-list em { font-style: normal; color: var(--text-faint); font-size: var(--fs-cap); }
.rg-d-empty { margin: 0 0 2px; font-size: var(--fs-cap); color: var(--text-faint); }
.rg-d-open {
  width: 100%; margin-top: 10px;
  border: 1px solid var(--accent-deep); background: var(--accent); color: #fff;
  border-radius: 6px; padding: 7px 10px; font-size: var(--fs-sm); cursor: pointer;
}
.rg-d-open:disabled { opacity: .5; cursor: not-allowed; }
.rg-d-open:not(:disabled):hover { filter: brightness(1.06); transform: translateY(-1px); }
.rg-d-open { transition: filter .16s ease, transform .16s ease; }
/* 详情面板里的"收起这批子类"：次级动作，不能跟"打开文件"抢主按钮 */
.rg-d-fold {
  width: 100%; margin-top: 6px;
  border: 1px solid var(--border); background: transparent; color: var(--text-muted);
  border-radius: 6px; padding: 6px 10px; font-size: var(--fs-sm); cursor: pointer;
}
.rg-d-fold:hover { color: var(--text); border-color: var(--border-strong); }

.rg-state {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-faint);
  font-size: var(--fs-sm);
}
.rg-error { color: var(--danger); }
.rg-retry { border: 1px solid var(--border-strong); background: transparent; color: var(--text); border-radius: 5px; padding: 5px 14px; cursor: pointer; }

.rg-legend {
  position: absolute;
  left: 14px;
  bottom: 12px;
  display: flex;
  align-items: center;
  gap: 14px;
  background: color-mix(in srgb, var(--bg-raised) 92%, transparent);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: var(--fs-cap);
  color: var(--text-muted);
  pointer-events: none;
}
.rg-legend span { display: inline-flex; align-items: center; gap: 5px; }
.rg-lg { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
/* 图例必须和画布用同一个派生式，否则"钥匙"对不上"锁"：
   以前命名类的图例是 var(--accent)，节点描边却是 #58a6ff，两套浅色皮肤下还各自不达标。 */
.rg-lg-class { background: var(--viz-class); border: 1px solid var(--viz-class); }
.rg-lg-script { background: var(--viz-script); border: 1px solid var(--viz-script); }
.rg-lg-scene { background: var(--viz-scene); border: 1px solid var(--viz-scene); }
.rg-lg-ext { background: var(--viz-external-soft); border: 1px dashed var(--viz-external); }
.rg-legend-tip { color: var(--text-faint); }

@keyframes rg-overlay-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes rg-panel-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }

@media (max-width: 900px) {
  .rg-overlay { padding: 14px; }
  .rg-panel { width: 100%; height: min(900px, 94vh); }
  .rg-toolbar { flex-wrap: wrap; }
  .rg-search { max-width: none; flex-basis: 100%; }
  .rg-toggles { min-width: 0; overflow-x: auto; padding-bottom: 2px; }
  .rg-zoom { margin-left: auto; }
  .rg-detail { width: min(300px, calc(100% - 24px)); }
  .rg-legend { max-width: calc(100% - 28px); overflow-x: auto; white-space: nowrap; }
}
@media (max-width: 560px) {
  .rg-overlay { padding: 0; align-items: stretch; }
  .rg-panel { height: 100%; max-height: none; border-radius: 0; border-left: 0; border-right: 0; }
  .rg-top { padding: 10px 12px; }
  .rg-stats { display: none; }
  .rg-toolbar { gap: 7px; padding: 8px 10px; }
  .rg-toggles { order: 2; flex-basis: 100%; }
  .rg-zoom { margin-left: 0; }
  .rg-detail { top: 8px; right: 8px; max-height: calc(100% - 16px); }
  .rg-legend { left: 8px; bottom: 8px; padding: 5px 9px; }
}
@media (prefers-reduced-motion: reduce) {
  .rg-overlay, .rg-panel, .rg-toggle, .rg-zoom button, .rg-node rect, .rg-d-open { animation: none; transition: none; }
}
</style>
