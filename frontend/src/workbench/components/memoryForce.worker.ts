import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
} from 'd3-force'
import { MEMORY_GRAPH } from './memoryGraphTokens'
import type { MemoryBaseGraph, MemoryForceLink, MemoryForceNode } from './memoryForceLayout'

let simulation: Simulation<MemoryForceNode, MemoryForceLink> | null = null
let base: MemoryBaseGraph | null = null
let alpha = 1
let ticks = 0
let initMs = 0
let firstTickMs = 0
let workerStartedAt = 0
let firstChunkMs = 0
let maxChunkMs = 0
let chunks = 0
let timer: ReturnType<typeof setTimeout> | undefined

function step() {
  if (!simulation) return
  const config = MEMORY_GRAPH.force
  simulation.tick()
  alpha += (config.alphaTarget - alpha) * config.alphaDecay
  simulation.alpha(alpha)
  ticks += 1
  for (const node of base?.simulationNodes || []) {
    if (node.isHub) continue
    const padding = config.boundaryPadding + node.radius
    node.x = Math.max(padding, Math.min(MEMORY_GRAPH.width - padding, node.x))
    node.y = Math.max(padding, Math.min(MEMORY_GRAPH.height - padding, node.y))
  }
}

function done() {
  if (!base) return
  const positions = Object.fromEntries(base.simulationNodes.map(node => [node.key, { x: node.x, y: node.y }]))
  self.postMessage({
    type: 'done',
    alpha,
    ticks,
    positions,
    capped: ticks >= MEMORY_GRAPH.force.maxTicks && alpha > MEMORY_GRAPH.force.alphaMin,
    initMs,
    firstTickMs,
    firstChunkMs,
    maxChunkMs,
    chunks,
    workerElapsedMs: performance.now() - workerStartedAt,
  })
  simulation?.stop()
  simulation = null
}

function chunk() {
  const config = MEMORY_GRAPH.force
  const startedAt = performance.now()
  let steps = 0
  while (
    steps < config.maxStepsPerFrame
    && alpha > config.alphaMin
    && ticks < config.maxTicks
    && performance.now() - startedAt < config.frameBudgetMs
  ) {
    const tickStartedAt = performance.now()
    step()
    if (!firstTickMs) firstTickMs = performance.now() - tickStartedAt
    steps += 1
  }
  const finished = alpha <= config.alphaMin || ticks >= config.maxTicks
  const chunkMs = performance.now() - startedAt
  chunks += 1
  if (chunks === 1) firstChunkMs = chunkMs
  maxChunkMs = Math.max(maxChunkMs, chunkMs)
  if (finished) done()
  else timer = setTimeout(chunk, 0)
}

self.onmessage = (event: MessageEvent) => {
  if (event.data.type !== 'start') return
  const startedAt = performance.now()
  workerStartedAt = startedAt
  clearTimeout(timer)
  simulation?.stop()
  base = event.data.base as MemoryBaseGraph
  alpha = MEMORY_GRAPH.force.alphaInitial
  ticks = 0
  firstTickMs = 0
  firstChunkMs = 0
  maxChunkMs = 0
  chunks = 0
  const config = MEMORY_GRAPH.force
  simulation = forceSimulation(base.simulationNodes)
    .stop()
    .alpha(alpha)
    .alphaTarget(config.alphaTarget)
    .velocityDecay(config.velocityDecay)
    .force('charge', forceManyBody<MemoryForceNode>()
      .strength(node => node.isHub ? 0 : -config.repulsion * (0.75 + node.importance * 0.5))
      .distanceMax(config.repulsionDistanceMax)
      .theta(config.theta))
    .force('link', forceLink<MemoryForceNode, MemoryForceLink>(base.links)
      .id(node => node.key)
      .distance(link => link.used ? config.injectedLinkDistance : config.linkDistance)
      .strength(link => link.used ? config.injectedSpring : config.spring))
    .force('x', forceX<MemoryForceNode>(MEMORY_GRAPH.core.x)
      .strength(node => node.isHub ? 0 : node.used ? config.injectedCenterStrength : config.centerStrength))
    .force('y', forceY<MemoryForceNode>(MEMORY_GRAPH.core.y)
      .strength(node => node.isHub ? 0 : node.used ? config.injectedCenterStrengthY : config.centerStrengthY))
    .force('collide', forceCollide<MemoryForceNode>()
      .radius(node => node.isHub ? MEMORY_GRAPH.hub.outerRadius + config.collisionPadding : node.radius + config.collisionPadding)
      .iterations(config.collisionIterations))
  initMs = performance.now() - startedAt

  if (event.data.reduced) {
    while (alpha > config.alphaMin && ticks < config.maxTicks) step()
    done()
  } else {
    timer = setTimeout(chunk, 0)
  }
}
