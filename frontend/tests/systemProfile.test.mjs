import assert from 'node:assert/strict'
import { test } from 'node:test'
import { judgeProfile } from '../src/workbench/systemProfile.ts'

/**
 * 能力画像合并的门禁：浏览器信号只加权 / 明确证据才拉低，
 * 最重要的是「GPU 不可用 ≠ 低端」「接口不可用 ≠ 低端」。
 */

const healthyBrowser = {
  device_memory_gb: 8,
  js_heap_limit_mb: 2048,
  prefers_reduced_motion: false,
  has_raf: true,
  webgl_renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11)',
  webgl_software: false,
}

test('GPU 采样不可用时不得误判为低端', () => {
  const server = {
    tier: 'high',
    reasons: [{ kind: 'gpu_unknown', detail: 'GPU 占用率不可用：nvidia-smi 不存在', value: 'unavailable', threshold: '仅作信息，不参与降级' }],
    gpu: { models: [], available: false, reason: 'nvidia-smi 不存在' },
  }
  const merged = judgeProfile(server, healthyBrowser)
  assert.equal(merged.tier, 'high', '拿不到 GPU 占用率不等于机器弱')
  assert.ok(merged.reasons.some(reason => reason.kind === 'gpu_unknown'))
})

test('后端画像缺失（接口失败）时保持 mid，不擅自降级', () => {
  const merged = judgeProfile(null, healthyBrowser)
  assert.equal(merged.tier, 'mid')
})

test('软件渲染是明确证据：拉低为 low', () => {
  const merged = judgeProfile({ tier: 'high', reasons: [] }, {
    ...healthyBrowser,
    webgl_renderer: 'Google SwiftShader',
    webgl_software: true,
  })
  assert.equal(merged.tier, 'low')
  assert.ok(merged.reasons.some(reason => reason.kind === 'gpu_software_render'))
})

test('浏览器报告老 Intel HD 4xxx → low', () => {
  const merged = judgeProfile({ tier: 'high', reasons: [] }, {
    ...healthyBrowser,
    webgl_renderer: 'ANGLE (Intel(R) HD Graphics 4000 Direct3D11)',
  })
  assert.equal(merged.tier, 'low')
})

test('UHD / Iris 不被老型号规则误伤', () => {
  const merged = judgeProfile({ tier: 'high', reasons: [] }, {
    ...healthyBrowser,
    webgl_renderer: 'ANGLE (Intel(R) UHD Graphics 630 Direct3D11)',
  })
  assert.equal(merged.tier, 'high')
})

test('deviceMemory / JS 堆 / reduce-motion 只加权，不单独否决', () => {
  const merged = judgeProfile({ tier: 'high', reasons: [] }, {
    ...healthyBrowser,
    device_memory_gb: 8,
    prefers_reduced_motion: true,
  })
  assert.equal(merged.tier, 'high', '单开减少动效不该把整机判成低端')

  const weak = judgeProfile({ tier: 'high', reasons: [] }, {
    ...healthyBrowser,
    device_memory_gb: 4,
    prefers_reduced_motion: true,
  })
  assert.equal(weak.tier, 'mid', '≤4GB + reduce-motion 加权到 mid，但不到 low')
})

test('后端已判 low 时前端不得洗白', () => {
  const merged = judgeProfile({
    tier: 'low',
    reasons: [{ kind: 'cpu_physical', detail: '物理核数 2 ≤ 2', value: '2', threshold: '物理核 ≤ 2' }],
    gpu: { models: [], available: false },
  }, healthyBrowser)
  assert.equal(merged.tier, 'low')
  assert.ok(merged.reasons.some(reason => reason.kind === 'cpu_physical'))
})
