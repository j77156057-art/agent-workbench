/**
 * M2·③b 接线层**纯状态机**的行为守卫：直接跑生产模块 `realtimeModeTracker.ts`（无 vue / 无 DOM）。
 * tracker 只管"取数 + fail-closed"，展示视图由 `realtimeModeDisplay.describeRealtimeMode` 单独映射；
 * 本测把两者一起驱动，验证"降级/失败不会变隐形"这条端到端不变式。
 *
 * 盯死：
 * 1. 初始没拿到事实 → raw=null → unknown 且 isDegraded=true，徽章继续亮，不静默 pass。
 * 2. REST native 事实 → raw 置有效 → tone native、不降级、loaded=true。
 * 3. REST sampled 事实 → tone degraded。
 * 4. **上一次成功拿到 native，这一次 fetch 抛错 → raw 必须归零 → unknown，绝不保留旧 native 假象**
 *    （fail-closed 核心；"绿了不代表对"的前端版）。
 * 5. 返回带 error 字段的体 / 返回非对象脏值 → raw=null，且写 lastError。
 * 6. busy 只在飞行中为真，结束（成功/失败）必归 false。
 * 7. reset() 回到 raw=null、loaded=false。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createRealtimeModeTracker } from '../src/workbench/realtimeModeTracker.ts'
import { describeRealtimeMode } from '../src/workbench/realtimeModeDisplay.ts'

const nativeRest = {
  mode: 'native-realtime', mode_label: '原生实时',
  mode_reason: '已配置原生 provider；最终会话模式以 hello.ok 返回值为准。',
  native_available: true, native_reason: '', limitations: ['x'],
}
const sampledRest = {
  mode: 'sampled-frames', mode_label: '兼容抽帧', mode_reason: '未配置原生实时模型',
  native_available: false, native_reason: '未配置原生实时模型', limitations: ['y'],
}

test('初始（还没拉取）→ raw=null → unknown，且 isDegraded=true，loaded=false', () => {
  const t = createRealtimeModeTracker(async () => nativeRest)
  const s = t.snapshot()
  assert.equal(s.raw, null)
  assert.equal(describeRealtimeMode(s.raw).tone, 'unknown')
  assert.equal(describeRealtimeMode(s.raw).isDegraded, true)
  assert.equal(s.loaded, false)
})

test('REST native 事实 → raw 置有效 → tone=native、不降级、loaded=true', async () => {
  const t = createRealtimeModeTracker(async () => nativeRest)
  const s = await t.refresh()
  assert.deepEqual(s.raw, nativeRest)
  assert.equal(describeRealtimeMode(s.raw).tone, 'native')
  assert.equal(describeRealtimeMode(s.raw).isDegraded, false)
  assert.equal(s.loaded, true)
  assert.equal(s.busy, false)
})

test('REST sampled 事实 → tone=degraded、isDegraded=true', async () => {
  const t = createRealtimeModeTracker(async () => sampledRest)
  const s = await t.refresh()
  assert.equal(describeRealtimeMode(s.raw).tone, 'degraded')
  assert.equal(describeRealtimeMode(s.raw).isDegraded, true)
  assert.equal(s.lastError, '')
})

test('fail-closed：先拿到 native，下一次 fetch 抛错 → raw 归零、回退 unknown，绝不保留旧 native', async () => {
  let boom = false
  const t = createRealtimeModeTracker(async () => {
    if (boom) throw new Error('ws handshake timeout')
    return nativeRest
  })
  const good = await t.refresh()
  assert.equal(describeRealtimeMode(good.raw).tone, 'native')
  // 翻脸：连接断了 / 拉取失败
  boom = true
  const bad = await t.refresh()
  assert.equal(bad.raw, null, '失败必须把 raw 归零，不能停在旧 native')
  assert.equal(describeRealtimeMode(bad.raw).tone, 'unknown')
  assert.equal(describeRealtimeMode(bad.raw).isDegraded, true)
  assert.match(bad.lastError, /timeout/)
  assert.equal(bad.busy, false, '失败也要把 busy 归位，不卡态')
})

test('返回带 error 字段的响应体 → raw=null 并写 lastError', async () => {
  const t = createRealtimeModeTracker(async () => ({ ok: false, error: '视觉链路整体故障' }))
  const s = await t.refresh()
  assert.equal(s.raw, null)
  assert.equal(describeRealtimeMode(s.raw).tone, 'unknown')
  assert.match(s.lastError, /视觉链路整体故障/)
  assert.equal(s.loaded, false)
})

test('返回非对象脏值（null / 字符串 / 数组 / 数字）→ raw=null → unknown', async () => {
  for (const dirty of [null, undefined, 'oops', [1, 2, 3], 42]) {
    const t = createRealtimeModeTracker(async () => dirty)
    const s = await t.refresh()
    assert.equal(s.raw, null, `脏值 ${JSON.stringify(dirty)} 不应被当有效事实`)
    assert.equal(describeRealtimeMode(s.raw).tone, 'unknown')
    assert.equal(s.loaded, false)
  }
})

test('busy 在飞行中为真、结束后归 false', async () => {
  let release = null
  const t = createRealtimeModeTracker(() => new Promise(resolve => {
    release = () => resolve(nativeRest)
  }))
  const p = t.refresh()
  assert.equal(t.snapshot().busy, true, '飞行中应 busy')
  release()
  const s = await p
  assert.equal(s.busy, false, '结束必须不 busy')
})

test('reset() 把状态清回 raw=null、loaded=false', async () => {
  const t = createRealtimeModeTracker(async () => nativeRest)
  await t.refresh()
  assert.equal(describeRealtimeMode(t.snapshot().raw).tone, 'native')
  t.reset()
  const s = t.snapshot()
  assert.equal(s.raw, null)
  assert.equal(describeRealtimeMode(s.raw).tone, 'unknown')
  assert.equal(s.loaded, false)
})
