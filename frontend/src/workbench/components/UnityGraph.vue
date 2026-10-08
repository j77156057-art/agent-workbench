<script setup lang="ts">
// P1-2：Unity GUID 引用图。.meta GUID 为节点，.unity/.prefab/.asset 中的
// guid 引用为有向边；项目内解析不到的 guid 聚合为"缺失/外部"节点（默认折叠）。
// 零第三方依赖：手写力导向 + 矩形碰撞，SVG 渲染，滚轮缩放/拖拽平移/节点拖动。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { fsApi } from '../api'
import type { UnityGraphResp, UnityNode, UnityEdge } from '../api'
import { useWorkbench } from '../composables/workbench'

const { unityGraphOpen, closeUnityGraph, jumpToLine } = useWorkbench()

const data = ref<UnityGraphResp | null>(null)
const loading = ref(false)
const error = ref('')
const ready = ref(false)
const query = ref('')
const showMissing = ref(false)
const hiddenKinds = ref<Set<string>>(new Set())
const selectedId = ref('')
const copied = ref(false)
const svgEl = ref<SVGSVGElement | null>(null)

// 资产类型配色（fill 透明底 + stroke 实色），missing 为红虚线
const KIND_STYLE: Record<string, { stroke: string; mark: string; label: string }> = {
  scene: { stroke: '#f0883e', mark: 'S', label: '场景' },
  prefab: { stroke: '#d2a8ff', mark: 'P', label: '预制体' },
  script: { stroke: '#2ec4b6', mark: 'C', label: '脚本' },
  asset: { stroke: '#8b97a7', mark: 'A', label: '资产' },
  material: { stroke: '#58a6ff', mark: 'M', label: '材质' },
  texture: { stroke: '#7ee787', mark: 'T', label: '贴图' },
  audio: { stroke: '#f2cc60', mark: '♪', label: '音频' },
  animation: { stroke: '#ffa657', mark: 'N', label: '动画' },
  animator: { stroke: '#ff9ce2', mark: 'R', label: '状态机' },
  model: { stroke: '#79c0ff', mark: 'F', label: '模型' },
  shader: { stroke: '#e3b341', mark: 'H', label: '着色器' },
  font: { stroke: '#a5d6ff', mark: 'f', label: '字体' },
  folder: { stroke: '#6e7681', mark: '▸', label: '文件夹' },
  'orphan-meta': { stroke: '#ff7b72', mark: '!', label: '孤儿 meta' },
  missing: { stroke: '#ff7b72', mark: '?', label: '缺失/外部' },
}
function styleOf(kind: string) {
  return KIND_STYLE[kind] || { stroke: '#8b97a7', mark: '•', label: kind }
}
function fillOf(kind: string) {
  return styleOf(kind).stroke + '1f'
}

interface Sim {
  id: string
  node: UnityNode
  x: number; y: number; vx: number; vy: number
  w: number; h: number
}
interface SimEdge { a: Sim; b: Sim; e: UnityEdge }
interface Geo { w: number; h: number; label: string; sub: string }

let sim: Sim[] = []
const byId = new Map<string, Sim>()
let simEdges: SimEdge[] = []
const edgeEls = new Map<string, { line: SVGLineElement }>()
const nodeEls = ref(new Map<string, SVGGElement>())
const geometry = ref<Record<string, Geo>>({})

const zoom = ref(1)
const panX = ref(0)
const panY = ref(0)

let raf = 0
let alpha = 0
let dragSim: Sim | null = null
let panning = false
let panStart = { x: 0, y: 0, px: 0, py: 0 }
let down: { id: string; x: number; y: number; moved: boolean } | null = null

let measureCtx: CanvasRenderingContext2D | null = null

// ------------------------------------------------------------ 数据加载

async function reload() {
  loading.value = true
  error.value = ''
  ready.value = false
  selectedId.value = ''
  cancelRaf()
  try {
    data.value = await fsApi.unityGuidGraph()
    if (!data.value.ok) throw new Error(data.value.error || '构建引用图失败')
    await nextTick()
    await new Promise((r) => setTimeout(r, 30))
    buildLayout()
    ready.value = true
    await nextTick()
    fitView()
    syncDom()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

watch(unityGraphOpen, (open) => {
  if (open) {
    void reload()
    window.addEventListener('keydown', onKey)
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
  } else {
    window.removeEventListener('keydown', onKey)
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
    cancelRaf()
  }
  // immediate：组件在首次打开时才由 App 异步挂载，挂载即 open=true，需立即加载与绑键
}, { immediate: true })

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  cancelRaf()
})

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    if (selectedId.value) selectedId.value = ''
    else closeUnityGraph()
  }
}

// ------------------------------------------------------------ 过滤 / 搜索

const kindChips = computed<{ kind: string; count: number; label: string }[]>(() => {
  const by = data.value?.stats.by_kind || {}
  return Object.keys(by)
    .filter((k) => k !== 'missing')
    .sort((a, b) => by[b] - by[a])
    .map((k) => ({ kind: k, count: by[k], label: styleOf(k).label }))
})

const viewNodes = computed<UnityNode[]>(() => {
  const d = data.value
  if (!d) return []
  return d.nodes.filter((n) => {
    if (n.external && !showMissing.value) return false
    if (hiddenKinds.value.has(n.kind)) return false
    return true
  })
})

const viewEdges = computed<UnityEdge[]>(() => {
  const d = data.value
  if (!d) return []
  const ids = new Set(viewNodes.value.map((n) => n.id))
  return d.edges.filter((e) => ids.has(e.source) && ids.has(e.target))
})

const matched = computed<Set<string>>(() => {
  const out = new Set<string>()
  const q = query.value.trim().toLowerCase()
  if (!q || !data.value) return out
  const words = q.split(/\s+/).filter(Boolean)
  for (const n of data.value.nodes) {
    const hay = `${n.label} ${n.sub} ${n.rel} ${n.guid}`.toLowerCase()
    if (words.every((w) => hay.includes(w))) out.add(n.id)
  }
  for (const e of data.value.edges) {
    if (out.has(e.source)) out.add(e.target)
    if (out.has(e.target)) out.add(e.source)
  }
  return out
})

function nodeDim(n: UnityNode): boolean {
  if (query.value.trim() !== '') return !matched.value.has(n.id)
  if (selectedId.value) return !neighborIds.value.has(n.id) && n.id !== selectedId.value
  return false
}
function edgeDim(e: UnityEdge): boolean {
  if (query.value.trim() !== '') {
    return !matched.value.has(e.source) || !matched.value.has(e.target)
  }
  if (selectedId.value) {
    return e.source !== selectedId.value && e.target !== selectedId.value
  }
  return false
}

function toggleKind(kind: string) {
  const next = new Set(hiddenKinds.value)
  if (next.has(kind)) next.delete(kind)
  else next.add(kind)
  hiddenKinds.value = next
  rebuild()
}

// ------------------------------------------------------------ 选中 / 侧栏

const selectedNode = computed<UnityNode | null>(
  () => data.value?.nodes.find((n) => n.id === selectedId.value) || null,
)

const neighborIds = computed<Set<string>>(() => {
  const out = new Set<string>()
  if (!selectedId.value || !data.value) return out
  out.add(selectedId.value)
  for (const e of data.value.edges) {
    if (e.source === selectedId.value) out.add(e.target)
    if (e.target === selectedId.value) out.add(e.source)
  }
  return out
})

interface RefRow { node: UnityNode; edge: UnityEdge }
const outRefs = computed<RefRow[]>(() => {
  if (!data.value || !selectedId.value) return []
  const rows: RefRow[] = []
  for (const e of data.value.edges) {
    if (e.source !== selectedId.value) continue
    const node = data.value.nodes.find((n) => n.id === e.target)
    if (node) rows.push({ node, edge: e })
  }
  return rows.sort((a, b) => b.edge.count - a.edge.count)
})
const inRefs = computed<RefRow[]>(() => {
  if (!data.value || !selectedId.value) return []
  const rows: RefRow[] = []
  for (const e of data.value.edges) {
    if (e.target !== selectedId.value) continue
    const node = data.value.nodes.find((n) => n.id === e.source)
    if (node) rows.push({ node, edge: e })
  }
  return rows.sort((a, b) => b.edge.count - a.edge.count)
})

function selectNode(id: string, pan = false) {
  selectedId.value = id
  if (pan) centerOn(id)
}

async function onNodeDblClick(n: UnityNode) {
  if (!n.rel) return
  selectedId.value = n.id
  await openSelected()
}

function centerOn(id: string) {
  const s = byId.get(id)
  const svg = svgEl.value
  if (!s || !svg) return
  panX.value = (svg.clientWidth || 900) / 2 - s.x * zoom.value
  panY.value = (svg.clientHeight || 600) / 2 - s.y * zoom.value
}

async function openSelected() {
  const n = selectedNode.value
  if (n && n.rel) {
    closeUnityGraph()
    await jumpToLine(n.rel, 1)
  }
}

async function copyGuid() {
  const n = selectedNode.value
  if (!n) return
  try {
    await navigator.clipboard.writeText(n.guid)
    copied.value = true
    setTimeout(() => (copied.value = false), 1200)
  } catch {
    /* 剪贴板不可用就静默 */
  }
}

watch(showMissing, () => rebuild())

function rebuild() {
  if (!data.value || !ready.value) return
  buildLayout()
  void nextTick(() => syncDom())
}

// ------------------------------------------------------------ 布局

function measure(text: string, size: number, bold: boolean): number {
  if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d')
  if (!measureCtx) return text.length * size * 0.62
  measureCtx.font = `${bold ? '600 ' : ''}${size}px "Segoe UI", "Microsoft YaHei", sans-serif`
  return measureCtx.measureText(text).width
}

function fitText(text: string, maxW: number, size: number, bold: boolean): string {
  if (measure(text, size, bold) <= maxW) return text
  let t = text
  while (t.length > 1 && measure(t + '…', size, bold) > maxW) t = t.slice(0, -1)
  return t + '…'
}

function edgeKey(e: UnityEdge): string {
  return `${e.source}>${e.target}`
}

function buildLayout() {
  const nodes = viewNodes.value
  const edges = viewEdges.value
  const dense = nodes.length > 350

  const prev = new Map(sim.map((s) => [s.id, s]))
  sim = []
  byId.clear()
  simEdges = []
  const geo: Record<string, Geo> = {}

  nodes.forEach((n, i) => {
    const label = fitText(n.label, dense ? 150 : 200, 12, true)
    const subRaw = n.external ? '' : n.sub
    const sub = !dense && subRaw ? fitText(subRaw, 200, 9.5, false) : ''
    const labelW = measure(label, 12, true)
    const subW = sub ? measure(sub, 9.5, false) : 0
    const w = Math.max(dense ? 70 : 92, Math.min(dense ? 170 : 240, Math.max(labelW, subW) + 38))
    const h = sub ? 46 : dense ? 26 : 32
    geo[n.id] = { w, h, label, sub }

    const old = prev.get(n.id)
    let x: number, y: number
    if (old) {
      x = old.x
      y = old.y
    } else {
      const ring = n.external ? 2 : 1
      const ang = (i / Math.max(1, nodes.length)) * Math.PI * 2
      const r = (90 + nodes.length * 8) * ring
      x = Math.cos(ang) * r
      y = Math.sin(ang) * r * 0.62
    }
    const s: Sim = { id: n.id, node: n, x, y, vx: 0, vy: 0, w, h }
    sim.push(s)
    byId.set(n.id, s)
  })

  for (const e of edges) {
    const a = byId.get(e.source)
    const b = byId.get(e.target)
    if (a && b) simEdges.push({ a, b, e })
  }

  const iters = sim.length > 900 ? 45 : sim.length > 400 ? 85 : 220
  for (let i = 0; i < iters; i++) step(1)
  geometry.value = geo
  fitView()
}

function step(a: number) {
  const REP = 46000
  const SPRING = 0.05
  const IDEAL = 140
  const n = sim.length

  for (let i = 0; i < n; i++) {
    const s = sim[i]
    for (let j = i + 1; j < n; j++) {
      const t = sim[j]
      let dx = s.x - t.x
      let dy = s.y - t.y
      let d2 = dx * dx + dy * dy
      if (d2 < 0.01) {
        dx = (Math.random() - 0.5) * 2
        dy = (Math.random() - 0.5) * 2
        d2 = dx * dx + dy * dy
      }
      const d = Math.sqrt(d2)
      const f = (REP / d2) * a
      const ux = dx / d
      const uy = dy / d
      s.vx += ux * f
      s.vy += uy * f
      t.vx -= ux * f
      t.vy -= uy * f
      const minD = (s.w + t.w) / 4 + 10
      if (d < minD) {
        const push = (minD - d) * 0.5 * a
        s.vx += ux * push
        s.vy += uy * push
        t.vx -= ux * push
        t.vy -= uy * push
      }
    }
    s.vx -= s.x * 0.012
    s.vy -= s.y * 0.012
  }

  for (const { a: sa, b: sb } of simEdges) {
    const dx = sb.x - sa.x
    const dy = sb.y - sa.y
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01
    const f = (d - IDEAL) * SPRING * (0.25 + 0.75 * a)
    const ux = dx / d
    const uy = dy / d
    sa.vx += ux * f
    sa.vy += uy * f
    sb.vx -= ux * f
    sb.vy -= uy * f
  }

  for (const s of sim) {
    if (s === dragSim) {
      s.vx = 0
      s.vy = 0
      continue
    }
    s.vx *= 0.82
    s.vy *= 0.82
    const cap = 26
    const v = Math.hypot(s.vx, s.vy)
    if (v > cap) {
      s.vx = (s.vx / v) * cap
      s.vy = (s.vy / v) * cap
    }
    s.x += s.vx
    s.y += s.vy
  }
}

function reheat(v = 0.6) {
  alpha = Math.max(alpha, v)
  if (!raf && ready.value) raf = requestAnimationFrame(loop)
}

function loop() {
  if (alpha <= 0.02 && !dragSim) {
    raf = 0
    syncDom()
    return
  }
  step(alpha)
  alpha *= 0.96
  syncDom()
  raf = requestAnimationFrame(loop)
}

function cancelRaf() {
  if (raf) cancelAnimationFrame(raf)
  raf = 0
  alpha = 0
}

function endpoints(sa: Sim, sb: Sim) {
  const dx = sb.x - sa.x
  const dy = sb.y - sa.y
  const d = Math.hypot(dx, dy) || 0.01
  const ux = dx / d
  const uy = dy / d
  const ta = Math.min(sa.w / 2 / Math.abs(ux || 1e-6), sa.h / 2 / Math.abs(uy || 1e-6))
  const tb = Math.min(sb.w / 2 / Math.abs(ux || 1e-6), sb.h / 2 / Math.abs(uy || 1e-6))
  return {
    x1: sa.x + ux * ta,
    y1: sa.y + uy * ta,
    x2: sb.x - ux * tb,
    y2: sb.y - uy * tb,
  }
}

function syncDom() {
  for (const se of simEdges) {
    const els = edgeEls.get(edgeKey(se.e))
    if (!els) continue
    const p = endpoints(se.a, se.b)
    els.line.setAttribute('x1', String(p.x1))
    els.line.setAttribute('y1', String(p.y1))
    els.line.setAttribute('x2', String(p.x2))
    els.line.setAttribute('y2', String(p.y2))
  }
  for (const s of sim) {
    const el = nodeEls.value.get(s.id)
    if (el) el.setAttribute('transform', `translate(${s.x - s.w / 2}, ${s.y - s.h / 2})`)
  }
}

function initialPos(id: string, axis: 'x' | 'y', wh: 'w' | 'h', half: boolean): number {
  const s = byId.get(id)
  if (!s) return 0
  return axis === 'x' ? s.x - (half ? s[wh] / 2 : 0) : s.y - (half ? s[wh] / 2 : 0)
}
function edgePos(e: UnityEdge, end: 'x1' | 'y1' | 'x2' | 'y2'): number {
  const a = byId.get(e.source)
  const b = byId.get(e.target)
  if (!a || !b) return 0
  return endpoints(a, b)[end]
}

// ------------------------------------------------------------ 视口

function fitView() {
  const svg = svgEl.value
  if (!svg || sim.length === 0) {
    zoom.value = 1
    panX.value = 0
    panY.value = 0
    return
  }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const s of sim) {
    minX = Math.min(minX, s.x - s.w / 2)
    maxX = Math.max(maxX, s.x + s.w / 2)
    minY = Math.min(minY, s.y - s.h / 2)
    maxY = Math.max(maxY, s.y + s.h / 2)
  }
  const cw = svg.clientWidth || 900
  const ch = svg.clientHeight || 600
  const bw = Math.max(1, maxX - minX + 80)
  const bh = Math.max(1, maxY - minY + 80)
  const z = Math.min(1.25, Math.max(0.12, Math.min(cw / bw, ch / bh)))
  zoom.value = z
  panX.value = cw / 2 - ((minX + maxX) / 2) * z
  panY.value = ch / 2 - ((minY + maxY) / 2) * z
}

function zoomBy(f: number) {
  const svg = svgEl.value
  if (!svg) return
  applyZoom(f, (svg.clientWidth || 900) / 2, (svg.clientHeight || 600) / 2)
}

function applyZoom(f: number, cx: number, cy: number) {
  const nz = Math.min(2.5, Math.max(0.12, zoom.value * f))
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

function setNodeRef(el: Element | unknown, id: string) {
  if (el instanceof SVGGElement) nodeEls.value.set(id, el)
  else nodeEls.value.delete(id)
}
function setEdgeRef(el: Element | unknown, key: string) {
  if (el instanceof SVGLineElement) edgeEls.set(key, { line: el })
  else edgeEls.delete(key)
}

function onNodeDown(ev: PointerEvent, n: UnityNode) {
  const s = byId.get(n.id)
  if (!s) return
  ev.stopPropagation()
  down = { id: n.id, x: ev.clientX, y: ev.clientY, moved: false }
  dragSim = s
  reheat(0.7)
}

function onBgDown(ev: PointerEvent) {
  if (!ready.value) return
  panning = true
  panStart = { x: ev.clientX, y: ev.clientY, px: panX.value, py: panY.value }
  ;(ev.currentTarget as Element).setPointerCapture?.(ev.pointerId)
}

function onPointerMove(ev: PointerEvent) {
  if (dragSim && down) {
    if (Math.hypot(ev.clientX - down.x, ev.clientY - down.y) > 4) down.moved = true
    const w = toWorld(ev.clientX, ev.clientY)
    dragSim.x = w.x
    dragSim.y = w.y
    dragSim.vx = 0
    dragSim.vy = 0
    reheat(0.5)
  } else if (panning) {
    panX.value = panStart.px + (ev.clientX - panStart.x)
    panY.value = panStart.py + (ev.clientY - panStart.y)
  }
}

function onPointerUp() {
  const clicked = down
  dragSim = null
  panning = false
  down = null
  if (clicked && !clicked.moved) selectNode(clicked.id)
}

const viewport = computed(() => `translate(${panX.value}, ${panY.value}) scale(${zoom.value})`)
const stats = computed(() => data.value?.stats)
const isEmpty = computed(() => ready.value && (stats.value?.assets_total || 0) === 0)
const tooDense = computed(() => viewNodes.value.length > 900)
const externalIds = computed<Set<string>>(
  () => new Set((data.value?.nodes || []).filter((n) => n.external).map((n) => n.id)),
)
</script>

<template>
  <div v-if="unityGraphOpen" class="ug-overlay wb-modal-backdrop" @mousedown.self="closeUnityGraph">
    <section class="ug-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="Unity GUID 引用图">
      <header class="ug-top">
        <div class="ug-title">
          <svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">
            <rect x="1.5" y="2" width="5" height="4" rx="1" fill="none" stroke="#f0883e" stroke-width="1.1" />
            <rect x="8.8" y="2" width="5" height="4" rx="1" fill="none" stroke="#2ec4b6" stroke-width="1.1" />
            <rect x="5" y="9.5" width="5" height="4" rx="1" fill="none" stroke="#58a6ff" stroke-width="1.1" />
            <path d="M4.5 4.5 L8.8 3.6 M6 6 L7 9.5 M11 5.8 L9.2 9.5" stroke="#7d90aa" stroke-width="0.85" />
          </svg>
          <h2>Unity 引用图</h2>
          <span v-if="stats" class="ug-stats">
            {{ stats.assets_total }} 个 GUID · {{ stats.resolved_edges }} 条引用边
            <i v-if="stats.missing_edges" class="ug-warn">· {{ stats.missing_edges }} 条指向缺失/外部</i>
            <i v-if="stats.duplicate_guids" class="ug-bad">· {{ stats.duplicate_guids }} 个 GUID 冲突</i>
          </span>
        </div>
        <button class="ug-iconbtn" title="重新分析" @click="reload">
          <svg width="13" height="13" viewBox="0 0 13 13"><path d="M11 6.5 A4.5 4.5 0 1 1 6.5 2 M11 1.4 V4.2 H8.2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <button class="ug-iconbtn" title="关闭（Esc）" @click="closeUnityGraph">
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
        </button>
      </header>

      <div class="ug-toolbar">
        <div class="ug-search">
          <svg width="12" height="12" viewBox="0 0 12 12"><circle cx="5.2" cy="5.2" r="3.4" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M7.8 7.8 L10.5 10.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
          <input v-model="query" type="text" placeholder="搜索资产名 / 路径 / GUID（高亮直接引用关系）" />
          <kbd v-if="query" class="ug-clear" @click="query = ''">清空</kbd>
        </div>
        <div class="ug-chips">
          <button
            v-for="c in kindChips"
            :key="c.kind"
            class="ug-chip"
            :class="{ off: hiddenKinds.has(c.kind) }"
            :style="{ borderColor: styleOf(c.kind).stroke + '88', color: styleOf(c.kind).stroke }"
            :title="`${c.label} ${c.count} 个（点击隐藏/显示）`"
            @click="toggleKind(c.kind)"
          >{{ c.label }} {{ c.count }}</button>
          <button class="ug-chip ug-chip-missing" :class="{ off: !showMissing }"
                  title="项目 Assets 内找不到 .meta 的 guid（可能是 Packages 包资源或断裂引用）"
                  @click="showMissing = !showMissing">
            缺失/外部 {{ stats?.missing_nodes || 0 }}
          </button>
        </div>
        <div class="ug-zoom">
          <button title="放大" @click="zoomBy(1.2)">+</button>
          <button title="缩小" @click="zoomBy(1 / 1.2)">−</button>
          <button title="适应窗口" @click="fitView">适应</button>
        </div>
      </div>

      <div class="ug-body">
        <div class="ug-canvas-wrap">
          <div v-if="loading" class="ug-state"><div class="cv-spinner" /><p>正在扫描 .meta 与序列化资产…</p></div>
          <div v-else-if="error" class="ug-state">
            <p class="ug-error">{{ error }}</p>
            <button class="ug-retry" @click="reload">重试</button>
          </div>
          <div v-else-if="isEmpty" class="ug-state">
            <p class="ug-faint">未发现 Unity 资产：当前代码库的 Assets 目录下没有 .meta 文件。</p>
            <p class="ug-faint ug-hint">如需分析，请把代码库根目录指向 Unity 工程（含 ProjectSettings/ 与 Assets/）。</p>
          </div>

          <svg
            v-show="ready && !isEmpty"
            ref="svgEl"
            class="ug-svg"
            :class="{ grabbing: panning }"
            @pointerdown="onBgDown"
            @wheel="onWheel"
          >
            <defs>
              <marker id="ug-arrow" viewBox="0 0 10 10" refX="8.5" refY="5"
                      markerWidth="8" markerHeight="8" orient="auto-start-reverse">
                <path d="M0 0 L9 5 L0 10 Z" fill="#6e7f96" />
              </marker>
              <marker id="ug-arrow-miss" viewBox="0 0 10 10" refX="8.5" refY="5"
                      markerWidth="8" markerHeight="8" orient="auto-start-reverse">
                <path d="M0 0 L9 5 L0 10 Z" fill="#ff7b72" />
              </marker>
            </defs>

            <g :transform="viewport">
              <g class="ug-edges">
                <line
                  v-for="e in viewEdges"
                  :key="edgeKey(e)"
                  :ref="(el) => setEdgeRef(el, edgeKey(e))"
                  class="ug-line"
                  :class="{ miss: externalIds.has(e.target), dim: edgeDim(e) }"
                  :x1="edgePos(e, 'x1')" :y1="edgePos(e, 'y1')"
                  :x2="edgePos(e, 'x2')" :y2="edgePos(e, 'y2')"
                  :marker-end="externalIds.has(e.target) ? 'url(#ug-arrow-miss)' : 'url(#ug-arrow)'"
                />
              </g>

              <g class="ug-nodes">
                <g
                  v-for="n in viewNodes"
                  :key="n.id"
                  :ref="(el) => setNodeRef(el, n.id)"
                  class="ug-node"
                  :class="{ ext: n.external, dim: nodeDim(n), sel: n.id === selectedId }"
                  :transform="`translate(${initialPos(n.id, 'x', 'w', true)}, ${initialPos(n.id, 'y', 'h', true)})`"
                  @pointerdown="onNodeDown($event, n)"
                  @dblclick="onNodeDblClick(n)"
                >
                  <rect
                    :width="geometry[n.id]?.w || 100"
                    :height="geometry[n.id]?.h || 32"
                    rx="7"
                    :fill="n.external ? 'transparent' : fillOf(n.kind)"
                    :stroke="styleOf(n.kind).stroke"
                    :stroke-dasharray="n.external ? '4 3' : undefined"
                  />
                  <text class="ug-mark" :fill="styleOf(n.kind).stroke" x="13"
                        :y="(geometry[n.id]?.h || 32) / 2 + 3.6">
                    {{ styleOf(n.kind).mark }}
                  </text>
                  <text class="ug-label" :fill="styleOf(n.kind).stroke" x="26"
                        :y="geometry[n.id]?.sub ? 17 : ((geometry[n.id]?.h || 32) / 2 + 4)">
                    {{ geometry[n.id]?.label }}
                  </text>
                  <text v-if="geometry[n.id]?.sub" class="ug-sub" x="26"
                        :y="(geometry[n.id]?.h || 32) - 9">
                    {{ geometry[n.id]?.sub }}
                  </text>
                  <title>{{ n.rel || n.label }}{{ n.doc ? `\n${n.doc}` : '' }}\n{{ n.guid }}</title>
                </g>
              </g>
            </g>
          </svg>

          <div v-if="ready && viewNodes.length" class="ug-legend">
            <span class="ug-legend-tip">
              滚轮缩放 · 拖动平移 · 点节点看引用关系 · 双击资产节点打开文件
            </span>
            <span v-if="tooDense" class="ug-legend-warn">节点较多（&gt;900），已切换紧凑布局</span>
          </div>
        </div>

        <aside v-if="selectedNode" class="ug-detail">
          <div class="ug-d-head">
            <span class="ug-d-badge" :style="{ color: styleOf(selectedNode.kind).stroke, borderColor: styleOf(selectedNode.kind).stroke + '88' }">
              {{ styleOf(selectedNode.kind).label }}
            </span>
            <button class="ug-iconbtn" title="关闭详情" @click="selectedId = ''">
              <svg width="11" height="11" viewBox="0 0 11 11"><path d="M2.2 2.2 L8.8 8.8 M8.8 2.2 L2.2 8.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
            </button>
          </div>
          <h3 class="ug-d-name" :title="selectedNode.label">{{ selectedNode.label }}</h3>
          <p v-if="selectedNode.rel" class="ug-d-rel" :title="selectedNode.rel">{{ selectedNode.sub }}/{{ selectedNode.label }}</p>
          <p v-else class="ug-d-rel ug-d-miss-note">{{ selectedNode.sub }}</p>

          <div class="ug-d-guid">
            <code>{{ selectedNode.guid }}</code>
            <button class="ug-d-mini" @click="copyGuid">{{ copied ? '已复制' : '复制' }}</button>
          </div>
          <button v-if="selectedNode.rel" class="ug-d-open" @click="openSelected">在工作台打开此文件</button>

          <div class="ug-d-section">
            <h4>引用了 {{ outRefs.length }} 个资产</h4>
            <ul>
              <li v-for="r in outRefs" :key="r.edge.target"
                  :class="{ miss: r.node.external }"
                  @click="selectNode(r.node.id, true)">
                <i class="ug-d-dot" :style="{ background: styleOf(r.node.kind).stroke }" />
                <span class="ug-d-li-label">{{ r.node.label }}</span>
                <em v-if="r.edge.count > 1">×{{ r.edge.count }}</em>
              </li>
              <li v-if="!outRefs.length" class="ug-d-empty">无 outgoing 引用</li>
            </ul>
          </div>
          <div class="ug-d-section">
            <h4>被 {{ inRefs.length }} 个资产引用</h4>
            <ul>
              <li v-for="r in inRefs" :key="r.edge.source"
                  @click="selectNode(r.node.id, true)">
                <i class="ug-d-dot" :style="{ background: styleOf(r.node.kind).stroke }" />
                <span class="ug-d-li-label">{{ r.node.label }}</span>
                <em v-if="r.edge.count > 1">×{{ r.edge.count }}</em>
              </li>
              <li v-if="!inRefs.length" class="ug-d-empty">无 incoming 引用（孤立资产）</li>
            </ul>
          </div>
        </aside>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ug-overlay {
  position: fixed;
  inset: 0;
  background: rgba(38, 52, 77, 0.38);
  backdrop-filter: blur(2px);
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 26px;
  animation: ug-overlay-in .18s ease-out both;
}
.ug-panel {
  width: min(1320px, 97vw);
  height: min(840px, 92vh);
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 24px 70px rgba(35, 52, 84, 0.16);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: ug-panel-in .22s cubic-bezier(.2,.8,.2,1) both;
}
.ug-top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.ug-title { display: flex; align-items: center; gap: 9px; flex: 1; }
.ug-title h2 { margin: 0; font-size: 14px; font-weight: 600; }
.ug-stats { font-size: 12px; color: var(--text-faint); }
.ug-warn { color: #b5791f; font-style: normal; }
.ug-bad { color: #c23a40; font-style: normal; font-weight: 600; }
.ug-iconbtn {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  width: 26px; height: 26px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.ug-iconbtn:hover { background: var(--bg-hover); color: var(--text); }
.ug-iconbtn:focus-visible,
.ug-chip:focus-visible,
.ug-zoom button:focus-visible,
.ug-clear:focus-visible,
.ug-d-mini:focus-visible,
.ug-d-open:focus-visible,
.ug-retry:focus-visible {
  outline: 2px solid #2f6fed88;
  outline-offset: 2px;
}

.ug-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 16px;
  border-bottom: 1px solid var(--border);
}
.ug-search {
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
  max-width: 420px;
}
.ug-search:focus-within { border-color: #2f6fed66; box-shadow: 0 0 0 2px #2f6fed18; }
.ug-search input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
  min-width: 0;
}
.ug-clear {
  font-style: normal;
  font-size: 10px;
  color: var(--text-muted);
  cursor: pointer;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
}
.ug-clear:hover { color: var(--text); }
.ug-chips { flex: 1; display: flex; gap: 5px; flex-wrap: wrap; }
.ug-chip {
  border: 1px solid var(--border);
  background: transparent;
  font-size: 11px;
  padding: 2px 9px;
  border-radius: 11px;
  cursor: pointer;
  opacity: 0.92;
}
.ug-chip:hover { filter: brightness(1.25); }
.ug-chip.off { opacity: 0.3; }
.ug-chip { transition: opacity .16s ease, filter .16s ease, background .16s ease, border-color .16s ease; }
.ug-chip:not(.off) { background: #2f6fed0d; }
.ug-chip-missing { color: #c23a40 !important; border-color: #c23a4066 !important; }
.ug-zoom { display: flex; gap: 4px; }
.ug-zoom button {
  min-width: 26px; height: 24px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
  padding: 0 7px;
}
.ug-zoom button:hover { color: var(--text); border-color: var(--border-strong); }
.ug-zoom button { transition: color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease; }
.ug-zoom button:active { transform: translateY(1px); }

.ug-body { flex: 1; display: flex; min-height: 0; }
.ug-canvas-wrap { flex: 1; position: relative; overflow: hidden; background: var(--bg); min-width: 0; }
.ug-svg {
  width: 100%;
  height: 100%;
  display: block;
  touch-action: none;
  cursor: grab;
  background-image: radial-gradient(circle, rgba(125, 133, 144, 0.10) 1px, transparent 1px);
  background-size: 26px 26px;
}
.ug-svg.grabbing { cursor: grabbing; }

.ug-line {
  fill: none;
  stroke: #93a0b2;
  stroke-width: 1.15;
}
.ug-line.miss { stroke: #e0484f; stroke-dasharray: 5 4; }
.ug-line.dim { opacity: 0.08; }

.ug-node { cursor: pointer; }
.ug-node rect { stroke-width: 1.3; transition: stroke-width .14s ease, filter .14s ease, opacity .14s ease; }
.ug-node:hover rect { stroke-width: 2; filter: brightness(1.25); }
.ug-node.sel rect { stroke-width: 2.4; }
.ug-node.dim { opacity: 0.12; }
.ug-mark { font-family: var(--font-mono); font-size: 10px; font-weight: 700; text-anchor: middle; }
.ug-label { font-size: 12px; font-weight: 600; }
.ug-sub { font-size: 10px; fill: var(--text-faint); font-family: var(--font-mono); }

.ug-state {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-faint);
  font-size: 12px;
  text-align: center;
  padding: 0 40px;
}
.ug-error { color: #c23a40; }
.ug-hint { font-size: 12px; }
.ug-retry { border: 1px solid var(--border-strong); background: transparent; color: var(--text); border-radius: 5px; padding: 5px 14px; cursor: pointer; }

.ug-legend {
  position: absolute;
  left: 14px;
  bottom: 12px;
  display: flex;
  align-items: center;
  gap: 14px;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 10px;
  color: var(--text-muted);
  pointer-events: none;
}
.ug-legend-warn { color: #b5791f; }

/* ------------------------------------------------------------ 详情侧栏 */
.ug-detail {
  flex: 0 0 286px;
  border-left: 1px solid var(--border);
  background: var(--bg-raised);
  padding: 14px;
  overflow-y: auto;
}
.ug-d-head { display: flex; justify-content: space-between; align-items: center; }
.ug-d-badge {
  font-size: 10px;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  padding: 1px 9px;
}
.ug-d-name { margin: 10px 0 4px; font-size: 14px; word-break: break-all; }
.ug-d-rel {
  margin: 0 0 10px;
  font-size: 10px;
  font-family: var(--font-mono);
  color: var(--text-faint);
  word-break: break-all;
}
.ug-d-miss-note { font-family: inherit; line-height: 1.6; }
.ug-d-guid {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 5px 8px;
  margin-bottom: 10px;
}
.ug-d-guid code {
  flex: 1;
  font-size: 10px;
  color: var(--text-muted);
  word-break: break-all;
}
.ug-d-mini {
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  font-size: 10px;
  border-radius: 4px;
  padding: 1px 8px;
  cursor: pointer;
  flex: 0 0 auto;
}
.ug-d-mini:hover { color: var(--text); border-color: var(--border-strong); }
.ug-d-mini { transition: color .16s ease, border-color .16s ease, background .16s ease; }
.ug-d-open {
  width: 100%;
  border: 1px solid var(--border-strong);
  background: transparent;
  color: var(--text);
  font-size: 12px;
  border-radius: 5px;
  padding: 6px 0;
  cursor: pointer;
  margin-bottom: 14px;
}
.ug-d-open:hover { background: var(--bg-hover); }
.ug-d-open { transition: background .16s ease, border-color .16s ease, transform .16s ease; }
.ug-d-open:active { transform: translateY(1px); }
.ug-d-section h4 {
  margin: 12px 0 6px;
  font-size: 11px;
  color: var(--text-muted);
  font-weight: 600;
}
.ug-d-section ul { list-style: none; margin: 0; padding: 0; }
.ug-d-section li {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  padding: 4px 6px;
  border-radius: 4px;
  cursor: pointer;
}
.ug-d-section li:hover { background: var(--bg-hover); }
.ug-d-section li.miss .ug-d-li-label { color: #c23a40; }
.ug-d-dot { width: 8px; height: 8px; border-radius: 2px; flex: 0 0 auto; }
.ug-d-li-label {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ug-d-section em {
  font-style: normal;
  font-size: 10px;
  color: var(--text-faint);
}
.ug-d-empty { color: var(--text-faint) !important; cursor: default !important; font-size: 10px; }
.ug-d-empty:hover { background: transparent !important; }

@keyframes ug-overlay-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes ug-panel-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }

@media (max-width: 900px) {
  .ug-overlay { padding: 14px; }
  .ug-panel { width: 100%; height: min(900px, 94vh); }
  .ug-toolbar { flex-wrap: wrap; }
  .ug-search { max-width: none; flex-basis: 100%; }
  .ug-chips { order: 2; min-width: 0; overflow-x: auto; flex-wrap: nowrap; padding-bottom: 2px; }
  .ug-zoom { margin-left: auto; }
  .ug-detail { flex-basis: 250px; }
}
@media (max-width: 620px) {
  .ug-overlay { padding: 0; align-items: stretch; }
  .ug-panel { height: 100%; border-radius: 0; border-left: 0; border-right: 0; }
  .ug-top { padding: 10px 12px; }
  .ug-stats { display: none; }
  .ug-toolbar { gap: 7px; padding: 8px 10px; }
  .ug-zoom { margin-left: 0; }
  .ug-body { flex-direction: column; }
  .ug-canvas-wrap { min-height: 44vh; }
  .ug-detail { flex: 0 0 auto; max-height: 40vh; border-left: 0; border-top: 1px solid var(--border); padding: 10px 12px; }
  .ug-d-open { margin-bottom: 8px; }
  .ug-legend { left: 8px; bottom: 8px; max-width: calc(100% - 16px); overflow-x: auto; white-space: nowrap; padding: 5px 9px; }
}
@media (prefers-reduced-motion: reduce) {
  .ug-overlay, .ug-panel, .ug-chip, .ug-zoom button, .ug-node rect, .ug-d-mini, .ug-d-open { animation: none; transition: none; }
}
</style>
