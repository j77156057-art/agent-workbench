// 弱机降级判据：按 vsync 归一化的「帧超支率」判定，不看设备型号绝对值。
//
// 为什么不用绝对毫秒 p95（2026-11-04 改）：真实 60Hz 屏的健康帧间隔本来就 ≈16.7ms，
// 「p95 > 12ms 进入降级」会把绝大多数正常机器常驻降级（真机 4 次长跑里 3 次出现
// 2/4/8 次无意义进出）。改为：
//   refreshInterval = clamp(最近 medianWindow 帧间隔的中位数)
//   每帧超支 = max(0, dt - refreshInterval) / refreshInterval
//   窗口超支率 = 最近 windowFrames 帧超支的平均值
// 进入：超支率 > enterRatio 连续 enterFrames 帧；
// 退出：超支率 < exitRatio 连续 exitFrames 帧（滞回，避免阈值附近抖动）。
//
// 上限夹取很重要：如果一台机器稳定跑 30ms/帧，不能让它把「中位数 30ms」当成刷新率
// 从而把自身的慢归一化成 0 超支；clamp 到常见显示器区间后它才是真的超支。
//
// 低配画像（profile.tier === 'low'）开局直接预降级，运行期仍可用更严格的
// upgradeRatio / upgradeFrames 升回，避免一次判错就永久锁死。
//
// 纯逻辑，不碰 DOM，便于 node 单测；DOM 侧节流在 memoryMotion.ts。
export interface MotionDegradeConfig {
  medianWindow: number
  refreshFallbackMs: number
  refreshMinMs: number
  refreshMaxMs: number
  windowFrames: number
  enterRatio: number
  enterFrames: number
  exitRatio: number
  exitFrames: number
  upgradeRatio: number
  upgradeFrames: number
}

export type CapabilityTier = 'high' | 'mid' | 'low'

/** 中位数（偶数取中间两值平均）；空数组返回 0。 */
export function median(values: readonly number[]): number {
  if (!values.length) return 0
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

/** 降级时脉冲每 pulseEvery 帧更新一次；正常路径每帧更新（与改造前一致）。 */
export function shouldPaintPulse(frameIndex: number, degraded: boolean, pulseEvery: number): boolean {
  if (!degraded) return true
  return frameIndex % Math.max(1, pulseEvery) === 0
}

export class MotionDegrade {
  private intervals: number[] = []
  private ratios: number[] = []
  private above = 0
  private below = 0
  private upgrades = 0
  private degraded = false
  private seededLow = false
  private refresh = 0
  private ratio = 0
  private readonly config: MotionDegradeConfig

  constructor(config: MotionDegradeConfig) {
    this.config = config
    this.refresh = config.refreshFallbackMs
  }

  get isDegraded() { return this.degraded }
  get seeded() { return this.seededLow }
  get sampleCount() { return this.intervals.length }
  get ratioSampleCount() { return this.ratios.length }
  /** 最近一个窗口的平均超支率（0~N，1 = 恰好超支一倍刷新间隔）。 */
  get lastOverrunRatio() { return this.ratio }
  /** 当前的刷新间隔估计（已夹取）。 */
  get lastRefreshMs() { return this.refresh }

  reset() {
    this.intervals = []
    this.ratios = []
    this.above = 0
    this.below = 0
    this.upgrades = 0
    this.ratio = 0
    this.refresh = this.config.refreshFallbackMs
    // reset 不清「画像预降级」：换图重新 attach 时低配机器应继续保持降级。
    this.degraded = this.seededLow
  }

  /**
   * 应用能力画像。返回 true 表示因此发生了降级翻转（调用方需要更新 DOM 观测字段）。
   * 只在 low 时预降级；high/mid 不改变运行期已经观测到的状态。
   */
  seedTier(tier: CapabilityTier): boolean {
    if (tier !== 'low') return false
    const changed = !this.degraded
    this.seededLow = true
    this.degraded = true
    this.above = 0
    this.below = 0
    this.upgrades = 0
    return changed
  }

  /** 喂一帧的间隔；仅在状态翻转时返回 true。 */
  report(frameMs: number): boolean {
    if (!Number.isFinite(frameMs) || frameMs <= 0) return false
    this.intervals.push(frameMs)
    if (this.intervals.length > this.config.medianWindow) this.intervals.shift()
    this.refresh = clamp(
      median(this.intervals),
      this.config.refreshMinMs,
      this.config.refreshMaxMs,
    )
    const overrun = Math.max(0, frameMs - this.refresh) / this.refresh
    this.ratios.push(overrun)
    if (this.ratios.length > this.config.windowFrames) this.ratios.shift()
    if (this.ratios.length < this.config.windowFrames) return false
    this.ratio = this.ratios.reduce((sum, value) => sum + value, 0) / this.ratios.length

    // 画像预降级：用更严格的恢复门（连续 upgradeFrames 帧超支率 < upgradeRatio）。
    if (this.seededLow) {
      if (this.degraded) {
        if (this.ratio < this.config.upgradeRatio) {
          this.upgrades += 1
          if (this.upgrades >= this.config.upgradeFrames) {
            this.degraded = false
            this.seededLow = false
            this.upgrades = 0
            return true
          }
        } else {
          this.upgrades = 0
        }
        return false
      }
    }

    if (this.ratio > this.config.enterRatio) {
      this.above += 1
      this.below = 0
    } else if (this.ratio < this.config.exitRatio) {
      this.below += 1
      this.above = 0
    } else {
      // 滞回带内：两个计数都清零，阈值附近来回波动不得反复换挡。
      this.above = 0
      this.below = 0
    }

    if (!this.degraded && this.above >= this.config.enterFrames) {
      this.degraded = true
      this.above = 0
      return true
    }
    if (this.degraded && this.below >= this.config.exitFrames) {
      this.degraded = false
      this.below = 0
      return true
    }
    return false
  }
}
