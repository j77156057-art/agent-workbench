<script setup lang="ts">
// P1：关系图。全项目类/脚本/场景节点 + 三类有向边：
//   inherits（子 → 父）、mounts（场景 → 挂载脚本）、calls（项目内调用）。
// 零第三方依赖：手写力导向布局（预迭代收敛 + rAF 余温），SVG 渲染，滚轮缩放/拖拽平移/节点拖动。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { fsApi } from '../api'
import type { RelationGraphResp, RelationNode, RelationEdge } from '../api'
import { useWorkbench } from '../composables/workbench'
import { graphNodeStyle, regionColor } from '../theme'

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
const svgEl = ref<SVGSVGElement | null>(null)

interface Sim {
  id: string
  node: RelationNode
  x: number; y: number; vx: number; vy: number
  w: number; h: number
}
interface SimEdge { a: Sim; b: Sim; e: RelationEdge }
interface Geo { w: number; h: number; label: string; sub: string; doc: string }

let sim: Sim[] = []
const byId = new Map<string, Sim>()
let simEdges: SimEdge[] = []
const edgeEls = new Map<string, { line: SVGLineElement; label: SVGGElement }>()
const geometry = ref<Record<string, Geo>>({})

// 视口变换（唯一一个每帧响应式更新的绑定，节点/边坐标全部命令式写 DOM）
const zoom = ref(1)
const panX = ref(0)
const panY = ref(0)

let raf = 0
let alpha = 0
let dragSim: Sim | null = null
let panning = false
let panStart = { x: 0, y: 0, px: 0, py: 0 }
let down: { id: string; x: number; y: number; moved: boolean } | null = null
let bgDown: { x: number; y: number; moved: boolean } | null = null
const selectedId = ref<string | null>(null)

// ------------------------------------------------------------ 数据加载

async function reload() {
  loading.value = true
  error.value = ''
  ready.value = false
  cancelRaf()
  try {
    data.value = await fsApi.relationGraph()
    // 让 loading 态先绘制一帧，避免预迭代布局卡住 spinner
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
  if (e.key === 'Escape') closeRelationGraph()
}

// 切换过滤项：保留旧坐标重建
watch([showExternal, showInherits, showMounts, showCalls], () => {
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

const matched = computed<Set<string>>(() => {
  const out = new Set<string>()
  const q = query.value.trim().toLowerCase()
  if (!q || !data.value) return out
  const words = q.split(/\s+/).filter(Boolean)
  for (const n of data.value.nodes) {
    const hay = `${n.label} ${n.sub} ${n.rel} ${n.doc} ${n.region_name}`.toLowerCase()
    if (words.every((w) => hay.includes(w))) out.add(n.id)
  }
  // 直接邻居也点亮
  for (const e of data.value.edges) {
    if (out.has(e.source)) out.add(e.target)
    if (out.has(e.target)) out.add(e.source)
  }
  return out
})

function nodeDim(n: RelationNode): boolean {
  return query.value.trim() !== '' && !matched.value.has(n.id)
}
function edgeDim(e: RelationEdge): boolean {
  if (query.value.trim() === '') return false
  return !matched.value.has(e.source) || !matched.value.has(e.target)
}

// ------------------------------------------------------------ 布局

let measureCtx: CanvasRenderingContext2D | null = null

function measure(text: string, size: number, bold: boolean): number {
  if (!measureCtx) {
    measureCtx = document.createElement('canvas').getContext('2d')
  }
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

function edgeKey(e: RelationEdge): string {
  return `${e.kind}|${e.source}>${e.target}`
}

function buildLayout() {
  const nodes = viewNodes.value
  const edges = viewEdges.value

  const prev = new Map(sim.map((s) => [s.id, s]))
  sim = []
  byId.clear()
  simEdges = []
  const geo: Record<string, Geo> = {}

  nodes.forEach((n, i) => {
    const st = graphNodeStyle(n.kind)
    const label = fitText(n.label, 220, 12, true)
    const subRaw = n.external ? (n.sub ? `(${n.sub})` : '') : n.sub
    const sub = subRaw ? fitText(subRaw, 220, 9.5, false) : ''
    // 卡片第三行：文档/注释首行，帮用户不打开文件也能认出这个类/场景是干什么的
    const docRaw = (n.doc || '').split(/\r?\n/)[0].trim()
    const doc = !n.external && docRaw && docRaw !== n.label ? fitText(docRaw, 220, 9.5, false) : ''
    const labelW = measure(label, 12, true)
    const subW = sub ? measure(sub, 9.5, false) : 0
    const docW = doc ? measure(doc, 9.5, false) : 0
    const w = Math.max(112, Math.min(264, Math.max(labelW, subW, docW) + 42))
    const h = doc ? 58 : sub ? 46 : 32
    geo[n.id] = { w, h, label, sub, doc }

    const old = prev.get(n.id)
    let x: number, y: number
    if (old) {
      x = old.x
      y = old.y
    } else {
      const ring = n.external ? 2.1 : 1
      const ang = (i / Math.max(1, nodes.length)) * Math.PI * 2
      const r = (120 + nodes.length * 12) * ring
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

  // 同步预迭代（小图毫秒级；大图最多 1000 文件上限内可接受）
  const iters = sim.length > 400 ? 120 : 260
  for (let i = 0; i < iters; i++) step(1)
  geometry.value = geo
  fitView()
}

function step(a: number) {
  const REP = 52000
  const SPRING = 0.05
  const IDEAL_INH = 170
  const IDEAL_MNT = 210
  const IDEAL_CALL = 190
  // 碰撞分离不能随 alpha 一起消失：否则拖动余温结束后节点会失去防重叠保护
  const settle = Math.max(a, 0.35)
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
      // 矩形碰撞：沿 AABB 最小穿透轴做位置级硬分离（保证卡片绝不叠在一起），
      // 另补一点速度让自然布局也保持间隙；正在拖的卡不吃位移，份额全给另一张
      const ox = (s.w + t.w) / 2 + 8 - Math.abs(dx)
      const oy = (s.h + t.h) / 2 + 8 - Math.abs(dy)
      if (ox > 0 && oy > 0) {
        const pen = Math.min(ox, oy)
        let sx = 0, sy = 0
        if (ox < oy) sx = dx >= 0 ? 1 : -1
        else sy = dy >= 0 ? 1 : -1
        const shareA = s === dragSim ? 0 : t === dragSim ? 1 : 0.5
        const shareB = t === dragSim ? 0 : s === dragSim ? 1 : 0.5
        s.x += sx * pen * shareA
        s.y += sy * pen * shareA
        t.x -= sx * pen * shareB
        t.y -= sy * pen * shareB
        const vk = 0.15 * settle
        s.vx += sx * pen * vk
        s.vy += sy * pen * vk
        t.vx -= sx * pen * vk
        t.vy -= sy * pen * vk
      }
    }
    // 中心引力必须随 alpha 衰减，否则停止加热后所有节点会被恒力拽回 (0,0) 叠成一堆
    s.vx -= s.x * 0.02 * a
    s.vy -= s.y * 0.02 * a
  }

  for (const { a: sa, b: sb, e } of simEdges) {
    const dx = sb.x - sa.x
    const dy = sb.y - sa.y
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01
    const ideal = e.kind === 'mounts' ? IDEAL_MNT : e.kind === 'calls' ? IDEAL_CALL : IDEAL_INH
    const f = (d - ideal) * SPRING * (0.25 + 0.75 * a)
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

// 边端点收进矩形边界，箭头不扎进节点
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
    els.label.setAttribute('transform', `translate(${(p.x1 + p.x2) / 2}, ${(p.y1 + p.y2) / 2})`)
    els.label.style.display = zoom.value < 0.55 ? 'none' : ''
  }
  for (const s of sim) {
    const el = nodeEl(s.id)
    if (el) el.setAttribute('transform', `translate(${s.x - s.w / 2}, ${s.y - s.h / 2})`)
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
function initialPos(id: string, axis: 'x' | 'y', wh: 'w' | 'h', half: boolean): number {
  const s = byId.get(id)
  if (!s) return 0
  return axis === 'x' ? s.x - (half ? s[wh] / 2 : 0) : s.y - (half ? s[wh] / 2 : 0)
}
function edgePos(e: RelationEdge, end: 'x1' | 'y1' | 'x2' | 'y2'): number {
  const a = byId.get(e.source)
  const b = byId.get(e.target)
  if (!a || !b) return 0
  const p = endpoints(a, b)
  return p[end]
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
  const z = Math.min(1.25, Math.max(0.18, Math.min(cw / bw, ch / bh)))
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
  const nz = Math.min(2.5, Math.max(0.18, zoom.value * f))
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
      line: el.querySelector('.rg-line') as SVGLineElement,
      label: el.querySelector('.rg-elabel') as SVGGElement,
    })
  } else {
    edgeEls.delete(key)
  }
}

function onNodeDown(ev: PointerEvent, n: RelationNode) {
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
  bgDown = { x: ev.clientX, y: ev.clientY, moved: false }
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
    if (bgDown && Math.hypot(ev.clientX - bgDown.x, ev.clientY - bgDown.y) > 4) bgDown.moved = true
    panX.value = panStart.px + (ev.clientX - panStart.x)
    panY.value = panStart.py + (ev.clientY - panStart.y)
  }
}

function settleBurst(steps = 120) {
  // 松手/加热结束后的收尾：只做碰撞分离与弹簧归位，不引入新的中心引力
  for (let i = 0; i < steps; i++) step(0.05)
  alpha = 0
  syncDom()
}

let suppressBgClick = false

function onNodeClick(ev: MouseEvent, n: RelationNode) {
  // 选中走 click：浏览器对“按下后轻微抖动”有自带容差，比手写 pointerup 判定更稳
  ev.stopPropagation()
  selectedId.value = n.id
}

function onBgClick() {
  // 平移画布松手也会触发 click，平移过就不当作“取消选中”
  if (suppressBgClick) {
    suppressBgClick = false
    return
  }
  selectedId.value = null
}

function onPointerUp() {
  const clicked = down
  const draggedNode = !!dragSim
  dragSim = null
  down = null
  if (clicked && clicked.moved && draggedNode) {
    // 拖完节点后立即补一轮短迭代，把被挤开/重叠的卡片排开
    settleBurst()
  }
  suppressBgClick = !!(bgDown && bgDown.moved)
  bgDown = null
  panning = false
}

async function onNodeDbl(n: RelationNode) {
  if (n.rel) {
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
function edgeKindText(k: string): string {
  return k === 'inherits' ? '继承' : k === 'mounts' ? '场景挂载' : k === 'calls' ? '调用' : k
}
function incidentMethods(it: Incident): string {
  const ms = it.edge.methods || []
  const head = ms.slice(0, 4).join('、')
  return ms.length > 4 ? `${head} 等 ${ms.length} 个方法` : head
}

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
            <circle cx="3.6" cy="4" r="1.7" fill="none" stroke="#58a6ff" stroke-width="1.1" />
            <circle cx="11.4" cy="4" r="1.7" fill="none" stroke="#2ec4b6" stroke-width="1.1" />
            <circle cx="7.5" cy="11.2" r="1.7" fill="none" stroke="#f0883e" stroke-width="1.1" />
            <path d="M5 4.8 L9.6 4.8 M4.6 5.4 L6.6 9.7 M10.4 5.4 L8.4 9.7" stroke="#7d90aa" stroke-width="0.9" />
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
          <defs>
            <marker id="rg-arrow-inh" viewBox="0 0 10 10" refX="8.5" refY="5"
                    markerWidth="9" markerHeight="9" orient="auto-start-reverse">
              <path d="M0 0 L9 5 L0 10 Z" fill="#7fa3d0" />
            </marker>
            <marker id="rg-arrow-mnt" viewBox="0 0 10 10" refX="8.5" refY="5"
                    markerWidth="9" markerHeight="9" orient="auto-start-reverse">
              <path d="M0 0 L9 5 L0 10 Z" fill="#f0883e" />
            </marker>
            <marker id="rg-arrow-call" viewBox="0 0 10 10" refX="8.5" refY="5"
                    markerWidth="9" markerHeight="9" orient="auto-start-reverse">
              <path d="M0 0 L9 5 L0 10 Z" fill="#4f9e6a" />
            </marker>
          </defs>

          <g :transform="viewport">
            <g class="rg-edges">
              <g v-for="e in viewEdges" :key="edgeKey(e)"
                 :ref="(el) => setEdgeRef(el, edgeKey(e))"
                 :class="['rg-edge-g', { dim: edgeDim(e) }]">
                <title v-if="e.kind === 'calls'">调用：{{ (e.methods || []).map((m) => m + '()').join('、') }}（首个调用点第 {{ e.line }} 行）</title>
                <title v-else>{{ e.label }}{{ e.line ? `（第 ${e.line} 行）` : '' }}</title>
                <line
                  class="rg-line"
                  :class="e.kind"
                  :x1="edgePos(e, 'x1')" :y1="edgePos(e, 'y1')"
                  :x2="edgePos(e, 'x2')" :y2="edgePos(e, 'y2')"
                  :marker-end="e.kind === 'mounts' ? 'url(#rg-arrow-mnt)' : e.kind === 'calls' ? 'url(#rg-arrow-call)' : 'url(#rg-arrow-inh)'"
                />
                <g class="rg-elabel" :transform="`translate(${edgePos(e, 'x1')}, ${edgePos(e, 'y1')})`">
                  <rect x="-26" y="-8" width="52" height="13" rx="3" class="rg-elabel-bg-inh" v-if="e.kind === 'inherits'" />
                  <rect x="-20" y="-8" width="40" height="13" rx="3" class="rg-elabel-bg-mnt" v-else-if="e.kind === 'mounts'" />
                  <rect x="-20" y="-8" width="40" height="13" rx="3" class="rg-elabel-bg-call" v-else />
                  <text text-anchor="middle" y="2" :class="['rg-elabel-tx', e.kind]">{{ e.label }}</text>
                </g>
              </g>
            </g>

            <g class="rg-nodes">
              <g
                v-for="n in viewNodes"
                :key="n.id"
                :ref="(el) => setNodeRef(el, n.id)"
                class="rg-node"
                :class="{ ext: n.external, dim: nodeDim(n), selected: selectedId === n.id }"
                :transform="`translate(${initialPos(n.id, 'x', 'w', true)}, ${initialPos(n.id, 'y', 'h', true)})`"
                @pointerdown="onNodeDown($event, n)"
                @click="onNodeClick($event, n)"
                @dblclick="onNodeDbl(n)"
              >
                <rect
                  :width="geometry[n.id]?.w || 100"
                  :height="geometry[n.id]?.h || 32"
                  rx="7"
                  :fill="styleOf(n.kind).fill"
                  :stroke="styleOf(n.kind).stroke"
                  :stroke-dasharray="styleOf(n.kind).dashed ? '4 3' : undefined"
                />
                <circle
                  v-if="n.region"
                  class="rg-region-dot"
                  :cx="9" :cy="(geometry[n.id]?.h || 32) / 2" r="3"
                  :fill="colorOf(n.region)"
                />
                <text class="rg-mark" :fill="styleOf(n.kind).stroke"
                      :x="n.region ? 17 : 12"
                      :y="(geometry[n.id]?.h || 32) / 2 + 3.6">
                  {{ styleOf(n.kind).mark }}
                </text>
                <text class="rg-label" :fill="styleOf(n.kind).text" x="30"
                      :y="geoY(n.id, 'label')">
                  {{ geometry[n.id]?.label }}
                </text>
                <text v-if="geometry[n.id]?.sub" class="rg-sub" x="30"
                      :y="geoY(n.id, 'sub')">
                  {{ geometry[n.id]?.sub }}
                </text>
                <text v-if="geometry[n.id]?.doc" class="rg-doc" x="30"
                      :y="geoY(n.id, 'doc')">
                  {{ geometry[n.id]?.doc }}
                </text>
                <title>{{ n.rel ? `${n.rel}${n.doc ? '\n' + n.doc : ''}` : `${n.label}（${styleOf(n.kind).label}）` }}</title>
              </g>
            </g>
          </g>
        </svg>

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
  background: rgba(38, 52, 77, 0.38);
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
.rg-title h2 { margin: 0; font-size: 14px; font-weight: 600; }
.rg-stats { font-size: 12px; color: var(--text-faint); }
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
  outline: 2px solid #2f6fed88;
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
.rg-search:focus-within { border-color: #2f6fed66; box-shadow: 0 0 0 2px #2f6fed18; }
.rg-search input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
  min-width: 0;
}
.rg-clear {
  font-style: normal;
  font-size: 10px;
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
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 11px;
  cursor: pointer;
}
.rg-toggle:hover { border-color: var(--border-strong); color: var(--text); }
.rg-toggle { transition: color .16s ease, border-color .16s ease, background .16s ease, opacity .16s ease; }
.rg-toggle:not(.off) { background: #2f6fed0d; border-color: #2f6fed55; }
.rg-toggle.off { opacity: 0.42; }
.rg-tg-line { width: 14px; height: 0; border-top: 2px solid #4d7fc0; display: inline-block; }
.rg-tg-line.mnt { border-top: 2px dashed #f0883e; }
.rg-tg-line.call { border-top: 2px solid #1c9e66; }
.rg-tg-ext {
  width: 11px; height: 9px;
  border: 1.5px dashed #98a3b4;
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
  font-size: 12px;
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

.rg-line { fill: none; stroke-width: 1.3; }
.rg-line.inherits { stroke: #4d7fc0; }
.rg-line.mounts { stroke: #c9762f; stroke-dasharray: 6 4; }
.rg-line.calls { stroke: #1c9e66; }
.rg-edge-g.dim { opacity: 0.10; }
.rg-elabel-bg-inh { fill: var(--bg); stroke: #4d7fc055; stroke-width: 0.8; }
.rg-elabel-bg-mnt { fill: var(--bg); stroke: #c9762f55; stroke-width: 0.8; }
.rg-elabel-bg-call { fill: var(--bg); stroke: #1c9e6666; stroke-width: 0.8; }
.rg-elabel-tx { font-size: 8px; }
.rg-elabel-tx.inherits { fill: #1b3c73; }
.rg-elabel-tx.mounts { fill: #8a4d12; }
.rg-elabel-tx.calls { fill: #0c6b58; }

.rg-node { cursor: pointer; }
.rg-node rect { stroke-width: 1.3; transition: stroke-width .14s ease, filter .14s ease, opacity .14s ease; }
.rg-node:hover rect { stroke-width: 2; filter: brightness(1.08); }
.rg-node.selected rect { stroke-width: 2.4; filter: drop-shadow(0 2px 6px rgba(47,111,237,.35)); }
.rg-node.dim { opacity: 0.13; }
.rg-mark { font-family: var(--font-mono); font-size: 10px; font-weight: 700; text-anchor: middle; }
.rg-label { font-size: 12px; font-weight: 600; }
.rg-sub { font-size: 10px; fill: var(--text-faint); font-family: var(--font-mono); }
.rg-doc { font-size: 10px; fill: var(--text-muted); }

/* 节点详情卡 */
.rg-detail {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 300px;
  max-height: calc(100% - 24px);
  overflow-y: auto;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  box-shadow: 0 10px 30px rgba(35, 52, 84, 0.16);
  padding: 11px 13px;
  font-size: 12px;
  z-index: 5;
}
.rg-d-head { display: flex; align-items: center; gap: 8px; }
.rg-d-mark { font-family: var(--font-mono); font-weight: 700; font-size: 13px; }
.rg-d-title { flex: 1 1 auto; font-size: 13px; font-weight: 600; word-break: break-all; }
.rg-d-x {
  border: none; background: none; font-size: 17px; line-height: 1;
  color: var(--text-faint); cursor: pointer; padding: 0 2px;
}
.rg-d-x:hover { color: var(--text); }
.rg-d-tags { display: flex; flex-wrap: wrap; gap: 5px; margin: 7px 0; }
.rg-d-tags span {
  font-size: 10px; border-radius: 4px; padding: 1px 7px;
  border: 1px solid var(--border); color: var(--text-muted); background: var(--bg);
  display: inline-flex; align-items: center; gap: 4px;
}
.rg-d-kind { color: #1b3c73 !important; border-color: #4d7fc066 !important; }
.rg-d-region i { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.rg-d-ext { color: #8a4d12 !important; border-color: #c9762f66 !important; }
.rg-d-doc { margin: 4px 0 6px; color: var(--text); line-height: 1.5; }
.rg-d-rel { margin: 0 0 6px; word-break: break-all; }
.rg-d-path { font-family: var(--font-mono); font-size: 10px; color: var(--text-muted); }
.rg-d-line { font-family: var(--font-mono); font-size: 10px; color: #2f6fed; }
.rg-d-faint { color: var(--text-faint); }
.rg-d-sec { font-size: 10px; font-weight: 600; color: var(--text-faint); margin: 8px 0 3px; }
.rg-d-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.rg-d-list li { display: flex; align-items: baseline; gap: 6px; font-size: 11px; }
.rg-d-arrow { font-style: normal; font-family: var(--font-mono); width: 14px; text-align: center; flex: 0 0 auto; }
.rg-d-arrow.inherits { color: #4d7fc0; }
.rg-d-arrow.mounts { color: #c9762f; }
.rg-d-arrow.calls { color: #1c9e66; }
.rg-d-link { color: #2f6fed; cursor: pointer; word-break: break-all; }
.rg-d-link:hover { text-decoration: underline; }
.rg-d-list em { font-style: normal; color: var(--text-faint); font-size: 10px; }
.rg-d-empty { margin: 0 0 2px; font-size: 10px; color: var(--text-faint); }
.rg-d-open {
  width: 100%; margin-top: 10px;
  border: 1px solid #2560d4; background: #2f6fed; color: #fff;
  border-radius: 6px; padding: 7px 10px; font-size: 12px; cursor: pointer;
}
.rg-d-open:disabled { opacity: .5; cursor: not-allowed; }
.rg-d-open:not(:disabled):hover { filter: brightness(1.06); transform: translateY(-1px); }
.rg-d-open { transition: filter .16s ease, transform .16s ease; }

.rg-state {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-faint);
  font-size: 12px;
}
.rg-error { color: #c23a40; }
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
  font-size: 10px;
  color: var(--text-muted);
  pointer-events: none;
}
.rg-legend span { display: inline-flex; align-items: center; gap: 5px; }
.rg-lg { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.rg-lg-class { background: rgba(47, 111, 237, 0.3); border: 1px solid #2f6fed; }
.rg-lg-script { background: rgba(46, 196, 182, 0.25); border: 1px solid #2ec4b6; }
.rg-lg-scene { background: rgba(240, 136, 62, 0.25); border: 1px solid #f0883e; }
.rg-lg-ext { background: rgba(152, 163, 180, 0.1); border: 1px dashed #98a3b4; }
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
