/**
 * M2·③ 展示层的**行为守卫**：`describeRealtimeMode` 是纯函数，直接跑生产模块本体。
 *
 * 锁四件事，都是开工文档 §5.4 强调的"降级要响亮上报"落点：
 * 1. REST 与 WS `hello.ok` 两种字段拼法都要吃，出同一个 view。
 * 2. 只要 `degraded_to` 非空 / `native_available=false` → 一律标降级，绝不装原生。
 * 3. 空/未知 mode → tone=unknown、isDegraded=true，让徽章仍亮着，不静默 pass。
 * 4. hint 优先 mode_reason → reason → native_reason → limitations 兜底；顺序不能变。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { describeRealtimeMode } from '../src/workbench/realtimeModeDisplay.ts'

test('REST native 报告：label=原生实时、tone=native、不降级', () => {
  const v = describeRealtimeMode({
    mode: 'native-realtime',
    mode_label: '原生实时',
    mode_reason: '已配置原生 provider；最终会话模式以 hello.ok 返回值为准。',
    native_provider: 'qwen-omni',
    native_available: true,
    native_reason: '',
    limitations: ['当前会话会把音频和视频交给原生实时模型', '模型连接失败时自动降级为兼容抽帧模式'],
  })
  assert.equal(v.label, '原生实时')
  assert.equal(v.tone, 'native')
  assert.equal(v.isDegraded, false)
  assert.match(v.hint, /hello\.ok/)
  assert.ok(v.capabilities.length >= 1)
})

test('REST 兼容抽帧：degraded 徽章亮着、hint 讲清掉档原因', () => {
  const v = describeRealtimeMode({
    mode: 'sampled-frames',
    mode_label: '兼容抽帧',
    mode_reason: '未配置原生实时模型',
    native_available: false,
    native_reason: '未配置原生实时模型',
    limitations: ['当前只处理最新视频帧'],
  })
  assert.equal(v.label, '兼容抽帧（已降级）')
  assert.equal(v.tone, 'degraded')
  assert.equal(v.isDegraded, true)
  assert.match(v.hint, /未配置原生/)
})

test('WS hello.ok native 与 REST 结果同 shape（字段名不同也要一致）', () => {
  const ws = describeRealtimeMode({
    mode: 'native-realtime',
    degraded_to: null,
    reason: 'provider ready',
    provider: 'qwen-omni',
    provider_capabilities: ['audio.in', 'video.in', 'text.out', 'audio.out', 'interrupt'],
  })
  assert.equal(ws.tone, 'native')
  assert.equal(ws.isDegraded, false)
  assert.deepEqual(ws.capabilities, ['audio.in', 'video.in', 'text.out', 'audio.out', 'interrupt'])
})

test('WS hello.ok 已降级到 sampled-frames：仍标 degraded、hint 用 reason', () => {
  const v = describeRealtimeMode({
    mode: 'sampled-frames',
    degraded_to: 'sampled-frames',
    reason: '实时模型启动失败：RuntimeError: ws handshake timeout',
    provider: '',
    provider_capabilities: [],
  })
  assert.equal(v.tone, 'degraded')
  assert.equal(v.isDegraded, true)
  assert.match(v.hint, /启动失败/)
})

test('mode=native 但 degraded_to 也非空 → 按降级判，不装没事', () => {
  const v = describeRealtimeMode({
    mode: 'native-realtime',
    degraded_to: 'sampled-frames',
    reason: 'provider 中途崩了',
  })
  assert.equal(v.tone, 'degraded')
  assert.equal(v.isDegraded, true)
})

test('unavailable 分支：mode 或 degraded_to 任一说到 unavailable 就报灰底', () => {
  const a = describeRealtimeMode({ mode: 'unavailable', mode_reason: '视觉链路整体故障' })
  assert.equal(a.tone, 'unavailable')
  assert.equal(a.isDegraded, true)
  assert.match(a.hint, /视觉链路整体故障/)
  const b = describeRealtimeMode({ mode: 'native-realtime', degraded_to: 'unavailable', reason: 'no vision' })
  assert.equal(b.tone, 'unavailable')
})

test('空 payload / 未拿到 hello.ok → 未知态也保持"降级中"的可见性', () => {
  for (const raw of [null, undefined, {}, { mode: '' }]) {
    const v = describeRealtimeMode(raw)
    assert.equal(v.tone, 'unknown')
    assert.equal(v.isDegraded, true, '未知态必须可见，不能静默 pass')
    assert.match(v.hint, /还没拿到|未知/)
  }
})

test('hint 回落顺序：mode_reason > reason > native_reason > 默认文案', () => {
  const v1 = describeRealtimeMode({ mode: 'sampled-frames', mode_reason: 'A', reason: 'B', native_reason: 'C' })
  assert.equal(v1.hint, 'A')
  const v2 = describeRealtimeMode({ mode: 'sampled-frames', reason: 'B', native_reason: 'C' })
  assert.equal(v2.hint, 'B')
  const v3 = describeRealtimeMode({ mode: 'sampled-frames', native_reason: 'C' })
  assert.equal(v3.hint, 'C')
  const v4 = describeRealtimeMode({ mode: 'sampled-frames' })
  assert.match(v4.hint, /原生实时不可用/)
})

test('capabilities 字段兼容 provider_capabilities / capabilities / limitations 三种键名', () => {
  assert.deepEqual(describeRealtimeMode({ mode: 'native-realtime', capabilities: ['x', 'y'] }).capabilities, ['x', 'y'])
  assert.deepEqual(describeRealtimeMode({ mode: 'native-realtime', provider_capabilities: ['z'] }).capabilities, ['z'])
  assert.deepEqual(describeRealtimeMode({ mode: 'sampled-frames', limitations: ['l1', 'l2'] }).capabilities, ['l1', 'l2'])
})
