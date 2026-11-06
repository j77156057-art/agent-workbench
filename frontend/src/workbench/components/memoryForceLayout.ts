import type { SimulationLinkDatum, SimulationNodeDatum } from 'd3-force'
import type { MemoryGraphEdge, MemoryInspectNode, MemoryLayerKey } from '../api'
import { MEMORY_GRAPH } from './memoryGraphTokens'

export interface MemoryForceNode extends SimulationNodeDatum, MemoryInspectNode {
  key: string
  x: number
  y: number
  radius: number
  ring: number
  phase: number
  depth: number
  opacity: number
  /** 真实边里至少出现过一次；hub 注入连线不算（它不是邻接）。 */
  hasEdge: boolean
  edgeCount: number
  isHub?: boolean
}
export interface MemoryForceLink extends SimulationLinkDatum<MemoryForceNode> {
  id: string
  bend: number
  width: number
  used: boolean
  phase: number
  /** true = 注入连线（节点→hub），不是记忆之间的真实边。 */
  injection: boolean
  type?: string
}
export interface MemoryBaseGraph {
  nodes: MemoryForceNode[]
  simulationNodes: MemoryForceNode[]
  links: MemoryForceLink[]
  hidden: number
  /** 真实邻接边条数（不含 hub 注入连线）。 */
  realEdges: number
  /** 无任何真实边的节点数。 */
  isolated: number
  /** 参与至少一条真实边的节点数。 */
  edgeNodes: number
}
export interface ForceLayoutStats {
  state: 'running' | 'converged' | 'capped'
  alpha: number
  ticks: number
  frames: number
  elapsedMs: number
  fps: number
  visibleNodes: number
  firstVisibleMs: number
  maxFrameMs: number
  maxWorkMs: number
}

function seed(text: string) {
  let value = 2166136261
  for (let index = 0; index < text.length; index += 1) {
    value ^= text.charCodeAt(index)
    value = Math.imul(value, 16777619)
  }
  return (value >>> 0) / 4294967295
}

const LAYERS = MEMORY_GRAPH.layers
const RING_LAYERS: readonly MemoryLayerKey[] = ['shared', 'legacy', 'project', 'hook']

/** 未知 layer_key 落到 project（后端 fallback_layer 同口径），不猜“最亮/最暗”。 */
export function normalizeLayer(layer?: string): MemoryLayerKey {
  return (LAYERS.order as readonly string[]).includes(String(layer || ''))
    ? layer as MemoryLayerKey
    : 'project'
}

/** 层级亮度 → 节点实际 opacity；注入在层级基线上提亮一档，不抹平层级差。 */
export function layerOpacity(layer: MemoryLayerKey, used: boolean, depth: number) {
  const base = LAYERS.brightness[layer] ?? LAYERS.brightness.project
  const weight = used ? Math.min(1, base * LAYERS.injectedBoost) : base
  return Math.max(LAYERS.opacityMin, weight * (1 - depth * LAYERS.depthFalloff))
}

export function buildMemoryBaseGraph(all: MemoryInspectNode[], edges: MemoryGraphEdge[] = []): MemoryBaseGraph {
  const visible = [...all]
    .sort((a, b) => Number(b.used) - Number(a.used)
      || (LAYERS.brightness[normalizeLayer(b.layer_key)] - LAYERS.brightness[normalizeLayer(a.layer_key)])
      || b.importance - a.importance)
    .slice(0, MEMORY_GRAPH.maxVisible)
  const nodes: MemoryForceNode[] = []
  const ringNodes: MemoryForceNode[][] = RING_LAYERS.map(() => [])

  for (let ring = 0; ring < RING_LAYERS.length; ring += 1) {
    const layer = RING_LAYERS[ring]
    const ringConfig = (LAYERS.rings as Record<string, { rx: number; ry: number; depth: number }>)[layer]
    const inRing = visible
      .filter(node => normalizeLayer(node.layer_key) === layer)
      .sort((a, b) => b.importance - a.importance || Number(b.used) - Number(a.used))

    inRing.forEach((node, index) => {
      const key = `${node.store}:${node.id}`
      const jitter = seed(key) - 0.5
      const angle = MEMORY_GRAPH.angleOffset + index * MEMORY_GRAPH.goldenAngle + ring * MEMORY_GRAPH.ringAngleStep + jitter * MEMORY_GRAPH.angleJitter
      const radiusScale = MEMORY_GRAPH.radialBase + seed(`${key}:radius`) * MEMORY_GRAPH.radialJitter
      const depth = Math.min(1, ringConfig.depth + seed(`${key}:depth`) * MEMORY_GRAPH.depthJitter)
      const depthScale = 1 - depth * MEMORY_GRAPH.depthScale
      const importance = Math.max(0, Math.min(1, node.importance))
      const positioned: MemoryForceNode = {
        ...node, key, ring, phase: nodes.length, depth, layer_key: layer,
        hasEdge: false, edgeCount: 0,
        x: MEMORY_GRAPH.core.x + Math.cos(angle) * ringConfig.rx * radiusScale,
        y: MEMORY_GRAPH.core.y + Math.sin(angle) * ringConfig.ry * radiusScale,
        radius: (MEMORY_GRAPH.nodeRadiusMin + importance * MEMORY_GRAPH.nodeRadiusRange) * depthScale,
        opacity: layerOpacity(normalizeLayer(node.layer_key), node.used, depth),
      }
      nodes.push(positioned)
      ringNodes[ring].push(positioned)
    })
  }
  const byKey = new Map(nodes.map(node => [node.key, node]))

  const hub: MemoryForceNode = {
    key: 'core', id: 'core', store: 'system', title: '本轮上下文', kind: 'hub', source: 'system', status: 'active',
    content: '', importance: 1, used: false, use_count: 0,
    x: MEMORY_GRAPH.core.x, y: MEMORY_GRAPH.core.y, fx: MEMORY_GRAPH.core.x, fy: MEMORY_GRAPH.core.y,
    radius: MEMORY_GRAPH.hub.outerRadius, ring: 0, phase: -1, depth: 0, opacity: 1, isHub: true,
    layer_key: 'rule', layer_label: MEMORY_GRAPH.layers.labels.rule,
    hasEdge: false, edgeCount: 0,
  }
  const simulationNodes = [...nodes, hub]
  const links: MemoryForceLink[] = []
  const linked = new Set<string>()
  function addLink(a: MemoryForceNode, b: MemoryForceNode, used: boolean, injection: boolean, type?: string) {
    const id = [a.key, b.key].sort().join('~')
    if (linked.has(id)) return
    linked.add(id)
    const importance = b.isHub ? a.importance : (a.importance + b.importance) / 2
    const depth = b.isHub ? a.depth * 0.4 : (a.depth + b.depth) / 2
    links.push({
      id, source: a.key, target: b.key, bend: (seed(id) - 0.5) * MEMORY_GRAPH.edgeBend,
      width: (MEMORY_GRAPH.edgeWidthMin + importance * MEMORY_GRAPH.edgeWidthRange) * (1 - depth * MEMORY_GRAPH.edgeDepthScale),
      used, phase: links.length, injection, type,
    })
  }

  /* 真实邻接：只画后端 `edges` 表里解析出来的边（两端都在可见集合内才画）。
     不拿“同环相邻 / 内环同位”兜底——边上没有数据就空着。 */
  let realEdges = 0
  for (const edge of edges) {
    const a = byKey.get(edge.source)
    const b = byKey.get(edge.target)
    if (!a || !b || a === b) continue
    addLink(a, b, false, false, edge.type)
    realEdges += 1
    for (const node of [a, b]) {
      node.hasEdge = true
      node.edgeCount += 1
    }
  }

  /* 注入路径（节点→hub）不是邻接边，保留；它只表达“这条进了本轮提示词”。 */
  for (const node of nodes) if (node.used) addLink(node, hub, true, true)

  const isolated = nodes.filter(node => !node.hasEdge).length
  const edgeNodes = nodes.filter(node => node.hasEdge).length
  return { nodes, simulationNodes, links, hidden: Math.max(0, all.length - nodes.length), realEdges, isolated, edgeNodes }
}

/** Worker 负责物理；主线程只跑轻量骨架与性能采样，稳态后一次性提交坐标。 */
export class MemoryForceLayout {
  private worker: Worker | null = null
  private frameId = 0
  private base: MemoryBaseGraph | null = null
  private reduced = false
  private openedAt = 0
  private firstVisibleMs = 0
  private previousFrameAt = 0
  private frameDeltas: number[] = []
  private maxFrameMs = 0
  private maxWorkMs = 0
  private frames = 0
  private alpha = 1
  private ticks = 0
  private onUpdate: (stats: ForceLayoutStats) => void

  constructor(onUpdate: (stats: ForceLayoutStats) => void) { this.onUpdate = onUpdate }

  start(base: MemoryBaseGraph, reduced: boolean, openedAt = performance.now(), firstVisibleMs = 0) {
    this.stop()
    this.base = base
    this.reduced = reduced
    this.openedAt = openedAt
    this.firstVisibleMs = firstVisibleMs
    this.previousFrameAt = 0
    this.frameDeltas = []
    this.maxFrameMs = 0
    this.maxWorkMs = 0
    this.frames = 0
    this.alpha = 1
    this.ticks = 0
    if (!base.nodes.length) {
      this.emit('converged')
      return
    }

    performance.mark('memory:worker:construct:start')
    this.worker = new Worker(new URL('./memoryForce.worker.ts', import.meta.url), { type: 'module' })
    performance.mark('memory:worker:construct:end')
    this.worker.onmessage = event => {
      if (event.data.type !== 'done') return
      performance.mark('memory:worker:done', {
        detail: {
          initMs: event.data.initMs,
          firstTickMs: event.data.firstTickMs,
          firstChunkMs: event.data.firstChunkMs,
          maxChunkMs: event.data.maxChunkMs,
          chunks: event.data.chunks,
          workerElapsedMs: event.data.workerElapsedMs,
        },
      })
      this.alpha = event.data.alpha
      this.ticks = event.data.ticks
      this.applyPositions(event.data.positions as Record<string, { x: number; y: number }>)
      performance.mark('memory:positions:applied')
      this.finish(event.data.capped ? 'capped' : 'converged')
    }
    this.worker.onerror = () => this.finish('capped')
    this.emit('running')
    if (!this.reduced) this.frameId = requestAnimationFrame(this.onFrame)
    this.worker.postMessage({ type: 'start', base, reduced })
    performance.mark('memory:worker:posted')
  }

  reheat() {
    if (this.base) this.start(this.base, this.reduced, performance.now(), this.firstVisibleMs)
  }

  stop() {
    this.worker?.terminate()
    this.worker = null
    if (this.frameId) cancelAnimationFrame(this.frameId)
    this.frameId = 0
  }

  private applyPositions(positions: Record<string, { x: number; y: number }>) {
    if (!this.base) return
    const byKey = new Map(this.base.simulationNodes.map(node => [node.key, node]))
    for (const node of this.base.simulationNodes) {
      const position = positions[node.key]
      if (position) { node.x = position.x; node.y = position.y }
    }
    for (const link of this.base.links) {
      const sourceKey = typeof link.source === 'string' ? link.source : typeof link.source === 'number' ? this.base.simulationNodes[link.source].key : link.source.key
      const targetKey = typeof link.target === 'string' ? link.target : typeof link.target === 'number' ? this.base.simulationNodes[link.target].key : link.target.key
      link.source = byKey.get(sourceKey)!
      link.target = byKey.get(targetKey)!
    }
  }

  private onFrame = (time: number) => {
    this.frameId = 0
    const startedAt = performance.now()
    if (this.previousFrameAt) {
      const delta = time - this.previousFrameAt
      this.frameDeltas.push(delta)
      this.maxFrameMs = Math.max(this.maxFrameMs, delta)
      if (this.frameDeltas.length > 120) this.frameDeltas.shift()
    }
    this.previousFrameAt = time
    this.frames += 1
    this.maxWorkMs = Math.max(this.maxWorkMs, performance.now() - startedAt)
    this.frameId = requestAnimationFrame(this.onFrame)
  }

  private finish(state: 'converged' | 'capped') {
    if (this.frameId) cancelAnimationFrame(this.frameId)
    this.frameId = 0
    this.worker?.terminate()
    this.worker = null
    this.emit(state)
  }

  private emit(state: ForceLayoutStats['state']) {
    const average = this.frameDeltas.length
      ? this.frameDeltas.reduce((sum, value) => sum + value, 0) / this.frameDeltas.length : 0
    this.onUpdate({
      state,
      alpha: this.alpha,
      ticks: this.ticks,
      frames: this.frames,
      elapsedMs: performance.now() - this.openedAt,
      fps: average > 0 ? 1000 / average : 0,
      visibleNodes: this.base?.nodes.length || 0,
      firstVisibleMs: this.firstVisibleMs,
      maxFrameMs: this.maxFrameMs,
      maxWorkMs: this.maxWorkMs,
    })
  }
}
