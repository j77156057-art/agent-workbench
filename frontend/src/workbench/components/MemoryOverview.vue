<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, shallowRef, watch } from 'vue'
import type { MemoryInspectNode, MemoryInspectResult } from '../api'
import { MEMORY_GRAPH } from './memoryGraphTokens'
import { MemoryMotion, type MotionLink, type MotionNode } from './memoryMotion'
import { loadMotionProfile, motionRuntime } from '../motionRuntime'
import {
  buildMemoryBaseGraph,
  MemoryForceLayout,
  type ForceLayoutStats,
  type MemoryBaseGraph,
  type MemoryForceNode,
} from './memoryForceLayout'

const props = withDefaults(defineProps<{
  data: MemoryInspectResult | null
  loading?: boolean
  error?: string
  permission?: boolean
}>(), { loading: false, error: '', permission: false })
const emit = defineEmits<{ (e: 'retry'): void }>()
const selectedKey = ref('')

const sourceLabels: Record<string, string> = {
  user: '用户最初要求', project: '项目级', session: '会话级', system: '系统级',
  legacy: '项目长期记忆', shared: '跨项目共享', hook: '会话钩子', global: '跨项目共享',
}
const statusLabels: Record<string, string> = { pass: '符合', fail: '未遵守', unknown: '无法判定' }

const GRAPH_WIDTH = MEMORY_GRAPH.width
const GRAPH_HEIGHT = MEMORY_GRAPH.height
const CORE = MEMORY_GRAPH.core

interface PositionedNode extends MemoryInspectNode {
  key: string
  x: number
  y: number
  radius: number
  ring: number
  phase: number
  depth: number
  opacity: number
  hasEdge: boolean
  edgeCount: number
  labelX: number
  labelAnchor: 'start' | 'end'
}
interface NeuralLink {
  id: string
  d: string
  x1: number
  y1: number
  x2: number
  y2: number
  width: number
  used: boolean
  phase: number
  sourceKey: string
  targetKey: string
  bend: number
  injection: boolean
  type?: string
}

function curvedPath(a: { x: number; y: number }, b: { x: number; y: number }, bend: number) {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const cx = (a.x + b.x) / 2 - dy * bend
  const cy = (a.y + b.y) / 2 + dx * bend
  return `M ${a.x.toFixed(2)} ${a.y.toFixed(2)} Q ${cx.toFixed(2)} ${cy.toFixed(2)} ${b.x.toFixed(2)} ${b.y.toFixed(2)}`
}

const RING_LAYERS = ['shared', 'legacy', 'project', 'hook'] as const
const emptyBaseGraph = (): MemoryBaseGraph => ({
  nodes: [], simulationNodes: [], links: [], hidden: 0, realEdges: 0, isolated: 0, edgeNodes: 0,
})
const baseGraph = shallowRef<MemoryBaseGraph>(emptyBaseGraph())
const layoutVersion = ref(0)
const graphSvg = ref<SVGSVGElement | null>(null)
const detailsStage = ref(0)
const stableDomReady = ref(false)
const waving = ref(false)
const renderNodeLimit = ref(0)
const renderLinkLimit = ref(0)
const catalogLimit = ref(MEMORY_GRAPH.presentation.catalogInitial)
const layoutStats = reactive<ForceLayoutStats>({
  state: 'converged', alpha: 0, ticks: 0, frames: 0, elapsedMs: 0, fps: 0, visibleNodes: 0,
  firstVisibleMs: 0, maxFrameMs: 0, maxWorkMs: 0,
})
const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
let layoutGeneration = 0

const layout = new MemoryForceLayout(stats => {
  // Worker 进度不改变 Vue 树；只有状态切换才提交，避免预热期全面板 diff。
  if (stats.state !== layoutStats.state) Object.assign(layoutStats, stats)
  if (stats.state !== 'running') {
    performance.mark('memory:converged', { detail: { ticks: stats.ticks, nodes: stats.visibleNodes } })
    Object.assign(layoutStats, stats)
    renderNodeLimit.value = 0
    renderLinkLimit.value = 0
    stableDomReady.value = false
    layoutVersion.value += 1
    prepareStableDom()
  }
})

function rebuildLayout() {
  const generation = ++layoutGeneration
  const openedAt = performance.now()
  performance.mark('memory:overview:rebuild:start', { detail: { nodes: props.data?.memories.length || 0 } })
  layout.stop()
  selectedKey.value = ''
  detailsStage.value = 0
  stableDomReady.value = false
  renderNodeLimit.value = 0
  renderLinkLimit.value = 0
  catalogLimit.value = MEMORY_GRAPH.presentation.catalogInitial
  baseGraph.value = emptyBaseGraph()
  layoutVersion.value += 1
  Object.assign(layoutStats, {
    state: 'running', alpha: 1, ticks: 0, frames: 0, elapsedMs: 0, fps: 0,
    visibleNodes: Math.min(props.data?.memories.length || 0, MEMORY_GRAPH.maxVisible),
    firstVisibleMs: 0, maxFrameMs: 0, maxWorkMs: 0,
  })
  performance.mark('memory:skeleton:commit', { detail: { kind: props.data ? 'data' : 'loading' } })

  if (!props.data) {
    void nextTick(() => requestAnimationFrame(() => {
      performance.mark('memory:skeleton:raf', { detail: { kind: 'loading' } })
      requestAnimationFrame(() => performance.mark('memory:skeleton:paint', { detail: { kind: 'loading' } }))
    }))
    return
  }

  const start = () => {
    if (generation !== layoutGeneration) return
    const firstVisibleMs = performance.now() - openedAt
    performance.mark('memory:graph:build:start')
    baseGraph.value = buildMemoryBaseGraph(props.data?.memories || [], props.data?.edges || [])
    performance.mark('memory:graph:build:end')
    requestAnimationFrame(() => {
      if (generation !== layoutGeneration) return
      layout.start(baseGraph.value, motionQuery.matches, openedAt, firstVisibleMs)
    })
  }
  if (motionQuery.matches) start()
  else {
    // 双 rAF：保证骨架先实际画到屏幕上；图构建与 Worker 启动再拆一帧。
    void nextTick(() => requestAnimationFrame(() => {
      performance.mark('memory:skeleton:raf', { detail: { kind: 'data' } })
      requestAnimationFrame(() => {
        performance.mark('memory:skeleton:paint', { detail: { kind: 'data' } })
        start()
      })
    }))
  }
}

const graphSignature = computed(() => {
  const data = props.data
  if (!data) return 'empty'
  const nodes = data.memories.map(node => `${node.store}:${node.id}:${node.layer_key}:${Number(node.used)}:${node.importance}`).join('|')
  const edges = (data.edges || []).map(edge => `${edge.source}>${edge.target}:${edge.type || ''}`).join('|')
  return `${data.session_id}:${data.agent_id}:${nodes}#${edges}`
})
watch(graphSignature, rebuildLayout, { immediate: true })
motionQuery.addEventListener('change', rebuildLayout)
const motion = new MemoryMotion()
// 静态能力画像：low 时开局直接预降级（不挂激活波），但运行期仍可被超支观测升回。
void loadMotionProfile()
watch(() => motionRuntime.tier, tier => motion.setProfileTier(tier), { immediate: true })
let waveTimer = 0
let motionToken = 0
const waitFrame = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
async function syncMotion(ready: boolean) {
  const token = ++motionToken
  if (!ready) {
    motion.detach()
    waving.value = false
    return
  }
  // 稳定态骨架与真实节点是两套 svg；v-if 切换和 flow 元素挂载可能差 1~2 帧，
  // 这里等到「节点已挂载 + 注入边信号已挂载」再 attach，避免把脉冲建在空集合上。
  for (let attempt = 0; attempt < 16; attempt += 1) {
    await nextTick()
    await waitFrame()
    if (token !== motionToken) return
    const svg = graphSvg.value
    if (!svg) continue
    const nodeCount = svg.querySelectorAll('.mo-node').length
    const signalCount = svg.querySelectorAll('[data-flow-signal]').length
    const usedLinks = graph.value.flowLinks.length
    if (nodeCount < graph.value.nodes.length) continue
    if (usedLinks > 0 && signalCount === 0) continue
    const nodes: MotionNode[] = graph.value.nodes.map(node => ({
      key: node.key, x: node.x, y: node.y, radius: node.radius,
      importance: node.importance, used: node.used,
    }))
    // 注入边的另一个端点是中心节点，它不在 graph.nodes 里（单独渲染在 .mo-hub），
    // 但脉冲/拖拽的路径计算都需要它，所以补一个运动层专用的中心节点。
    nodes.push({ key: 'core', x: CORE.x, y: CORE.y, radius: MEMORY_GRAPH.hub.outerRadius, importance: 1, used: true })
    const links: MotionLink[] = graph.value.links.map(link => ({
      id: link.id, source: link.sourceKey, target: link.targetKey,
      bend: link.bend, width: link.width, used: link.used,
    }))
    motion.setProfileTier(motionRuntime.tier)
    motion.attach(svg, nodes, links, motionQuery.matches)
    if (motionQuery.matches) return
    // 低配画像已预降级：不挂激活波，避免开局就播一段注定被抽帧的动画。
    if (motion.isDegraded) return
    // 注入激活波：从注入节点按 BFS 跳数向外扩散，越远越晚、越弱。
    motion.applyWave()
    waving.value = true
    window.clearTimeout(waveTimer)
    waveTimer = window.setTimeout(() => { waving.value = false }, 1500)
    return
  }
}
watch(stableDomReady, ready => { void syncMotion(ready) }, { flush: 'post' })
onBeforeUnmount(() => {
  motionToken += 1
  layoutGeneration += 1
  motionQuery.removeEventListener('change', rebuildLayout)
  window.clearTimeout(waveTimer)
  motion.destroy()
  layout.stop()
})

const graph = computed(() => {
  // 显式订阅布局版本，让 Worker 推进后刷新定位。
  void layoutVersion.value
  const nodes: PositionedNode[] = baseGraph.value.nodes.slice(0, renderNodeLimit.value).map(node => {
    const labelDirection = node.x > CORE.x ? -1 : 1
    return {
      ...node,
      labelX: node.x + labelDirection * (node.radius + MEMORY_GRAPH.labelPadding),
      labelAnchor: labelDirection < 0 ? 'end' : 'start',
    }
  })
  const links: NeuralLink[] = baseGraph.value.links.slice(0, renderLinkLimit.value).map(link => {
    const a = link.source as MemoryForceNode
    const b = link.target as MemoryForceNode
    return {
      id: link.id,
      injection: link.injection,
      type: link.type,
      d: curvedPath(a, b, link.bend),
      x1: a.x,
      y1: a.y,
      x2: b.x,
      y2: b.y,
      width: link.width,
      used: link.used,
      phase: link.phase,
      sourceKey: a.key,
      targetKey: b.key,
      bend: link.bend,
    }
  })
  return {
    nodes,
    links,
    flowLinks: links.filter(link => link.used && link.injection),
    realEdges: baseGraph.value.realEdges,
    isolated: baseGraph.value.isolated,
    edgeNodes: baseGraph.value.edgeNodes,
    hidden: baseGraph.value.hidden,
  }
})
function prepareStableDom() {
  const generation = layoutGeneration
  performance.mark('memory:stable:prepare:start')
  const finish = () => {
    if (generation !== layoutGeneration) return
    performance.mark('memory:reveal:commit')
    stableDomReady.value = true
    void nextTick(() => {
      performance.mark('memory:reveal:dom')
      requestAnimationFrame(() => {
        performance.mark('memory:reveal:frame')
        requestAnimationFrame(() => performance.mark('memory:reveal:paint'))
      })
    })
    setTimeout(() => {
      if (generation !== layoutGeneration) return
      const advance = () => {
        if (generation !== layoutGeneration) return
        detailsStage.value += 1
        performance.mark('memory:details:commit', { detail: { stage: detailsStage.value } })
        void nextTick(() => performance.mark('memory:details:dom', { detail: { stage: detailsStage.value } }))
        if (detailsStage.value < 4) requestAnimationFrame(advance)
      }
      requestAnimationFrame(advance)
    }, 50)
  }
  const presentation = MEMORY_GRAPH.presentation
  let nodeBatch: number = presentation.initialNodeBatch
  let linkBatch: number = presentation.initialLinkBatch
  let previousBatchFrame = 0
  const mountBatch = async (time: number) => {
    if (generation !== layoutGeneration) return
    const frameGap = previousBatchFrame ? time - previousBatchFrame : 0
    previousBatchFrame = time
    const startedAt = performance.now()
    performance.mark('memory:stable:batch:start', { detail: { nodeBatch, linkBatch, frameGap } })
    renderNodeLimit.value = Math.min(baseGraph.value.nodes.length, renderNodeLimit.value + nodeBatch)
    renderLinkLimit.value = Math.min(baseGraph.value.links.length, renderLinkLimit.value + linkBatch)
    const detail = { nodes: renderNodeLimit.value, links: renderLinkLimit.value }
    performance.mark('memory:stable:batch:commit', { detail })
    await nextTick()
    if (generation !== layoutGeneration) return
    const workMs = performance.now() - startedAt
    performance.mark('memory:stable:batch:dom', { detail: { ...detail, workMs } })
    if (workMs > presentation.mountBudgetMs || (frameGap > 14 && workMs > presentation.mountBudgetMs / 2)) {
      nodeBatch = Math.max(2, Math.floor(nodeBatch / 2))
      linkBatch = Math.max(4, Math.floor(linkBatch / 2))
    } else {
      nodeBatch = Math.min(presentation.maxNodeBatch, nodeBatch + 4)
      linkBatch = Math.min(presentation.maxLinkBatch, linkBatch + 8)
    }
    if (renderNodeLimit.value < baseGraph.value.nodes.length || renderLinkLimit.value < baseGraph.value.links.length) {
      requestAnimationFrame(mountBatch)
    } else {
      // 最后一批先独立绘制，下一帧再切换骨架与稳态，避免两类提交同帧。
      requestAnimationFrame(finish)
    }
  }
  void nextTick(() => requestAnimationFrame(mountBatch))
}

const layoutStateLabel = computed(() => layoutStats.state === 'running'
  ? '正在排布'
  : layoutStats.state === 'capped' ? '排布已达上限' : '布局已稳定')
const skeletonNodes = Array.from({ length: MEMORY_GRAPH.skeletonNodes }, (_, index) => {
  const angle = index * MEMORY_GRAPH.goldenAngle
  const distance = 72 + (index % 4) * 48
  return {
    x: CORE.x + Math.cos(angle) * distance,
    y: CORE.y + Math.sin(angle) * distance * 0.68,
    radius: 2 + index % 3,
  }
})
const skeletonLinks = skeletonNodes.map((node, index) => ({
  x1: node.x,
  y1: node.y,
  x2: skeletonNodes[(index + 1) % skeletonNodes.length].x,
  y2: skeletonNodes[(index + 1) % skeletonNodes.length].y,
}))

const selected = computed(() => props.data?.memories.find(node => `${node.store}:${node.id}` === selectedKey.value) || null)
const usedCount = computed(() => props.data?.injected.length || 0)
const hubLabel = computed(() => props.data?.hub?.label || props.data?.hub?.layer_label || '常驻守则')
const layerCounts = computed(() => {
  const counts: Record<string, number> = { rule: 1, shared: 0, legacy: 0, project: 0, hook: 0 }
  for (const node of baseGraph.value.nodes) counts[node.layer_key] = (counts[node.layer_key] || 0) + 1
  return counts
})
const selectedGraphNode = computed(() => baseGraph.value.nodes.find(node => node.key === selectedKey.value) || null)
const realEdgeCount = computed(() => baseGraph.value.realEdges)
const edgeStats = computed(() => props.data?.edge_stats)
const layerNotice = computed(() => props.data?.layer_config?.pending_confirmation
  || MEMORY_GRAPH.layers.pendingConfirmation)
function layerLabel(layer: string) {
  const labels = MEMORY_GRAPH.layers.labels as Record<string, string>
  return labels[layer] || layer
}
function layerBrightness(layer: string) {
  return (MEMORY_GRAPH.layers.brightness as Record<string, number>)[layer] ?? MEMORY_GRAPH.layers.brightness.project
}
const edgeCoverage = computed(() => {
  const total = baseGraph.value.nodes.length
  return {
    edges: baseGraph.value.realEdges,
    edgeNodes: baseGraph.value.edgeNodes,
    total,
    // 用户口径「关系覆盖 N/总数」：N = 真实边条数，总数 = 节点数；另附节点覆盖数，避免混读。
    text: `${baseGraph.value.realEdges}/${total}`,
    nodeText: `${baseGraph.value.edgeNodes}/${total}`,
  }
})
const snapshotLabel = computed(() => {
  const value = props.data?.captured_at
  if (!value) return '等待首个快照'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : `快照 ${date.toLocaleString('zh-CN', { hour12: false })}`
})

function select(node: MemoryInspectNode) {
  selectedKey.value = `${node.store}:${node.id}`
  layout.reheat()
}
function sourceLabel(source: string) { return sourceLabels[source] || source }
function metric(value: number) { return String(value).padStart(2, '0') }
function rowIndex(index: number) { return String(index + 1).padStart(2, '0') }
function timeLabel(value?: string) {
  if (!value) return '未记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
function short(text: string, length = 180) {
  const value = text.trim().replace(/\s+/g, ' ')
  return value.length > length ? `${value.slice(0, length)}…` : value || '（空）'
}

</script>

<template>
  <div class="mo" :key="`${data?.session_id || ''}:${data?.agent_id || ''}`">
    <div v-if="loading" class="mo-loading-layout" role="status" aria-live="polite">
      <header class="mo-overview-head"><div><h2>注入快照</h2><p>正在读取真实注入，先显示轻量骨架</p></div></header>
      <section class="mo-section mo-neural-section" aria-labelledby="mo-neurons-loading">
        <header class="mo-section-head"><div><span class="mo-section-index">01</span><h3 id="mo-neurons-loading">记忆神经网络</h3></div><span>等待快照</span></header>
        <div class="mo-graph-wrap" data-layout-state="running" data-loading-skeleton>
          <svg class="mo-graph mo-graph-skeleton" :viewBox="`0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`" role="img" aria-label="记忆快照加载骨架">
            <g aria-hidden="true">
              <line v-for="(link, index) in skeletonLinks" :key="`loading-link-${index}`" :x1="link.x1" :y1="link.y1" :x2="link.x2" :y2="link.y2" />
              <circle class="mo-skeleton-hub" :cx="CORE.x" :cy="CORE.y" :r="MEMORY_GRAPH.hub.innerRadius" />
              <circle v-for="(node, index) in skeletonNodes" :key="`loading-node-${index}`" :cx="node.x" :cy="node.y" :r="node.radius" />
              <text :x="CORE.x" :y="CORE.y + MEMORY_GRAPH.hub.labelOffset" text-anchor="middle">正在读取记忆快照 · 骨架不代表真实位置</text>
            </g>
          </svg>
        </div>
        <p class="mo-visible-note">骨架先于接口返回呈现；物理预热在 Worker 中分片执行。</p>
      </section>
    </div>

    <div v-else-if="error" class="mo-state" :class="{ 'is-permission': permission }" role="alert">
      <svg v-if="permission" class="mo-state-icon" viewBox="0 0 32 32" aria-hidden="true">
        <rect x="7" y="14" width="18" height="13" rx="2"></rect><path d="M11 14V9a5 5 0 0 1 10 0v5M16 19v4"></path>
      </svg>
      <svg v-else class="mo-state-icon" viewBox="0 0 32 32" aria-hidden="true">
        <circle cx="16" cy="16" r="11"></circle><path d="M16 10v7M16 22v1"></path>
      </svg>
      <strong>{{ permission ? '没有查看这份记忆的权限' : '快照暂时不可用' }}</strong>
      <p>{{ permission ? '当前身份无法读取此会话的注入记录。请切换有权限的账号，或联系会话所有者。' : error }}</p>
      <small v-if="permission">权限不足不会用空数据冒充“没有记忆”。</small>
      <button type="button" class="mo-state-action" @click="emit('retry')">重新读取</button>
    </div>

    <div v-else-if="!data" class="mo-state">
      <svg class="mo-state-icon" viewBox="0 0 32 32" aria-hidden="true">
        <circle cx="16" cy="16" r="3"></circle><circle cx="7" cy="10" r="2"></circle><circle cx="25" cy="9" r="2"></circle><circle cx="24" cy="24" r="2"></circle><path d="m9 11 5 4m4-1 5-4m-6 8 5 5"></path>
      </svg>
      <strong>还没有可观测的记忆快照</strong>
      <p>打开一个会话后，这里会显示真正进入模型提示词的内容，而不是文件清单。</p>
      <button type="button" class="mo-state-action" @click="emit('retry')">读取当前会话</button>
    </div>

    <template v-else>
      <header class="mo-overview-head">
        <div><h2>注入快照</h2><p>只呈现本轮实际进入模型的上下文</p></div>
        <span class="mo-snapshot-state" :class="{ stale: data.stale }"><i></i>{{ data.stale ? '快照待更新' : '已捕获' }}</span>
      </header>

      <div class="mo-summary" aria-label="记忆快照关键数字">
        <div><span>本轮注入</span><strong>{{ metric(usedCount) }}</strong><small>条进入提示词</small></div>
        <div><span>记忆库条目</span><strong>{{ metric(data.memories.length) }}</strong><small>条可追溯记录</small></div>
        <div><span>生效守则</span><strong>{{ metric(data.rules.length) }}</strong><small>条当前约束</small></div>
      </div>
      <div class="mo-snapshot-meta"><span>{{ snapshotLabel }}</span><span>{{ data.turn_id || '尚无回合 ID' }}</span></div>
      <p v-for="note in data.degraded" :key="note" class="mo-note">{{ note }}</p>

      <section class="mo-section mo-neural-section" aria-labelledby="mo-neurons">
        <header class="mo-section-head">
          <div><span class="mo-section-index">01</span><h3 id="mo-neurons">记忆神经网络</h3></div>
          <span class="mo-tabular">{{ layoutStateLabel }} · {{ layoutStats.visibleNodes }} 条记忆</span>
        </header>
        <p class="mo-section-description">环与亮度由 store 的真实语义分层驱动：共享记忆最内、会话钩子最外，常驻守则居中。亮度依据是内容稳定度（常驻守则 &gt; 共享记忆 &gt; 项目长期记忆 &gt; 晋升候选 &gt; 会话钩子），不是“越底层越亮”。真实邻接只画后端 <code>edges</code> 表里的边，没有关联的节点留空，不补合成边。拖拽节点会沿真实边带动它周围的邻域，点击可查看原文。</p>

        <div
          v-if="data.memories.length" class="mo-graph-wrap" :class="{ 'has-detail': selected }"
          data-force-engine="d3-force"
          :data-layout-state="layoutStats.state"
          :data-alpha="layoutStats.alpha.toFixed(5)"
          :data-layout-ticks="layoutStats.ticks"
          :data-layout-frames="layoutStats.frames"
          :data-layout-ms="layoutStats.elapsedMs.toFixed(1)"
          :data-layout-fps="layoutStats.fps.toFixed(2)"
          :data-visible-nodes="layoutStats.visibleNodes"
          :data-rendered-nodes="layoutStats.state === 'running' ? 0 : graph.nodes.length"
          :data-first-visible-ms="layoutStats.firstVisibleMs.toFixed(1)"
          :data-max-frame-ms="layoutStats.maxFrameMs.toFixed(2)"
          :data-max-work-ms="layoutStats.maxWorkMs.toFixed(2)"
          :data-real-edges="realEdgeCount"
          :data-edge-in-table="edgeStats?.in_table ?? 0"
          :data-edge-unresolved="edgeStats?.unresolved ?? 0"
          :data-isolated-nodes="graph.isolated"
          :data-edge-nodes="graph.edgeNodes"
          :data-edge-coverage="edgeCoverage.text"
          :data-layer-counts="`shared:${layerCounts.shared},legacy:${layerCounts.legacy},project:${layerCounts.project},hook:${layerCounts.hook}`"
          :data-layer-calibrated="data.layer_config?.calibrated ? '1' : '0'"
        >
          <svg v-if="layoutStats.state === 'running' || !stableDomReady" class="mo-graph mo-graph-skeleton" :viewBox="`0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`" role="img" aria-label="力导向预热骨架，非真实节点位置">
            <g aria-hidden="true">
              <line v-for="(link, index) in skeletonLinks" :key="`skeleton-link-${index}`" :x1="link.x1" :y1="link.y1" :x2="link.x2" :y2="link.y2" />
              <circle class="mo-skeleton-hub" :cx="CORE.x" :cy="CORE.y" :r="MEMORY_GRAPH.hub.innerRadius" />
              <circle v-for="(node, index) in skeletonNodes" :key="`skeleton-node-${index}`" :cx="node.x" :cy="node.y" :r="node.radius" />
              <text :x="CORE.x" :y="CORE.y + MEMORY_GRAPH.hub.labelOffset" text-anchor="middle">预热 {{ layoutStats.visibleNodes }} 个节点 · 骨架不代表真实位置</text>
            </g>
          </svg>
          <svg v-if="layoutStats.state !== 'running'" ref="graphSvg" class="mo-graph mo-stable-graph" :class="{ 'is-ready': stableDomReady, 'is-waving': waving }" :viewBox="`0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`" role="group" aria-label="有力导向景深与沿边传导脉冲的记忆神经网络，可拖拽节点">
            <defs>
              <radialGradient id="mo-hub-gradient">
                <stop offset="0" class="mo-hub-gradient-start"></stop><stop offset="1" class="mo-hub-gradient-end"></stop>
              </radialGradient>
              <radialGradient id="mo-signal-gradient">
                <stop offset="0" class="mo-signal-gradient-start"></stop><stop offset="1" class="mo-signal-gradient-end"></stop>
              </radialGradient>
              <linearGradient id="mo-edge-neutral" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" class="mo-gradient-start"></stop><stop offset="1" class="mo-gradient-end"></stop>
              </linearGradient>
              <linearGradient id="mo-edge-used" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" class="mo-gradient-start used"></stop><stop offset="1" class="mo-gradient-end used"></stop>
              </linearGradient>
            </defs>

            <g class="mo-layer-guides" aria-hidden="true">
              <!-- 环只作层级落点；半径来自 tokens，不按重要度切。 -->
              <g v-for="layer in RING_LAYERS" :key="`guide-${layer}`">
                <ellipse
                  class="mo-layer-ring"
                  :cx="CORE.x" :cy="CORE.y"
                  :rx="MEMORY_GRAPH.layers.rings[layer].rx" :ry="MEMORY_GRAPH.layers.rings[layer].ry"
                />
                <text
                  class="mo-layer-label" :x="CORE.x - MEMORY_GRAPH.layers.rings[layer].rx + 4"
                  :y="CORE.y - MEMORY_GRAPH.layers.rings[layer].ry - 4"
                >{{ layerLabel(layer) }} · {{ layerCounts[layer] }}</text>
              </g>
            </g>
            <g aria-hidden="true">
              <circle class="mo-field-halo" :cx="CORE.x" :cy="CORE.y" :r="MEMORY_GRAPH.hub.haloRadius * MEMORY_GRAPH.depthHaloScale" />
              <path
                v-for="link in graph.links" :key="link.id" class="mo-edge" :class="{ used: link.used, real: !link.injection }"
                :data-link-id="link.id" :data-edge-source="link.sourceKey" :data-edge-target="link.targetKey"
                :data-edge-injection="link.injection ? '1' : '0'" :data-edge-type="link.type || ''"
                :d="link.d" :stroke="`url(#mo-edge-${link.used ? 'used' : 'neutral'})`" :style="{ strokeWidth: `${link.width}px` }"
              />
              <path
                v-for="link in graph.flowLinks" :key="`flow-${link.id}`" class="mo-flow" :data-link-id="link.id"
                :d="link.d" :stroke="`url(#mo-edge-${link.used ? 'used' : 'neutral'})`" :style="{ strokeWidth: `${link.width + MEMORY_GRAPH.flowWidthBoost}px` }"
              />
              <circle
                v-for="link in graph.flowLinks" :key="`signal-a-${link.id}`"
                class="mo-signal" :r="MEMORY_GRAPH.motion.pulseRadius" :data-signal-link="link.id" data-pulse-variant="head" data-flow-signal
              ></circle>
              <circle
                v-for="link in graph.flowLinks" :key="`signal-b-${link.id}`"
                class="mo-signal mo-signal-soft" :r="MEMORY_GRAPH.motion.trailRadius" :data-signal-link="link.id" data-pulse-variant="tail" data-flow-signal
              ></circle>
            </g>

            <g class="mo-hub" :class="{ active: usedCount > 0 }" :data-hub-layer-key="data.hub?.layer_key || 'rule'" :data-hub-layer-label="hubLabel" aria-hidden="true">
              <circle class="mo-hub-halo" :cx="CORE.x" :cy="CORE.y" :r="MEMORY_GRAPH.hub.haloRadius" />
              <circle class="mo-hub-ring" :cx="CORE.x" :cy="CORE.y" :r="MEMORY_GRAPH.hub.outerRadius" />
              <circle class="mo-hub-inner" :cx="CORE.x" :cy="CORE.y" :r="MEMORY_GRAPH.hub.innerRadius" />
              <circle class="mo-hub-core" :cx="CORE.x" :cy="CORE.y" :r="MEMORY_GRAPH.hub.coreRadius" />
              <line :x1="CORE.x" :y1="CORE.y - MEMORY_GRAPH.hub.outerRadius - MEMORY_GRAPH.hub.tickLength" :x2="CORE.x" :y2="CORE.y - MEMORY_GRAPH.hub.outerRadius" />
              <line :x1="CORE.x" :y1="CORE.y + MEMORY_GRAPH.hub.outerRadius" :x2="CORE.x" :y2="CORE.y + MEMORY_GRAPH.hub.outerRadius + MEMORY_GRAPH.hub.tickLength" />
              <line :x1="CORE.x - MEMORY_GRAPH.hub.outerRadius - MEMORY_GRAPH.hub.tickLength" :y1="CORE.y" :x2="CORE.x - MEMORY_GRAPH.hub.outerRadius" :y2="CORE.y" />
              <line :x1="CORE.x + MEMORY_GRAPH.hub.outerRadius" :y1="CORE.y" :x2="CORE.x + MEMORY_GRAPH.hub.outerRadius + MEMORY_GRAPH.hub.tickLength" :y2="CORE.y" />
              <text :x="CORE.x" :y="CORE.y + MEMORY_GRAPH.hub.labelOffset" text-anchor="middle">{{ hubLabel }}</text>
            </g>

            <g
              v-for="node in graph.nodes" :key="node.key" class="mo-node" :data-node-key="node.key" :data-store="node.store"
              :class="{ used: node.used, selected: selectedKey === node.key, isolated: !node.hasEdge }"
              :data-layer-key="node.layer_key" :data-layer-label="layerLabel(node.layer_key)" :data-has-edge="node.hasEdge ? '1' : '0'" :data-edge-count="node.edgeCount"
              :data-importance="node.importance" :data-depth="node.depth" :data-ring="node.ring"
              :style="{ '--node-o': node.opacity, '--node-layer-o': layerBrightness(node.layer_key), '--delay': `${-node.phase * MEMORY_GRAPH.pulsePhaseStep}s` }"
              role="button" tabindex="0"
              :aria-label="`${node.title}，${layerLabel(node.layer_key)}，${node.used ? '本轮已注入' : '本轮未注入'}，${node.hasEdge ? `${node.edgeCount} 条真实关联边` : '无关联边'}，重要度 ${node.importance.toFixed(2)}`"
              @click="select(node)" @keydown.enter.prevent="select(node)" @keydown.space.prevent="select(node)"
            >
              <circle v-if="node.used" class="mo-wave" :cx="node.x" :cy="node.y" :r="node.radius + MEMORY_GRAPH.waveRingPadding" />
              <circle class="mo-wave-glow" :cx="node.x" :cy="node.y" :r="node.radius + MEMORY_GRAPH.waveGlowPadding" />
              <circle v-if="node.used || selectedKey === node.key" class="mo-halo" :cx="node.x" :cy="node.y" :r="node.radius + MEMORY_GRAPH.haloPadding" />
              <circle v-if="selectedKey === node.key" class="mo-state-ring" :cx="node.x" :cy="node.y" :r="node.radius + MEMORY_GRAPH.stateRingPadding" />
              <circle v-if="node.used" class="mo-used-ring" :cx="node.x" :cy="node.y" :r="node.radius + MEMORY_GRAPH.usedRingPadding" />
              <circle class="mo-neuron-core" :cx="node.x" :cy="node.y" :r="node.radius" />
              <circle v-if="!node.hasEdge" class="mo-isolated-mark" :cx="node.x" :cy="node.y" :r="node.radius + MEMORY_GRAPH.usedRingPadding" />
              <text v-if="selectedKey === node.key" class="mo-node-label" :x="node.labelX" :y="node.y - node.radius - MEMORY_GRAPH.labelPadding" :text-anchor="node.labelAnchor">{{ short(node.title, MEMORY_GRAPH.labelLength) }}</text>
              <title>{{ node.title }} · {{ layerLabel(node.layer_key) }} · {{ node.used ? '本轮注入' : '未注入' }} · {{ node.hasEdge ? `${node.edgeCount} 条真实关联边` : '无关联边' }} · 重要度 {{ node.importance.toFixed(2) }}</title>
            </g>
          </svg>

          <div class="mo-graph-caption">
            <div class="mo-graph-legend" aria-label="图例">
              <span><i class="is-live"></i>注入中</span><span><i></i>未注入</span><span><b></b>选中 / 悬停</span>
              <span><i class="is-isolated"></i>无关联边</span>
            </div>
            <span class="mo-graph-scale" data-edge-summary>
              真实边 {{ realEdgeCount }} 条<template v-if="edgeStats">（索引表 {{ edgeStats.in_table }}，端点缺失 {{ edgeStats.unresolved }}）</template>
              · 关系覆盖 <b data-edge-coverage>{{ edgeCoverage.text }}</b><small>（边/节点；有关联节点 {{ edgeCoverage.nodeText }}）</small> · 无关联 {{ graph.isolated }} 个
            </span>
          </div>
          <p class="mo-visible-note">
            {{ layerLabel('rule') }}（hub，最稳）· {{ layerLabel('shared') }} {{ layerCounts.shared }} · {{ layerLabel('legacy') }} {{ layerCounts.legacy }} · {{ layerLabel('project') }} {{ layerCounts.project }} · {{ layerLabel('hook') }} {{ layerCounts.hook }}；
            亮度 = 内容稳定度（常驻守则最亮 → 会话钩子最暗，注入在同层基线上再提亮一档）· 线宽 = 重要度 · 环只是层级落点{{ realEdgeCount ? '' : '；当前真实边 0 条，图呈无边状态' }}。
            <span class="mo-layer-pending">{{ layerNotice }}</span>
          </p>

          <transition name="mo-slide">
            <aside v-if="selected" class="mo-detail" aria-label="记忆节点详情" @keydown.esc="selectedKey = ''">
              <header class="mo-selected-head">
                <div><small :class="{ live: selected.used }">{{ selected.used ? '正在注入' : '本轮未注入' }}</small><h4>{{ selected.title }}</h4></div>
                <button type="button" aria-label="关闭记忆详情" @click="selectedKey = ''">×</button>
              </header>
              <dl class="mo-detail-meta">
                <dt>来源</dt><dd>{{ selected.source }}</dd>
                <dt>所属作用域</dt><dd>{{ sourceLabel(selected.store) }}</dd>
                <dt>层级</dt><dd>{{ layerLabel(selected.layer_key) }}</dd>
                <dt>真实关联边</dt>
                <dd class="mo-tabular">{{ selectedGraphNode?.edgeCount || 0 }} 条<small v-if="!selectedGraphNode?.hasEdge">无关联边，激活波只覆盖自身</small></dd>
                <dt>重要度</dt><dd class="mo-tabular">{{ selected.importance.toFixed(2) }}</dd>
                <dt>被引用次数</dt><dd class="mo-tabular">{{ selected.use_count_semantics === 'unavailable' ? '未记录' : selected.use_count }}</dd>
                <dt>写入时间</dt><dd>{{ timeLabel(selected.created_at) }}<small v-if="selected.created_at_semantics === 'updated_fallback'">旧库仅记录更新时间</small></dd>
                <dt>最近更新</dt><dd>{{ timeLabel(selected.updated_at) }}</dd>
              </dl>
              <section class="mo-detail-copy"><h5>记忆原文</h5><pre>{{ selected.injected_content || selected.content || '（无正文）' }}</pre></section>
              <p v-if="selected.evidence" class="mo-evidence"><strong>来源证据</strong>{{ selected.evidence }}</p>
            </aside>
          </transition>
        </div>

        <div v-else class="mo-state mo-state-embedded">
          <svg class="mo-state-icon" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="3"></circle><path d="M16 4v6m0 12v6M4 16h6m12 0h6"></path></svg>
          <strong>记忆场仍是空的</strong>
          <p>第一条项目记忆写入后，这里会显示它与本轮上下文的关系；空库存不会被绘制成装饰节点。</p>
          <button type="button" class="mo-state-action" @click="emit('retry')">刷新记忆场</button>
        </div>
      </section>

      <section v-if="detailsStage >= 1" class="mo-section" aria-labelledby="mo-injected">
        <header class="mo-section-head">
          <div><span class="mo-section-index">02</span><h3 id="mo-injected">本轮实际注入</h3></div>
          <span>{{ data.injected.length }} 条</span>
        </header>
        <div v-if="data.injected.length" class="mo-list">
          <button v-for="(node, index) in data.injected" :key="`injected-${node.store}:${node.id}`" type="button" class="mo-memory-row" @click="select(node)">
            <span class="mo-row-index"><i class="mo-live-dot"></i>{{ rowIndex(index) }}</span>
            <span class="mo-row-copy"><strong>{{ node.title }}</strong><small>{{ sourceLabel(node.store) }} · {{ node.kind }} · {{ node.status }}</small></span>
            <span class="mo-row-value">{{ node.importance.toFixed(2) }}<small>重要度</small></span>
          </button>
        </div>
        <div v-else class="mo-inline-empty"><i></i><div><strong>这一轮没有记忆进入提示词</strong><p>下方库存仍然保留；这里不会用“有库存”冒充“已注入”。</p></div></div>
        <details v-for="block in data.prompt_blocks" :key="`${block.layer_key}:${block.chars}`" class="mo-disclosure">
          <summary><span>提示词原文 · {{ block.title || block.layer_key }}</span><span class="mo-tabular">{{ block.chars || 0 }} 字符</span></summary>
          <pre>{{ block.text || '（空）' }}</pre>
        </details>
      </section>

      <section v-if="detailsStage >= 2" class="mo-section" aria-labelledby="mo-rules">
        <header class="mo-section-head">
          <div><span class="mo-section-index">03</span><h3 id="mo-rules">此刻生效的守则</h3></div>
          <span>按来源核对</span>
        </header>
        <details v-for="rule in data.rules" :key="rule.id" class="mo-disclosure mo-rule">
          <summary><span class="mo-source">{{ sourceLabel(rule.source) }}</span><span class="mo-rule-title">{{ rule.title }}</span></summary>
          <pre>{{ rule.text }}</pre>
        </details>
        <div v-if="!data.rules.length" class="mo-inline-empty"><i></i><div><strong>没有守则快照</strong><p>当前接口没有返回可核对的规则内容，因此不推断模型正在遵循什么。</p></div></div>
      </section>

      <section v-if="detailsStage >= 3" class="mo-section" aria-labelledby="mo-compliance">
        <header class="mo-section-head">
          <div><span class="mo-section-index">04</span><h3 id="mo-compliance">合规核对</h3></div>
          <span>无证据不判通过</span>
        </header>
        <article v-for="item in data.compliance" :key="item.id" class="mo-check" :class="`is-${item.status}`">
          <div class="mo-check-head"><strong>{{ statusLabels[item.status] || item.status }}</strong><span>{{ sourceLabel(item.source) }}</span></div>
          <div class="mo-check-copy"><p>{{ short(item.requirement) }}</p><small>{{ item.evidence }}</small></div>
        </article>
        <div v-if="!data.compliance.length" class="mo-inline-empty"><i></i><div><strong>暂无可核对的合规证据</strong><p>需要同时提供原始要求与当前行为证据，才会给出“符合”判断。</p></div></div>
      </section>

      <section v-if="detailsStage >= 4" class="mo-section" aria-labelledby="mo-catalog">
        <header class="mo-section-head">
          <div><span class="mo-section-index">05</span><h3 id="mo-catalog">已记录的全部内容</h3></div>
          <span>{{ data.memories.length }} 条</span>
        </header>
        <div class="mo-catalog">
          <button v-for="(node, index) in data.memories.slice(0, catalogLimit)" :key="`catalog-${node.store}:${node.id}`" type="button" class="mo-memory-row" :class="{ dim: !node.used }" @click="select(node)">
            <span class="mo-row-index"><i class="mo-catalog-light" :class="{ on: node.used }"></i>{{ rowIndex(index) }}</span>
            <span class="mo-row-copy"><strong>{{ node.title }}</strong><small>{{ sourceLabel(node.store) }} · {{ node.status }} · {{ timeLabel(node.updated_at) }} · {{ node.use_count_semantics === 'unavailable' ? '使用次数未记录' : `引用 ${node.use_count}` }}</small></span>
            <span class="mo-row-value">{{ node.importance.toFixed(2) }}<small>重要度</small></span>
          </button>
        </div>
        <button v-if="catalogLimit < data.memories.length" type="button" class="mo-catalog-more" @click="catalogLimit += 96">
          显示更多 · 剩余 {{ data.memories.length - catalogLimit }} 条
        </button>
      </section>
    </template>
  </div>
</template>

<style scoped>
.mo { min-width: 0; color: var(--mi-text); font-family: var(--mi-font-ui); }
.mo button, .mo summary { font-family: inherit; }
.mo button:focus-visible, .mo summary:focus-visible, .mo-node:focus-visible { outline: var(--mi-focus-width) solid var(--mi-accent); outline-offset: var(--mi-space-1); }
.mo-tabular, .mo-summary strong, .mo-row-index, .mo-row-value { font-variant-numeric: tabular-nums lining-nums; }
.mo-overview-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--mi-space-4); padding: var(--mi-space-5) 0 var(--mi-space-4); }
.mo-overview-head h2 { margin: 0; font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); font-weight: var(--mi-weight-semibold); letter-spacing: 0; }
.mo-overview-head p, .mo-section-description { margin: var(--mi-space-1) 0 0; color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mo-snapshot-state { display: inline-flex; align-items: center; gap: var(--mi-space-2); padding-top: var(--mi-space-1); color: var(--mi-accent); font-size: var(--mi-fs-secondary); white-space: nowrap; }
.mo-snapshot-state i { width: var(--mi-status-dot); height: var(--mi-status-dot); border-radius: var(--mi-radius-lg); background: var(--mi-accent); box-shadow: var(--mi-glow-node); }
.mo-snapshot-state.stale { color: var(--mi-warning); }
.mo-snapshot-state.stale i { background: var(--mi-warning); box-shadow: none; }
.mo-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); padding: var(--mi-space-4) 0 var(--mi-space-5); border-block: var(--mi-border-width) solid var(--mi-border-subtle); }
.mo-summary > div { display: grid; grid-template-columns: 1fr auto; align-items: end; gap: var(--mi-space-1) var(--mi-space-3); padding-inline: var(--mi-space-5); border-left: var(--mi-border-width) solid var(--mi-border-subtle); }
.mo-summary > div:first-child { padding-left: 0; border-left: 0; }
.mo-summary > div:last-child { padding-right: 0; }
.mo-summary span { align-self: center; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mo-summary strong { grid-row: span 2; font-family: var(--mi-font-mono); font-size: var(--mi-fs-display); line-height: var(--mi-lh-display); font-weight: var(--mi-weight-medium); color: var(--mi-text); }
.mo-summary > div:first-child strong { color: var(--mi-accent-strong); }
.mo-summary small { color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mo-snapshot-meta { display: flex; justify-content: space-between; gap: var(--mi-space-4); padding: var(--mi-space-2) 0 0; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mo-snapshot-meta span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mo-note { margin: var(--mi-space-3) 0 0; padding-left: var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-warning); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mo-section { padding: var(--mi-space-6) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.mo-section:last-child { border-bottom: 0; padding-bottom: var(--mi-space-5); }
.mo-section-head { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-4); margin-bottom: var(--mi-space-2); }
.mo-section-head > div { display: flex; align-items: baseline; gap: var(--mi-space-3); min-width: 0; }
.mo-section-head h3 { margin: 0; font-size: var(--mi-fs-section); line-height: var(--mi-lh-section); font-weight: var(--mi-weight-semibold); }
.mo-section-head > span { color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); text-align: right; }
.mo-section-index { color: var(--mi-accent-dim); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); letter-spacing: var(--mi-tracking-label); }
.mo-section-description { max-width: var(--mi-copy-width); margin: 0 0 var(--mi-space-4); }
.mo-graph-wrap { position: relative; margin-inline: calc(var(--mi-space-4) * -1); overflow: hidden; border-block: var(--mi-border-width) solid var(--mi-border-subtle); background-color: var(--mi-bg-deep); background-image: radial-gradient(circle at var(--mi-graph-center-x) var(--mi-graph-center-y), color-mix(in oklch, var(--mi-accent) var(--mi-graph-background-accent), transparent), transparent var(--mi-graph-halo-stop)), linear-gradient(var(--mi-graph-grid) var(--mi-border-width), transparent var(--mi-border-width)), linear-gradient(90deg, var(--mi-graph-grid) var(--mi-border-width), transparent var(--mi-border-width)); background-size: auto, var(--mi-space-7) var(--mi-space-7), var(--mi-space-7) var(--mi-space-7); }
.mo-graph { display: block; width: 100%; min-height: var(--mi-graph-height); overflow: hidden; touch-action: none; user-select: none; }
.mo-graph.is-dragging { cursor: grabbing; }
.mo-graph.is-dragging .mo-node { cursor: grabbing; }
.mo-graph-skeleton { animation: mo-skeleton-enter var(--mi-duration-base) var(--mi-ease-standard); }
.mo-graph-skeleton line { stroke: var(--mi-border); stroke-width: var(--mi-border-width); opacity: var(--mi-opacity-skeleton); }
.mo-graph-skeleton circle { fill: var(--mi-border-strong); opacity: var(--mi-opacity-skeleton); }
.mo-graph-skeleton .mo-skeleton-hub { fill: var(--mi-accent-dim); opacity: var(--mi-opacity-used-ring); }
.mo-graph-skeleton text { fill: var(--mi-text-secondary); font-family: var(--mi-font-ui); font-size: var(--mi-fs-caption); letter-spacing: var(--mi-tracking-label); }
.mo-stable-graph { opacity: 0; }
.mo-stable-graph.is-ready { opacity: 1; animation: mo-stable-reveal var(--mi-duration-slow) var(--mi-ease-enter); }
.mo-field-halo { fill: url(#mo-hub-gradient); opacity: var(--mi-opacity-halo); }
/* 层级落点：只标环与数量，不参与亮度/重要度编码。 */
.mo-layer-ring { fill: none; stroke: var(--mi-border); stroke-width: var(--mi-border-width); stroke-dasharray: 64 10; opacity: .30; }
.mo-layer-label { fill: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); letter-spacing: var(--mi-tracking-label); opacity: .62; }
/* “无关联边”第二信号：虚线外圈，不覆盖注入/未注入的颜色编码。 */
.mo-isolated-mark { fill: none; stroke: var(--mi-border-strong); stroke-width: var(--mi-border-width); stroke-dasharray: 3 3; opacity: .55; pointer-events: none; }
.mo-layer-pending { margin-left: var(--mi-space-2); color: var(--mi-warning); }
.mo-hub-gradient-start { stop-color: var(--mi-accent); stop-opacity: var(--mi-opacity-halo-active); }
.mo-hub-gradient-end { stop-color: var(--mi-accent); stop-opacity: 0; }
.mo-signal-gradient-start { stop-color: var(--mi-accent-strong); stop-opacity: 1; }
.mo-signal-gradient-end { stop-color: var(--mi-accent); stop-opacity: 0; }
.mo-gradient-start { stop-color: var(--mi-graph-edge); stop-opacity: var(--mi-graph-edge-start-opacity); }
.mo-gradient-end { stop-color: var(--mi-graph-edge); stop-opacity: var(--mi-graph-edge-end-opacity); }
.mo-gradient-start.used { stop-color: var(--mi-accent-dim); stop-opacity: var(--mi-graph-flow-start-opacity); }
.mo-gradient-end.used { stop-color: var(--mi-accent); stop-opacity: var(--mi-graph-flow-end-opacity); }
.mo-edge, .mo-flow { fill: none; stroke-linecap: round; }
.mo-flow { opacity: var(--mi-opacity-flow); }
.mo-signal { fill: url(#mo-signal-gradient); filter: drop-shadow(var(--mi-glow-signal)); pointer-events: none; }
.mo-graph-wrap[data-layout-state='running'] .mo-signal { visibility: hidden; }
.mo-signal-soft { opacity: var(--mi-opacity-used-ring); }
.mo-hub line { stroke: var(--mi-border-strong); stroke-width: var(--mi-border-width); }
.mo-hub text { fill: var(--mi-text-secondary); font-family: var(--mi-font-ui); font-size: var(--mi-fs-caption); letter-spacing: var(--mi-tracking-label); }
.mo-hub-halo { fill: url(#mo-hub-gradient); opacity: var(--mi-opacity-halo); }
.mo-hub-ring { fill: var(--mi-surface); stroke: var(--mi-border-strong); stroke-width: var(--mi-border-width); }
.mo-hub-inner { fill: var(--mi-bg-deep); stroke: var(--mi-border); stroke-width: var(--mi-border-width); }
.mo-hub-core { fill: var(--mi-graph-node); }
.mo-hub.active .mo-hub-halo { animation: mo-hub-pulse var(--mi-duration-pulse) var(--mi-ease-standard) infinite; }
.mo-hub.active .mo-hub-ring { stroke: var(--mi-accent-dim); }
.mo-hub.active .mo-hub-core { fill: var(--mi-accent-strong); filter: drop-shadow(var(--mi-glow-node)); }
.mo-node { cursor: pointer; color: var(--mi-graph-node); outline: none; }
/* 注入不再换填充色：填充色承担层级亮度（同一色相才可跨层比亮度）。换 accent 会让
   project 注入节点的像素亮度高过 shared 未注入，层级就被抹平。注入信号改由 halo / used-ring /
   发光 / 1px accent 描边这组第二信号承担，观感仍最显眼。（旧编号 L1–L4 已废弃） */
.mo-node.selected { color: var(--mi-text); }
.mo-node.used.selected { color: var(--mi-accent-strong); }
.mo-neuron-core { fill: currentColor; opacity: var(--node-o); stroke: var(--mi-bg-deep); stroke-width: var(--mi-border-width); transform-box: fill-box; transform-origin: center; transition: opacity var(--mi-duration-fast) var(--mi-ease-standard); }
.mo-halo { fill: var(--mi-accent); opacity: 0; transform-box: fill-box; transform-origin: center; pointer-events: none; }
.mo-wave { fill: none; stroke: var(--mi-accent); stroke-width: 1px; opacity: 0; transform-box: fill-box; transform-origin: center; pointer-events: none; }
.mo-wave-glow { fill: var(--mi-accent); opacity: 0; pointer-events: none; }
.mo-state-ring { fill: none; stroke: var(--mi-text); stroke-width: var(--mi-focus-width); opacity: 0; transition: opacity var(--mi-duration-fast) var(--mi-ease-standard); pointer-events: none; }
.mo-used-ring { fill: none; stroke: var(--mi-accent); stroke-width: var(--mi-border-width); opacity: var(--mi-opacity-used-ring); pointer-events: none; }
.mo-node:hover .mo-state-ring, .mo-node:focus-visible .mo-state-ring { opacity: var(--mi-opacity-hover-ring); }
.mo-node.selected .mo-state-ring { stroke: var(--mi-accent-strong); opacity: 1; }
/* 悬停只在本层基线上再抬 0.18，不拉满 —— 避免交互状态把层级差抹平。 */
.mo-node:hover .mo-neuron-core { opacity: min(1, calc(var(--node-o) + .18)); }
/* 2026-11-04：去掉 `opacity: 1` 的硬覆盖。注入不动填充亮度，只加光晕/描边/呼吸，
   亮度仍由层级基线 `--node-o` 决定（注入值本身就是“层级基线 × 1.22”）。 */
.mo-node.used .mo-neuron-core { stroke: var(--mi-accent); filter: drop-shadow(var(--mi-glow-node)); animation: mo-node-breathe var(--breath-dur, 3.2s) var(--mi-ease-standard) infinite; animation-delay: var(--delay); }
.mo-node.used .mo-halo { opacity: var(--mi-opacity-halo); animation: mo-breathe var(--breath-dur, 3.2s) var(--mi-ease-standard) infinite; animation-delay: var(--delay); }
.mo-node.used .mo-used-ring { animation: mo-ring-pulse var(--breath-dur, 3.2s) var(--mi-ease-standard) infinite; animation-delay: var(--delay); }
/* 弱机降级：只停呼吸与中枢脉动，不删节点、不改坐标、不动字号。
   prefers-reduced-motion 的全局闸门在文件末尾，优先级更高。 */
.mo-stable-graph.is-motion-degraded .mo-node.used .mo-neuron-core,
.mo-stable-graph.is-motion-degraded .mo-node.used .mo-halo,
.mo-stable-graph.is-motion-degraded .mo-node.used .mo-used-ring,
.mo-stable-graph.is-motion-degraded .mo-hub.active .mo-hub-halo { animation: none; }
.mo-stable-graph.is-motion-degraded .mo-node.used .mo-halo { opacity: var(--mi-opacity-halo-active); }
.mo-graph.is-waving .mo-wave { animation: mo-injection-wave 1.1s var(--mi-ease-enter) both; animation-delay: var(--wave-delay, 0ms); }
.mo-graph.is-waving .mo-wave-glow { animation: mo-travel-glow 720ms var(--mi-ease-standard) both; animation-delay: var(--wave-delay, 0ms); }
.mo-node-label { fill: var(--mi-text); stroke: var(--mi-bg-deep); stroke-width: var(--mi-label-backdrop-width); paint-order: stroke; font-family: var(--mi-font-ui); font-size: var(--mi-fs-caption); opacity: 0; pointer-events: none; transition: opacity var(--mi-duration-fast) var(--mi-ease-standard); }
.mo-node:hover .mo-node-label, .mo-node.selected .mo-node-label, .mo-node:focus-visible .mo-node-label { opacity: 1; }
.mo-graph-caption { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-4); padding: var(--mi-space-3) var(--mi-space-4) 0; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mo-graph-legend { display: flex; flex-wrap: wrap; gap: var(--mi-space-4); }
.mo-graph-legend span { display: inline-flex; align-items: center; gap: var(--mi-space-2); }
.mo-graph-legend i { width: var(--mi-node-legend-dot); height: var(--mi-node-legend-dot); border-radius: var(--mi-radius-lg); background: var(--mi-graph-node); }
.mo-graph-legend i.is-live { background: var(--mi-accent); box-shadow: var(--mi-glow-node); }
.mo-graph-legend b { width: var(--mi-node-legend-dot); height: var(--mi-node-legend-dot); border: var(--mi-border-width) solid var(--mi-accent-strong); border-radius: var(--mi-radius-lg); }
.mo-graph-legend i.is-isolated { background: transparent; border: var(--mi-border-width) dashed var(--mi-border-strong); box-shadow: none; }
.mo-graph-scale { text-align: right; }
.mo-visible-note { margin: 0; padding: var(--mi-space-1) var(--mi-space-4) var(--mi-space-3); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mo-detail { position: absolute; z-index: var(--mi-z-drawer); top: var(--mi-space-3); right: var(--mi-space-3); bottom: var(--mi-space-3); width: min(var(--mi-detail-width), calc(100% - var(--mi-space-7))); padding: var(--mi-space-5); overflow-y: auto; border: var(--mi-border-width) solid var(--mi-border-strong); border-radius: var(--mi-radius-sm); background: color-mix(in oklch, var(--mi-surface-raised) 96%, transparent); box-shadow: var(--mi-shadow-drawer); backdrop-filter: blur(var(--mi-backdrop-blur)); }
.mo-selected-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--mi-space-3); padding-bottom: var(--mi-space-4); border-bottom: var(--mi-border-width) solid var(--mi-border); }
.mo-selected-head > div { min-width: 0; }
.mo-selected-head small { display: block; margin-bottom: var(--mi-space-2); color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mo-selected-head small.live { color: var(--mi-accent-strong); }
.mo-selected-head h4 { margin: 0; overflow-wrap: anywhere; font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); font-weight: var(--mi-weight-medium); }
.mo-selected-head button { flex: 0 0 var(--mi-icon-button); width: var(--mi-icon-button); height: var(--mi-icon-button); padding: 0; border: var(--mi-border-width) solid var(--mi-border); border-radius: var(--mi-radius-sm); background: transparent; color: var(--mi-text-secondary); font-size: var(--mi-fs-title); line-height: 1; cursor: pointer; }
.mo-selected-head button:hover { color: var(--mi-text); border-color: var(--mi-border-strong); background: var(--mi-hover); }
.mo-detail-meta { display: grid; grid-template-columns: minmax(var(--mi-detail-label-width), 0.42fr) minmax(0, 1fr); margin: var(--mi-space-4) 0 var(--mi-space-5); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mo-detail-meta dt, .mo-detail-meta dd { padding: var(--mi-space-2) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.mo-detail-meta dt { color: var(--mi-text-muted); }
.mo-detail-meta dd { margin: 0; overflow-wrap: anywhere; color: var(--mi-text); text-align: right; }
.mo-detail-meta dd small { display: block; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.mo-detail-copy h5 { margin: 0 0 var(--mi-space-2); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); font-weight: var(--mi-weight-medium); }
.mo-detail pre, .mo-disclosure pre { max-width: var(--mi-copy-width); max-height: var(--mi-pre-height); margin: 0; overflow: auto; white-space: pre-wrap; word-break: break-word; color: var(--mi-text-secondary); font: var(--mi-fs-secondary)/var(--mi-lh-secondary) var(--mi-font-mono); }
.mo-evidence { margin: var(--mi-space-4) 0 0; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mo-evidence strong { display: block; margin-bottom: var(--mi-space-1); color: var(--mi-text-secondary); font-weight: var(--mi-weight-medium); }
.mo-list, .mo-catalog { display: flex; flex-direction: column; }
.mo-catalog { max-height: var(--mi-catalog-height); overflow-y: auto; }
.mo-catalog-more { align-self: center; margin-top: var(--mi-space-3); padding: var(--mi-space-2) var(--mi-space-4); border: var(--mi-border-width) solid var(--mi-border-level-2); border-radius: var(--mi-radius-control); background: var(--mi-surface-level-2); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); cursor: pointer; }
.mo-catalog-more:hover { background: var(--mi-hover); color: var(--mi-text); }
.mo-memory-row { display: grid; grid-template-columns: var(--mi-space-7) minmax(0, 1fr) var(--mi-space-8); align-items: center; gap: var(--mi-space-3); width: 100%; min-height: var(--mi-space-8); padding: var(--mi-space-3) var(--mi-space-2); border: 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); background: transparent; color: var(--mi-text); text-align: left; cursor: pointer; transition: background var(--mi-duration-fast) var(--mi-ease-standard); }
.mo-memory-row:hover, .mo-memory-row:focus-visible { background: var(--mi-hover); }
.mo-row-index { display: inline-flex; align-items: center; gap: var(--mi-space-2); color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); }
.mo-row-copy { min-width: 0; }
/* 标题最多两行：任务名经常长过一行，单行省略只剩「…endpoi…」，读不出是哪条记忆。
   摘要仍是单行，避免每条都长成小作文把目录撑散。 */
.mo-row-copy strong, .mo-row-copy small { overflow: hidden; }
.mo-row-copy strong {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  white-space: normal;
  overflow-wrap: anywhere;
  font-size: var(--mi-fs-body);
  line-height: var(--mi-lh-body);
  font-weight: var(--mi-weight-medium);
}
.mo-row-copy small { display: block; text-overflow: ellipsis; white-space: nowrap; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mo-row-value { color: var(--mi-text-secondary); font-family: var(--mi-font-mono); font-size: var(--mi-fs-body); text-align: right; }
.mo-row-value small { display: block; color: var(--mi-text-muted); font-family: var(--mi-font-ui); font-size: var(--mi-fs-caption); }
.mo-memory-row.dim .mo-row-copy strong { color: var(--mi-text-secondary); }
.mo-live-dot, .mo-catalog-light { flex: 0 0 var(--mi-status-dot); width: var(--mi-status-dot); height: var(--mi-status-dot); border-radius: var(--mi-radius-lg); background: var(--mi-accent); box-shadow: var(--mi-glow-node); }
.mo-catalog-light { background: var(--mi-border-strong); box-shadow: none; }
.mo-catalog-light.on { background: var(--mi-accent); box-shadow: var(--mi-glow-node); }
.mo-disclosure { border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.mo-disclosure summary { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-3); min-height: var(--mi-control-height); padding: var(--mi-space-2) 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); cursor: pointer; list-style: none; }
.mo-disclosure summary::-webkit-details-marker { display: none; }
.mo-disclosure summary::after { content: '+'; flex: 0 0 auto; color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-section); }
.mo-disclosure[open] summary::after { content: '−'; color: var(--mi-accent); }
.mo-disclosure pre { margin: var(--mi-space-1) 0 var(--mi-space-4); }
.mo-rule summary { justify-content: flex-start; }
.mo-rule summary::after { margin-left: auto; }
.mo-source { flex: 0 0 var(--mi-select-width); color: var(--mi-accent-dim); font-size: var(--mi-fs-caption); }
.mo-rule-title { min-width: 0; color: var(--mi-text); }
.mo-check { display: grid; grid-template-columns: var(--mi-select-width) minmax(0, 1fr); gap: var(--mi-space-5); padding: var(--mi-space-4) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.mo-check-head strong, .mo-check-head span { display: block; font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mo-check-head strong { color: var(--mi-text-secondary); font-weight: var(--mi-weight-semibold); }
.mo-check-head span { color: var(--mi-text-muted); }
.mo-check.is-pass .mo-check-head strong { color: var(--mi-success); }
.mo-check.is-fail .mo-check-head strong { color: var(--mi-danger); }
.mo-check.is-unknown .mo-check-head strong { color: var(--mi-warning); }
.mo-check-copy p { max-width: var(--mi-copy-width); margin: 0 0 var(--mi-space-1); color: var(--mi-text); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); }
.mo-check-copy small { display: block; max-width: var(--mi-copy-width); color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mo-inline-empty { display: flex; align-items: flex-start; gap: var(--mi-space-3); padding: var(--mi-space-4) 0; color: var(--mi-text-secondary); }
.mo-inline-empty > i { flex: 0 0 var(--mi-status-dot); width: var(--mi-status-dot); height: var(--mi-status-dot); margin-top: var(--mi-space-2); border: var(--mi-border-width) solid var(--mi-border-strong); border-radius: var(--mi-radius-lg); }
.mo-inline-empty strong { font-size: var(--mi-fs-body); font-weight: var(--mi-weight-medium); }
.mo-inline-empty p { max-width: var(--mi-copy-width); margin: var(--mi-space-1) 0 0; color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mo-state { display: flex; flex-direction: column; align-items: flex-start; justify-content: center; min-height: var(--mi-graph-height); padding: var(--mi-space-7) var(--mi-space-6); border-block: var(--mi-border-width) solid var(--mi-border-subtle); background: var(--mi-bg-deep); }
.mo-state-icon { width: var(--mi-space-6); height: var(--mi-space-6); margin-bottom: var(--mi-space-5); fill: none; stroke: var(--mi-accent-dim); stroke-width: var(--mi-border-width); }
.mo-state strong { font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); font-weight: var(--mi-weight-medium); }
.mo-state p { max-width: var(--mi-copy-width); margin: var(--mi-space-3) 0 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); }
.mo-state small { margin-top: var(--mi-space-2); color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); }
.mo-state.is-permission .mo-state-icon { stroke: var(--mi-warning); }
.mo-state-action { min-height: var(--mi-control-height); margin-top: var(--mi-space-5); padding: 0 var(--mi-space-4); border: var(--mi-border-width) solid var(--mi-border-level-3); border-radius: var(--mi-radius-control); background: var(--mi-surface-level-2); color: var(--mi-text); font-size: var(--mi-fs-secondary); cursor: pointer; }
.mo-state-action:hover { border-color: var(--mi-accent-dim); background: var(--mi-hover); }
.mo-state-embedded { min-height: var(--mi-graph-height); margin-inline: calc(var(--mi-space-4) * -1); }
.mo-loading-orbit { position: relative; width: var(--mi-space-8); height: var(--mi-space-8); margin-bottom: var(--mi-space-5); }
.mo-loading-orbit i { position: absolute; inset: 0; border: var(--mi-border-width) solid var(--mi-border); border-radius: var(--mi-radius-lg); }
.mo-loading-orbit i:nth-child(2) { inset: var(--mi-space-3); border-color: var(--mi-accent-dim); }
.mo-loading-orbit i:nth-child(3) { inset: var(--mi-space-5); border: 0; background: var(--mi-accent); box-shadow: var(--mi-glow-node); animation: mo-loading var(--mi-duration-flow) var(--mi-ease-standard) infinite; }
.mo-loading-lines { display: flex; flex-direction: column; gap: var(--mi-space-2); width: min(var(--mi-detail-width), 100%); margin-top: var(--mi-space-5); }
.mo-loading-lines i { height: var(--mi-space-1); background: var(--mi-surface-raised); opacity: var(--mi-opacity-skeleton); animation: mo-skeleton var(--mi-duration-flow) var(--mi-ease-standard) infinite; }
.mo-loading-lines i:nth-child(2) { width: var(--mi-skeleton-line-medium); }
.mo-loading-lines i:nth-child(3) { width: var(--mi-skeleton-line-short); }
.mo-slide-enter-active, .mo-slide-leave-active { transition: opacity var(--mi-duration-base) var(--mi-ease-standard), transform var(--mi-duration-base) var(--mi-ease-standard); }
.mo-slide-enter-from, .mo-slide-leave-to { opacity: 0; transform: translateX(var(--mi-space-4)); }
/* 呼吸只在层级基线的 78%–100% 之间起伏，不冲到 1 —— 否则动画又把层级抹平。 */
@keyframes mo-node-breathe { 0%, 100% { opacity: calc(var(--node-o) * .78); transform: scale(1); } 50% { opacity: var(--node-o); transform: scale(calc(1 + var(--breath-amp, .08))); } }
@keyframes mo-breathe { 0%, 100% { opacity: var(--mi-opacity-halo); transform: scale(1); } 50% { opacity: var(--mi-opacity-halo-active); transform: scale(calc(1 + var(--breath-amp, .08))); } }
@keyframes mo-injection-wave { 0% { opacity: 0; transform: scale(.72); } 16% { opacity: var(--wave-amp, .5); } 100% { opacity: 0; transform: scale(1.55); } }
@keyframes mo-travel-glow { 0%, 100% { opacity: 0; } 28% { opacity: var(--wave-amp, .5); } }
@keyframes mo-ring-pulse { 0%, 100% { opacity: var(--mi-opacity-used-ring); } 50% { opacity: 1; } }
@keyframes mo-hub-pulse { 0%, 100% { opacity: var(--mi-opacity-halo); } 50% { opacity: var(--mi-opacity-halo-active); } }
@keyframes mo-skeleton-enter { from { opacity: 0; transform: scale(var(--mi-reveal-scale)); } to { opacity: 1; transform: scale(1); } }
@keyframes mo-stable-reveal { from { opacity: 0; } to { opacity: 1; } }
@keyframes mo-loading { 0%, 100% { opacity: var(--mi-opacity-skeleton); } 50% { opacity: 1; } }
@keyframes mo-skeleton { 0%, 100% { opacity: var(--mi-opacity-skeleton); } 50% { opacity: 1; } }
@media (max-width: 1100px) {
  .mo-graph { min-height: var(--mi-graph-height-compact); }
  .mo-detail { width: min(var(--mi-detail-width-compact), calc(100% - var(--mi-space-7))); padding: var(--mi-space-4); }
  .mo-summary > div { padding-inline: var(--mi-space-4); }
  .mo-graph-scale { display: none; }
}
@media (max-width: 640px) {
  .mo-summary > div { display: flex; flex-direction: column; align-items: flex-start; gap: 0; padding-inline: var(--mi-space-3); }
  .mo-summary strong { order: -1; }
  .mo-snapshot-meta { flex-direction: column; gap: 0; }
  .mo-detail { inset: 0; width: 100%; border-radius: 0; }
  .mo-check { grid-template-columns: 1fr; gap: var(--mi-space-2); }
  .mo-graph-caption { align-items: flex-start; }
}
@media (prefers-reduced-motion: reduce) {
  .mo, .mo *, .mo *::before, .mo *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
  .mo-signal { display: none; }
  .mo-wave { display: none; }
  .mo-wave-glow { display: none; }
  .mo-node.used .mo-neuron-core { opacity: var(--node-o); filter: drop-shadow(var(--mi-glow-node)); }
  .mo-node.used .mo-halo { opacity: var(--mi-opacity-halo-active); }
  .mo-node.used .mo-used-ring { opacity: 1; }
  .mo-flow { opacity: var(--mi-opacity-flow); }
}
</style>
