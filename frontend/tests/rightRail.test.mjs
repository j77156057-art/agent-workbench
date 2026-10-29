import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  RIGHT_RAIL_DEFAULT_W,
  RIGHT_RAIL_MIN_W,
  RIGHT_RAIL_MAX_W,
  RIGHT_RAIL_MAIN_MIN_W,
  clampRightRailWidth,
  canUseRightRail,
  normalizeAddress,
  previewKindOfUrl,
  previewSandboxFor,
} from '../src/workbench/lib/rightRail.ts'

/**
 * 右侧预览栏的尺寸与地址门禁。
 *
 * 这些断言锁的是「判错会直接伤到用户」的几件事：
 *   - 右栏把主区挤到低于 520px（对话正文不可读）；
 *   - 宽度能拖出视口 / 拖到 0；
 *   - 地址栏把 javascript:/data: 当成可打开地址；
 *   - 把外部站点误判成同源，或反过来。
 * 不是给实现拍快照，是给行为立规矩。
 */

test('宽度夹取：默认值、最小值、最大值与视口上限', () => {
  const base = { innerWidth: 1440, leftWidth: 288 }
  assert.equal(clampRightRailWidth(RIGHT_RAIL_DEFAULT_W, base), RIGHT_RAIL_DEFAULT_W)
  assert.equal(clampRightRailWidth(100, base), RIGHT_RAIL_MIN_W)
  // 1440 - 288 - 520 = 632：主区下限先于 MAX(720) 生效，硬拖也到不了 720
  assert.equal(clampRightRailWidth(2000, base), 632)
  assert.equal(clampRightRailWidth(900, base), 632)
  // 左栏收起后上限放宽到 1440 - 520 = 920 → 仍受 MAX 720 限制
  assert.equal(clampRightRailWidth(900, { ...base, leftCollapsed: true }), RIGHT_RAIL_MAX_W)
  // 视口很窄时上限不低于 MIN（否则返回负数）
  assert.equal(clampRightRailWidth(460, { innerWidth: 900, leftWidth: 288 }), RIGHT_RAIL_MIN_W)
  // 非法视口宽度退回默认，不返回 NaN
  assert.equal(clampRightRailWidth(460, { innerWidth: 0 }), RIGHT_RAIL_DEFAULT_W)
})

test('主区下限：任何夹取结果都给主区留出 520px', () => {
  const width = 1200
  const left = 288
  const rail = clampRightRailWidth(999, { innerWidth: width, leftWidth: left })
  assert(width - left - rail >= RIGHT_RAIL_MAIN_MIN_W)
})

test('可用性：只认 fixed 布局且视口不小于 1200', () => {
  assert.equal(canUseRightRail({ layout: 'fixed', innerWidth: 1440 }), true)
  assert.equal(canUseRightRail({ layout: 'fixed', innerWidth: 1199 }), false)
  assert.equal(canUseRightRail({ layout: 'dock', innerWidth: 1440 }), false)
  assert.equal(canUseRightRail({ layout: 'split', innerWidth: 1920 }), false)
  // 未写入 data-layout 时按默认 fixed 处理
  assert.equal(canUseRightRail({ layout: null, innerWidth: 1280 }), true)
})

test('地址规整：本机地址补 http，普通域名补 https', () => {
  assert.equal(normalizeAddress('localhost:5199').url, 'http://localhost:5199')
  assert.equal(normalizeAddress('127.0.0.1:8000/workbench').url, 'http://127.0.0.1:8000/workbench')
  assert.equal(normalizeAddress('example.com/docs').url, 'https://example.com/docs')
  assert.equal(normalizeAddress('https://example.com').url, 'https://example.com')
  assert.equal(normalizeAddress('/play/abc').url, '/play/abc')
})

test('地址规整：危险/非法协议一律拒绝', () => {
  for (const bad of ['javascript:alert(1)', 'data:text/html,<b>x</b>', 'file:///C:/Windows', 'vbscript:msgbox']) {
    const r = normalizeAddress(bad)
    assert.equal(r.ok, false, bad)
    assert.equal(r.url, '', bad)
  }
  assert.equal(normalizeAddress('   ').ok, false)
  assert.equal(normalizeAddress('随便一段话').ok, false)
})

test('可嵌入判定：同源路径、外部站点与不支持协议', () => {
  assert.equal(previewKindOfUrl('/play/token-1'), 'same-origin')
  assert.equal(previewKindOfUrl('http://127.0.0.1:8000/workbench', 'http://127.0.0.1:8000'), 'same-origin')
  assert.equal(previewKindOfUrl('https://example.com/a', 'http://127.0.0.1:8000'), 'external')
  assert.equal(previewKindOfUrl('javascript:alert(1)'), 'unsupported')
  assert.equal(previewKindOfUrl(''), 'unsupported')
})

test('sandbox：只给模型生成的 workflow 预览产物加，不给应用自身路径加', () => {
  assert.match(previewSandboxFor('/api/agent/workflow/wf-1/preview/artifact/a1') || '', /allow-scripts/)
  assert.equal(previewSandboxFor('/play/abc'), undefined)
  assert.equal(previewSandboxFor('https://example.com'), undefined)
})
