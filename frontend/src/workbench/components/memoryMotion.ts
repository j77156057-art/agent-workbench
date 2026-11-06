// 记忆神经网的动效引擎：沿边传导脉冲 / 注入激活波 / 重要度呼吸 / 拖拽力导向。
//
// 设计约束（2026-11-04）：
// - 单一 rAF 驱动，所有位置写入都用 setAttribute / style.transform，绕开 Vue 响应式；
// - 每帧只跑「当前注入边（≤24 条）× 头尾两点」和「拖拽邻域（≤36 节点）」，
//   稳态空闲时每帧工作量在 0.5ms 量级；
// - 每 30 帧把实测最大帧耗时写到 svg[data-motion-frame-ms]，便于外部验收；
// - reduced-motion 下不启动 rAF、不挂拖拽，脉冲元素由 CSS 全局闸门隐藏。
import { MEMORY_GRAPH } from './memoryGraphTokens'
import { MotionDegrade, shouldPaintPulse, type CapabilityTier } from './motionDegrade'

export interface MotionNode {
  key: string
  x: number
  y: number
  radius: number
  importance: number
  used: boolean
}
export interface MotionLink {
  id: string
  source: string
  target: string
  bend: number
  width: number
  used: boolean
}

const M = MEMORY_GRAPH.motion

interface Offset { dx: number; dy: number; vx: number; vy: number }
interface Pulse {
  el: SVGCircleElement
  a: MotionNode
  b: MotionNode
  bend: number
  duration: number
  phase: number
  head: boolean
}
interface Affected {
  key: string
  dirX: number
  dirY: number
  influence: number
  offset: Offset
}
interface DragState {
  key: string
  originX: number
  originY: number
  dx: number
  dy: number
  pointerId: number
  moved: boolean
  released: boolean
  last: number
  releasedAt: number
  self: Offset
  affected: Affected[]
}

function quadPoint(a: MotionNode, b: MotionNode, bend: number, t: number) {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const cx = (a.x + b.x) / 2 - dy * bend
  const cy = (a.y + b.y) / 2 + dx * bend
  const u = 1 - t
  return {
    x: u * u * a.x + 2 * u * t * cx + t * t * b.x,
    y: u * u * a.y + 2 * u * t * cy + t * t * b.y,
  }
}

function curveLength(a: MotionNode, b: MotionNode, bend: number) {
  let length = 0
  let px = a.x
  let py = a.y
  for (let index = 1; index <= 24; index += 1) {
    const point = quadPoint(a, b, bend, index / 24)
    length += Math.hypot(point.x - px, point.y - py)
    px = point.x
    py = point.y
  }
  return length
}

function pathD(ax: number, ay: number, bx: number, by: number, bend: number) {
  const dx = bx - ax
  const dy = by - ay
  const cx = (ax + bx) / 2 - dy * bend
  const cy = (ay + by) / 2 + dx * bend
  return `M ${ax.toFixed(2)} ${ay.toFixed(2)} Q ${cx.toFixed(2)} ${cy.toFixed(2)} ${bx.toFixed(2)} ${by.toFixed(2)}`
}

export class MemoryMotion {
  private svg: SVGSVGElement | null = null
  private nodes = new Map<string, MotionNode>()
  private links = new Map<string, MotionLink>()
  private nodeEls = new Map<string, SVGGElement>()
  private linkEls = new Map<string, SVGGraphicsElement[]>()
  private linkWidths = new Map<string, number>()
  private incident = new Map<string, string[]>()
  private pulses: Pulse[] = []
  private raf = 0
  private startedAt = 0
  private reduced = false
  private frameMax = 0
  private frameCount = 0
  private peakFrame = 0
  private drag: DragState | null = null
  private suppressClick = false
  private onFrame?: (workMs: number) => void
  // 弱机降级：帧超支率（已按 vsync 归一化）连续超阈值时把脉冲降到每 2 帧、停呼吸；
  // 只调节奏，不改节点/坐标/字号。阈值与滞回参数在 memoryGraphTokens.motion.degrade。
  private degrade = new MotionDegrade(M.degrade)
  private profileTier: CapabilityTier = 'mid'
  private lastFrameAt = 0
  private pulseFrame = 0
  private pulsePaints = 0
  private degradeCount = 0

  constructor(onFrame?: (workMs: number) => void) { this.onFrame = onFrame }

  /**
   * 应用静态能力画像：low 时开局直接预降级（不挂激活波、停呼吸），
   * 但运行期仍可被观测升回（连续 120 帧超支率 <10%）。
   */
  setProfileTier(tier: CapabilityTier) {
    this.profileTier = tier
    if (!this.svg) return
    if (this.degrade.seedTier(tier)) this.applyDegraded()
  }

  get isDegraded() { return this.degrade.isDegraded }

  get overrunPercent() { return this.degrade.lastOverrunRatio * 100 }

  get refreshMs() { return this.degrade.lastRefreshMs }

  attach(svg: SVGSVGElement, nodes: MotionNode[], links: MotionLink[], reduced: boolean) {
    this.detach()
    this.svg = svg
    this.reduced = reduced
    this.nodes = new Map(nodes.map(node => [node.key, node]))
    this.links = new Map(links.map(link => [link.id, link]))
    this.incident = new Map()
    for (const link of links) {
      this.addIncident(link.source, link.id)
      this.addIncident(link.target, link.id)
    }
    for (const el of svg.querySelectorAll<SVGGElement>('.mo-node[data-node-key]')) {
      const key = el.dataset.nodeKey
      if (key) this.nodeEls.set(key, el)
    }
    for (const el of svg.querySelectorAll<SVGGraphicsElement>('[data-link-id]')) {
      const id = el.dataset.linkId
      if (!id) continue
      const list = this.linkEls.get(id) || []
      list.push(el)
      this.linkEls.set(id, list)
      if (!this.linkWidths.has(id)) {
        const width = parseFloat(getComputedStyle(el).strokeWidth)
        this.linkWidths.set(id, Number.isFinite(width) ? width : 1)
      }
    }
    this.applyBreathing()
    if (!reduced) this.buildPulses()
    this.degrade.reset()
    this.degrade.seedTier(this.profileTier)
    this.lastFrameAt = 0
    this.pulseFrame = 0
    this.pulsePaints = 0
    this.degradeCount = 0
    svg.dataset.motionDegraded = this.degrade.isDegraded ? '1' : '0'
    svg.dataset.motionDegradeCount = '0'
    svg.dataset.motionPulseUpdates = '0'
    svg.dataset.motionOverrunPct = '0'
    svg.dataset.motionRefreshMs = this.degrade.lastRefreshMs.toFixed(1)
    svg.classList.toggle('is-motion-degraded', this.degrade.isDegraded)
    svg.dataset.motionLinks = String(this.links.size)
    svg.dataset.motionPulses = String(this.pulses.length)
    if (!reduced) {
      svg.addEventListener('pointerdown', this.onPointerDown)
      svg.addEventListener('click', this.onClickCapture, true)
      this.startedAt = performance.now()
      this.raf = requestAnimationFrame(this.tick)
    }
  }

  detach() {
    if (this.svg) {
      this.svg.removeEventListener('pointerdown', this.onPointerDown)
      this.svg.removeEventListener('click', this.onClickCapture, true)
      this.svg.classList.remove('is-motion-degraded')
    }
    if (this.raf) cancelAnimationFrame(this.raf)
    this.raf = 0
    this.resetDrag()
    this.pulses = []
    this.nodeEls.clear()
    this.linkEls.clear()
    this.linkWidths.clear()
    this.incident.clear()
    this.nodes.clear()
    this.links.clear()
    this.svg = null
  }

  /** 注入激活波：按图上的 BFS 跳数给每个节点排延迟，越远越晚、越弱。 */
  applyWave() {
    if (!this.svg || this.reduced) return
    const distance = new Map<string, number>()
    const queue: string[] = []
    for (const node of this.nodes.values()) {
      if (node.used && node.key !== 'core') { distance.set(node.key, 0); queue.push(node.key) }
    }
    for (let head = 0; head < queue.length; head += 1) {
      const key = queue[head]
      const depth = distance.get(key) || 0
      if (depth >= M.waveMaxHops) continue
      for (const id of this.incident.get(key) || []) {
        const link = this.links.get(id)
        if (!link) continue
        const other = link.source === key ? link.target : link.source
        if (other === 'core' || distance.has(other)) continue
        distance.set(other, depth + 1)
        queue.push(other)
      }
    }
    for (const [key, el] of this.nodeEls) {
      const depth = distance.get(key)
      if (depth === undefined) continue
      const delay = Math.min(depth, M.waveMaxHops) * M.waveHopMs
      const amp = M.waveAmp * Math.max(0.18, 1 - depth / (M.waveMaxHops + 2))
      el.style.setProperty('--wave-delay', `${delay}ms`)
      el.style.setProperty('--wave-amp', amp.toFixed(3))
    }
    this.svg.dataset.motionWave = String(distance.size)
    this.svg.dataset.motionWaveSeeds = String(distance.size > 0
      ? [...this.nodes.values()].filter(node => node.used && node.key !== 'core').length : 0)
  }

  private addIncident(key: string, id: string) {
    const list = this.incident.get(key) || []
    if (!list.includes(id)) list.push(id)
    this.incident.set(key, list)
  }

  private applyBreathing() {
    for (const [key, el] of this.nodeEls) {
      const node = this.nodes.get(key)
      if (!node) continue
      const importance = Math.max(0, Math.min(1, node.importance))
      const duration = M.breathMax - importance * (M.breathMax - M.breathMin)
      const amp = M.breathAmpMin + importance * (M.breathAmpMax - M.breathAmpMin)
      el.style.setProperty('--breath-dur', `${duration.toFixed(2)}s`)
      el.style.setProperty('--breath-amp', amp.toFixed(3))
    }
  }

  private buildPulses() {
    const used = [...this.links.values()].filter(link => link.used).slice(0, M.maxPulseLinks)
    const circles = new Map<string, SVGCircleElement>()
    for (const el of this.svg?.querySelectorAll<SVGCircleElement>('[data-flow-signal]') || []) {
      const id = el.dataset.signalLink
      const variant = el.dataset.pulseVariant
      if (id && variant) circles.set(`${id}:${variant}`, el)
    }
    this.pulses = []
    for (const link of used) {
      const a = this.nodes.get(link.source)
      const b = this.nodes.get(link.target)
      if (!a || !b) continue
      const length = curveLength(a, b, link.bend)
      const duration = Math.max(0.55, length / M.pulseSpeed)
      const phase = ((this.pulses.length / 2) % 7) * 0.14
      for (const [variant, head] of [['head', true], ['tail', false]] as const) {
        const el = circles.get(`${link.id}:${variant}`)
        if (!el) continue
        this.pulses.push({ el, a, b, bend: link.bend, duration, head, phase })
      }
    }
  }

  private tick = (now: number) => {
    this.raf = 0
    if (!this.svg) return
    const startedAt = performance.now()
    const visible = !document.hidden
    if (visible) {
      // 只把「真正显示出来的帧」喂给判据；标签页隐藏时的长间隔不算弱机。
      const frameMs = this.lastFrameAt ? now - this.lastFrameAt : 0
      this.lastFrameAt = now
      if (this.degrade.report(frameMs)) this.applyDegraded()
    } else {
      this.lastFrameAt = now
    }
    const elapsed = (now - this.startedAt) / 1000
    this.pulseFrame = (this.pulseFrame + 1) % M.degrade.pulseEvery
    // 正常路径每帧都画（与改造前完全一致）；只有降级时按 pulseEvery 抽帧。
    if (visible && shouldPaintPulse(this.pulseFrame, this.degrade.isDegraded, M.degrade.pulseEvery)) {
      this.paintPulses(elapsed)
      this.pulsePaints += 1
    }
    if (this.drag) this.stepDrag(now)
    const work = performance.now() - startedAt
    this.frameMax = Math.max(this.frameMax, work)
    this.peakFrame = Math.max(this.peakFrame, work)
    this.frameCount += 1
    if (this.frameCount % 30 === 0) {
      this.svg.dataset.motionFrameMs = this.frameMax.toFixed(2)
      this.svg.dataset.motionPulseUpdates = String(this.pulsePaints)
      this.svg.dataset.motionOverrunPct = (this.degrade.lastOverrunRatio * 100).toFixed(1)
      this.svg.dataset.motionRefreshMs = this.degrade.lastRefreshMs.toFixed(1)
      this.frameMax = 0
    }
    this.onFrame?.(work)
    this.raf = requestAnimationFrame(this.tick)
  }

  /** 降级状态翻转：只动 class 与观测字段，不改节点、坐标、字号。 */
  private applyDegraded() {
    const svg = this.svg
    if (!svg) return
    const degraded = this.degrade.isDegraded
    this.degradeCount += 1
    // 进入/退出都从 0 开始计数，保证换挡后的第一帧就出画，不出现空档。
    this.pulseFrame = 0
    svg.classList.toggle('is-motion-degraded', degraded)
    svg.dataset.motionDegraded = degraded ? '1' : '0'
    svg.dataset.motionDegradeCount = String(this.degradeCount)
  }

  private paintPulses(elapsed: number) {
    for (const pulse of this.pulses) {
      let t = (elapsed / pulse.duration + pulse.phase) % 1
      if (!pulse.head) t = (t + 1 - M.trailRatio) % 1
      const point = quadPoint(pulse.a, pulse.b, pulse.bend, t)
      pulse.el.setAttribute('cx', point.x.toFixed(1))
      pulse.el.setAttribute('cy', point.y.toFixed(1))
      // 两端淡入淡出，避免脉冲在节点上"跳变"；下限保住中段可读性。
      const edge = Math.min(t, 1 - t) * 8
      pulse.el.style.opacity = String((pulse.head ? 1 : 0.5) * Math.min(1, Math.max(0.28, edge)))
    }
  }

  private toLocal(event: PointerEvent) {
    const svg = this.svg
    if (!svg) return null
    const matrix = svg.getScreenCTM()
    if (!matrix) return null
    const point = svg.createSVGPoint()
    point.x = event.clientX
    point.y = event.clientY
    return point.matrixTransform(matrix.inverse())
  }

  private onPointerDown = (event: PointerEvent) => {
    if (this.reduced || event.button !== 0 || !this.svg) return
    const target = event.target as Element | null
    const group = target?.closest?.('.mo-node') as SVGGElement | null
    const key = group?.dataset.nodeKey
    if (!key || key === 'core') return
    const node = this.nodes.get(key)
    const local = this.toLocal(event)
    if (!node || !local) return
    const affected = this.collectAffected(key)
    // 验收可读：这次拖拽真实影响了多少个节点（0 = 该节点无关联边）。
    if (this.svg) {
      this.svg.dataset.dragKey = key
      this.svg.dataset.dragAffected = String(affected.length)
    }
    this.drag = {
      key, originX: node.x, originY: node.y, dx: 0, dy: 0, pointerId: event.pointerId,
      moved: false, released: false, last: performance.now(), releasedAt: 0,
      self: { dx: 0, dy: 0, vx: 0, vy: 0 }, affected,
    }
    window.addEventListener('pointermove', this.onPointerMove)
    window.addEventListener('pointerup', this.onPointerUp)
    window.addEventListener('pointercancel', this.onPointerUp)
    event.preventDefault()
  }

  private onPointerMove = (event: PointerEvent) => {
    const drag = this.drag
    const local = this.toLocal(event)
    if (!drag || drag.released || !local) return
    drag.dx = local.x - drag.originX
    drag.dy = local.y - drag.originY
    if (Math.hypot(drag.dx, drag.dy) > M.clickSuppressPx && !drag.moved) {
      drag.moved = true
      this.suppressClick = true
      this.svg?.classList.add('is-dragging')
    }
  }

  private onPointerUp = () => {
    const drag = this.drag
    window.removeEventListener('pointermove', this.onPointerMove)
    window.removeEventListener('pointerup', this.onPointerUp)
    window.removeEventListener('pointercancel', this.onPointerUp)
    if (!drag) return
    if (!drag.moved) {
      this.drag = null
      this.clearOffsets()
      return
    }
    drag.released = true
    drag.releasedAt = performance.now()
    drag.last = drag.releasedAt
  }

  private onClickCapture = (event: Event) => {
    if (!this.suppressClick) return
    this.suppressClick = false
    event.stopPropagation()
    event.preventDefault()
  }

  private collectAffected(startKey: string): Affected[] {
    const seen = new Set([startKey])
    let frontier = [startKey]
    for (let depth = 0; depth < M.dragHops; depth += 1) {
      const next: string[] = []
      for (const key of frontier) {
        for (const id of this.incident.get(key) || []) {
          const link = this.links.get(id)
          if (!link) continue
          const other = link.source === key ? link.target : link.source
          if (other === 'core' || seen.has(other)) continue
          seen.add(other)
          next.push(other)
          if (seen.size >= M.dragMaxNodes) break
        }
        if (seen.size >= M.dragMaxNodes) break
      }
      frontier = next
      if (!frontier.length || seen.size >= M.dragMaxNodes) break
    }
    seen.delete(startKey)
    const origin = this.nodes.get(startKey)
    const out: Affected[] = []
    if (!origin) return out
    for (const key of seen) {
      const node = this.nodes.get(key)
      if (!node) continue
      const distance = Math.hypot(node.x - origin.x, node.y - origin.y)
      if (distance < 1 || distance > M.dragReach) continue
      const influence = (1 - distance / M.dragReach) ** 2
      out.push({
        key, dirX: (node.x - origin.x) / distance, dirY: (node.y - origin.y) / distance,
        influence, offset: { dx: 0, dy: 0, vx: 0, vy: 0 },
      })
    }
    return out
  }

  private stepDrag(now: number) {
    const drag = this.drag
    if (!drag) return
    const dt = Math.max(0.001, Math.min(0.05, (now - drag.last) / 1000))
    drag.last = now
    const offsets = new Map<string, Offset>()
    if (!drag.released) {
      drag.self.dx = drag.dx
      drag.self.dy = drag.dy
      offsets.set(drag.key, drag.self)
      const length = Math.hypot(drag.dx, drag.dy)
      for (const item of drag.affected) {
        const targetX = item.dirX * length * item.influence * M.dragPush + drag.dx * item.influence * 0.08
        const targetY = item.dirY * length * item.influence * M.dragPush + drag.dy * item.influence * 0.08
        this.spring(item.offset, targetX, targetY, M.dragStiffness, M.dragDamping, dt)
        offsets.set(item.key, item.offset)
      }
    } else {
      this.spring(drag.self, 0, 0, M.releaseStiffness, M.releaseDamping, dt)
      offsets.set(drag.key, drag.self)
      let energy = Math.max(Math.abs(drag.self.dx), Math.abs(drag.self.dy))
      for (const item of drag.affected) {
        this.spring(item.offset, 0, 0, M.releaseStiffness, M.releaseDamping, dt)
        energy = Math.max(energy, Math.abs(item.offset.dx), Math.abs(item.offset.dy), Math.abs(item.offset.vx) * 0.05, Math.abs(item.offset.vy) * 0.05)
        offsets.set(item.key, item.offset)
      }
      if (energy < 0.05 || now - drag.releasedAt > 1200) {
        this.finishDrag()
        return
      }
    }
    this.paintOffsets(offsets)
  }

  private spring(offset: Offset, targetX: number, targetY: number, stiffness: number, damping: number, dt: number) {
    offset.vx += (targetX - offset.dx) * stiffness * dt
    offset.vy += (targetY - offset.dy) * stiffness * dt
    const decay = Math.exp(-damping * dt)
    offset.vx *= decay
    offset.vy *= decay
    offset.dx += offset.vx * dt
    offset.dy += offset.vy * dt
  }

  private paintOffsets(offsets: Map<string, Offset>) {
    const touched = new Set<string>()
    for (const [key, offset] of offsets) {
      const el = this.nodeEls.get(key)
      if (!el) continue
      const moving = Math.abs(offset.dx) > 0.05 || Math.abs(offset.dy) > 0.05
      el.style.transform = moving ? `translate(${offset.dx.toFixed(2)}px, ${offset.dy.toFixed(2)}px)` : ''
      if (!moving) continue
      for (const id of this.incident.get(key) || []) touched.add(id)
    }
    for (const id of touched) this.paintLink(id, offsets)
  }

  private paintLink(id: string, offsets: Map<string, Offset>) {
    const link = this.links.get(id)
    if (!link) return
    const a = this.nodes.get(link.source)
    const b = this.nodes.get(link.target)
    const els = this.linkEls.get(id)
    if (!a || !b || !els) return
    const ao = offsets.get(a.key)
    const bo = offsets.get(b.key)
    const ax = a.x + (ao?.dx || 0)
    const ay = a.y + (ao?.dy || 0)
    const bx = b.x + (bo?.dx || 0)
    const by = b.y + (bo?.dy || 0)
    const base = Math.max(1, Math.hypot(b.x - a.x, b.y - a.y))
    const stretch = Math.max(0, Math.hypot(bx - ax, by - ay) / base - 1)
    const width = this.linkWidths.get(id) || link.width
    const d = pathD(ax, ay, bx, by, link.bend)
    for (const el of els) {
      el.setAttribute('d', d)
      // 张力感：被拉长的边会略微变细，松开后恢复。
      el.setAttribute('stroke-width', (width * (1 - Math.min(0.24, stretch * 0.5))).toFixed(2))
    }
  }

  private finishDrag() {
    this.resetDrag()
  }

  private resetDrag() {
    if (this.drag) {
      window.removeEventListener('pointermove', this.onPointerMove)
      window.removeEventListener('pointerup', this.onPointerUp)
      window.removeEventListener('pointercancel', this.onPointerUp)
    }
    this.drag = null
    this.suppressClick = false
    this.svg?.classList.remove('is-dragging')
    this.clearOffsets()
  }

  private clearOffsets() {
    for (const el of this.nodeEls.values()) el.style.transform = ''
    for (const [id, els] of this.linkEls) {
      const link = this.links.get(id)
      if (!link) continue
      const a = this.nodes.get(link.source)
      const b = this.nodes.get(link.target)
      if (!a || !b) continue
      const d = pathD(a.x, a.y, b.x, b.y, link.bend)
      const width = this.linkWidths.get(id) || link.width
      for (const el of els) {
        el.setAttribute('d', d)
        el.setAttribute('stroke-width', width.toFixed(2))
      }
    }
  }

  /** 供验收脚本读取的峰值每帧 JS 耗时。 */
  peakWorkMs() { return this.peakFrame }

  destroy() { this.detach() }
}
