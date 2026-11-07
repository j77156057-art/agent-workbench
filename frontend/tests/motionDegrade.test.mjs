import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MEMORY_GRAPH } from '../src/workbench/components/memoryGraphTokens.ts'
import { MotionDegrade, median, shouldPaintPulse } from '../src/workbench/components/motionDegrade.ts'

/**
 * 帧超支率判定的门禁（第二版判据）。
 *
 * 判据是纯逻辑：refreshInterval = clamp(最近 30 帧间隔中位数)，每帧超支
 * = max(0, dt - refreshInterval) / refreshInterval，窗口均值 > 50% 连续 30 帧进入，
 * < 20% 连续 60 帧退出；低配画像预降级后需连续 120 帧 < 10% 才升回。
 */

const CFG = MEMORY_GRAPH.motion.degrade

function feed(deg, values) {
  const transitions = []
  values.forEach((frameMs, index) => {
    if (deg.report(frameMs)) transitions.push({ index, degraded: deg.isDegraded })
  })
  return transitions
}

function frames(count, ms) {
  return Array.from({ length: count }, () => ms)
}

test('配置：超支率阈值、滞回与预降级参数集中在 tokens 里', () => {
  assert.deepEqual(
    {
      medianWindow: CFG.medianWindow, refreshFallbackMs: CFG.refreshFallbackMs,
      refreshMinMs: CFG.refreshMinMs, refreshMaxMs: CFG.refreshMaxMs,
      windowFrames: CFG.windowFrames, enterRatio: CFG.enterRatio, enterFrames: CFG.enterFrames,
      exitRatio: CFG.exitRatio, exitFrames: CFG.exitFrames,
      upgradeRatio: CFG.upgradeRatio, upgradeFrames: CFG.upgradeFrames,
      pulseEvery: CFG.pulseEvery,
    },
    {
      medianWindow: 30, refreshFallbackMs: 16.7, refreshMinMs: 8, refreshMaxMs: 17.5,
      windowFrames: 30, enterRatio: 0.5, enterFrames: 30,
      exitRatio: 0.2, exitFrames: 60,
      upgradeRatio: 0.1, upgradeFrames: 120, pulseEvery: 2,
    },
    '阈值与滞回参数必须集中在 tokens 里，不能散在逻辑中',
  )
})

test('60Hz 健康帧 3600 帧不得触发降级', () => {
  const deg = new MotionDegrade(CFG)
  // 60Hz 屏的健康帧间隔 ≈16.7ms；带 ±1.4ms 抖动，偶发到 20ms 也不算超支严重。
  const healthy = []
  for (let i = 0; i < 3600; i += 1) {
    healthy.push([16.6, 16.8, 16.7, 20.0, 16.5, 16.9, 17.2, 16.4, 16.7, 16.8][i % 10])
  }
  const transitions = feed(deg, healthy)
  assert.equal(transitions.length, 0, '真实 60Hz 健康帧不得进入降级')
  assert.equal(deg.isDegraded, false)
  assert.ok(deg.lastOverrunRatio < 0.1, `健康帧超支率应远低于 50%，实测 ${(deg.lastOverrunRatio * 100).toFixed(1)}%`)
  assert.ok(deg.lastRefreshMs >= CFG.refreshMinMs && deg.lastRefreshMs <= CFG.refreshMaxMs,
    '刷新间隔估计必须落在夹取区间内')

  // 正常路径每帧都画脉冲，行为与改造前一致。
  for (let frame = 0; frame < 30; frame += 1) {
    assert.equal(shouldPaintPulse((frame + 1) % CFG.pulseEvery, false, CFG.pulseEvery), true)
  }
})

test('真·超支：60Hz 上稳定 30ms/帧 → 超支率 >50%，连续 30 帧后进入降级', () => {
  const deg = new MotionDegrade(CFG)
  const transitions = feed(deg, frames(200, 30))
  assert.equal(transitions.length, 1, '只应发生一次进入')
  // 前 30 帧攒窗口，再连续 30 帧超支才换挡。
  assert.equal(transitions[0].index, 58)
  assert.equal(deg.isDegraded, true)
  assert.ok(deg.lastOverrunRatio > CFG.enterRatio,
    `30ms/帧相对 17.5ms 刷新上限应超支 >50%，实测 ${(deg.lastOverrunRatio * 100).toFixed(1)}%`)

  // 降级后连续 30 帧，脉冲只应画 15 次。
  let paints = 0
  for (let frame = 0; frame < 30; frame += 1) {
    if (shouldPaintPulse((frame + 1) % CFG.pulseEvery, true, CFG.pulseEvery)) paints += 1
  }
  assert.equal(paints, 15, '降级后脉冲每 2 帧更新一次')
})

test('恢复：连续 60 帧超支率 <20% 才退出，且恢复后脉冲复原', () => {
  const deg = new MotionDegrade(CFG)
  feed(deg, frames(200, 30))
  assert.equal(deg.isDegraded, true, '前置条件：已进入降级')

  const early = feed(deg, frames(30, 16.7))
  assert.equal(early.length, 0, '窗口里还留着超支样本时不得恢复')
  assert.equal(deg.isDegraded, true)

  const settled = feed(deg, frames(60, 16.7))
  assert.equal(settled.length, 1, '窗口排空后再连续 60 帧 <20% 才恢复')
  assert.equal(settled[0].degraded, false)
  assert.equal(deg.isDegraded, false)
  assert.equal(shouldPaintPulse(1, false, CFG.pulseEvery), true)
})

test('滞回：超支率在 20%~50% 带内反复波动时不得反复换挡', () => {
  const deg = new MotionDegrade(CFG)
  let transitions = 0
  // 22.5ms 相对 60Hz 是约 28.6% 超支（带内）：既够不到 50% 进入门，也不低于 20% 退出门。
  for (let i = 0; i < 3000; i += 1) {
    if (deg.report(i % 2 ? 22.5 : 22.4)) transitions += 1
  }
  assert.equal(transitions, 0, '滞回带内不得进入降级')
  assert.equal(deg.isDegraded, false)

  // 贴近进入门抖动（30ms / 22ms）：最多进入一次，之后保持。
  const entered = feed(deg, Array.from({ length: 3000 }, (_, i) => (i % 4 === 0 ? 16.6 : 30)))
  transitions += entered.length
  assert.equal(entered.length, 1, '只应进入一次')
  assert.equal(deg.isDegraded, true)

  // 已降级 + 带内波动（28% 超支）：不得退出。
  const stayed = feed(deg, Array.from({ length: 3000 }, (_, i) => (i % 2 ? 22.5 : 22.4)))
  assert.equal(stayed.length, 0, '带内波动不得退出降级')
  assert.equal(transitions, 1, '没有重复换挡')
})

test('低配画像：开局预降级，连续 120 帧超支率 <10% 才升回', () => {
  const deg = new MotionDegrade(CFG)
  assert.equal(deg.seedTier('low'), true, 'low 画像应触发预降级')
  assert.equal(deg.isDegraded, true)
  assert.equal(deg.seeded, true)

  // 前 30 帧先攒满窗口，之后 100 帧健康仍不到 120 帧门槛。
  feed(deg, frames(30, 16.7))
  feed(deg, frames(100, 16.7))
  assert.equal(deg.isDegraded, true, '不足 120 帧不得升回')
  assert.equal(deg.seeded, true)

  const upgraded = feed(deg, frames(30, 16.7))
  assert.equal(upgraded.length, 1, '满 120 帧健康观测后应升回')
  assert.equal(upgraded[0].degraded, false)
  assert.equal(deg.seeded, false)

  // 升回后回到常规判据：真实超支仍可再次进入降级。
  const again = feed(deg, frames(120, 30))
  assert.equal(again.length, 1)
  assert.equal(deg.isDegraded, true)
})

test('中位数与夹取：稳定慢机不会被自身慢归一化成 0 超支', () => {
  assert.equal(median([16.6, 16.8, 16.7]), 16.7)
  assert.equal(median([16, 17]), 16.5)
  const deg = new MotionDegrade(CFG)
  feed(deg, frames(60, 28))
  assert.equal(deg.lastRefreshMs, CFG.refreshMaxMs, '中位数 28ms 必须被上限夹到 17.5ms')
  assert.ok(deg.lastOverrunRatio > 0.5, '稳定 28ms 仍是真超支，不能被归一化成 0')
})
