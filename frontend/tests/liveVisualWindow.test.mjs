import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createLiveVisualWindow } from '../src/workbench/liveVisualWindow.ts'
import { changedVisualRegion } from '../src/workbench/liveVisionSampling.ts'

const frame = (capturedAt, value = 100, bytes = 1000) => ({ capturedAt,
  image: { size: bytes }, focusImage: null,
  sample: { width: 1280, height: 720, pixels: new Uint8Array(64 * 36 * 3).fill(value) } })

test('flash frame survives restoration and is selected with before/current frames', () => {
  const window = createLiveVisualWindow()
  const before = frame(1000), flash = frame(1125, 220), restored = frame(1250)
  window.push(before); window.uploaded(before, 1000, 1000)
  window.push(flash); window.push(restored)
  const selection = window.select(1300)
  assert.equal(selection.before, before)
  assert.equal(selection.key, flash)
  assert.equal(selection.current, restored)
  assert.equal(window.due(1300, 300, true, 90), true)
  window.uploaded(restored, 1300, 1000)
  assert.equal(window.select(1300), null, '已上传的关键帧不会再次排队')
})

test('ordinary delivery does not run at the local probe rate; provider backoff wins', () => {
  const window = createLiveVisualWindow()
  assert.equal(window.captureInterval(1000, 500, 90), 125)
  assert.equal(window.interval(1000, 500, true), 800)
  window.activate(1000, '用户开始说话')
  assert.equal(window.interval(1100, 300, true), 300)
  assert.equal(window.interval(1100, 4000, true), 4000)
  assert.equal(window.interval(1100, 500, false), 1400)
  assert.equal(window.interval(5000, 500, true), 800)
})

test('image and byte budgets are sliding limits and cannot be bypassed by reconnect', () => {
  const window = createLiveVisualWindow(), first = frame(1000)
  window.push(first); window.uploaded(first, 1000, 4_000_000)
  assert.equal(window.canUpload(1300, 4_000_001, 90), false)
  window.uploaded(frame(1300), 1300, 1000)
  assert.equal(window.due(1600, 300, true, 2), false)
  window.reset()
  assert.equal(window.usage(1800), 2)
  assert.equal(window.captureInterval(1800, 500, 2), 500)
  assert.equal(window.canUpload(61_301, 1000, 2), true)
})

test('history is bounded by bytes and time; reset prevents old clues in a new capture', () => {
  const window = createLiveVisualWindow()
  const first = frame(1000, 100, 1_000_000)
  window.push(first); window.uploaded(first, 1000, 1000)
  for (let at = 1125; at < 5000; at += 125) window.push(frame(at, at % 256, 1_000_000))
  assert.ok(window.retainedBytes(5000) <= 6_000_000)
  assert.equal(window.select(10_000), null, '不补发几秒前的过期帧')
  window.reset()
  window.push(frame(11_000))
  assert.equal(window.select(11_000).before, null)
  assert.equal(window.select(11_000).key, null)
})

test('localized change gets a padded region while noise and full-screen changes do not', () => {
  const before = frame(1000).sample, current = frame(1100).sample
  for (let y = 10; y < 16; y++) for (let x = 20; x < 30; x++) {
    current.pixels.fill(220, (y * 64 + x) * 3, (y * 64 + x) * 3 + 3)
  }
  const region = changedVisualRegion(before, current)
  assert.ok(region && region.x < 20 / 64 && region.y < 10 / 36)
  assert.ok(region.width > 10 / 64 && region.width < .5)
  assert.equal(changedVisualRegion(before, frame(1100, 102).sample), null)
  assert.equal(changedVisualRegion(before, frame(1100, 220).sample), null)
})
